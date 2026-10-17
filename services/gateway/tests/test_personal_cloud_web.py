"""Synthetic web/desktop identity and transport regressions; no company calls."""
import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocket
from app import personal_cloud as cloud, personal_cloud_web as web, iam_oauth as iam
from app.desktop_auth import TrustedActor, RedisDesktopStore
from test_iam_oauth import config,env_config,make_service
from test_personal_cloud import catalog_manager

A=TrustedActor('EMPLOYEE-A','account-A','A',(),'a'*43)
A2=TrustedActor('EMPLOYEE-A','account-A','A',(),'b'*43)
B=TrustedActor('EMPLOYEE-B','account-B','B',(),'c'*43)

def request(*,method='GET',origin=None,cookie=None,token=None,websocket=False):
    headers=[]
    if origin:headers.append((b'origin',origin.encode()))
    if cookie:headers.append((b'cookie',(iam.SESSION_COOKIE+'='+cookie).encode()))
    if token:headers.append((b'authorization',('Bearer '+token).encode()))
    scope={'type':'websocket' if websocket else 'http','method':method,'headers':headers,'path':'/api/session/prompt','query_string':b'','scheme':'https','server':('app.test',443)}
    return WebSocket(scope,None,None) if websocket else Request(scope)

@pytest.mark.parametrize('websocket',[False,True])
@pytest.mark.parametrize('origin',[None,'null','https://evil.test'])
def test_cookie_native_mutation_and_ws_require_exact_origin(config,monkeypatch,websocket,origin):
    monkeypatch.setattr(iam.IAMConfig,'from_env',lambda:config)
    async def forbidden(_):raise AssertionError('cross-site request authenticated')
    monkeypatch.setattr(iam,'authenticate_request',forbidden)
    with pytest.raises(iam.IAMFailure) as error:
        asyncio.run(cloud.actor_for(request(method='POST',origin=origin,cookie=A.auth_session_id,websocket=websocket)))
    assert error.value.status==403

def test_same_origin_web_cookie_and_desktop_are_independent(config,monkeypatch):
    monkeypatch.setattr(iam.IAMConfig,'from_env',lambda:config)
    async def authenticate(req):return A.platform() if req.cookies.get(iam.SESSION_COOKIE)==A.auth_session_id else None
    monkeypatch.setattr(iam,'authenticate_request',authenticate)
    actor=asyncio.run(cloud.actor_for(request(method='POST',origin=config.public_origin,cookie=A.auth_session_id)))
    assert actor.user_no==A.user_no and actor.auth_session_id==A.auth_session_id
    async def desktop(req):return A2
    monkeypatch.setattr(cloud,'authenticate_desktop_request',desktop)
    assert asyncio.run(cloud.actor_for(request(token='mdt_'+'z'*43)))==A2

def test_model_lease_never_falls_back_to_other_client_on_logout_expiry_or_identity_change(monkeypatch):
    class Redis:
        def __init__(self):self.data={}
        async def get(self,key):return self.data.get(key)
        async def set(self,key,value,ex=None,nx=False):
            if nx and key in self.data:return False
            self.data[key]=value;return True
    redis=Redis();active={a.auth_session_id:a for a in (A,A2,B)}
    service=SimpleNamespace(store=SimpleNamespace(redis=redis))
    service.desktop_store=RedisDesktopStore(redis)
    async def authenticate(sid):return active[sid].platform() if sid in active else None
    async def deadline(sid,*identity):
        if sid not in active:raise iam.IAMFailure(401,'expired')
        return int(time.time())+3600
    service.authenticate=authenticate;service.session_deadline=deadline
    async def run(fn):return await fn(service)
    monkeypatch.setattr(iam,'_with_service',run)
    token='mdt_'+'z'*43;key=service.desktop_store.key('grant',token)
    redis.data[key]=json.dumps({'sid':A2.auth_session_id,'expires_at':int(time.time())+800})
    async def scenario():
        web_lease,_=await cloud.issue_lease(request(cookie=A.auth_session_id),A)
        desktop_lease,_=await cloud.issue_lease(request(token=token),A2)
        other,_=await cloud.issue_lease(request(cookie=B.auth_session_id),B)
        assert len({web_lease,desktop_lease,other})==3
        assert cloud.owner_key(A)==cloud.owner_key(A2)!=cloud.owner_key(B)
        active.pop(A.auth_session_id)
        with pytest.raises(iam.IAMFailure):await cloud.lease_actor(web_lease)
        assert (await cloud.lease_actor(desktop_lease)).auth_session_id==A2.auth_session_id
        redis.data[key]=json.dumps({'sid':A2.auth_session_id,'expires_at':time.time()-1})
        with pytest.raises(iam.IAMFailure):await cloud.lease_actor(desktop_lease)
        assert (await cloud.lease_actor(other)).user_no==B.user_no
        active[B.auth_session_id]=A
        with pytest.raises(iam.IAMFailure):await cloud.lease_actor(other)
    asyncio.run(scenario())

