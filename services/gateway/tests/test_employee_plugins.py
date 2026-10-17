"""Plugin selection is independent of teams, but always employee authorized."""
from uuid import uuid4, UUID
import base64
import hashlib
import pytest
from test_enterprise_tasks import database
from app import enterprise_plugins as plugins, personal_cloud, enterprise_tasks as cloud
from app.desktop_auth import TrustedActor

A=TrustedActor('LOCAL-FIXTURE-A','LOCAL-FIXTURE-A','A',('LOCAL-TEST',),'sid-a')
B=TrustedActor('LOCAL-FIXTURE-B','LOCAL-FIXTURE-B','B',('OTHER',),'sid-b')

@pytest.fixture
def private(database,monkeypatch):
    monkeypatch.setattr(plugins,'_connect',database)
    monkeypatch.setattr(personal_cloud,'_connect',database)
    with database() as conn,conn.cursor() as cur:
        cur.execute('''INSERT INTO platform.enterprise_plugin_catalog(plugin_key,version,name,runner,model_policy)
            VALUES('synthetic-summary','1.0.0','Synthetic','synthetic-summary-v1','{"mode":"deterministic-no-model"}')''')
        cur.execute('''INSERT INTO platform.enterprise_plugin_grants(plugin_key,version,user_no)
            VALUES('synthetic-summary','1.0.0',%s)''',(A.user_no,))
    return database

def payload(**values):
    defaults=dict(request_id=uuid4(),plugin_key='synthetic-summary',plugin_version='1.0.0',input='summarize',
                  native_session_id='session-'+str(uuid4()),location='local')
    defaults.update(values)
    return plugins.PluginTask(**defaults)

def test_catalog_no_team_required_and_no_implicit_grants(private):
    assert plugins.catalog(A)['items'][0]['plugin_key']=='synthetic-summary'
    assert plugins.catalog(B)['items']==[]
    with pytest.raises(cloud.CloudError,match='plugin_access_denied'):
        plugins.sync_scope(B,'synthetic-summary','1.0.0')
    w=plugins.sync_scope(A,'synthetic-summary','1.0.0')
    with private() as conn,conn.cursor() as cur:
        assert cloud.workspace(cur,A,w)['kind']=='personal'
        with pytest.raises(cloud.CloudError,match='workspace_access_denied'): cloud.workspace(cur,B,w)
    moved=TrustedActor(A.user_no,A.account_no,'A',('NEW-DEPT',),'sid-new')
    assert plugins.sync_scope(moved)==w

def test_native_binding_cannot_be_moved_by_retry(private):
    a=payload()
    plugins.bind_request(A,a);plugins.bind_request(A,a)
    for replacement in ({'native_session_id':'session-'+str(uuid4())},{'location':'cloud'},{'input':'changed'}):
        with pytest.raises(cloud.CloudError,match='scope_immutable'):
            plugins.bind_request(A,a.model_copy(update=replacement))
    with pytest.raises(cloud.CloudError,match='plugin_conversation_scope_denied'):
        plugins.bind_request(A,payload(conversation_id=uuid4()))

def test_prefixed_native_identity_is_not_normalized_into_another_session(private):
    a=payload()
    plugins.bind_request(A,a)
    with pytest.raises(cloud.CloudError,match='scope_immutable'):
        plugins.bind_request(A,a.model_copy(update={'native_session_id':a.native_session_id.removeprefix('session-')}))
    from pydantic import ValidationError
    for value in ('../../other','session-'+a.native_session_id,'native-session-unknown'):
        with pytest.raises(ValidationError):payload(native_session_id=value)

def test_result_owned_by_native_session_and_grant_revocation(private):
    text=b'Synthetic file only'
    task=payload()
    up=plugins.PluginUpload(request_id=task.request_id,name='fixture.txt',content_base64=base64.b64encode(text).decode(),
                            sha256=hashlib.sha256(text).hexdigest(),explicit_upload=True,
                            plugin_key=task.plugin_key,plugin_version=task.plugin_version)
    file=plugins.upload(A,up)
    task=task.model_copy(update={'attachment_ids':[UUID(file['attachment_id'])]})
    plugins.bind_request(A,task)
    w=plugins.sync_scope(A,task.plugin_key,task.plugin_version)
    prepared=cloud.prepare_run(A,w,cloud.TaskRequest(**task.model_dump(exclude={'native_session_id','location'})),'plugin')
    cloud.finish_run(A,prepared['run_id'],cloud.synthetic_summary(prepared['files']))
    result=plugins.associate(A,task,cloud.read_run(A,prepared['run_id']))
    assert plugins.results(A,str(task.native_session_id),'local')['items'][0]['run_id']==result['run_id']
    assert plugins.results(B,str(task.native_session_id),'local')['items']==[]
    assert plugins.results(A,str(task.native_session_id),'cloud')['items']==[]
    continuation=payload(native_session_id=task.native_session_id,conversation_id=result['conversation_id'])
    plugins.bind_request(A,continuation)
    with pytest.raises(cloud.CloudError,match='scope_denied'):
        plugins.bind_request(A,payload(conversation_id=result['conversation_id']))
    with private() as conn,conn.cursor() as cur:
        cur.execute('UPDATE platform.enterprise_plugin_grants SET enabled=FALSE WHERE user_no=%s',(A.user_no,))
    assert plugins.results(A,str(task.native_session_id),'local')['items']==[]
    with pytest.raises(cloud.CloudError,match='plugin_access_denied'): cloud.read_run(A,result['run_id'])

def test_retired_team_routes_not_mounted():
    from app.main import app
    assert not any(getattr(r,'path','').startswith('/v1/cloud/') for r in app.routes)
