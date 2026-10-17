"""Cloud admission, real activity, and irreversible archive HTTP boundaries."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from starlette.requests import Request
from app import personal_cloud as cloud
from app.desktop_auth import TrustedActor
from app.iam_oauth import IAMFailure

ACTOR=TrustedActor('RETENTION-ROUTE-A','account-A','A',(),'login-A')
SID='session-12345678-1234-1234-1234-123456789abc'

def request(method='POST',body=b'{}',desktop=False):
    headers=[(b'content-type',b'application/json')]
    if desktop:headers.append((b'authorization',b'Bearer mdt_synthetic'))
    async def receive():return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':method,'headers':headers,'path':'/native',
        'query_string':b'','scheme':'https','server':('app.test',443)},receive)

@pytest.fixture
def setup(monkeypatch):
    calls=[]
    monkeypatch.setenv('PERSONAL_CLOUD_RETENTION_POLICY','workspace-idle-30d-v1')
    async def actor(req):return ACTOR
    async def lease(req,actor):return 'mcl_synthetic',9999999999
    async def manager(method,path,body=None):
        calls.append(('manager',path,body))
        return {'origin':'http://worker.test:3084','route_token':'synthetic-token',
          'status':'ready','busy':False,'expires_at':9999999999,'native_workspace_id':'native'}
    def access(owner,user,**kwargs):calls.append(('access',owner,user,kwargs))
    def queue(owner,user,sid):calls.append(('queue',owner,user,sid))
    monkeypatch.setattr(cloud,'actor_for',actor)
    monkeypatch.setattr(cloud,'issue_lease',lease)
    monkeypatch.setattr(cloud,'ensure_personal_workspace',lambda actor:{'workspace_id':'workspace'})
    monkeypatch.setattr(cloud,'manager',manager)
    monkeypatch.setattr(cloud.retention,'access',access)
    monkeypatch.setattr(cloud.retention,'queue_session_delete',queue)
    class Client:
        def __init__(self,**kwargs):pass
        def build_request(self,method,url,**kwargs):return kwargs
        async def send(self,req,**kwargs):
            calls.append(('forward',json.loads(req['content'])))
            return SimpleNamespace(headers={},status_code=200,aclose=self.aclose)
        async def aclose(self):pass
    monkeypatch.setattr(cloud.httpx,'AsyncClient',Client)
    return calls

def native(method,args=None,**extra):
    value={'type':'client-request','rpcId':'synthetic-rpc','method':method,
      'payload':{'args':{'request':{'sessionId':SID}} if args is None else args}}
    value.update(extra)
    return request(body=json.dumps(value).encode())

@pytest.mark.parametrize('desktop',[False,True])
def test_passive_transport_does_not_renew_storage_retention(setup,desktop):
    asyncio.run(cloud.touch_host(request(desktop=desktop),ACTOR))
    assert setup[0]==('access',cloud.owner_key(ACTOR),ACTOR.user_no,{'activity':False})

def test_human_activity_renews_authenticated_storage_owner(setup):
    asyncio.run(cloud.touch_host(request(),ACTOR,activity=True))
    assert setup[0]==('access',cloud.owner_key(ACTOR),ACTOR.user_no,{'activity':True})

def test_pending_workspace_wipe_blocks_manager_access(setup,monkeypatch):
    def blocked(*a,**kw):raise IAMFailure(409,'cloud_workspace_cleanup_in_progress')
    monkeypatch.setattr(cloud.retention,'access',blocked)
    with pytest.raises(IAMFailure):asyncio.run(cloud.open_workspace(request()))
    assert setup==[]

def test_archive_commits_permanent_intent_before_native_forward(setup):
    asyncio.run(cloud.native_http(native('workspace/archiveSession'),'api/workspace/archiveSession'))
    queue=next(i for i,call in enumerate(setup) if call[0]=='queue')
    forward=next(i for i,call in enumerate(setup) if call[0]=='forward')
    assert queue<forward and setup[queue][1:]==(cloud.owner_key(ACTOR),ACTOR.user_no,SID)

def test_unarchive_is_unavailable_and_never_forwarded(setup):
    with pytest.raises(IAMFailure) as error:
        asyncio.run(cloud.native_http(native('workspace/unarchiveSession'),'api/workspace/unarchiveSession'))
    assert error.value.status==410 and not any(c[0]=='forward' for c in setup)

def test_archived_session_cannot_be_read_again(setup,monkeypatch):
    def blocked(*args,**kwargs):
        if kwargs.get('session_id')==SID:raise IAMFailure(410,'cloud_session_permanently_deleted')
    monkeypatch.setattr(cloud.retention,'access',blocked)
    with pytest.raises(IAMFailure) as error:
        asyncio.run(cloud.native_http(native('session/page'),'api/session/page'))
    assert error.value.status==410 and not any(c[0]=='forward' for c in setup)

@pytest.mark.parametrize('method,activity',[('session/page',True),('session/prompt',True),('session/list',False),('settings/get',False)])
def test_native_activity_policy_distinguishes_reading_from_background_catalog(setup,method,activity):
    asyncio.run(cloud.native_http(native(method),'api/'+method))
    accesses=[c for c in setup if c[0]=='access']
    assert accesses[-1][3]['activity'] is activity

def test_unrelated_or_malformed_envelope_never_queues_deletion(setup):
    for payload in [None,[],{'args':None},{'args':[]},{'args':{'request':None}}]:
        asyncio.run(cloud.native_http(native('session/list',payload=payload),'api/session/list'))
    asyncio.run(cloud.native_http(native('workspace/archiveSession'),'api/session/list'))
    assert not any(c[0]=='queue' for c in setup)

def test_removed_recycle_bin_has_no_restoreable_rows(setup):
    result=asyncio.run(cloud.history_trash(request('GET')))
    assert json.loads(result.body)=={'items':[],'next_page':None,'recovery_days':0,'permanent_deletion':True}
    with pytest.raises(IAMFailure) as error:asyncio.run(cloud.history_restore(request()))
    assert error.value.status==410

def test_original_model_task_renews_retention(setup,monkeypatch):
    async def actor(lease):return ACTOR
    monkeypatch.setattr(cloud,'lease_actor',actor)
    req=request('GET');req.scope['path']='/internal/personal-cloud/model/v1/session'
    result=asyncio.run(cloud.worker_model(req,'session'))
    assert result.status_code==200 and setup==[('access',cloud.owner_key(ACTOR),ACTOR.user_no,{'activity':True})]
