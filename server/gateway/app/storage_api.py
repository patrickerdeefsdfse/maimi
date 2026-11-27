"""Authenticated managed-file transport. No request accepts owner or disk path."""
from __future__ import annotations
import asyncio
import base64
import functools
import hashlib
import logging
import re
import time
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import personal_cloud as cloud
from .storage_lifecycle import DOWNLOAD_BLOCK_BYTES, StorageError, default_store, identity, require,managed_output_path,workspace_request_id

router = APIRouter()
logger = logging.getLogger(__name__)
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff'}
UUID = r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}'
NATIVE_PREFIX = '/internal/storage/native'


def internal_route(request):
    path, method = request.url.path, request.method
    return ((path == NATIVE_PREFIX + '/files' and method == 'POST') or
            (path == NATIVE_PREFIX + '/output' and method == 'POST') or
            (re.fullmatch(NATIVE_PREFIX + '/files/' + UUID, path) is not None and method == 'GET') or
            (path == NATIVE_PREFIX + '/resolve' and method == 'POST') or
            (path == NATIVE_PREFIX + '/events' and method == 'GET'))


async def invoke(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
    except StorageError:
        raise
    except Exception:
        raise StorageError('storage_temporarily_unavailable', 503) from None


def response(value, status=200):
    return JSONResponse(jsonable_encoder(value), status_code=status, headers=HEADERS)


def error(exc):
    return response({'detail': exc.code}, exc.status)


async def native_actor(request):
    auth = request.headers.get('authorization', '')
    require(auth.startswith('Bearer ') and len(auth) <= 100, 'storage_authentication_required', 401)
    return await cloud.lease_actor(auth[7:])


def session_header(request):
    value = request.headers.get('x-maimi-session-id', '')
    identity('0' * 32, value)
    return value


def encoded_header(request,name,maximum):
    raw=request.headers.get(name,'')
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,6000}',raw) is not None,'storage_output_descriptor_invalid',400)
    try:value=base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)).decode('utf-8','strict')
    except (ValueError,UnicodeError):raise StorageError('storage_output_descriptor_invalid',400) from None
    require(0<len(value)<=maximum,'storage_output_descriptor_invalid',400)
    return value


@router.post(NATIVE_PREFIX+'/output')
async def native_output(request:Request):
    """Only the current owner worker can register an allocated output source."""
    try:
        require(not request.query_params and not request.headers.get('origin') and not request.headers.get('sec-fetch-site'),'storage_query_invalid',400)
        owner=request.headers.get('x-maimi-owner','');session=session_header(request);identity(owner,session)
        authorization=request.headers.get('authorization','');generation_text=request.headers.get('x-maimi-generation','')
        require(re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}',authorization) is not None,'storage_authentication_required',401)
        require(re.fullmatch(r'[1-9][0-9]{0,15}',generation_text) is not None,'storage_generation_invalid',400)
        generation=int(generation_text);token=authorization[7:]
        async def authorized():
            result=await cloud.manager('POST','/v1/hosts/'+owner+'/history-authorize',{'generation':generation,'route_token':token})
            require(result=={'authorized':True,'generation':generation},'storage_authentication_required',403)
        await authorized()
        path=managed_output_path(session,encoded_header(request,'x-maimi-source-path',4096))
        version=encoded_header(request,'x-maimi-source-version',256)
        raw_length=request.headers.get('content-length','')
        require(re.fullmatch(r'0|[1-9][0-9]{0,8}',raw_length) is not None and int(raw_length)<=32*1024**2,'storage_upload_too_large',413)
        size=int(raw_length)
        require(re.fullmatch(r'[0-9]{1,30}:[0-9]{1,30}:[0-9]{1,12}:-?[0-9]{1,30}:-?[0-9]{1,30}',version) is not None and
                int(version.split(':')[2])==size,'storage_output_descriptor_invalid',400)
        fingerprint=hashlib.sha256(token.encode()).hexdigest();guard=(generation,fingerprint);store=default_store()
        record=await invoke(store.reserve,owner,session,request_id=workspace_request_id(path,version,fingerprint),kind='ordinary',
            name=path.rsplit('/',1)[-1],media_type='application/octet-stream',size=size,sha256=request.headers.get('x-maimi-sha256',''),native_guard=guard)
        data=bytearray()
        async for chunk in request.stream():
            require(len(data)+len(chunk)<=size,'storage_content_mismatch',400);data.extend(chunk)
        require(len(data)==size,'storage_content_mismatch',400);await authorized()
        result=await invoke(store.commit,owner,session,record['file_id'],bytes(data),native_guard=guard)
        # A failed NAS write must never schedule deletion of the sole C copy.
        await invoke(store.record_source_copy,owner,session,record['file_id'],path=path,version=version,runtime_fingerprint=fingerprint,native_guard=guard)
        return response(result,201)
    except StorageError as exc:return error(exc)


