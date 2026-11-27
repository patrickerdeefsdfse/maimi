"""Private A-to-C capability relay. Opening ordinary chat never calls this route."""
import hashlib
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid5
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from . import personal_cloud as cloud
from .iam_oauth import IAMFailure
from .task_network_scope import session_id

router=APIRouter()
PREFIX='/internal/execution/v1/'
OPS=frozenset(('open','call','release','cancel'))

def internal_route(request):
    return request.method=='POST' and request.url.path in {PREFIX+x for x in OPS}

def need(value,code='execution_request_invalid',status=400):
    if not value: raise IAMFailure(status,code)

async def execution_manager(method,path,body=None):
    namespace=os.getenv('PERSONAL_EXECUTION_NAMESPACE','maimi-execution')
    need(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',namespace),'execution_configuration',503)
    suffix=re.escape(namespace)+r'\.svc\.cluster\.local'
    base=os.getenv('PERSONAL_EXECUTION_MANAGER_URL','http://execution-manager.'+namespace+'.svc.cluster.local:8092')
    need(re.fullmatch(r'http://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.'+suffix+r':8092',base),'execution_configuration',503)
    try:
        token=Path(os.environ['PERSONAL_EXECUTION_MANAGER_SECRET_FILE']).read_text().strip()
        need(re.fullmatch(r'[A-Za-z0-9_-]{32,256}',token),'execution_configuration',503)
        async with httpx.AsyncClient(timeout=95,trust_env=False,follow_redirects=False) as client:
            response=await client.request(method,base+path,json=body,headers={'Authorization':'Bearer '+token})
        need(len(response.content)<=32768,'execution_manager_invalid',503)
        value=response.json()
        if response.status_code==202:
            need(value.get('status') in ('queued','starting'),'execution_manager_invalid',503)
            return value
        need(response.status_code==200,'execution_unavailable',409 if response.status_code==409 else 503)
        if 'origin' in value:
            need(re.fullmatch(r'http://mmw-[a-f0-9]{32}-g[1-9][0-9]*\.'+suffix+r':3084',value['origin']),'execution_origin_invalid',503)
            need(re.fullmatch(r'[A-Za-z0-9_-]{43}',value.get('route_token','')),'execution_auth_invalid',503)
        return value
    except IAMFailure: raise
    except Exception: raise IAMFailure(503,'execution_unavailable') from None

async def authorize(request,cleanup=False):
    # Both a current chat generation and its independently revocable user lease.
    auth=request.headers.get('authorization','')
    lease=auth[7:] if auth.startswith('Bearer ') else ''
    actor=await cloud.lease_actor(lease)
    owner=cloud.owner_key(actor)
    token=request.headers.get('x-maimi-chat-token','')
    generation=request.headers.get('x-maimi-chat-generation','')
    need(re.fullmatch(r'[A-Za-z0-9_-]{43}',token) and re.fullmatch(r'[1-9][0-9]{0,9}',generation),'execution_chat_identity',403)
    await cloud.manager('POST','/v1/hosts/'+owner+'/history-authorize',{'route_token':token,'generation':int(generation)})
    # A task lease authenticates the existing chat host, then the server chooses
    # the execution owner and its isolated persistent volumes. Never take the
    # execution scope or target from worker RPC parameters.
    scope = getattr(actor,'task_network_mode',None)
    need(scope in ('public','restricted'),'execution_task_scope_invalid',403)
    if scope == 'restricted':
        sid=session_id(actor.task_session_id)
        need(sid,'execution_task_session_invalid',403)
        task_generation=request.headers.get('x-maimi-task-generation','')
        need(re.fullmatch(r'[1-9][0-9]{0,9}',task_generation),'execution_task_identity',403)
        namespace=UUID(hex=cloud.owner_key(replace(actor,network_mode='confidential')))
        turn=':'.join((sid.lower().removeprefix('session-'),generation,task_generation))
        digest=uuid5(namespace,turn).hex
        # Reserved UUIDv8 namespace. Ordinary employee volumes use UUIDv5.
        task_owner='f7a9'+digest[4:12]+'8'+digest[13:]
        return task_owner,lease,'confidential',actor
    return owner,lease,actor.network_mode,actor

@router.post(PREFIX+'{operation}')
async def internal(request:Request,operation:str):
    need(operation in OPS,'not_found',404)
    need(not request.headers.get('origin') and not request.headers.get('sec-fetch-site'),'execution_browser_denied',403)
    owner,lease,network_mode,actor=await authorize(request)
    data=bytearray()
    async for chunk in request.stream():
        need(len(data)+len(chunk)<=12*1024*1024,'execution_body_too_large',413);data.extend(chunk)
    try: body=json.loads(data)
    except (ValueError,UnicodeError): raise IAMFailure(400,'execution_request_invalid') from None
    need(isinstance(body,dict))
    if operation=='cancel':
        need(set(body)=={'request_id'} and isinstance(body['request_id'],str) and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',body['request_id']))
        value=await execution_manager('POST','/v1/hosts/'+owner+'/queue/cancel',body)
        return JSONResponse(value,headers=cloud.HEADERS)
    if operation=='open':
        need(set(body)<= {'resource_class','queue_request_id'} and body.get('resource_class','standard') in ('light','standard','office'))
        options = {'network_mode': 'confidential'} if network_mode == 'confidential' else {}
        if actor.task_network_mode=='restricted':
            options.update(temporary_turn=True)
            # Profile upgrades replace Pods. A temporary workspace reserves the
            # Office profile initially so a later parser can use the same files.
            body['resource_class']='office'
        value=await execution_manager('POST','/v1/hosts/'+owner+'/open',{'lease':lease,'expires_at':int(time.time())+120,'catalog':{'data':[],'default_model':None},**options,**body})
        # No route token, internal host address or user identifier escapes this relay.
        result={k:v for k,v in value.items() if k in ('status','phase','position','request_id','retry_after','generation','resource_class')}
        if actor.task_network_mode=='restricted' and value.get('status')=='ready':
            result['transfer_required']=True
        return JSONResponse(result,status_code=202 if value.get('status') in ('queued','starting') else 200,headers=cloud.HEADERS)
    need(type(body.get('generation')) is int and body['generation']>0)
    if operation=='call':
        restricted=actor.task_network_mode=='restricted'
        expected={'generation','rpc','transfer'} if restricted else {'generation','rpc'}
        need(set(body)==expected and isinstance(body['rpc'],dict))
        if actor.task_network_mode=='restricted':
            sid=session_id(body['rpc'].get('session_id'))
            need(sid and sid.lower().removeprefix('session-')==
                 actor.task_session_id.lower().removeprefix('session-'),'execution_task_session_invalid',403)
    host=await execution_manager('POST','/v1/hosts/'+owner+'/heartbeat',{'lease':lease,'expires_at':int(time.time())+120,'activity':True})
    need(host.get('generation')==body['generation'],'execution_generation_changed',409)
    if operation=='release':
        value=await execution_manager('POST','/v1/hosts/'+owner+'/release-hint',{'generation':body['generation']})
        return JSONResponse(value,headers=cloud.HEADERS)
    if restricted:
        from .restricted_transfer import prepare, collect
        chat_guard=(int(request.headers['x-maimi-chat-generation']),
                    hashlib.sha256(request.headers['x-maimi-chat-token'].encode()).hexdigest())
        transfer=await prepare(actor,lease,host,body['transfer'],body['rpc'],chat_guard=chat_guard)
    async with httpx.AsyncClient(timeout=45,trust_env=False,follow_redirects=False) as client:
        async with client.stream('POST',host['origin']+'/__cloud/execute',json=body['rpc'],headers={'Authorization':'Bearer '+host['route_token']}) as response:
            content=bytearray()
            async for chunk in response.aiter_bytes():
                need(len(content)+len(chunk)<=16*1024*1024,'execution_response_too_large',503);content.extend(chunk)
            response_status=response.status_code
        try: value=json.loads(content)
        except (ValueError,UnicodeError): raise IAMFailure(503,'execution_response_invalid') from None
        need(isinstance(value,dict),'execution_response_invalid',503)
    # Recheck before returning data. Unknown outcomes are not replayed in a new generation.
    await cloud.lease_actor(lease)
    if response_status!=200:
        code=value.get('code','EXECUTION_UNAVAILABLE')
        return JSONResponse({'code':code if isinstance(code,str) and re.fullmatch(r'[A-Z_]{3,80}',code) else 'EXECUTION_UNAVAILABLE'},status_code=409,headers=cloud.HEADERS)
    if restricted:
        await collect(transfer,body['rpc'],value.get('value'))
        await cloud.lease_actor(lease)
    if body['rpc'].get('operation') in ('process.forget','document.read') or body['rpc'].get('operation','').startswith('fs.'):
        # A hint never overrides the execution agent's independent busy/drain fence.
        try: await execution_manager('POST','/v1/hosts/'+owner+'/release-hint',{'generation':body['generation']})
        except IAMFailure: pass
    return JSONResponse(value,headers=cloud.HEADERS)
