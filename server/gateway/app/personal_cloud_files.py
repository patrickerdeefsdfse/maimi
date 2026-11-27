"""Browser downloads of the signed-in employee's existing cloud files.

The native composed filesystem remains the reader. No host filesystem mounts,
public file URLs, conversion service, or model task authorization are used.
"""
import base64
import binascii
import json
import hashlib
import re
import secrets
from email import policy
from email.parser import BytesParser
from urllib.parse import quote

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from . import iam_oauth as iam
from . import personal_cloud as cloud
from . import storage_lifecycle as managed_storage

router = APIRouter()
MAX_FILE_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
SESSION_ID = re.compile(r'(?:session-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
HEADERS = {**cloud.HEADERS, 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'}
NATIVE_FORM = re.compile(r'multipart/form-data;\s*boundary=(?:"([\w.-]{1,70})"|([\w.-]{1,70}))', re.ASCII | re.I)
NATIVE_PART_HEADERS = re.compile(rb'\r\n(?:[a-zA-Z0-9-]+:[^\r\n]+\r\n)+\r\n')


def native_form_parts(data, content_type):
    """Use the MIME parser for structure and headers, without parsing large binary bodies as email text."""
    envelope = b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('ascii') + b'\r\n\r\n'
    wire = NATIVE_FORM.fullmatch(content_type)
    parts_data = None
    if wire:
        boundary = b'--' + (wire[1] or wire[2]).encode('ascii')
        segments = data.split(boundary, 4)
        if len(segments) == 4 and segments[0] == b'' and segments[3] in (b'--', b'--\r\n'):
            skeleton = []
            payloads = []
            for segment in segments[1:3]:
                header_end = segment.find(b'\r\n\r\n', 2, 8192)
                headers = segment[:header_end + 4]
                if header_end < 0 or not NATIVE_PART_HEADERS.fullmatch(headers) or not segment.endswith(b'\r\n'):
                    break
                skeleton.append(headers + b'x\r\n')
                payloads.append(bytes(memoryview(segment)[header_end + 4:-2]))
            if len(skeleton) == 2:
                data = boundary.join([b'', *skeleton, segments[3]])
                parts_data = payloads
    message = BytesParser(policy=policy.default).parsebytes(envelope + bytes(data))
    if message.defects or not message.is_multipart():
        raise ValueError('Malformed native multipart response')
    parts = list(message.iter_parts())
    if len(parts) != 2:
        raise ValueError('Unexpected native multipart fields')
    return [(part, parts_data[index] if parts_data is not None else part.get_payload(decode=True))
            for index, part in enumerate(parts)]


def native_response(data, content_type, method):
    """Decode the reviewed 0.1.7 single-byte-field Remote response only."""
    if content_type.split(';', 1)[0].strip().lower() != 'multipart/form-data':
        return json.loads(data)
    if (method != 'workspaceFiles/readBytes' or len(content_type) > 256
            or '\r' in content_type or '\n' in content_type):
        raise ValueError('Unexpected native binary response')
    fields = {}
    for part, payload in native_form_parts(data, content_type):
        name = part.get_param('name', header='content-disposition')
        if (part.defects or part.is_multipart() or part.get_content_disposition() != 'form-data'
                or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)
                or name in fields or part.get('Content-Transfer-Encoding') is not None):
            raise ValueError('Invalid native binary field')
        fields[name] = payload
    metadata = fields.pop('metadata', None)
    if not isinstance(metadata, bytes) or len(metadata) > 64 * 1024:
        raise ValueError('Invalid native binary metadata')
    value = json.loads(metadata)
    attachments = value.get('attachments') if isinstance(value, dict) else None
    result = value.get('result') if isinstance(value, dict) else None
    if (not isinstance(attachments, list) or len(attachments) != 1
            or not isinstance(attachments[0], dict) or set(attachments[0]) != {'codec', 'part', 'path'}
            or attachments[0]['codec'] != 'bytes' or attachments[0]['path'] != ['data']
            or not isinstance(attachments[0]['part'], str)
            or not isinstance(result, dict) or result.get('ok') is not True
            or not isinstance(result.get('value'), dict) or result['value'].get('data', False) is not None):
        raise ValueError('Invalid native binary attachment')
    content = fields.pop(attachments[0]['part'], None)
    if fields or not isinstance(content, bytes) or len(content) > CHUNK_BYTES:
        raise ValueError('Invalid native byte window')
    result['value']['data'] = content
    return value


def file_path(value, allow_relative=False):
    if (not isinstance(value, str) or len(value) > 4096 or not value
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value
            or any(part in ('', '.', '..') for part in value.split('/')[1 if value.startswith('/') else 0:])
            or not (value.startswith(('/workspace/', '/state/dsh/attachments/v1/files/'))
                    or (allow_relative and not value.startswith('/') and ':' not in value))):
        raise iam.IAMFailure(400, 'personal_cloud_file_path_invalid')
    return value


class NativeFileReader:
    """Bounded, version-consistent native reads reusable by browser previews."""
    def __init__(self, request, actor, upstream, lease, client, path, session_id, maximum, activity=True):
        self.request, self.actor, self.upstream = request, actor, upstream
        self.lease, self.client, self.path = lease, client, path
        self.session_id, self.maximum, self.info = session_id, maximum, None
        self.activity = activity

    @classmethod
    async def open(cls, request, path, session_id=None, max_bytes=MAX_FILE_BYTES, *, activity=True):
        path = file_path(path, allow_relative=session_id is not None)
        relative = not path.startswith('/')
        if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
            raise iam.IAMFailure(400, 'personal_cloud_file_scope_invalid')
        actor = await cloud.actor_for(request)
        upstream = await cloud.touch_host(request, actor, activity=activity)
        lease, _ = await cloud.issue_lease(request, actor)
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), trust_env=False, follow_redirects=False)
        reader = cls(request, actor, upstream, lease, client, path, session_id, max_bytes, activity)
        try:
            if session_id is None or relative:
                # Every cloud session has the same employee-owned /workspace.
                # Use an existing native lookup scope; never create a session or
                # infer it from unrelated DOM state or a different account.
                listing = await reader.rpc('session/list', {'_request': {}})
                items = listing.get('items', [])
                scopes = [item.get('sessionId') for item in items
                    if isinstance(item, dict) and item.get('cwd') == '/workspace'
                    and isinstance(item.get('sessionId'), str) and SESSION_ID.fullmatch(item['sessionId'])]
                if relative:
                    if session_id not in scopes:
                        raise iam.IAMFailure(403, 'personal_cloud_file_scope_invalid')
                    reader.path = file_path('/workspace/' + path)
                else:
                    reader.session_id = next(iter(scopes), None)
                if reader.session_id is None:
                    raise iam.IAMFailure(404, 'personal_cloud_file_scope_missing')
            reader.info = await reader.rpc('workspaceFiles/stat', reader.args())
            reader.check_info(reader.info)
            await reader.authorize()
            return reader
        except BaseException:
            await reader.close()
            raise

    def args(self):
        return {'workspaceFileScopeId': self.session_id, 'path': self.path}

    async def authorize(self):
        current = await cloud.actor_for(self.request)
        if ((current.user_no, current.account_no, current.auth_session_id)
                != (self.actor.user_no, self.actor.account_no, self.actor.auth_session_id)):
            raise iam.IAMFailure(401, 'personal_cloud_identity_changed')

    async def rpc(self, method, args):
        rpc_id = 'megmeet-file-' + secrets.token_hex(12)
        body = {'type': 'client-request', 'rpcId': rpc_id, 'method': method, 'payload': {'args': args}}
        headers = {'Authorization': 'Bearer ' + self.upstream['route_token'],
                   'X-Megmeet-Client-Lease': self.lease, 'Accept-Encoding': 'identity'}
        try:
            async with self.client.stream('POST', self.upstream['origin'] + '/api/' + method,
                                          json=body, headers=headers) as response:
                if response.status_code != 200:
                    raise iam.IAMFailure(502, 'personal_cloud_file_unavailable')
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > 8 * 1024 * 1024:
                        raise iam.IAMFailure(502, 'personal_cloud_file_response_invalid')
                    data.extend(part)
            if len(data) >= 64 * 1024:
                value = await anyio.to_thread.run_sync(native_response, data, response.headers.get('content-type', ''), method)
            else:
                value = native_response(data, response.headers.get('content-type', ''), method)
        except (httpx.HTTPError, ValueError):
            raise iam.IAMFailure(502, 'personal_cloud_file_unavailable') from None
        result = value.get('result') if isinstance(value, dict) else None
        if not isinstance(value, dict) or value.get('rpcId') != rpc_id or not isinstance(result, dict):
            raise iam.IAMFailure(502, 'personal_cloud_file_response_invalid')
        if result.get('ok') is not True:
            code = (result.get('error') or {}).get('code')
            status = 404 if code in ('workspace-file/not-found', 'gateway/lookup-not-found') else 403
            raise iam.IAMFailure(status, 'personal_cloud_file_unavailable')
        output = result.get('value')
        if method in ('officeToPdf/generation', 'officeKit/generation') and isinstance(output, str) and 0 < len(output) <= 512:
            return output
        if not isinstance(output, dict):
            raise iam.IAMFailure(502, 'personal_cloud_file_response_invalid')
        return output

    def check_info(self, info):
        # The native resolver returns the canonical path. A symlink to a readable
        # system file must not expand this browser download surface.
        file_path(info.get('absolutePath'))
        size = info.get('bytes')
        if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(info.get('version'), str) or not 1 <= len(info['version']) <= 4096):
            raise iam.IAMFailure(502, 'personal_cloud_file_response_invalid')
        if size > self.maximum:
            raise iam.IAMFailure(413, 'personal_cloud_file_too_large')
        if self.info is not None and any(info.get(k) != self.info.get(k) for k in ('absolutePath', 'version', 'bytes')):
            raise iam.IAMFailure(409, 'personal_cloud_file_changed')

    async def chunks(self):
        offset = 0
        try:
            while offset < self.info['bytes']:
                await self.authorize()
                await cloud.touch_host(self.request, self.actor, activity=self.activity)
                length = min(CHUNK_BYTES, self.info['bytes'] - offset)
                value = await self.rpc('workspaceFiles/readBytes', {**self.args(), 'options': {'range': {'offset': offset, 'length': length}}})
                self.check_info(value)
                try:
                    content = value.get('data', '')
                    if not isinstance(content, bytes):
                        content = base64.b64decode(content, validate=True)
                except (ValueError, TypeError, binascii.Error):
                    raise iam.IAMFailure(502, 'personal_cloud_file_response_invalid') from None
                if (value.get('offset') != offset or len(content) != length
                        or value.get('eof') is not (offset + length == self.info['bytes'])):
                    raise iam.IAMFailure(409, 'personal_cloud_file_changed')
                if offset + length == self.info['bytes']:
                    # Native readBytes reports its pre-read stat. Recheck after
                    # the final read as well, including a one-chunk file.
                    self.check_info(await self.rpc('workspaceFiles/stat', self.args()))
                await self.authorize()
                yield content
                offset += length
        finally:
            await self.close()

    async def close(self):
        # StreamingResponse cancels its AnyIO scope on browser disconnect. Let
        # the private HTTP connection close within a short bounded shield.
        with anyio.move_on_after(3, shield=True):
            await self.client.aclose()