@router.post(NATIVE_PREFIX + '/resolve')
async def native_resolve(request: Request):
    try:
        actor = await native_actor(request)
        require(not request.query_params, 'storage_query_invalid', 400)
        data = bytearray()
        async for chunk in request.stream():
            require(len(data) + len(chunk) <= 4096, 'storage_request_invalid', 400)
            data.extend(chunk)
        import json
        try:
            body = json.loads(data)
        except (ValueError, UnicodeError):
            raise StorageError('storage_request_invalid', 400) from None
        required = {'sha256', 'name', 'bytes'}
        require(type(body) is dict and set(body) in (required, required | {'imageMediaType'}),
                'storage_request_invalid', 400)
        if 'imageMediaType' in body:
            require(isinstance(body['imageMediaType'], str), 'storage_request_invalid', 400)
        return response(await invoke(default_store().resolve_native, cloud.owner_key(actor), session_header(request),
                                     sha256=body['sha256'], name=body['name'], size=body['bytes'],
                                     image_media_type=body.get('imageMediaType')))
    except StorageError as exc:
        return error(exc)


@router.post(NATIVE_PREFIX + '/files')
async def native_upload(request: Request):
    try:
        actor = await native_actor(request)
        require(not request.query_params, 'storage_query_invalid', 400)
        session = session_header(request)
        length = request.headers.get('content-length', '')
        require(re.fullmatch(r'0|[1-9][0-9]{0,8}', length) is not None and int(length) <= 32 * 1024**2,
                'storage_upload_too_large', 413)
        size = int(length)
        encoded = request.headers.get('x-maimi-filename', '')
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,1280}', encoded) is not None, 'storage_filename_invalid', 400)
        try:
            name = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8', 'strict')
        except (ValueError, UnicodeError):
            raise StorageError('storage_filename_invalid', 400) from None
        owner, store = cloud.owner_key(actor), default_store()
        reserved = await invoke(store.reserve, owner, session, request_id=request.headers.get('x-maimi-request-id', ''),
            kind='ordinary', name=name, media_type=request.headers.get('content-type', 'application/octet-stream'),
            size=size, sha256=request.headers.get('x-maimi-sha256', ''))
        data = bytearray()
        async for chunk in request.stream():
            require(len(data) + len(chunk) <= size, 'storage_content_mismatch', 400)
            data.extend(chunk)
        require(len(data) == size, 'storage_content_mismatch', 400)
        # Revoke/expiry is checked again immediately before publication.
        await native_actor(request)
        value = await invoke(store.commit, owner, session, reserved['file_id'], bytes(data))
        return response(value, 201)
    except StorageError as exc:
        return error(exc)


@router.get(NATIVE_PREFIX + '/files/{file_id}')
async def native_read(file_id: str, request: Request):
    lease = None
    try:
        actor = await native_actor(request)
        require(not request.query_params, 'storage_query_invalid', 400)
        session, owner, store = session_header(request), cloud.owner_key(actor), default_store()
        lease = await invoke(store.begin_download, owner, session, file_id)
        data = await invoke(store.read_download, owner, session, lease['lease_id'])
        await native_actor(request)
        # Native tool read is not human download; it must never shorten/renew TTL.
        return Response(data, media_type='application/octet-stream', headers={**HEADERS, 'X-Maimi-SHA256': lease['sha256']})
    except StorageError as exc:
        return error(exc)
    finally:
        if lease:
            await invoke(store.abandon_download, owner, session, lease['lease_id'])


@router.get(NATIVE_PREFIX + '/events')
async def native_events(request: Request):
    try:
        actor = await native_actor(request)
        require(set(request.query_params) <= {'after'}, 'storage_query_invalid', 400)
        raw = request.query_params.get('after', '0')
        require(re.fullmatch(r'[0-9]{1,18}', raw) is not None, 'storage_query_invalid', 400)
        return response({'events': await invoke(default_store().events, cloud.owner_key(actor), after=int(raw))})
    except StorageError as exc:
        return error(exc)


