import asyncio
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
from test_enterprise_tasks import database
from test_enterprise_tasks import upload,A,W
from app import cloud_history as h
from app.iam_oauth import IAMFailure

OWNER='a'*32
DIGEST='d'*64
USER='LOCAL-FIXTURE-A'
SID='synthetic-session'
NOW=datetime.now(timezone.utc)

@pytest.fixture
def store(database,monkeypatch):
    class Cursor:
        def __init__(self,base):self.base=base
        def __enter__(self):self.base.__enter__();return self
        def __exit__(self,*a):return self.base.__exit__(*a)
        def __getattr__(self,k):return getattr(self.base,k) if hasattr(self.base,k) else getattr(self.base.raw,k)
        def executemany(self,query,rows):
            for row in rows:self.base.execute(query,row)
    class Connection(database):
        def cursor(self):return Cursor(super().cursor())
    monkeypatch.setattr(h,'_connect',Connection)
    return Connection

def snapshot(days=91):
    return {'complete':True,'skipped':[], 'sessions':[{'sessionId':SID,'digest':DIGEST,'nativeFormat':3,
      'header':{'id':SID},'events':[{'type':'session/title','data':{'title':'Synthetic only'}}],
      'lastActivityAt':int((NOW-timedelta(days=days)).timestamp()*1000),'cleanupEligible':days>=90}]}

def test_projection_receipt_owner_and_project_files(store):
    h.save_inventory(OWNER,USER,snapshot())
    with store() as c,c.cursor() as q:
        q.execute('SELECT count(*) AS n FROM platform.personal_cloud_history_events WHERE owner_key=%s',(OWNER,));assert q.fetchone()['n']==1
        q.execute('SELECT title FROM platform.personal_cloud_history WHERE owner_key=%s',(OWNER,));assert q.fetchone()['title']=='Synthetic only'
    with pytest.raises(ValueError,match='owner'):h.save_inventory(OWNER,'OTHER',snapshot())
    assert h.trash_for(OWNER,USER)==[]
    with pytest.raises(IAMFailure):h.request_restore(OWNER,'OTHER',SID)


def test_reappeared_native_after_restore_crash_reconciles_even_after_new_event(store):
    h.save_inventory(OWNER,USER,snapshot())
    with store() as c,c.cursor() as q:
        q.execute("UPDATE platform.personal_cloud_history SET status='quarantined',trash_id=%s,restore_requested_at=now() WHERE owner_key=%s",(uuid4(),OWNER))
    changed=snapshot();changed['sessions'][0]['digest']='e'*64
    h.save_inventory(OWNER,USER,changed)
    with store() as c,c.cursor() as q:
        q.execute('SELECT status,trash_id,restore_requested_at,restored_at FROM platform.personal_cloud_history WHERE owner_key=%s',(OWNER,))
        row=q.fetchone();assert row['status']=='active' and not row['trash_id'] and not row['restore_requested_at'] and row['restored_at']


def test_project_attachment_uses_private_file_volume(store,monkeypatch,tmp_path):
    from app import enterprise_tasks as tasks
    from app.file_store import attachment_bytes
    monkeypatch.setenv('PLATFORM_FILE_ROOT',str(tmp_path/'project-files'))
    body,result=upload(text='retained project file')
    with store() as c,c.cursor() as q:
        q.execute('SELECT * FROM platform.hybrid_attachments WHERE workspace_id=%s',(W,))
        row=q.fetchone()
        assert row['content'] is None and row['object_key']
        assert attachment_bytes(row)==b'retained project file'
    assert len(list((tmp_path/'project-files').glob('*/*')))==1
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        with store() as c,c.cursor() as q:q.execute('UPDATE platform.hybrid_attachments SET content=NULL,object_key=NULL WHERE workspace_id=%s',(W,))