@router.get('/v1/personal-cloud/web/file')
async def download_file(request: Request):
    if request.headers.get('authorization'):
        raise iam.IAMFailure(403, 'web_session_required')
    paths = request.query_params.getlist('path')
    managed = managed_storage.configured()
    params = set(request.query_params)
    if (len(paths) != 1 or params not in ({'path'}, {'path', 'session_id'})
            or len(request.query_params.getlist('session_id')) > 1):
        raise iam.IAMFailure(400, 'personal_cloud_file_path_invalid')
    sid = request.query_params.get('session_id')
    path = file_path(paths[0], allow_relative=sid is not None)
    if managed:
        from .storage_api import human_download_response, invoke
        # A reopened card can carry a workspace-relative path. An already
        # published file must remain downloadable after its worker has stopped.
        published_path = path if path.startswith('/') else '/workspace/' + path
        allocated = re.match(r'^/workspace/maimi-outputs/([0-9a-f-]{36})-', published_path)
        if allocated and sid is None:
            # The publisher allocates a distinct directory per conversation.
            # Lookup must also match this owner and the complete published path.
            sid = 'session-' + allocated.group(1)
        if not sid or not SESSION_ID.fullmatch(sid):
            raise iam.IAMFailure(400, 'personal_cloud_file_scope_invalid')
        if allocated:
            actor = await cloud.actor_for(request)
            store, owner = managed_storage.default_store(), cloud.owner_key(actor)
            try:
                from .restricted_transfer import published
                file_id = await invoke(published, store, owner, sid, published_path)
                if not file_id and 'session_id' not in params:
                    legacy_sid = allocated.group(1)
                    file_id = await invoke(published, store, owner, legacy_sid, published_path)
                    if file_id:
                        sid = legacy_sid
                if file_id:
                    return await human_download_response(request, owner=owner, session=sid, file_id=file_id, store=store)
            except managed_storage.StorageError as exc:
                raise iam.IAMFailure(exc.status, exc.code) from None
    reader = await NativeFileReader.open(request, path, sid)
    if managed:
        return await managed_download(request, reader)
    iterator = reader.chunks()
    try:
        first = await anext(iterator, b'')
    except BaseException:
        await reader.close()
        raise
    async def content():
        try:
            if first:
                yield first
            async for chunk in iterator:
                yield chunk
        finally:
            await iterator.aclose()
            await reader.close()
    filename = reader.path.rsplit('/', 1)[-1]
    return StreamingResponse(content(), media_type='application/octet-stream', headers={**HEADERS,
        'Content-Length': str(reader.info['bytes']),
        'Content-Disposition': "attachment; filename=\"download\"; filename*=UTF-8''" + quote(filename, safe='')},
        background=BackgroundTask(reader.close))