def test_second_client_opens_existing_host_without_eviction_or_credential_replacement(monkeypatch):
    m,owner,record=catalog_manager(monkeypatch)
    catalog={'data':[],'default_model':None}
    import hashlib
    record['catalog_hash']=hashlib.sha256(json.dumps(catalog,sort_keys=True).encode()).hexdigest()
    calls=[]
    async def docker(method,path,**kwargs):
        calls.append((method,path));assert method=='GET'
        return {'Config':{'Labels':{m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':owner}},'State':{'Running':True}}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):return SimpleNamespace(status_code=200)
    monkeypatch.setattr(m,'docker',docker);monkeypatch.setattr(m.httpx,'AsyncClient',Client)
    before=dict(record)
    result=asyncio.run(m.open_host(owner,m.OpenBody(lease='mcl_'+'z'*43,catalog=catalog,expires_at=int(time.time())+800),None))
    assert result['route_token']==before['route_token']
    assert record['container_id']==before['container_id'] and record['lease']==before['lease']
    assert len(calls)==1

@pytest.mark.parametrize('path',['api/megmeet-cloud/task-state','%61pi/megmeet-cloud/task-state','__cloud/ready'])
def test_private_worker_control_never_reaches_browser(path):
    with pytest.raises(iam.IAMFailure):cloud.native_path(path)

def test_entry_has_no_identity_or_owner_input_and_fixed_iam_destination():
    app=FastAPI();app.include_router(web.router)
    with TestClient(app) as client:
        html=client.get('/maimi/web/');script=client.get('/maimi/web/entry.js')
    assert html.status_code==200 and 'return_to=%2Fmaimi%2Fweb%2F' in html.text
    assert "body:'{}'" in script.text and '/maimi/web/chat' in script.text
    assert 'frame-ancestors' in html.headers['content-security-policy']

def test_web_oauth_destination_is_fixed_and_bound_to_flow(config,env_config,monkeypatch):
    service,_=make_service(config)
    async def run(fn):return await fn(service)
    monkeypatch.setattr(iam,'_with_service',run)
    app=FastAPI();app.include_router(iam.router)
    with TestClient(app,base_url='https://app.test') as client:
        for destination in ['https://evil.test/','//evil.test/','/other','/maimi/web/?x=1']:
            assert client.get('/v1/iam/login',params={'return_to':destination},follow_redirects=False).status_code==400
        response=client.get('/v1/iam/login',params={'return_to':'/maimi/web/'},follow_redirects=False)
        state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        assert client.cookies.get(iam.WEB_DESTINATION_COOKIE)
        callback=client.get('/v1/iam/callback',params={'code':'synthetic-code','state':state},follow_redirects=False)
        assert callback.status_code==303 and callback.headers['location']=='https://app.test/maimi/web/'
        assert not client.cookies.get(iam.WEB_DESTINATION_COOKIE)

def test_browser_cannot_select_local_or_unknown_enterprise_route(monkeypatch):
    async def actor(_):return A
    monkeypatch.setattr(cloud,'actor_for',actor)
    with pytest.raises(iam.IAMFailure) as error:asyncio.run(web.no_local(request(method='POST')))
    assert error.value.code=='web_personal_cloud_only'
    with pytest.raises(iam.IAMFailure):web.enterprise_target('POST','../../admin','')
    assert web.enterprise_target('GET','plugins','')=='/v1/plugins/catalog'

def test_live_websocket_disconnects_only_revoked_login(config,monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    import websockets.legacy.client
    live={A.auth_session_id:True,A2.auth_session_id:True}
    monkeypatch.setattr(iam.IAMConfig,'from_env',lambda:config)
    monkeypatch.setattr(cloud,'NATIVE_CHECK_SECONDS',0.01)
    async def authenticate(req):
        sid=req.cookies.get(iam.SESSION_COOKIE)
        return A.platform() if live.get(sid) else None
    async def touch(req,actor):return {'origin':'http://owned:3084','route_token':'r'*43}
    monkeypatch.setattr(iam,'authenticate_request',authenticate)
    monkeypatch.setattr(cloud,'touch_host',touch)
    monkeypatch.setattr(cloud,'ensure_personal_workspace',lambda actor:{})
    class Connection:
        subprotocol=None
        def __init__(self):self.queue=asyncio.Queue()
        async def send(self,value):await self.queue.put(value)
        def __aiter__(self):return self
        async def __anext__(self):return await self.queue.get()
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(websockets.legacy.client,'connect',lambda *a,**k:Connection())
    app=FastAPI();app.include_router(web.router)
    with TestClient(app,base_url=config.public_origin) as client:
        with client.websocket_connect('/api/remote.mux',headers={'Origin':config.public_origin,'Cookie':iam.SESSION_COOKIE+'='+A.auth_session_id}) as first:
            with client.websocket_connect('/api/remote.mux',headers={'Origin':config.public_origin,'Cookie':iam.SESSION_COOKIE+'='+A2.auth_session_id}) as second:
                first.send_text('first');assert first.receive_text()=='first'
                second.send_text('second');assert second.receive_text()=='second'
                live[A.auth_session_id]=False
                with pytest.raises(WebSocketDisconnect):first.receive_text()
                second.send_text('still connected');assert second.receive_text()=='still connected'

def test_confirmation_receipt_is_one_use_and_bound_to_exact_client_and_action(monkeypatch):
    class Redis:
        def __init__(self):self.values={}
        async def set(self,key,value,ex,nx):assert ex==30 and nx;self.values[key]=value
        async def eval(self,script,n,key,expected):
            if self.values.get(key)==expected:self.values.pop(key);return 1
            return 0
    redis=Redis()
    async def run(fn):return await fn(SimpleNamespace(store=SimpleNamespace(redis=redis)))
    monkeypatch.setattr(iam,'_with_service',run)
    async def scenario():
        lease='mcl_'+'a'*43;ticket='t'*43
        receipt=await cloud.confirmation_receipt(lease,ticket)
        with pytest.raises(iam.IAMFailure):await cloud.consume_confirmation_receipt(receipt,'mcl_'+'b'*43,ticket)
        with pytest.raises(iam.IAMFailure):await cloud.consume_confirmation_receipt(receipt,lease,'x'*43)
        await cloud.consume_confirmation_receipt(receipt,lease,ticket)
        with pytest.raises(iam.IAMFailure):await cloud.consume_confirmation_receipt(receipt,lease,ticket)
        with pytest.raises(iam.IAMFailure):await cloud.consume_confirmation_receipt('',lease,ticket)
    asyncio.run(scenario())

def test_internal_connector_confirm_requires_user_receipt_even_with_valid_model_lease(monkeypatch):
    from app import enterprise_connectors as connectors,main
    writes=[]
    class Redis:
        def __init__(self):self.values={}
        async def set(self,key,value,ex,nx):self.values[key]=value
        async def eval(self,script,n,key,expected):
            if self.values.get(key)==expected:self.values.pop(key);return 1
            return 0
    redis=Redis()
    async def run(fn):return await fn(SimpleNamespace(store=SimpleNamespace(redis=redis)))
    monkeypatch.setattr(iam,'_with_service',run)
    async def lease_actor(lease):
        if lease!='mcl_'+'a'*43:raise iam.IAMFailure(401,'lease_invalid')
        return A
    monkeypatch.setattr(cloud,'lease_actor',lease_actor)
    async def confirm(actor,ticket):writes.append((actor.user_no,ticket));return {'status':'completed'}
    monkeypatch.setattr(connectors,'service',lambda:SimpleNamespace(confirm=confirm))
    monkeypatch.setenv('AUTH_MODE','iam_direct')
    lease='mcl_'+'a'*43;ticket='t'*43
    receipt=asyncio.run(cloud.confirmation_receipt(lease,ticket))
    headers={'Authorization':'Bearer '+lease}
    with TestClient(main.app) as client:
        url='/internal/personal-cloud/connectors/confirm'
        body={'confirmation_id':ticket,'confirmed':True}
        assert client.post(url,headers=headers,json=body).status_code==403
        assert not writes
        headers['X-Megmeet-User-Confirm']=receipt
        assert client.post(url,headers=headers,json=body).status_code==200
        assert writes==[(A.user_no,ticket)]
        assert client.post(url,headers=headers,json=body).status_code==403
        assert len(writes)==1