def test_90_30_retention_and_restoration_scope(store):
    trash=[];calls=[]
    tid=str(uuid4())
    async def rpc(owner,action,**args):
        assert owner==OWNER;calls.append(action)
        if action=='inventory':return snapshot()
        if action=='trash':return trash
        if action=='quarantine':
            # A distinct connection sees the committed projection before the file move.
            with store() as c,c.cursor() as q:
                q.execute('SELECT source_digest FROM platform.personal_cloud_history WHERE owner_key=%s',(OWNER,))
                assert q.fetchone()['source_digest']==args['archivedDigest']==DIGEST
            result={'trashId':tid,'sessionId':SID,'digest':DIGEST,'status':'trashed','payloadPresent':True,
              'trashedAt':int(NOW.timestamp()*1000),'purgeAfter':int((NOW+timedelta(days=30)).timestamp()*1000)}
            trash.append(result);return result
        raise AssertionError(action)
    assert asyncio.run(h.maintain_owner(OWNER,USER,rpc))['status']=='synced'
    assert calls.count('quarantine')==1 and 'purge' not in calls
    rows=h.trash_for(OWNER,USER);assert len(rows)==1
    assert h.trash_for('b'*32,USER)==h.trash_for(OWNER,'OTHER')==[]
    with pytest.raises(IAMFailure):h.request_restore('b'*32,USER,SID)
    assert h.request_restore(OWNER,USER,SID)['status']=='queued'
    async def restore_rpc(owner,action,**args):
        if action=='trash':return trash
        if action=='inventory':return {'complete':True,'sessions':[],'skipped':[]}
        assert action=='restore';trash[0].update(status='restored',payloadPresent=False,restoredAt=int(NOW.timestamp()*1000));return {'status':'restored'}
    asyncio.run(h.maintain_owner(OWNER,USER,restore_rpc))
    assert h.trash_for(OWNER,USER)==[]
    with store() as c,c.cursor() as q:
        q.execute('SELECT status,restored_at,restore_requested_at FROM platform.personal_cloud_history WHERE owner_key=%s',(OWNER,))
        row=q.fetchone();assert row['status']=='active' and row['restored_at'] and not row['restore_requested_at']


@pytest.mark.parametrize('reason',['active','incomplete','held'])
def test_unsafe_or_recent_sessions_never_quarantined(store,reason):
    value=snapshot(1 if reason=='active' else 91)
    if reason=='incomplete':value['complete']=False
    h.save_inventory(OWNER,USER,value)
    if reason=='held':
        with store() as c,c.cursor() as q:q.execute('UPDATE platform.personal_cloud_history SET legal_hold=TRUE WHERE owner_key=%s',(OWNER,))
    async def rpc(owner,action,**args):
        if action=='trash':return []
        if action=='inventory':return value
        raise AssertionError('unsafe mutation '+action)
    asyncio.run(h.maintain_owner(OWNER,USER,rpc))


def test_purge_erases_only_chat_projection_after_receipt(store):
    h.save_inventory(OWNER,USER,snapshot(130))
    tid=str(uuid4());trash=[{'trashId':tid,'sessionId':SID,'digest':DIGEST,'status':'trashed','payloadPresent':True,
      'trashedAt':int((NOW-timedelta(days=31)).timestamp()*1000),'purgeAfter':int((NOW-timedelta(days=1)).timestamp()*1000)}]
    async def rpc(owner,action,**args):
        if action=='trash':return trash
        if action=='inventory':return {'sessions':[],'complete':True,'skipped':[]}
        assert action=='purge' and args['archivedDigest']==DIGEST
        trash[0].update(status='purged',payloadPresent=False);return {'status':'purged'}
    asyncio.run(h.maintain_owner(OWNER,USER,rpc))
    with store() as c,c.cursor() as q:
        q.execute('SELECT status,header,title FROM platform.personal_cloud_history WHERE owner_key=%s',(OWNER,))
        row=q.fetchone();assert row=={'status':'purged','header':{},'title':''}
        q.execute('SELECT count(*) AS n FROM platform.personal_cloud_history_events WHERE owner_key=%s',(OWNER,));assert q.fetchone()['n']==0
        q.execute('SELECT count(*) AS n FROM platform.hybrid_workspaces');assert q.fetchone()['n']>0