@router.get('/v1/storage/usage')
async def usage(request: Request):
    try:
        actor = await cloud.actor_for(request)
        require(not request.query_params, 'storage_query_invalid', 400)
        return response(await invoke(default_store().usage, cloud.owner_key(actor)))
    except StorageError as exc:
        return error(exc)


@router.get('/v1/storage/files')
async def list_files(request: Request):
    try:
        actor = await cloud.actor_for(request)
        require(set(request.query_params) <= {'offset'}, 'storage_query_invalid', 400)
        raw = request.query_params.get('offset', '0')
        require(re.fullmatch(r'[0-9]{1,5}', raw) is not None, 'storage_query_invalid', 400)
        return response({'files': await invoke(default_store().list_files, cloud.owner_key(actor), offset=int(raw))})
    except StorageError as exc:
        return error(exc)


@router.post('/v1/storage/files/{file_id}/delete')
async def delete_file(file_id: str, request: Request):
    try:
        actor = await cloud.actor_for(request)
        require(not request.query_params, 'storage_query_invalid', 400)
        session = session_header(request)
        # Deletion is queued; clients see quota until exact physical ACK.
        return response(await invoke(default_store().expire_file, cloud.owner_key(actor), session, file_id), 202)
    except StorageError as exc:
        return error(exc)


class ManagedDownloadResponse(StreamingResponse):
    def __init__(self, content, *, abandon, **kwargs):
        super().__init__(content, **kwargs)
        self.abandon = abandon

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # The stream may never start, or may be suspended in send() after a yield.
            await self.abandon()


async def human_download_response(request, *, owner, session, file_id, store=None):
    """Can be used by the existing authenticated project artifact route."""
    store = store or default_store()
    lease = await invoke(store.begin_download, owner, session, file_id)
    size = lease['expected_bytes']
    deadline = time.monotonic() + lease['lease_seconds']
    settled = False

    async def abandon():
        nonlocal settled
        if settled:
            return
        settled = True
        with anyio.move_on_after(3, shield=True) as cleanup:
            try:
                await invoke(store.abandon_download, owner, session, lease['lease_id'])
            except StorageError:
                logger.warning('Managed download cleanup will fall back to lease expiration')
                return
        if cleanup.cancel_called:
            logger.warning('Managed download cleanup timed out')

    try:
        if size > DOWNLOAD_BLOCK_BYTES:
            # Verify the entire object before sending its first byte. Recheck each
            # block before delivery so even an out-of-band edit cannot reach a client.
            hashes = await invoke(store.read_download, owner, session, lease['lease_id'], block_hashes=True)
            data = None
        else:
            hashes = ()
            data = await invoke(store.read_download, owner, session, lease['lease_id'])
        await cloud.actor_for(request)
    except BaseException:
        await abandon()
        raise

    async def body():
        nonlocal settled
        try:
            if data is None:
                for index, digest in enumerate(hashes):
                    require(time.monotonic() < deadline, 'storage_download_expired', 410)
                    block = await invoke(store.blobs.download_block, owner, file_id, size, index, digest)
                    require(time.monotonic() < deadline, 'storage_download_expired', 410)
                    yield block
            else:
                for start in range(0, size, 256 * 1024):
                    require(time.monotonic() < deadline, 'storage_download_expired', 410)
                    yield data[start:start + 256 * 1024]
            # A completed HTTP transfer is NOT a desktop durable-save proof.
            await invoke(store.finish_download, owner, session, lease['lease_id'],
                         bytes_received=size, sha256=lease['sha256'])
            settled = True
        finally:
            await abandon()
    return ManagedDownloadResponse(body(), abandon=abandon, media_type=lease['media_type'], headers={**HEADERS,
        'Content-Length': str(size),
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(lease['name'], safe=''),
        'Content-Security-Policy': "default-src 'none'; sandbox"})


@router.get('/v1/storage/files/{file_id}/download')
async def download(file_id: str, request: Request):
    try:
        actor = await cloud.actor_for(request)
        require(set(request.query_params) == {'session_id'}, 'storage_query_invalid', 400)
        return await human_download_response(request, owner=cloud.owner_key(actor),
            session=request.query_params['session_id'], file_id=file_id)
    except StorageError as exc:
        return error(exc)
