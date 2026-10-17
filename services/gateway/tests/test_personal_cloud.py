"""Employee-bound native cloud policy; only synthetic DB/Redis actors."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from starlette.requests import Request

from test_enterprise_tasks import database
from app import personal_cloud as pc
from app.desktop_auth import TrustedActor, RedisDesktopStore
from app.iam_oauth import IAMFailure

A=TrustedActor('LOCAL-FIXTURE-A','LOCAL-FIXTURE-A','A',('DEPT-A',),'session-A')
B=TrustedActor('LOCAL-FIXTURE-B','LOCAL-FIXTURE-B','B',('DEPT-B',),'session-B')


def test_employee_binding_is_stable_private_and_old_spaces_preserved(database,monkeypatch):
    monkeypatch.setattr(pc,'_connect',database)
    one=pc.ensure_personal_workspace(A)
    moved=pc.ensure_personal_workspace(TrustedActor(A.user_no,A.account_no,'A',('MOVED',),'new-session'))
    two=pc.ensure_personal_workspace(B)
    assert one['workspace_id']==moved['workspace_id']!=two['workspace_id']
    assert one['name']=='云端工作区' and one['kind']=='personal' and not one['can_share']
    with database() as conn,conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM platform.hybrid_workspaces WHERE kind='department'")
        assert cur.fetchone()['n']>=1
        cur.execute('UPDATE platform.hybrid_workspaces SET enabled=FALSE WHERE id=%s',(one['workspace_id'],))
    with pytest.raises(IAMFailure):pc.ensure_personal_workspace(A)


@pytest.mark.parametrize('path',['../secret','%2e%2e/secret','//foreign','%2fetc','a\\b','__cloud/ready','a/%00'])
def test_native_path_rejects_escape_and_wrapper_admin(path):
    with pytest.raises(IAMFailure):pc.native_path(path)


def test_lease_checks_live_grant_session_and_identity(monkeypatch):
    class Redis:
        data={}
        async def get(self,key):return self.data.get(key)
        async def set(self,key,value,ex=None,nx=False):
            if nx and key in self.data:return False
            self.data[key]=value;return True
    redis=Redis();service=SimpleNamespace(store=SimpleNamespace(redis=redis))
    service.desktop_store=RedisDesktopStore(redis)
    active={'user_no':A.user_no,'account_no':A.account_no,'organization_codes':['DEPT-A'],'display_name':'A'}
    async def authenticate(sid):return dict(active) if sid=='session-A' else None
    service.authenticate=authenticate
    async def deadline(sid,user_no,account_no):
        return int(time.time())+3600
    service.session_deadline=deadline
    async def with_service(fn):return await fn(service)
    monkeypatch.setattr(pc.iam,'_with_service',with_service)
    token='mdt_'+'a'*43
    key=service.desktop_store.key('grant',token)
    redis.data[key]=json.dumps({'sid':'session-A','expires_at':int(time.time())+100})
    request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())]})
    async def run():
        lease,expires=await pc.issue_lease(request,A)
        assert (await pc.lease_actor(lease)).user_no==A.user_no
        assert (await pc.issue_lease(request,A))[0]==lease
        active['organization_codes']=['MOVED']
        assert (await pc.lease_actor(lease)).organization_codes==('MOVED',)
        active['user_no']=B.user_no
        with pytest.raises(IAMFailure):await pc.lease_actor(lease)
        active['user_no']=A.user_no
        redis.data.pop(key)
        with pytest.raises(IAMFailure):await pc.lease_actor(lease)
    asyncio.run(run())


def catalog_manager(monkeypatch):
    spec=importlib.util.spec_from_file_location('cloud_manager_hot_catalog',Path(__file__).parents[2]/'personal-cloud/manager.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    monkeypatch.setattr(m,'auth',lambda request:None)
    owner='a'*32
    record={'container_id':'stable-worker','lease':'mcl_'+'b'*43,'catalog_hash':'old-hash',
        'expires_at':int(time.time())+60,'touched':time.time()-5,
        'origin':'http://owned-worker:3084','route_token':'c'*43,'native_workspace_id':'same-workspace'}
    m.records[owner]=record
    return m,owner,record


def test_renewed_grant_keeps_worker_generation_and_does_not_recreate(monkeypatch):
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch)
    async def no_docker(*a,**kw):raise AssertionError('renewal must not recreate Host')
    monkeypatch.setattr(m,'docker',no_docker)
    expiry=int(time.time())+850
    value=asyncio.run(m.heartbeat(owner,m.HeartbeatBody(lease=record['lease'],expires_at=expiry),None))
    assert value['expires_at']==expiry and record['container_id']=='stable-worker'
    # Another independently authenticated client of the same owner extends
    # this Host without acquiring or overwriting its task's model authority.
    second=asyncio.run(m.heartbeat(owner,m.HeartbeatBody(lease='mcl_'+'z'*43,expires_at=expiry),None))
    assert second['expires_at']==expiry and record['lease']=='mcl_'+'b'*43
    record['expires_at']=time.time()-1
    with pytest.raises(HTTPException):asyncio.run(m.heartbeat(owner,m.HeartbeatBody(lease=record['lease'],expires_at=expiry),None))


def test_cloud_queue_is_bounded_fifo_deduplicated_and_expires_without_polling(monkeypatch):
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch)
    monkeypatch.setattr(m,'MAX_HOSTS',1);monkeypatch.setattr(m,'QUEUE_LIMIT',2)
    a,b,c='b'*32,'c'*32,'d'*32
    assert m.queue_admission(a,'first',100)['position']==1
    assert m.queue_admission(b,'second',100)['position']==2
    assert m.queue_admission(a,'first',110)['position']==1
    with pytest.raises(HTTPException):m.queue_admission(c,'third',110)
    m.records.clear()
    assert m.queue_admission(b,'second',115)['position']==2
    assert m.queue_admission(a,'first',115) is None
    assert m.queue_admission(b,'second',115) is None
    m.records[owner]=record
    m.queue_admission(a,'old',200);m.queue_admission(b,'second',200)
    assert m.queue_admission(a,'replacement-login',205)['position']==2
    assert m.queue_admission(c,'third',240)['position']==1


def test_reaper_accepts_missing_owned_worker_and_never_deletes_unknown_container(monkeypatch):
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch)
    calls=[]
    async def missing(method,path,**kw):calls.append(method);return None
    monkeypatch.setattr(m,'docker',missing)
    asyncio.run(m.remove(record));assert calls==['GET']
    async def foreign(method,path,**kw):return {'Config':{'Labels':{m.LABEL:'other-manager'}}}
    monkeypatch.setattr(m,'docker',foreign)
    with pytest.raises(HTTPException):asyncio.run(m.remove(record))


def test_storage_never_stops_active_host_or_takes_user_paths(monkeypatch):
    from pydantic import ValidationError
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch)
    async def no_docker(*a,**k):raise AssertionError('active host changed')
    monkeypatch.setattr(m,'docker',no_docker)
    with pytest.raises(HTTPException) as error:asyncio.run(m.storage(owner,m.StorageBody(action='inventory'),None))
    assert error.value.status_code==409
    for extra in ({'path':'/workspace'},{'image':'other'},{'volume':'other'},{'command':'shell'}):
        with pytest.raises(ValidationError):m.StorageBody(action='inventory',**extra)


@pytest.mark.parametrize('case',['foreign','missing','mounted'])
def test_storage_checks_persisted_volume_owner_and_running_mounts(monkeypatch,case):
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch);m.records.clear()
    async def docker(method,path,body=None,**kwargs):
        assert method=='GET'
        if path.startswith('/volumes/'):
            if case=='missing':return None
            return {'Labels':{m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':'foreign' if case=='foreign' else owner}}
        assert '/containers/json?' in path;return [{'Id':'another-running-container'}]
    monkeypatch.setattr(m,'docker',docker)
    with pytest.raises(HTTPException) as e:asyncio.run(m.storage(owner,m.StorageBody(action='inventory'),None))
    assert e.value.status_code=={'foreign':503,'missing':404,'mounted':409}[case]


@pytest.mark.parametrize('catalog',[
    {'data':[{'id':'deepseek-flash-v4.1','name':'DS'}],'default_model':'deepseek-flash-v4.1'},
    {'data':[],'default_model':None},
])
def test_catalog_refresh_updates_existing_worker_without_recreation(monkeypatch,catalog):
    import hashlib
    m,owner,record=catalog_manager(monkeypatch);calls=[]
    original_identity={k:record[k] for k in ('container_id','route_token','native_workspace_id','lease')}
    body=m.OpenBody(lease=record['lease'],catalog=catalog,expires_at=int(time.time())+90)
    async def docker(method,path,**options):
        calls.append((method,path))
        assert method=='GET' and path=='/containers/stable-worker/json'
        return {'State':{'Running':True},'Config':{'Labels':{m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':owner}}}
    monkeypatch.setattr(m,'docker',docker)
    class Client:
        def __init__(self,**kwargs):assert kwargs=={'timeout':35,'trust_env':False}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,headers,json):
            assert url=='http://owned-worker:3084/__cloud/catalog'
            assert headers=={'Authorization':'Bearer '+'c'*43}
            assert json=={'catalog':catalog}
            return SimpleNamespace(status_code=200,json=lambda:{'status':'updated','native_reload_required':False})
    monkeypatch.setattr(m.httpx,'AsyncClient',Client)
    result=asyncio.run(m.update_catalog(owner,body,None))
    assert result['native_workspace_id']=='same-workspace'
    assert {k:record[k] for k in original_identity}==original_identity
    assert record['catalog_hash']==hashlib.sha256(json.dumps(catalog,sort_keys=True).encode()).hexdigest()
    assert record['expires_at']==body.expires_at and len(calls)==1


@pytest.mark.parametrize('state',['missing','expired','stopped','gone','foreign-label'])
def test_catalog_refresh_never_opens_or_replaces_an_ineligible_host(monkeypatch,state):
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch)
    body=m.OpenBody(lease=record['lease'],catalog={'data':[],'default_model':None},expires_at=int(time.time())+60)
    if state=='missing':m.records.clear()
    if state=='expired':record['expires_at']=time.time()-1
    if state=='foreign-lease':body.lease='mcl_'+'z'*43
    calls=[]
    async def docker(method,path,**options):
        calls.append(method)
        assert method=='GET'
        if state=='gone':return None
        return {'State':{'Running':state!='stopped'},'Config':{'Labels':{
            m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':'b'*32 if state=='foreign-label' else owner}}}
    monkeypatch.setattr(m,'docker',docker)
    def no_client(**kwargs):raise AssertionError('ineligible worker received catalog')
    monkeypatch.setattr(m.httpx,'AsyncClient',no_client)
    with pytest.raises(HTTPException) as error:asyncio.run(m.update_catalog(owner,body,None))
    assert error.value.status_code==(503 if state=='foreign-label' else 409)
    assert record['catalog_hash']=='old-hash'
    if state in ('missing','expired','foreign-lease'):assert not calls


@pytest.mark.parametrize('failure',['http','timeout','bad-ack'])
def test_catalog_refresh_failure_does_not_acknowledge_or_renew(monkeypatch,failure):
    from fastapi import HTTPException
    m,owner,record=catalog_manager(monkeypatch);before=dict(record)
    async def docker(method,path,**options):
        assert method=='GET'
        return {'State':{'Running':True},'Config':{'Labels':{m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':owner}}}
    monkeypatch.setattr(m,'docker',docker)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):
            if failure=='timeout':raise m.httpx.ReadTimeout('synthetic timeout')
            return SimpleNamespace(status_code=503 if failure=='http' else 200,json=lambda:{'status':'partial'})
    monkeypatch.setattr(m.httpx,'AsyncClient',Client)
    body=m.OpenBody(lease=record['lease'],catalog={'data':[],'default_model':None},expires_at=int(time.time())+90)
    with pytest.raises(HTTPException) as error:asyncio.run(m.update_catalog(owner,body,None))
    assert error.value.status_code==503 and record==before


@pytest.mark.parametrize('empty',[False,True])
def test_gateway_refresh_fetches_actor_catalog_and_never_requires_reload(monkeypatch,empty):
    catalog={'data':[] if empty else [{'id':'deepseek-flash-v4.1','name':'DS'}],
        'default_model':None if empty else 'deepseek-flash-v4.1'}
    events=[]
    async def actor_for(request):events.append('auth');return A
    async def models(actor):assert actor is A;events.append('models');return catalog
    async def lease(request,actor):assert actor is A;events.append('lease');return 'mcl_'+'b'*43,12345
    async def manager(method,path,body):
        events.append('manager')
        assert method=='POST' and path=='/v1/hosts/'+pc.owner_key(A)+'/catalog'
        assert body=={'lease':'mcl_'+'b'*43,'expires_at':12345,'catalog':catalog}
    monkeypatch.setattr(pc,'actor_for',actor_for)
    monkeypatch.setattr(pc,'ensure_personal_workspace',lambda actor:{})
    monkeypatch.setattr(pc.model,'get_broker',lambda:SimpleNamespace(models=models))
    monkeypatch.setattr(pc,'issue_lease',lease);monkeypatch.setattr(pc,'manager',manager)
    result=json.loads(asyncio.run(pc.refresh_models(None)).body)
    assert result['native_reload_required'] is False and result['models']==catalog['data']
    assert result['defaultModel']==catalog['default_model']
    assert events==['auth','models','lease','manager','auth']
    if empty:assert '暂无可用模型' in result['notice']


@pytest.mark.parametrize('changed',['owner','session','logout'])
def test_gateway_refresh_rechecks_identity_after_catalog_update(monkeypatch,changed):
    calls=0
    async def actor_for(request):
        nonlocal calls
        calls+=1
        if calls==1:return A
        if changed=='logout':raise IAMFailure(401,'desktop_session_expired')
        return B if changed=='owner' else TrustedActor(A.user_no,A.account_no,'A',('DEPT-A',),'other-session')
    async def models(actor):return {'data':[],'default_model':None}
    async def lease(request,actor):return 'mcl_'+'b'*43,12345
    async def manager(*args):pass
    monkeypatch.setattr(pc,'actor_for',actor_for)
    monkeypatch.setattr(pc,'ensure_personal_workspace',lambda actor:{})
    monkeypatch.setattr(pc.model,'get_broker',lambda:SimpleNamespace(models=models))
    monkeypatch.setattr(pc,'issue_lease',lease);monkeypatch.setattr(pc,'manager',manager)
    with pytest.raises(IAMFailure) as error:asyncio.run(pc.refresh_models(None))
    assert error.value.status==401


def test_docker_worker_policy_never_mounts_host_or_uses_caller_image():
    path=Path(__file__).parents[2]/'personal-cloud/manager.py'
    spec=importlib.util.spec_from_file_location('cloud_manager_policy',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.image_id='sha256:approved-image'
    body=module.OpenBody(lease='mcl_'+'b'*43,catalog={'data':[{'id':'allowed'}]},expires_at=int(time.time())+60)
    conf=module.container_config('a'*32,body,'c'*43)
    assert conf['Image']=='sha256:approved-image' and conf['User']=='1000:1000'
    host=conf['HostConfig']
    assert host['ReadonlyRootfs'] and host['CapDrop']==['ALL'] and host['Memory']==1536*1024*1024
    assert host['MemorySwap']==host['Memory']
    assert host['PidsLimit']==128 and not host.get('PortBindings') and not host.get('Privileged')
    assert all(v['Type']=='volume' for v in host['Mounts'])
    assert {v['Target'] for v in host['Mounts']}=={'/state','/workspace'}
    assert not any('IAM_' in v or 'TOKENKEY_' in v for v in conf['Env'])
    with pytest.raises(Exception):module.OpenBody(lease='mcl_'+'b'*43,catalog={},expires_at=1,image='attacker')


@pytest.mark.parametrize('namespace,allowed',[('a'*30,True),('a'*31,False),('-bad',False),('bad-',False)])
def test_worker_namespace_keeps_complete_hostname_dns_compatible(monkeypatch,namespace,allowed):
    monkeypatch.setenv('PERSONAL_CLOUD_MANAGER_NAMESPACE',namespace)
    path=Path(__file__).parents[2]/'personal-cloud/manager.py'
    spec=importlib.util.spec_from_file_location('cloud_manager_dns',path)
    module=importlib.util.module_from_spec(spec)
    if not allowed:
        with pytest.raises(RuntimeError,match='manager namespace'):spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
        assert len(module.owner_key('b'*32).encode('idna'))==63


def test_full_catalog_chunks_preserve_models_and_fit_linux_environment():
    import subprocess
    path=Path(__file__).parents[2]/'personal-cloud/manager.py'
    spec=importlib.util.spec_from_file_location('cloud_manager_catalog',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    catalog={'data':[{'id':'model-'+str(i),'name':'模型'+'x'*100} for i in range(1000)],'default_model':'model-0'}
    body=m.OpenBody(lease='mcl_'+'b'*43,catalog=catalog,expires_at=int(time.time())+60)
    env=dict(part.split('=',1) for part in m.container_config('a'*32,body,'c'*43)['Env'])
    count=int(env['CLOUD_MODEL_CATALOG_COUNT'])
    assert 2<=count<=9 and 'CLOUD_MODEL_CATALOG' not in env
    pieces=[env['CLOUD_MODEL_CATALOG_'+str(i)] for i in range(count)]
    assert all(len(x.encode('ascii'))<=60000 for x in pieces)
    assert json.loads(''.join(pieces))==catalog
    subprocess.run(['/bin/true'],env=env,check=True)
    with pytest.raises(Exception):
        m.container_config('a'*32,m.OpenBody(lease=body.lease,catalog={'data':'x'*524288},expires_at=body.expires_at),'c'*43)


def test_manual_open_recovers_stopped_owned_host_and_keeps_volume_names(monkeypatch):
    import hashlib
    path=Path(__file__).parents[2]/'personal-cloud/manager.py'
    spec=importlib.util.spec_from_file_location('cloud_manager_recovery',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    owner='a'*32;calls=[];catalog={'data':[{'id':'allowed'}]}
    body=m.OpenBody(lease='mcl_'+'b'*43,catalog=catalog,expires_at=int(time.time())+60)
    m.records[owner]={'container_id':'old','lease':body.lease,'catalog_hash':hashlib.sha256(json.dumps(catalog,sort_keys=True).encode()).hexdigest()}
    monkeypatch.setattr(m,'auth',lambda request:None)
    async def docker(method,path,body=None,**options):
        calls.append((method,path,body))
        if path=='/containers/old/json':return {'Config':{'Labels':{m.LABEL:m.INSTANCE}},'State':{'Running':False}}
        if method=='GET' and path.startswith('/volumes/'):
            return {'Labels':{m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':owner}}
        if path.startswith('/containers/create'):return {'Id':'replacement'}
        return {}
    monkeypatch.setattr(m,'docker',docker)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):return SimpleNamespace(status_code=200,json=lambda:{'native_workspace_id':'native-stable'})
    monkeypatch.setattr(m.httpx,'AsyncClient',Client)
    result=asyncio.run(m.open_host(owner,body,None))
    assert result['native_workspace_id']=='native-stable'
    assert ('DELETE','/containers/old?force=true&v=false',None) in calls
    created=next(c[2] for c in calls if c[1].startswith('/containers/create'))
    assert {x['Source'] for x in created['HostConfig']['Mounts']}=={m.owner_key(owner)+'-state',m.owner_key(owner)+'-files'}


def test_manager_auth_precedes_body_parsing_and_bounds_chunked_body(tmp_path,monkeypatch):
    import httpx
    path=Path(__file__).parents[2]/'personal-cloud/manager.py'
    spec=importlib.util.spec_from_file_location('cloud_manager_body_guard',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    secret=tmp_path/'manager-secret';secret.write_text('s'*43)
    monkeypatch.setattr(m,'SECRET_FILE',str(secret))
    async def run():
        # Calling ASGI directly proves an unauthenticated body is never read.
        sent=[]
        async def receive():raise AssertionError('unauthenticated body read')
        async def send(message):sent.append(message)
        scope={'type':'http','http_version':'1.1','method':'POST','scheme':'http',
            'path':'/v1/hosts/'+'a'*32+'/open','raw_path':b'/irrelevant','query_string':b'',
            'headers':[(b'authorization',b'Bearer '+b'\xe9'*43),(b'content-type',b'application/json')],
            'server':('manager',8092),'client':('worker',1000)}
        await m.app(scope,receive,send)
        assert sent[0]['status']==401
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),base_url='http://manager') as client:
            async def chunks():
                for _ in range(7):yield b'x'*(100*1024)
            r=await client.post('/v1/hosts/'+'a'*32+'/open',
                headers={'Authorization':'Bearer '+'s'*43,'Content-Type':'application/json'},content=chunks())
            assert r.status_code==413 and r.json()['detail']=='manager_request_too_large'
            r=await client.post('/v1/hosts/'+'a'*32+'/open',content=b'invalid json')
            assert r.status_code==401
    asyncio.run(run())
