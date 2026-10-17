"""Employee-owned native Linux DSH entry. No browser-provided owner or path."""
from __future__ import annotations

from uuid import uuid5, NAMESPACE_URL
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse, Response
from starlette.background import BackgroundTask

from .desktop_auth import TrustedActor, authenticate_desktop_request, desktop_store, _actor
from .history_store import _connect
from .iam_oauth import IAMFailure
from . import iam_oauth as iam
from . import personal_model_proxy as model
from . import cloud_retention as retention
from .personal_cloud_static import PhaseTimings, exact_asset, read_verified_asset
from .personal_cloud_plugin_static import plugin_plan, read_verified_plugins

router=APIRouter()
HEADERS={'Cache-Control':'no-store','Pragma':'no-cache'}
PREFIX='/v1/personal-cloud'
NATIVE_CHECK_SECONDS=5
INTERNAL_MODEL_PATHS=frozenset(('/internal/personal-cloud/model/v1/models','/internal/personal-cloud/model/v1/chat/completions','/internal/personal-cloud/model/v1/session','/internal/personal-cloud/model/v1/vision-capability'))
INTERNAL_CONNECTOR_PATHS=frozenset('/internal/personal-cloud/connectors/'+p for p in ('catalog','invoke','confirm'))
# These endpoints validate a revocable worker lease plus a server-side permit.
# Keep exact methods/paths; the public native proxy still rejects __cloud.
INTERNAL_CONNECTOR_PATHS |= frozenset('/internal/personal-cloud/projects/'+p for p in
    ('catalog','invoke','status','cancel','results','attachments','current-step','drawing-pages','form-ticket','secure-form','human-proof'))


async def actor_for(request):
    # Native transports deliberately revalidate, never use middleware cache.
    if request.headers.get('authorization'):
        actor=await authenticate_desktop_request(request)
        if actor is None:raise IAMFailure(401,'desktop_session_invalid')
        return actor
    # Cookie-backed native RPCs are POSTs, including read operations. Require
    # exact public Origin on every mutation and WebSocket handshake (CSRF/CSWSH).
    if request.scope['type']=='websocket' or request.method not in ('GET','HEAD'):
        if request.headers.get('origin')!=iam.IAMConfig.from_env().public_origin:
            raise IAMFailure(403,'personal_cloud_origin_denied')
    if request.headers.get('sec-fetch-site')=='cross-site':
        raise IAMFailure(403,'personal_cloud_origin_denied')
    user=await iam.authenticate_request(request)
    if user is None:raise IAMFailure(401,'authentication_required')
    return _actor(user,request.cookies.get(iam.SESSION_COOKIE,''))


def personal_workspace_id(actor: TrustedActor) -> str:
    if not isinstance(actor, TrustedActor) or not actor.user_no or len(actor.user_no)>100:
        raise IAMFailure(401, 'trusted_actor_required')
    return str(uuid5(NAMESPACE_URL, 'megmeet:personal-cloud:v1:'+actor.user_no))