async def managed_download(request, reader):
    """Published ordinary output becomes a 24-hour managed NAS object.

    The source is still a separately bounded temporary workspace copy. Its exact
    version and runtime identity are journaled for later verified reclamation.
    Merely previewing a workspace file does not enter or renew this lifecycle.
    """
    from .storage_api import human_download_response, invoke
    data = bytearray()
    try:
        if not reader.info['absolutePath'].startswith('/workspace/'):
            raise iam.IAMFailure(400, 'personal_cloud_managed_source_required')
        async for chunk in reader.chunks():
            if len(data) + len(chunk) > MAX_FILE_BYTES:
                raise iam.IAMFailure(413, 'personal_cloud_file_too_large')
            data.extend(chunk)
        await reader.authorize()
        store, owner = managed_storage.default_store(), cloud.owner_key(reader.actor)
        fingerprint = hashlib.sha256(reader.upstream['route_token'].encode()).hexdigest()
        source = {'path': reader.info['absolutePath'], 'version': reader.info['version'], 'runtime': fingerprint}
        request_id = managed_storage.workspace_request_id(source['path'],source['version'],fingerprint)
        record = await invoke(store.reserve, owner, reader.session_id, request_id=request_id, kind='ordinary',
            name=source['path'].rsplit('/', 1)[-1], media_type='application/octet-stream', size=len(data), sha256=hashlib.sha256(data).hexdigest())
        await invoke(store.commit, owner, reader.session_id, record['file_id'], bytes(data))
        await invoke(store.record_source_copy, owner, reader.session_id, record['file_id'], path=source['path'],
                     version=source['version'], runtime_fingerprint=fingerprint)
        return await human_download_response(request, owner=owner, session=reader.session_id, file_id=record['file_id'], store=store)
    except managed_storage.StorageError as exc:
        raise iam.IAMFailure(exc.status, exc.code) from None
    finally:
        await reader.close()


