"""Real PostgreSQL contract tests: isolated generated schema, synthetic data."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_enterprise_tasks import database,A,B,C,W,grant,upload
from app import enterprise_tasks as cloud,workspace_resources as resources,workspace_admin as admin


def enable_resources(database,actor=A):
    with database() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE platform.hybrid_workspace_grants SET can_retrieve=TRUE,can_publish=TRUE,can_share=TRUE
            WHERE workspace_id=%s AND user_no=%s''',(W,actor.user_no))


def publish(actor=A,visibility='workspace',content='共享实验规范：温度为 28 度',name='实验规范.txt',workspace=W):
    return resources.publish_document(actor,workspace,resources.PublishDocument(
        name=name,content=content,visibility=visibility,explicit_upload=True))


def install_knowledge(database,user=A):
    with database() as conn,conn.cursor() as cur:
        cur.execute('''INSERT INTO platform.hybrid_plugins(workspace_id,plugin_key,version,name,runner,model_policy)
            VALUES(%s,'knowledge-qa','1.0.0','授权知识问答','knowledge-qa-v1','{"mode":"actor-model"}') ON CONFLICT DO NOTHING''',(W,))
        cur.execute('''INSERT INTO platform.hybrid_plugin_grants(workspace_id,plugin_key,version,user_no)
            VALUES(%s,'knowledge-qa','1.0.0',%s) ON CONFLICT DO NOTHING''',(W,user.user_no))


def task(refs,request_id=None,**changes):
    return cloud.TaskRequest(request_id=request_id or uuid4(),plugin_key='knowledge-qa',plugin_version='1.0.0',
        input='规范要求多少温度？',document_refs=refs,explicit_cloud_context=True,**changes)


def ref(doc):
    return {'document_id':doc['document_id'],'version':doc['version']}


def test_explicit_cross_department_group_membership_without_auto_department_grant(database):
    args=admin.parser().parse_args(['--operator','LOCAL-ADMIN','create','--kind','group','--name','跨部门小组'])
    group=admin.execute(args)['workspace_id']
    member=admin.parser().parse_args(['--operator','LOCAL-ADMIN','member','--workspace',group,'--user',C.user_no,
        '--can-chat','--can-retrieve','--can-publish','--can-share'])
    admin.execute(member)
    assert cloud.list_workspaces(C)['items'][0]['kind']=='group'
    assert cloud.list_workspaces(B)['items']==[]
    doc=publish(C,workspace=group)
    assert resources.read_document(C,doc['document_id'])['content']
    member.enabled=False
    admin.execute(member)
    assert cloud.list_workspaces(C)['items']==[]
    with pytest.raises(cloud.CloudError,match='workspace_access_denied'):
        resources.read_document(C,doc['document_id'])


def test_document_capabilities_private_default_and_literal_retrieval(database):
    with pytest.raises(cloud.CloudError,match='document_publish_denied'):
        publish()
    enable_resources(database)
    grant(database,B,share=True)
    enable_resources(database,B)
    private=publish(visibility='private',content='SECRET exact-needle')
    public=publish(content='PUBLIC exact-needle')
    assert len(resources.list_documents(A,W)['items'])==2
    assert [x['document_id'] for x in resources.list_documents(B,W)['items']]==[public['document_id']]
    with pytest.raises(cloud.CloudError,match='document_not_found'):
        resources.read_document(B,private['document_id'],1)
    result=resources.retrieve(B,W,resources.RetrievalRequest(query='exact-needle'))
    assert [x['document_id'] for x in result['items']]==[public['document_id']]
    assert resources.retrieve(B,W,resources.RetrievalRequest(query='%'))['items']==[]


def test_optimistic_edit_versions_and_shared_member_visibility_boundary(database):
    enable_resources(database)
    grant(database,B,share=True)
    enable_resources(database,B)
    doc=publish(content='OLD VERSION')
    change=resources.UpdateDocument(expected_version=1,name=doc['name'],content='NEW VERSION',visibility='workspace',explicit_upload=True)
    updated=resources.update_document(B,doc['document_id'],change)
    assert updated['version']==2
    assert resources.read_document(B,doc['document_id'],1)['content']=='OLD VERSION'
    with pytest.raises(cloud.CloudError,match='document_version_conflict'):
        resources.update_document(A,doc['document_id'],change)
    with pytest.raises(cloud.CloudError,match='document_visibility_owner_required'):
        resources.update_document(B,doc['document_id'],change.model_copy(update={'expected_version':2,'visibility':'private'}))
    resources.update_document(A,doc['document_id'],change.model_copy(update={'expected_version':2,'visibility':'private'}))
    with pytest.raises(cloud.CloudError,match='document_not_found'):
        resources.read_document(B,doc['document_id'],1)
    with database() as conn,conn.cursor() as cur:
        with pytest.raises(Exception,match='document versions are immutable'):
            cur.execute('UPDATE platform.hybrid_document_versions SET content=%s WHERE document_id=%s',('tamper',doc['document_id']))


def test_concurrent_document_edit_only_one_writer_succeeds(database):
    enable_resources(database)
    doc=publish()
    def update(index):
        try:
            return resources.update_document(A,doc['document_id'],resources.UpdateDocument(
                expected_version=1,name=doc['name'],content=str(index),visibility='workspace',explicit_upload=True))
        except cloud.CloudError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(update,[1,2]))
    assert len([r for r in results if isinstance(r,dict)])==1
    assert results.count('document_version_conflict')==1


def test_hybrid_knowledge_uses_actor_and_frozen_selected_versions_and_owned_attachment(database,monkeypatch):
    enable_resources(database)
    install_knowledge(database)
    doc=publish(content='CLOUD PINNED ONE')
    request_id=uuid4()
    body,receipt=upload(operation='plugin',request_id=request_id,text='LOCAL CHOSEN TEXT',
        plugin_key='knowledge-qa',plugin_version='1.0.0')
    resources.update_document(A,doc['document_id'],resources.UpdateDocument(expected_version=1,name=doc['name'],
        content='CLOUD CURRENT TWO',visibility='workspace',explicit_upload=True))
    calls=[]
    async def actor(_): return A
    async def runner(trusted,messages,model,run):
        assert trusted is A
        assert 'CLOUD PINNED ONE' in messages[-1]['content'] and 'CLOUD CURRENT TWO' not in messages[-1]['content']
        assert 'LOCAL CHOSEN TEXT' in messages[-1]['content']
        calls.append(run)
        return {'text':'actor-specific-model-answer','model':model}
    monkeypatch.setattr(cloud,'_actor',actor)
    monkeypatch.setattr(cloud,'model_runner',runner)
    app=FastAPI()
    app.include_router(cloud.router)
    app.include_router(resources.router)
    with TestClient(app) as client:
        assert client.get('/v1/cloud/capabilities').json()['version']==2
        payload=task([ref(doc)],request_id=request_id,attachment_ids=[receipt['attachment_id']]).model_dump(mode='json')
        result=client.post(f'/v1/cloud/workspaces/{W}/tasks',json=payload)
        assert result.status_code==200,result.text
        assert result.json()['status']=='completed'
        assert result.json()['simulation'] is False
        assert result.json()['result']['sources'][0]['version']==1
        assert result.json()['runner_manifest']['id']=='knowledge-qa'
        assert client.post(f'/v1/cloud/workspaces/{W}/tasks',json=payload).json()['run_id']==result.json()['run_id']
        assert len(calls)==1


def test_explicit_context_required_and_cross_workspace_document_denied(database):
    enable_resources(database)
    install_knowledge(database)
    doc=publish()
    with pytest.raises(cloud.CloudError,match='explicit_cloud_context_required'):
        cloud.prepare_run(A,W,task([ref(doc)]).model_copy(update={'explicit_cloud_context':False}),'plugin')
    with pytest.raises(cloud.CloudError,match='document_scope_denied'):
        with database() as conn,conn.cursor() as cur:
            resources.authorize_refs(cur,A,str(uuid4()),[ref(doc)])


def test_revocation_during_model_discards_result_and_stops_future_history_export(database,monkeypatch):
    enable_resources(database)
    grant(database,B,share=True)
    enable_resources(database,B)
    install_knowledge(database,A)
    install_knowledge(database,B)
    doc=publish()
    initial=cloud.prepare_run(A,W,task([ref(doc)]),'plugin')
    cloud.finish_run(A,initial['run_id'],{'text':'PRIVATE DERIVED ANSWER'})
    run=cloud.read_run(A,initial['run_id'])
    cloud.share_conversation(A,run['conversation_id'],'workspace')
    assert cloud.read_run(B,run['run_id'])['result']['text']=='PRIVATE DERIVED ANSWER'
    resources.update_document(A,doc['document_id'],resources.UpdateDocument(expected_version=1,name=doc['name'],
        content='now private',visibility='private',explicit_upload=True))
    with pytest.raises(cloud.CloudError,match='conversation_source_access_denied'):
        cloud.read_run(B,run['run_id'])
    assert cloud.list_conversations(B,W)['items']==[]
    async def actor(_): return A
    async def runner(*_):
        with database() as conn,conn.cursor() as cur:
            cur.execute('UPDATE platform.hybrid_workspace_grants SET can_retrieve=FALSE WHERE workspace_id=%s AND user_no=%s',(W,A.user_no))
        return {'text':'MUST NEVER BE STORED OR RETURNED'}
    monkeypatch.setattr(cloud,'_actor',actor)
    monkeypatch.setattr(cloud,'model_runner',runner)
    app=FastAPI();app.include_router(cloud.router)
    with TestClient(app) as client:
        response=client.post(f'/v1/cloud/workspaces/{W}/tasks',json=task([ref(doc)]).model_dump(mode='json'))
        assert response.status_code==403
    with database() as conn,conn.cursor() as cur:
        cur.execute("SELECT result,error_code FROM platform.hybrid_runs WHERE status='failed'")
        assert cur.fetchone()=={'result':None,'error_code':'task_permissions_changed'}


def test_task_limits_and_stale_receipt_do_not_repeat_model(database,monkeypatch):
    monkeypatch.setenv('CLOUD_TASK_EMPLOYEE_LIMIT','1')
    first=cloud.prepare_run(A,W,cloud.ChatRequest(request_id=uuid4(),input='one'),'chat')
    with pytest.raises(cloud.CloudError,match='cloud_task_capacity_reached'):
        cloud.prepare_run(A,W,cloud.ChatRequest(request_id=uuid4(),input='two'),'chat')
    with database() as conn,conn.cursor() as cur:
        cur.execute("UPDATE platform.hybrid_runs SET created_at=now()-interval '10 minutes' WHERE id=%s",(first['run_id'],))
    second=cloud.prepare_run(A,W,cloud.ChatRequest(request_id=uuid4(),input='two'),'chat')
    assert second['run_id']!=first['run_id']
    assert cloud.read_run(A,first['run_id'])['error_code']=='task_interrupted'


def test_model_timeout_and_expired_identity_never_return_answer(database,monkeypatch):
    async def actor(_):return A
    async def runner(*_):
        await asyncio.sleep(.1)
        return {'text':'TOO LATE'}
    monkeypatch.setattr(cloud,'_actor',actor)
    monkeypatch.setattr(cloud,'model_runner',runner)
    monkeypatch.setattr(cloud,'task_timeout',lambda:.02)
    app=FastAPI();app.include_router(cloud.router)
    with TestClient(app) as client:
        payload={'request_id':str(uuid4()),'input':'timeout'}
        response=client.post(f'/v1/cloud/workspaces/{W}/chats',json=payload)
        assert response.json()['error_code']=='cloud_task_timeout'
        assert client.post(f'/v1/cloud/workspaces/{W}/chats',json=payload).json()['run_id']==response.json()['run_id']


def test_fixed_registry_rejects_arbitrary_runner_and_policy_mutation(database):
    install_knowledge(database)
    with database() as conn,conn.cursor() as cur:
        with pytest.raises(Exception,match='plugin contracts are immutable'):
            cur.execute("UPDATE platform.hybrid_plugins SET runner='shell' WHERE workspace_id=%s",(W,))
    p=admin.parser().parse_args(['--operator','LOCAL-ADMIN','plugin-install','--workspace',W,'--plugin','shell'])
    with pytest.raises(ValueError,match='not in the release registry'):
        admin.execute(p)


@pytest.mark.parametrize('change',['grant-expired','department-moved'])
def test_entry_cached_actor_is_not_used_after_model_finishes(database,monkeypatch,change):
    from app import desktop_auth,iam_oauth
    from fastapi.responses import JSONResponse
    stale=desktop_auth.TrustedActor(A.user_no,'account-a','A',A.organization_codes,'synthetic-session')
    fresh=desktop_auth.TrustedActor(A.user_no,'account-a','A',('NEW-DEPT',),'synthetic-session')
    checks=[]
    async def authenticate(request):
        checks.append(request.headers.get('authorization'))
        if change=='grant-expired':
            raise iam_oauth.IAMFailure(401,'desktop_session_expired')
        return fresh
    async def runner(trusted,*_):
        assert trusted is stale
        return {'text':'REPLY MUST BE DISCARDED'}
    monkeypatch.setenv('AUTH_MODE','iam_direct')
    monkeypatch.setattr(desktop_auth,'authenticate_desktop_request',authenticate)
    monkeypatch.setattr(cloud,'model_runner',runner)
    app=FastAPI()
    @app.middleware('http')
    async def cached_auth(request,call_next):
        request.state.trusted_actor=stale
        return await call_next(request)
    @app.exception_handler(iam_oauth.IAMFailure)
    async def iam_error(_request,exc):
        return JSONResponse({'detail':exc.code},status_code=exc.status)
    app.include_router(cloud.router)
    with TestClient(app) as client:
        response=client.post(f'/v1/cloud/workspaces/{W}/chats',
            headers={'Authorization':'Bearer synthetic-grant'},
            json={'request_id':str(uuid4()),'input':'synthetic work'})
        assert response.status_code==(401 if change=='grant-expired' else 403),response.text
        assert 'REPLY MUST BE DISCARDED' not in response.text
    assert checks==['Bearer synthetic-grant']
    with database() as conn,conn.cursor() as cur:
        cur.execute('SELECT status,result,error_code FROM platform.hybrid_runs')
        row=cur.fetchone()
        assert row['status']=='failed' and row['result'] is None
        assert row['error_code']==('trusted_identity_expired' if change=='grant-expired' else 'task_permissions_changed')


def test_cloud_history_pages_cursor_scope_and_sql_bounded_model_history(database,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from psycopg.types.json import Jsonb
    first=cloud.prepare_run(A,W,cloud.ChatRequest(request_id=uuid4(),input='0'),'chat')
    cloud.finish_run(A,first['run_id'],{'text':'reply'})
    conversation=cloud.read_run(A,first['run_id'])['conversation_id']
    stamp=datetime(2020,1,1,tzinfo=timezone.utc)
    with database() as conn,conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_runs SET created_at=%s WHERE id=%s',(stamp,first['run_id']))
        for n in range(1,105):
            cur.execute('''INSERT INTO platform.hybrid_runs
                (id,conversation_id,workspace_id,owner_user_no,request_id,request_hash,input_text,status,result,created_at,finished_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'completed',%s,%s,%s)''',
                (uuid4(),conversation,W,A.user_no,uuid4(),'0'*64,str(n),Jsonb({'text':'reply'}),stamp+timedelta(seconds=n),stamp))
    newest=cloud.read_conversation(A,conversation)
    assert len(newest['runs'])==50 and newest['has_more']
    assert [r['input'] for r in newest['runs']]==[str(n) for n in range(55,105)]
    middle=cloud.read_conversation(A,conversation,newest['next_before'])
    oldest=cloud.read_conversation(A,conversation,middle['next_before'])
    assert [r['input'] for r in middle['runs']]==[str(n) for n in range(5,55)]
    assert [r['input'] for r in oldest['runs']]==[str(n) for n in range(5)]
    assert oldest['has_more'] is False and oldest['next_before'] is None
    foreign=cloud.prepare_run(A,W,cloud.ChatRequest(request_id=uuid4(),input='other conversation'),'chat')
    cloud.finish_run(A,foreign['run_id'],{'text':'reply'})
    with pytest.raises(cloud.CloudError,match='conversation_cursor_not_found'):
        cloud.read_conversation(A,conversation,foreign['run_id'])
    monkeypatch.setattr(cloud,'MODEL_HISTORY_RUNS',3)
    prepared=cloud.prepare_run(A,W,cloud.ChatRequest(request_id=uuid4(),conversation_id=conversation,input='next'),'chat')
    assert [m['content'] for m in prepared['messages'] if m['role']=='user']==['102','103','104','next']


def test_workspace_quota_counts_all_versions_and_attachments_but_not_upload_retries(database,monkeypatch):
    enable_resources(database)
    monkeypatch.setenv('CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES','10')
    doc=publish(content='1234')
    body,attachment=upload(operation='chat',text='5678')
    # Idempotent repeat consumes no new space even with remaining quota < file.
    assert cloud.upload_attachment(A,W,body)['attachment_id']==attachment['attachment_id']
    with pytest.raises(cloud.CloudError,match='workspace_storage_quota_exceeded'):
        resources.update_document(A,doc['document_id'],resources.UpdateDocument(
            name=doc['name'],expected_version=1,content='abcd',visibility='workspace',explicit_upload=True))
    assert resources.read_document(A,doc['document_id'])['version']==1
    with pytest.raises(cloud.CloudError,match='workspace_storage_quota_exceeded'):
        upload(operation='chat',text='NEW')
    resources.update_document(A,doc['document_id'],resources.UpdateDocument(
        name=doc['name'],expected_version=1,content='ab',visibility='workspace',explicit_upload=True))
    with pytest.raises(cloud.CloudError,match='workspace_storage_quota_exceeded'):
        publish(content='x')


def test_concurrent_quota_admission_cannot_overcommit_document_and_attachment(database,monkeypatch):
    enable_resources(database)
    monkeypatch.setenv('CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES','3')
    def write(kind):
        try:
            if kind=='doc':
                publish(content='ab')
            else:
                upload(operation='chat',text='cd')
            return 'ok'
        except cloud.CloudError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(write,['doc','attachment']))
    assert results.count('ok')==1 and results.count('workspace_storage_quota_exceeded')==1
