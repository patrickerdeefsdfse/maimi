"""Human activity and passive authorization renewal are different events."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from app import personal_cloud as cloud
from app.desktop_auth import TrustedActor
from app.iam_oauth import IAMFailure

ACTOR=TrustedActor('M00096389','synthetic-account','Developer',(),'s'*43)


def request(method='GET',body=b'',desktop=False):
    headers=[(b'authorization',b'Bearer mdt_synthetic')] if desktop else []
    async def receive():return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':method,'headers':headers,'path':'/v1/personal-cloud/web/activity','query_string':b''},receive)


@pytest.mark.parametrize('desktop,explicit,expected',[(False,None,False),(True,None,True),(False,True,True)])
def test_passive_web_transport_never_extends_human_activity(monkeypatch,desktop,explicit,expected):
    calls=[]
    async def manager(method,path,body=None):
        calls.append((method,path,body))
        return {'origin':'http://synthetic:3084','route_token':'r'*43,'expires_at':int(time.time())+10}
    async def lease(req,actor):return 'mcl_'+'x'*43,int(time.time())+850
    monkeypatch.setattr(cloud,'manager',manager)
    monkeypatch.setattr(cloud,'issue_lease',lease)
    asyncio.run(cloud.touch_host(request(desktop=desktop),ACTOR,activity=explicit))
    assert ('?touch=true' in calls[0][1]) is expected
    assert calls[1][2]['activity'] is expected
    assert calls[1][1].endswith('/heartbeat')


def test_status_poll_never_opens_or_touches_host(monkeypatch):
    calls=[]
    async def actor(req):return ACTOR
    async def manager(method,path,body=None):
        calls.append((method,path,body))
        return {'status':'ready','idle_seconds':600,'idle_for_seconds':599,'busy':False}
    monkeypatch.setattr(cloud,'actor_for',actor)
    monkeypatch.setattr(cloud,'manager',manager)
    result=asyncio.run(cloud.web_activity(request()))
    assert json.loads(result.body)['idle_for_seconds']==599
    assert calls==[('GET','/v1/hosts/'+cloud.owner_key(ACTOR)+'/activity',None)]


def test_released_status_never_reacquires_capacity(monkeypatch):
    calls=[]
    async def actor(req):return ACTOR
    async def manager(method,path,body=None):
        calls.append(path)
        raise IAMFailure(409,'personal_cloud_open_required')
    monkeypatch.setattr(cloud,'actor_for',actor)
    monkeypatch.setattr(cloud,'manager',manager)
    result=asyncio.run(cloud.web_activity(request()))
    value=json.loads(result.body)
    assert value['status']=='released' and value['idle_seconds']==600
    assert len(calls)==1 and calls[0].endswith('/activity')


def test_activity_does_not_accept_client_identity(monkeypatch):
    async def actor(req):return ACTOR
    async def forbidden(*args,**kwargs):raise AssertionError('untrusted activity reached manager')
    monkeypatch.setattr(cloud,'actor_for',actor)
    monkeypatch.setattr(cloud,'manager',forbidden)
    with pytest.raises(IAMFailure) as failure:
        asyncio.run(cloud.web_activity(request('POST',b'{"owner":"foreign","user_no":"M00096389"}')))
    assert failure.value.status==400


def test_explicit_interaction_uses_authenticated_owner(monkeypatch):
    calls=[]
    async def actor(req):return ACTOR
    async def touch(req,actor,*,activity):calls.append(('touch',actor.user_no,activity))
    async def manager(method,path,body=None):
        calls.append(('get',path))
        return {'status':'ready','idle_seconds':600,'idle_for_seconds':0,'busy':False}
    monkeypatch.setattr(cloud,'actor_for',actor)
    monkeypatch.setattr(cloud,'touch_host',touch)
    monkeypatch.setattr(cloud,'manager',manager)
    asyncio.run(cloud.web_activity(request('POST',b'{}')))
    assert calls[0]==('touch','M00096389',True)
    assert calls[1]==('get','/v1/hosts/ba69af8b187e5cc5bf806ceaf2041ff2/activity')


def test_desktop_token_cannot_use_web_activity_route():
    with pytest.raises(IAMFailure) as failure:asyncio.run(cloud.web_activity(request(desktop=True)))
    assert failure.value.status==403


@pytest.mark.parametrize('bad',[False,True])
def test_manager_activity_reply_filters_transport_secrets(tmp_path,monkeypatch,bad):
    token=tmp_path/'synthetic-secret'
    token.write_text('s'*43)
    monkeypatch.setenv('PERSONAL_CLOUD_MANAGER_SECRET_FILE',str(token))
    monkeypatch.setenv('PERSONAL_CLOUD_MANAGER_URL','http://synthetic:8092')
    value={'status':'ready','idle_seconds':600,'idle_for_seconds':5,'busy':'false' if bad else False,
           'origin':'http://private:3084','route_token':'must-not-leave-manager'}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):return SimpleNamespace(status_code=200,json=lambda:value)
    monkeypatch.setattr(cloud.httpx,'AsyncClient',Client)
    if bad:
        with pytest.raises(IAMFailure):asyncio.run(cloud.manager('GET','/v1/hosts/'+cloud.owner_key(ACTOR)+'/activity'))
    else:
        result=asyncio.run(cloud.manager('GET','/v1/hosts/'+cloud.owner_key(ACTOR)+'/activity'))
        assert set(result)=={'status','idle_seconds','idle_for_seconds','busy'}