def ensure_personal_workspace(actor: TrustedActor) -> dict:
    """Internal DB binding; stable across department/device/session changes."""
    workspace_id = personal_workspace_id(actor)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO platform.hybrid_workspaces
            (id,organization_code,name,kind,owner_user_no) VALUES(%s,'','云端工作区','personal',%s)
            ON CONFLICT(owner_user_no) WHERE kind='personal' DO NOTHING''', (workspace_id,actor.user_no))
        cur.execute('''SELECT id::text AS workspace_id,owner_user_no,name,kind,default_model,enabled,
            TRUE AS can_chat,FALSE AS can_share,TRUE AS can_publish,TRUE AS can_retrieve
            FROM platform.hybrid_workspaces WHERE kind='personal' AND owner_user_no=%s''',(actor.user_no,))
        row=cur.fetchone()
        if not row or not row['enabled']:
            raise IAMFailure(403,'personal_cloud_disabled')
        return dict(row)


def owner_key(actor):
    return personal_workspace_id(actor).replace('-','')


# Public failure categories are closed, source-reviewed constants. Never echo
# arbitrary manager JSON, paths, provider messages, owner ids, or credentials.
MANAGER_OPEN_ERROR_CODES=frozenset(('manager_ownership_mismatch', 'personal_cloud_brand_overlay_unavailable', 'personal_cloud_canary_unavailable', 'personal_cloud_catalog_update_failed', 'personal_cloud_chat_overlay_unavailable', 'personal_cloud_docker_unavailable', 'personal_cloud_fast_entry_overlay_unavailable', 'personal_cloud_history_upgrade_refused', 'personal_cloud_overlay_runtime_mismatch', 'personal_cloud_start_timeout', 'personal_cloud_toolkit_overlay_unavailable', 'personal_cloud_volume_identity_unavailable', 'personal_cloud_volume_ownership_mismatch', 'personal_cloud_worker_overlay_unavailable', 'personal_cloud_worker_resume_failed', 'personal_cloud_worker_state_unavailable', 'personal_cloud_worker_unavailable'))
MANAGER_HISTORY_ERROR_CODES=frozenset(('ARTIFACT_LIMIT', 'DELETION_BASELINE_LIMIT', 'DUAL_GENERATION_WITHOUT_RECEIPT', 'HEADER_LIMIT', 'INCOMPLETE_DEPENDENCY_INVENTORY', 'INVALID_DELETE_MANIFEST', 'LEGACY_MIGRATION_REQUIRED', 'MAINTENANCE_LOCKED', 'MAINTENANCE_REFUSED', 'MIGRATION_ALREADY_ROLLED_BACK', 'MIGRATION_BACKUP_CONFLICT', 'MIGRATION_CORPUS_CHANGED', 'MIGRATION_CORPUS_LIMIT', 'MIGRATION_CORRUPT_ZSTD', 'MIGRATION_DELETION_PENDING', 'MIGRATION_EMPTY_LOG', 'MIGRATION_EVENT_LIMIT', 'MIGRATION_GENERATION_CONFLICT', 'MIGRATION_INCOMPLETE_LOG', 'MIGRATION_INCOMPLETE_ZSTD', 'MIGRATION_RECEIPT_INVALID', 'MIGRATION_RECEIPT_PENDING', 'MIGRATION_SOURCE_CHANGED', 'NATIVE_NOT_QUIESCENT', 'PROJECTION_LIMIT', 'RUNTIME_SNAPSHOT_REQUIRED', 'SESSION_LIMIT', 'SNAPSHOT_CHANGED', 'TRASH_LIMIT', 'UNRECOGNIZED_ARTIFACTS', 'UNRECOGNIZED_GENERATION', 'UNSAFE_DIRECTORY', 'UNSAFE_FILE', 'UNSAFE_MAINTENANCE_LOCK', 'UNSUPPORTED_FORMAT', 'UNSUPPORTED_RUNTIME'))


def manager_open_request(method,path):
    return method=='POST' and re.fullmatch(r'/v1/hosts/[a-f0-9]{32}/open',path) is not None


def manager_open_error(response,method,path):
    if response.status_code!=503 or not manager_open_request(method,path):return None
    try:
        if len(response.content)>4096:return None
        value=response.json()
        if not isinstance(value,dict) or set(value)!={'detail'} or not isinstance(value['detail'],str):return None
        code=value['detail']
        if code in MANAGER_OPEN_ERROR_CODES:return code
        prefix='personal_cloud_history_upgrade_refused:'
        if code.startswith(prefix) and code[len(prefix):] in MANAGER_HISTORY_ERROR_CODES:return code
    except (ValueError,TypeError):pass
    return None


async def manager(method,path,body=None):
    base=os.getenv('PERSONAL_CLOUD_MANAGER_URL','http://personal-cloud-manager:8092')
    if not re.fullmatch(r'http://[a-z0-9-]+:8092',base):raise IAMFailure(503,'personal_cloud_manager_configuration')
    try:
        token=Path(os.environ['PERSONAL_CLOUD_MANAGER_SECRET_FILE']).read_text().strip()
        if len(token)<32:raise ValueError()
        async with httpx.AsyncClient(timeout=100,trust_env=False) as client:
            r=await client.request(method,base+path,headers={'Authorization':'Bearer '+token},json=body)
            if r.status_code==202 and method=='POST' and path.endswith('/open'):
                value=r.json()
                if (value.get('status')=='starting' and value.get('phase')=='preparing'
                    and type(value.get('retry_after')) is int and value['retry_after']==1):
                    return {'status':'starting','phase':'preparing','retry_after':1}
                if value.get('status')!='queued' or type(value.get('position')) is not int or not 1<=value['position']<=128:raise ValueError()
                return {'status':'queued','position':value['position'],'retry_after':5}
            if r.status_code!=200:
                code=manager_open_error(r,method,path) or {429:'personal_cloud_capacity_busy',409:'personal_cloud_open_required'}.get(r.status_code,'personal_cloud_unavailable')
                raise IAMFailure(r.status_code if r.status_code in (429,409) else 503,code)
            result=r.json()
            if method=='GET' and re.fullmatch(r'/v1/hosts/[a-f0-9]{32}/activity',path):
                if (not isinstance(result,dict) or result.get('status')!='ready'
                    or type(result.get('idle_seconds')) is not int or not (result['idle_seconds']==0 or 30<=result['idle_seconds']<=3600)
                    or type(result.get('idle_for_seconds')) is not int or result['idle_for_seconds']<0
                    or result.get('busy') is not None and type(result.get('busy')) is not bool):raise ValueError()
                return {key:result[key] for key in ('status','idle_seconds','idle_for_seconds','busy')}
            if not re.fullmatch(r'http://[a-z0-9-]+:3084',result['origin']) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',result['route_token']):raise ValueError()
            return result
    except IAMFailure:raise
    except httpx.TimeoutException:
        raise IAMFailure(503,'personal_cloud_manager_timeout' if manager_open_request(method,path) else 'personal_cloud_unavailable') from None
    except httpx.NetworkError:
        raise IAMFailure(503,'personal_cloud_manager_connection_failed' if manager_open_request(method,path) else 'personal_cloud_unavailable') from None
    except Exception:raise IAMFailure(503,'personal_cloud_unavailable') from None


async def issue_lease(request,actor):
    token=request.headers.get('authorization','')[7:]
    async def issue(service):
        grant_store=desktop_store(service)
        grant=await grant_store.read_grant(token) if token else None
        if token and (not grant or grant['sid']!=actor.auth_session_id or grant['expires_at']<=time.time()):raise IAMFailure(401,'desktop_session_expired')
        lease_deadline=await service.session_deadline(actor.auth_session_id,actor.user_no,actor.account_no)
        # Client leases remain independently revocable. They do not own the Host.
        client=('desktop:'+token) if token else ('web:'+actor.auth_session_id)
        index='megmeet:personal-cloud:grant:'+hashlib.sha256(client.encode()).hexdigest()
        redis=service.store.redis
        value=await redis.get(index)
        if value is None:
            candidate='mcl_'+secrets.token_urlsafe(32)
            await redis.set(index,candidate,ex=max(1,int(lease_deadline-time.time())),nx=True)
            value=await redis.get(index)
        if isinstance(value,bytes):value=value.decode()
        record={'sid':actor.auth_session_id,'user_no':actor.user_no,'account_no':actor.account_no,
                'grant_key':grant_store.key('grant',token) if token else None,'expires_at':lease_deadline}
        await redis.set('megmeet:personal-cloud:lease:'+hashlib.sha256(value.encode()).hexdigest(),json.dumps(record),ex=max(1,int(lease_deadline-time.time())))
        return value,min(grant['expires_at'] if grant else lease_deadline,lease_deadline,int(time.time())+900)
    return await iam._with_service(issue)


async def lease_actor(token):
    if not re.fullmatch(r'mcl_[A-Za-z0-9_-]{43}',token):raise IAMFailure(401,'personal_cloud_lease_invalid')
    async def validate(service):
        raw=await service.store.redis.get('megmeet:personal-cloud:lease:'+hashlib.sha256(token.encode()).hexdigest())
        if not raw:raise IAMFailure(401,'personal_cloud_lease_expired')
        data=json.loads(raw)
        if data.get('grant_key'):
            grant_raw=await service.store.redis.get(data['grant_key'])
            grant=json.loads(grant_raw) if grant_raw else None
            if (not grant or grant['sid']!=data['sid'] or min(data['expires_at'],grant['expires_at'])<=time.time()):raise IAMFailure(401,'personal_cloud_lease_expired')
        elif data['expires_at']<=time.time():raise IAMFailure(401,'personal_cloud_lease_expired')
        user=await service.authenticate(data['sid'])
        if not user or user['user_no']!=data['user_no'] or user['account_no']!=data['account_no']:raise IAMFailure(401,'personal_cloud_lease_expired')
        return _actor(user,data['sid'])
    return await iam._with_service(validate)


async def confirmation_receipt(lease,confirmation_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}',confirmation_id or ''):
        raise IAMFailure(400,'connector_confirmation_invalid')
    receipt=secrets.token_urlsafe(32)
    async def issue(service):
        await service.store.redis.set('megmeet:cloud-confirm:'+hashlib.sha256(receipt.encode()).hexdigest(),
            json.dumps({'lease':lease,'confirmation_id':confirmation_id}),ex=30,nx=True)
    await iam._with_service(issue)
    return receipt


async def consume_confirmation_receipt(receipt,lease,confirmation_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',receipt or ''):
        raise IAMFailure(403,'connector_user_confirmation_required')
    expected=json.dumps({'lease':lease,'confirmation_id':confirmation_id})
    async def consume(service):
        return await service.store.redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then redis.call('DEL',KEYS[1]); return 1 else return 0 end",
            1,'megmeet:cloud-confirm:'+hashlib.sha256(receipt.encode()).hexdigest(),expected)
    if not await iam._with_service(consume):raise IAMFailure(403,'connector_user_confirmation_required')


async def touch_host(request,actor,*,activity=None,include_lease=False,presence_id=None,present=True):
    """Separate authorization renewal from a person's actual web interaction."""
    # Existing desktop clients do not yet send explicit user-activity events.
    # Their established behavior stays compatible; cookie-backed web transport
    # and its 5-second WebSocket guard never count as human activity.
    await asyncio.to_thread(retention.access,owner_key(actor),actor.user_no,activity=activity is True)
    if activity is None:activity=bool(request.headers.get('authorization'))
    lease,expires=await issue_lease(request,actor)
    # Authenticate and issue a fresh client lease *before* asking for the host.
    # The old GET-first sequence rejected a healthy retained process at minute
    # 15, leaving its UI reconnecting forever before renewal was reachable.
    body={'lease':lease,'expires_at':int(expires),'activity':bool(activity)}
    if presence_id:body.update(presence_id=presence_id,present=present)
    record=await manager('POST','/v1/hosts/'+owner_key(actor)+'/heartbeat',body)
    return (record,lease,expires) if include_lease else record


@router.post(PREFIX+'/presence')
async def client_presence(request:Request):
    actor=await actor_for(request)
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>256:raise IAMFailure(400,'personal_cloud_presence_invalid')
    try:
        value=json.loads(raw)
        if (not isinstance(value,dict) or set(value)!={'client_id','present'}
            or not isinstance(value['client_id'],str)
            or not re.fullmatch(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}',value['client_id'])
            or type(value['present']) is not bool):raise ValueError()
    except (ValueError,TypeError):raise IAMFailure(400,'personal_cloud_presence_invalid') from None
    # Client-selected instance IDs cannot remove another IAM session's page.
    identity=json.dumps([actor.auth_session_id,value['client_id'].lower()],separators=(',',':'))
    presence_id=hashlib.sha256(('page:'+identity).encode()).hexdigest()
    await touch_host(request,actor,activity=False,presence_id=presence_id,present=value['present'])
    busy=None
    if value['present']:
        try:
            activity=await manager('GET','/v1/hosts/'+owner_key(actor)+'/activity')
            if type(activity.get('busy')) is bool:busy=activity['busy']
        except IAMFailure as exc:
            # Process presence is still renewed. An unavailable activity sample
            # must never be interpreted as permission to refresh a busy UI.
            if exc.status!=503:raise
    await actor_for(request)
    return JSONResponse({'status':'ready','closed_retention_seconds':7200,'busy':busy},headers=HEADERS)


@router.api_route(PREFIX+'/web/activity',methods=['GET','POST'])
async def web_activity(request:Request):
    if request.headers.get('authorization'):raise IAMFailure(403,'web_session_required')
    actor=await actor_for(request)
    if request.method=='POST':
        payload=bytearray()
        async for chunk in request.stream():
            if len(payload)+len(chunk)>2:raise IAMFailure(400,'personal_cloud_activity_takes_no_identity')
            payload.extend(chunk)
        if payload not in (b'',b'{}'):raise IAMFailure(400,'personal_cloud_activity_takes_no_identity')
    try:
        await touch_host(request,actor,activity=request.method=='POST')
        value=await manager('GET','/v1/hosts/'+owner_key(actor)+'/activity')
    except IAMFailure as exc:
        if exc.status!=409:raise
        # An absent host is reported for the bounded entry controller to open.
        # Activity polling itself never allocates a new process.
        return JSONResponse({'status':'released','idle_seconds':0,'idle_for_seconds':0,'busy':False},headers=HEADERS)
    await actor_for(request)
    if value.get('busy') is True:
        await asyncio.to_thread(retention.access,owner_key(actor),actor.user_no,activity=True)
    return JSONResponse(value,headers=HEADERS)


@router.get(PREFIX+'/workspace')
async def metadata(request:Request):
    actor=await actor_for(request)
    row=await asyncio.to_thread(ensure_personal_workspace,actor)
    # Listing is metadata-only. It never launches or acquires a Host.
    return JSONResponse({'workspace_id':row['workspace_id'],'name':'云端工作区','kind':'personal-cloud',
       'storage':'server','manual_entry':True,'removable':False,'renamable':False,
       'protocol':'megmeet-personal-native','version':1},headers=HEADERS)


@router.get(PREFIX+'/history/trash')
async def history_trash(request:Request,page:int=0):
    from .cloud_history import trash_for
    actor=await actor_for(request)
    if retention.enabled():return JSONResponse({'items':[],'next_page':None,'recovery_days':0,'permanent_deletion':True},headers=HEADERS)
    await asyncio.to_thread(ensure_personal_workspace,actor)
    items=await asyncio.to_thread(trash_for,owner_key(actor),actor.user_no,page)
    return JSONResponse({'items':items[:50],'next_page':page+1 if len(items)>50 else None,'inactive_days':90,'recovery_days':30},headers=HEADERS)


@router.post(PREFIX+'/history/restore')
async def history_restore(request:Request):
    from .cloud_history import request_restore
    actor=await actor_for(request)
    if retention.enabled():raise IAMFailure(410,'cloud_history_restore_removed')
    await asyncio.to_thread(ensure_personal_workspace,actor)
    raw=await request.body()
    if len(raw)>2048:raise IAMFailure(400,'cloud_history_request_invalid')
    try:
        body=json.loads(raw)
        if set(body)!= {'session_id'} or not isinstance(body['session_id'],str) or not 1<=len(body['session_id'])<=200:raise ValueError()
    except (ValueError,TypeError):raise IAMFailure(400,'cloud_history_request_invalid') from None
    result=await asyncio.to_thread(request_restore,owner_key(actor),actor.user_no,body['session_id'])
    return JSONResponse(result,headers=HEADERS,status_code=202)


@router.post(PREFIX+'/open')
async def open_workspace(request:Request):
    timing=PhaseTimings('open')
    actor=await actor_for(request)
    timing.mark('auth')
    row=await asyncio.to_thread(ensure_personal_workspace,actor)
    timing.mark('workspace')
    if await request.body() not in (b'',b'{}'):raise IAMFailure(400,'personal_cloud_open_takes_no_identity')
    await asyncio.to_thread(retention.access,owner_key(actor),actor.user_no,activity=True)
    timing.mark('retention')
    lease,expires=await issue_lease(request,actor)
    timing.mark('lease')
    try:
        opened=await manager('POST','/v1/hosts/'+owner_key(actor)+'/resume',{'lease':lease,'expires_at':int(expires),'activity':True})
    except IAMFailure as exc:
        if exc.status!=409 or exc.code!='personal_cloud_open_required':raise
        catalog=await model.get_broker().models(actor)
        timing.mark('catalog')
        opened=await manager('POST','/v1/hosts/'+owner_key(actor)+'/open',{'lease':lease,'expires_at':int(expires),'catalog':catalog})
    timing.mark('manager')
    await actor_for(request)
    timing.emit()
    if opened.get('status') in ('queued','starting'):
        # Released 0.9.0 desktop only understands queued; the phase lets newer
        # clients show preparation without falsely claiming capacity is full.
        if opened['status']=='starting' and request.headers.get('authorization','').startswith('Bearer '):
            opened={'status':'queued','phase':'starting','position':1,'retry_after':1}
        return JSONResponse(opened,status_code=202,headers={**HEADERS,'Retry-After':str(opened['retry_after'])})
    return JSONResponse({'workspace_id':row['workspace_id'],'name':'云端工作区','native_workspace_id':opened['native_workspace_id'],
        'path':'/workspace','native_prefix':PREFIX+'/native/','storage':'server','expires_at':expires},headers=HEADERS)


@router.post(PREFIX+'/models/refresh')
async def refresh_models(request:Request):
    actor=await actor_for(request)
    await asyncio.to_thread(ensure_personal_workspace,actor)
    catalog=await model.get_broker().models(actor)
    lease,expires=await issue_lease(request,actor)
    await manager('POST','/v1/hosts/'+owner_key(actor)+'/catalog',{'lease':lease,'expires_at':int(expires),'catalog':catalog})
    current=await actor_for(request)
    if (current.user_no,current.account_no,current.auth_session_id)!=(actor.user_no,actor.account_no,actor.auth_session_id):
        raise IAMFailure(401,'personal_cloud_identity_changed')
    preferred=catalog['default_model']==model.DEFAULT_MODEL
    return JSONResponse({'models':catalog['data'],'defaultModel':catalog['default_model'],
        'preferredAvailable':preferred,'notice':('当前账号未开通 DeepSeek V4.1 Flash，请联系管理员开通。' if not catalog['data'] else
            '' if preferred else '当前账号未开通 DeepSeek V4.1 Flash，请联系管理员开通。'),
        'native_reload_required':False},headers=HEADERS)


def native_path(path):
    decoded=unquote(path)
    if len(path)>4096 or decoded.startswith('/') or any(x in decoded for x in ('\\','\x00','\r','\n')) or any(p in ('.','..') for p in decoded.split('/')) or decoded.startswith(('__cloud','api/megmeet-cloud/')):
        raise IAMFailure(400,'personal_cloud_path_invalid')
    return '/'+path


@router.api_route(PREFIX+'/native/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','HEAD'])
async def native_http(request:Request,path:str):
    plugins=plugin_plan(request.method,'/'+path,request.url.query,request.headers)
    timing=PhaseTimings('index' if path=='' else 'plugins' if plugins is not None else 'static' if exact_asset(request.method,'/'+path,request.url.query) else 'other')
    actor=await actor_for(request)
    timing.mark('auth')
    await asyncio.to_thread(ensure_personal_workspace,actor)
    timing.mark('workspace')
    path=native_path(path)
    upstream,client_lease,_=await touch_host(request,actor,include_lease=True)
    timing.mark('host')
    body=bytearray()
    upload_progress=time.monotonic()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>32*1024*1024:raise IAMFailure(413,'personal_cloud_request_too_large')
        if path=='/api/session/uploadFileBinary' and chunk and time.monotonic()-upload_progress>=15:
            # Incoming file bytes are real work. Protect a progressing upload
            # before it has reached the worker's own in-flight request fence.
            upstream,client_lease,_=await touch_host(request,actor,activity=True,include_lease=True)
            upload_progress=time.monotonic()
    timing.mark('body')
    if retention.enabled():
        # Body and endpoint must agree before interpreting a native identity.
        envelope=None
        if request.method=='POST' and path.startswith('/api/') and 'json' in request.headers.get('content-type',''):
            try:envelope=json.loads(body)
            except (ValueError,TypeError):pass
        if isinstance(envelope,dict) and envelope.get('type')=='client-request' and envelope.get('method')==path[5:]:
            native_payload=envelope.get('payload')
            native_args=native_payload.get('args') if isinstance(native_payload,dict) else None
            native_request=native_args.get('request') if isinstance(native_args,dict) else None
            if not isinstance(native_request,dict):native_request={}
            sid=native_request.get('sessionId')
            if path=='/api/workspace/unarchiveSession':raise IAMFailure(410,'cloud_history_restore_removed')
            if path=='/api/workspace/archiveSession':
                # Irreversible intent is committed before native UI hides it.
                # Offline maintenance removes the physical log after writers exit.
                await asyncio.to_thread(retention.queue_session_delete,owner_key(actor),actor.user_no,sid)
            else:
                meaningful=path in {'/api/session/page','/api/session/prompt','/api/session/create','/api/session/attachment',
                    '/api/workspaceFiles/read','/api/workspaceFiles/readBytes','/api/workspaceFiles/readAll'}
                await asyncio.to_thread(retention.access,owner_key(actor),actor.user_no,activity=meaningful,
                    session_id=sid if isinstance(sid,str) and path.startswith('/api/session/') else None)
        elif path in ('/','/api/session/uploadFileBinary'):
            await asyncio.to_thread(retention.access,owner_key(actor),actor.user_no,activity=True)
    timing.mark('retention')
    headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','accept','range','if-none-match','if-modified-since')}
    headers['Authorization']='Bearer '+upstream['route_token']
    # Never pass a browser-supplied lease. Worker task ownership is derived from
    # this request's independently authenticated client and remains server-only.
    # Reuse this request's issued lease, but revalidate it after request-body
    # reading. Logout/grant revocation still stops the worker request.
    fresh=await lease_actor(client_lease)
    if fresh!=actor:raise IAMFailure(401,'personal_cloud_identity_changed')
    timing.mark('lease')
    headers['X-Megmeet-Client-Lease']=client_lease
    from .project_platform import HUMAN_PATHS, issue_human_proof
    if path in HUMAN_PATHS:
        if request.method!='POST' or request.headers.get('origin')!=iam.IAMConfig.from_env().public_origin:
            raise IAMFailure(403,'project_human_origin_required')
        headers['X-Megmeet-Project-User']=await issue_human_proof(client_lease,path,bytes(body))
    if path=='/__megmeet/tools/confirm':
        if request.method!='POST' or request.headers.get('origin')!=iam.IAMConfig.from_env().public_origin:
            raise IAMFailure(403,'connector_user_confirmation_required')
        try:confirmation=json.loads(body)
        except (TypeError,ValueError):raise IAMFailure(400,'connector_confirmation_invalid') from None
        if isinstance(confirmation,dict) and confirmation.get('confirmed') is True:
            headers['X-Megmeet-User-Confirm']=await confirmation_receipt(client_lease,confirmation.get('confirmation_id'))
    headers['Accept-Encoding']='identity'
    client=httpx.AsyncClient(timeout=httpx.Timeout(120,connect=10),trust_env=False,follow_redirects=False)
    try:
        url=httpx.URL(upstream['origin']+path).copy_with(query=request.url.query.encode())
        response=await client.send(client.build_request(request.method,url,headers=headers,content=bytes(body)),stream=True)
    except Exception:
        await client.aclose();raise IAMFailure(502,'personal_cloud_native_unavailable') from None
    out={k:v for k,v in response.headers.items() if k.lower() in ('content-type','content-length','content-encoding','content-range','accept-ranges','etag','last-modified','content-disposition')}
    out.update(HEADERS)
    timing.mark('upstream')
    async def close():await response.aclose();await client.aclose()
    if (plugins is not None or exact_asset(request.method,path,request.url.query)) and response.status_code==200:
        async def validate_static():
            if await actor_for(request)!=actor:raise IAMFailure(401,'personal_cloud_identity_changed')
        try:
            static_body,cacheable=await (read_verified_plugins(response,plugins,validate_static) if plugins is not None else read_verified_asset(response,path,validate_static))
            out.pop('content-length',None)
            if cacheable:
                out['Cache-Control']='private, max-age=31536000, immutable'
                out.pop('Pragma',None)
            timing.mark('static');timing.emit(cached=cacheable)
            return Response(static_body,status_code=200,headers=out)
        finally:await close()
    timing.emit()
    async def chunks():
        checked=0
        try:
            async for chunk in response.aiter_raw():
                if time.monotonic()-checked>5:await actor_for(request);checked=time.monotonic()
                yield chunk
        finally:await close()
    return StreamingResponse(chunks(),status_code=response.status_code,headers=out,background=BackgroundTask(close))


@router.websocket(PREFIX+'/native/api/remote.mux')
async def native_ws(socket:WebSocket):
    from websockets.legacy.client import connect
    presence_id=hashlib.sha256(('socket:'+secrets.token_urlsafe(32)).encode()).hexdigest()
    actor=None;close_code=1012
    try:
        actor=await actor_for(socket)
        await asyncio.to_thread(ensure_personal_workspace,actor)
        record=await touch_host(socket,actor,activity=False,presence_id=presence_id)
        protocols=[x.strip() for x in socket.headers.get('sec-websocket-protocol','').split(',') if x.strip()]
        options={'subprotocols':protocols} if protocols else {}
        async with connect(record['origin'].replace('http://','ws://')+'/api/remote.mux',extra_headers={'Authorization':'Bearer '+record['route_token']},open_timeout=10,max_size=16*1024*1024,compression=None,**options) as upstream:
            await socket.accept(subprotocol=upstream.subprotocol)
            async def to_host():
                while True:
                    message=await socket.receive()
                    if message['type']=='websocket.disconnect':return
                    await upstream.send(message.get('bytes') if message.get('bytes') is not None else message['text'])
            async def to_browser():
                async for message in upstream:
                    if isinstance(message,bytes):await socket.send_bytes(message)
                    else:await socket.send_text(message)
            async def guard():
                while True:
                    await asyncio.sleep(NATIVE_CHECK_SECONDS)
                    fresh=await actor_for(socket)
                    if (fresh.user_no,fresh.account_no,fresh.auth_session_id)!=(actor.user_no,actor.account_no,actor.auth_session_id):raise IAMFailure(401,'personal_cloud_identity_changed')
                    current=await touch_host(socket,fresh,activity=False,presence_id=presence_id)
                    if current['route_token']!=record['route_token']:raise IAMFailure(401,'personal_cloud_generation_changed')
            tasks=[asyncio.create_task(f()) for f in (to_host,to_browser,guard)]
            try:
                done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in done:task.result()
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
    except Exception as exc:
        if getattr(exc,'status',None) in (401,403):close_code=1008
        logging.getLogger('megmeet.personal-cloud').warning('native websocket rejected: %s status=%s',type(exc).__name__,getattr(exc,'status_code',getattr(exc,'status','unknown')))
    finally:
        if actor:
            # Only release this socket's presence, never a sibling tab's.
            # Failed/revoked sessions expire their presence after the short TTL.
            try:
                fresh=await actor_for(socket)
                if (fresh.user_no,fresh.account_no,fresh.auth_session_id)==(actor.user_no,actor.account_no,actor.auth_session_id):
                    await touch_host(socket,fresh,activity=False,presence_id=presence_id,present=False)
            except Exception:pass
        try:await socket.close(code=close_code)
        except Exception:pass


async def _worker_model_response(request:Request,operation:str):
    if request.url.path not in INTERNAL_MODEL_PATHS:raise IAMFailure(404,'not_found')
    authorization=request.headers.get('authorization','')
    lease=authorization[7:] if authorization.startswith('Bearer ') else ''
    actor=await lease_actor(lease)
    if operation=='session' and request.method=='GET':
        # Called only while the worker has an original active task lease.
        await asyncio.to_thread(retention.access,owner_key(actor),actor.user_no,activity=True)
        return JSONResponse({'authenticated':True},headers=HEADERS)
    broker=model.get_broker()
    async def validate(current):
        latest=await lease_actor(lease)
        if latest.user_no!=current.user_no:raise IAMFailure(401,'personal_cloud_identity_changed')
        return latest
    broker.actor_validator=validate
    if operation=='models' and request.method=='GET':return JSONResponse(await broker.models(actor),headers=HEADERS)
    if operation=='vision-capability' and request.method=='POST':
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>1024:raise IAMFailure(413,'personal_model_request_too_large')
            raw.extend(chunk)
        try:
            value=json.loads(raw)
            if not isinstance(value,dict) or set(value)!={'model'} or not model._model_text(value['model']):raise ValueError()
        except (TypeError,ValueError):raise IAMFailure(400,'personal_model_request_invalid') from None
        return JSONResponse(await broker.ensure_vision(actor,value['model']),headers=HEADERS)
    if operation!='chat/completions' or request.method!='POST':raise IAMFailure(405,'method_not_allowed')
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>model.MAX_BODY:raise IAMFailure(413,'personal_model_request_too_large')
    try:body=model.validate_body(json.loads(raw))
    except (TypeError,ValueError):raise IAMFailure(400,'personal_model_request_invalid') from None
    if body.get('stream') is True:return await broker.streaming(actor,body)
    return JSONResponse(await broker.complete(actor,body),headers=HEADERS)


# Only fixed codes/messages leave the internal model route. Never forward a
# provider error, exception string, URL, token or employee identity.
_MODEL_ERROR_MESSAGES = {
    'personal_cloud_lease_invalid': '云端登录已失效，请重新进入工作区。',
    'personal_cloud_lease_expired': '云端登录已失效，请重新进入工作区。',
    'personal_cloud_identity_changed': '云端登录状态已变化，请重新进入工作区。',
    'personal_key_reauthorization_required': '模型授权已失效，请重新登录后再试。',
    'iam_personal_credentials_require_login': '模型授权已失效，请重新登录后再试。',
    'personal_key_unavailable': '当前账号的模型授权暂不可用，请联系管理员确认。',
    'personal_model_not_authorized': '当前账号没有该模型的使用权限，请切换已授权模型。',
    'personal_model_request_invalid': '模型请求格式不正确，请重新发送。',
    'personal_model_request_too_large': '本次模型请求过大，请减少附件或内容后再试。',
    'personal_model_upstream_unavailable': '模型服务暂未返回有效响应，可能超时或服务异常，请稍后重试或切换模型。',
    'personal_model_response_invalid': '模型服务返回的内容格式异常，请稍后重试或切换模型。',
    'personal_model_response_too_large': '模型返回的内容过大，请减少本次处理范围后再试。',
    'personal_key_service_unavailable': '模型授权服务暂时不可用，请稍后重试。',
    'iam_session_store_unavailable': '云端登录服务暂时不可用，请稍后重试。',
    'iam_service_unavailable': '云端登录服务暂时不可用，请稍后重试。',
}


def _model_failure_category(error):
    # Even suppressed ("from None") contexts retain their type. Follow at most
    # three nodes, and never inspect or serialize messages/requests/responses.
    for _ in range(3):
        if isinstance(error, httpx.TimeoutException): return 'timeout'
        if isinstance(error, httpx.ConnectError): return 'connect'
        error = error.__cause__ if error.__cause__ is not None else error.__context__
        if error is None: break
    return 'generic'


@router.api_route('/internal/personal-cloud/model/v1/{operation:path}',methods=['GET','POST'])
async def worker_model(request:Request,operation:str):
    try:
        return await _worker_model_response(request,operation)
    except IAMFailure as error:
        if request.url.path != '/internal/personal-cloud/model/v1/chat/completions': raise
        code = error.code if isinstance(error.code,str) and error.code in _MODEL_ERROR_MESSAGES else 'cloud_model_request_failed'
        message = _MODEL_ERROR_MESSAGES.get(code,'本次模型请求暂未完成，请稍后重试。')
        status = error.status
        if status == 429: message = '模型请求较多，请稍后重试。'
        log_status = status if type(status) is int and 400 <= status <= 599 else 'other'
        logging.getLogger('megmeet.personal-cloud').warning(
            'MEGMEET_MODEL_REQUEST_FAILED code=%s status=%s cause_category=%s',
            code,log_status,_model_failure_category(error))
        # This wraps failures before a response is returned. Errors raised while
        # iterating an already-started SSE stream retain the broker's contract.
        return JSONResponse({'error':{'message':message,'type':'api_error','code':code}},status_code=status,headers=HEADERS)


@router.api_route('/internal/personal-cloud/connectors/{operation}',methods=['GET','POST'])
async def worker_connector(request:Request,operation:str):
    from . import enterprise_connectors as connectors
    from pydantic import ValidationError
    if request.url.path not in INTERNAL_CONNECTOR_PATHS:raise IAMFailure(404,'not_found')
    authorization=request.headers.get('authorization','')
    lease=authorization[7:] if authorization.startswith('Bearer ') else ''
    actor=await lease_actor(lease)
    if operation=='catalog' and request.method=='GET':
        return JSONResponse(await connectors.respond(connectors.service().catalog(actor)),headers=HEADERS)
    if request.method!='POST' or operation not in ('invoke','confirm'):raise IAMFailure(405,'method_not_allowed')
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>32768:raise IAMFailure(413,'connector_request_too_large')
    try:
        schema=connectors.ConfirmRequest if operation=='confirm' else connectors.InvokeRequest
        body=schema.model_validate_json(bytes(raw))
    except ValidationError:raise IAMFailure(400,'connector_request_invalid') from None
    if operation=='confirm':
        await consume_confirmation_receipt(request.headers.get('x-megmeet-user-confirm',''),lease,body.confirmation_id)
        actor=await lease_actor(lease)
        result=await connectors.respond(connectors.service().confirm(actor,body.confirmation_id))
    else:
        async def validate(current):
            fresh=await lease_actor(lease)
            if current!=actor or fresh!=actor:raise IAMFailure(403,'connector_identity_changed')
            return fresh
        result=await connectors.respond(connectors.service().invoke(actor,body,actor_validator=validate))
    await lease_actor(lease)
    return JSONResponse(result,headers=HEADERS)


@router.post('/internal/personal-cloud/projects/{operation}')
async def worker_project(request:Request,operation:str):
    from .project_platform import INTERNAL_PATHS,internal
    if request.url.path not in INTERNAL_PATHS:raise IAMFailure(404,'not_found')
    return await internal(request,operation)


@router.get(PREFIX+'/projects/artifact')
@router.get('/__megmeet/projects/artifact')
async def project_artifact(request:Request):
    from .project_platform import artifact_response
    return await artifact_response(request)