@router.get('/maimi/web/files.js')
async def files_script(request: Request):
    from .web_login_entry import program_response
    return program_response(request, FILES_SCRIPT, HEADERS)


FILES_SCRIPT = r'''(()=>{
'use strict';
function validPath(path,scoped){
 if(typeof path!=='string'||!path||path.length>4096||/[\\\x00-\x1f\x7f]/.test(path))return false;
 const absolute=path.startsWith('/');
 if(absolute&&!/^(?:\/workspace\/|\/state\/dsh\/attachments\/v1\/files\/)/.test(path))return false;
 if(!absolute&&(!scoped||path.includes(':')))return false;
 return !path.split('/').slice(absolute?1:0).some(part=>!part||part==='.'||part==='..');
}
function update(){
 for(const card of document.querySelectorAll('[data-presented-file]')){
  const path=card.querySelector('button[title]')?.getAttribute('title');
  const sessionId=card.querySelector('[data-maimi-file-scope]')?.getAttribute('data-maimi-session-id');
  let link=card.querySelector('[data-maimi-download]');
  const allocated=/^\/workspace\/maimi-outputs\/[a-f0-9-]{36}-[a-f0-9-]{36}\//.test(path||'');
  const scoped=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(sessionId||'');
  if(!validPath(path,scoped)||(!allocated&&!scoped)){link?.remove();continue;}
  if(!link){
   link=document.createElement('a');link.setAttribute('data-maimi-download','');
   link.textContent='下载';link.style.cssText='position:relative;z-index:2;flex:none;align-self:center;padding:6px 10px;border:1px solid currentColor;border-radius:6px;font-size:12px;text-decoration:none;white-space:nowrap';
   link.addEventListener('click',event=>{event.stopPropagation();});
   card.appendChild(link);
  }
  const href='/v1/personal-cloud/web/file?'+new URLSearchParams(scoped?{path,session_id:sessionId}:{path});
  if(link.getAttribute('href')!==href)link.setAttribute('href',href);
  link.setAttribute('download',path.slice(path.lastIndexOf('/')+1));
  link.setAttribute('aria-label','下载 '+path.slice(path.lastIndexOf('/')+1));
 }
}
let scheduled=false;
function schedule(){if(scheduled)return;scheduled=true;queueMicrotask(()=>{scheduled=false;update();});}
new MutationObserver(schedule).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['title','data-maimi-session-id']});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',schedule,{once:true});else schedule();
})();'''

