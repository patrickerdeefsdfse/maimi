"""Browser downloads of the signed-in employee's existing cloud files.

The native composed filesystem remains the reader. No host filesystem mounts,
public file URLs, conversion service, or model task authorization are used.
"""
import base64
import binascii
import json
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

router = APIRouter()
MAX_FILE_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
SESSION_ID = re.compile(r'(?:session-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
HEADERS = {**cloud.HEADERS, 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'}


def native_response(data, content_type, method):
    """Decode the reviewed 0.1.7 single-byte-field Remote response only."""
    if content_type.split(';', 1)[0].strip().lower() != 'multipart/form-data':
        return json.loads(data)
    if (method != 'workspaceFiles/readBytes' or len(content_type) > 256
            or '\r' in content_type or '\n' in content_type):
        raise ValueError('Unexpected native binary response')
    message = BytesParser(policy=policy.default).parsebytes(
        b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('ascii') + b'\r\n\r\n' + bytes(data))
    if message.defects or not message.is_multipart():
        raise ValueError('Malformed native multipart response')
    parts = list(message.iter_parts())
    if len(parts) != 2:
        raise ValueError('Unexpected native multipart fields')
    fields = {}
    for part in parts:
        name = part.get_param('name', header='content-disposition')
        if (part.defects or part.is_multipart() or part.get_content_disposition() != 'form-data'
                or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)
                or name in fields or part.get('Content-Transfer-Encoding') is not None):
            raise ValueError('Invalid native binary field')
        fields[name] = part.get_payload(decode=True)
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
    result['value']['data'] = base64.b64encode(content).decode('ascii')
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
        if method == 'officeToPdf/generation' and isinstance(output, str) and 0 < len(output) <= 512:
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
                    content = base64.b64decode(value.get('data', ''), validate=True)
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
    if len(paths) != 1 or set(request.query_params) != {'path'}:
        raise iam.IAMFailure(400, 'personal_cloud_file_path_invalid')
    reader = await NativeFileReader.open(request, paths[0])
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


@router.get('/maimi/web/files.js')
async def files_script():
    return Response(FILES_SCRIPT, media_type='text/javascript', headers=HEADERS)


FILES_SCRIPT = r'''(()=>{
'use strict';
function validPath(path){return typeof path==='string'&&path.length<=4096&&
 /^(?:\/workspace\/|\/state\/dsh\/attachments\/v1\/files\/)/.test(path)&&
 !/[\\\x00-\x1f\x7f]/.test(path)&&!path.split('/').slice(1).some(part=>!part||part==='.'||part==='..');}
function update(){
 for(const card of document.querySelectorAll('[data-presented-file]')){
  const path=card.querySelector('button[title]')?.getAttribute('title');
  let link=card.querySelector('[data-maimi-download]');
  if(!validPath(path)){link?.remove();continue;}
  if(!link){
   link=document.createElement('a');link.setAttribute('data-maimi-download','');
   link.textContent='下载';link.style.cssText='position:relative;z-index:2;flex:none;align-self:center;padding:6px 10px;border:1px solid currentColor;border-radius:6px;font-size:12px;text-decoration:none;white-space:nowrap';
   link.addEventListener('click',event=>{event.stopPropagation();});
   card.appendChild(link);
  }
  const href='/v1/personal-cloud/web/file?'+new URLSearchParams({path});
  if(link.getAttribute('href')!==href)link.setAttribute('href',href);
  link.setAttribute('download',path.slice(path.lastIndexOf('/')+1));
  link.setAttribute('aria-label','下载 '+path.slice(path.lastIndexOf('/')+1));
 }
}
let scheduled=false;
function schedule(){if(scheduled)return;scheduled=true;queueMicrotask(()=>{scheduled=false;update();});}
new MutationObserver(schedule).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['title']});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',schedule,{once:true});else schedule();
})();'''