# Keep the immutable script URL embedded in previously issued chat pages.
PREVIOUS_FILES_SCRIPT = r'''(()=>{
'use strict';
function validPath(path){return typeof path==='string'&&path.length<=4096&&
 /^(?:\/workspace\/|\/state\/dsh\/attachments\/v1\/files\/)/.test(path)&&
 !/[\\\x00-\x1f\x7f]/.test(path)&&!path.split('/').slice(1).some(part=>!part||part==='.'||part==='..');}
function update(){
 for(const card of document.querySelectorAll('[data-presented-file]')){
  const path=card.querySelector('button[title]')?.getAttribute('title');
  const sessionId=card.querySelector('[data-maimi-file-scope]')?.getAttribute('data-maimi-session-id');
  let link=card.querySelector('[data-maimi-download]');
  const allocated=/^\/workspace\/maimi-outputs\/[a-f0-9-]{36}-[a-f0-9-]{36}\//.test(path||'');
  const scoped=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(sessionId||'');
  if(!validPath(path)||(!allocated&&!scoped)){link?.remove();continue;}
  if(!link){
   link=document.createElement('a');link.setAttribute('data-maimi-download','');
   link.textContent='下载';link.style.cssText='position:relative;z-index:2;flex:none;align-self:center;padding:6px 10px;border:1px solid currentColor;border-radius:6px;font-size:12px;text-decoration:none;white-space:nowrap';
   link.addEventListener('click',event=>{event.stopPropagation();});
   card.appendChild(link);
  }
  const href='/v1/personal-cloud/web/file?'+new URLSearchParams(scoped?{path,session_id:sessionId}:{path});
  if(link.getAttribute('href')!==href)link.setAttribute('href',href);
  link.setAttribute('download',path.slice(path.lastIndexOf('/')+1));
  link.setAttribute('aria-label','下载 '+path.slice(path.lastIndexOf('/')+1));
 }
}
let scheduled=false;
function schedule(){if(scheduled)return;scheduled=true;queueMicrotask(()=>{scheduled=false;update();});}
new MutationObserver(schedule).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['title','data-maimi-session-id']});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',schedule,{once:true});else schedule();
})();'''
