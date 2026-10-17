"""Real disposable PG transactions for whole-cloud-workspace retirement."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta
import os
from pathlib import Path
import re
import threading
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from test_enterprise_tasks import database, W
from app import cloud_retention as r
from app import file_store
from app.iam_oauth import IAMFailure

OWNER, OTHER = 'a' * 32, 'b' * 32
USER, OTHER_USER = 'RETENTION-FIXTURE-A', 'RETENTION-FIXTURE-B'
SID = 'session-10000000-0000-4000-8000-000000000001'


@pytest.fixture
def store(database, monkeypatch):
    monkeypatch.setattr(r, '_connect', database)
    monkeypatch.setenv('PERSONAL_CLOUD_RETENTION_POLICY', r.POLICY)
    monkeypatch.delenv('PLATFORM_FILE_ROOT', raising=False)
    for owner, user in ((OWNER, USER), (OTHER, OTHER_USER)):
        with database() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO platform.hybrid_workspaces
                (id,organization_code,name,kind,owner_user_no) VALUES(%s,'LOCAL-TEST','Synthetic personal','personal',%s)''',
                (str(UUID(hex=owner)), user))
    return database


def row(store, owner=OWNER):
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM platform.personal_cloud_retention WHERE owner_key=%s', (owner,))
        return cur.fetchone()


def age(store, owner=OWNER, user=USER, *, baseline_days=31, access_days=31):
    r.access(owner, user)
    with store() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE platform.personal_cloud_retention SET policy_started_at=clock_timestamp()-%s,
            last_access_at=clock_timestamp()-%s WHERE owner_key=%s''',
            (timedelta(days=baseline_days), timedelta(days=access_days), owner))


async def successful_wipe(owner, action, **kwargs):
    assert owner == OWNER and action == 'wipe-workspace' and not kwargs
    return {'status': 'deleted', 'stateDeleted': True, 'filesDeleted': True}


def test_adoption_once_and_readonly_polls_do_not_renew(store, monkeypatch):
    r.access(OWNER, USER)
    original = row(store)
    with store() as conn, conn.cursor() as cur:
        cur.execute("UPDATE platform.personal_cloud_retention SET last_access_at=clock_timestamp()-interval '2 days' WHERE owner_key=%s", (OWNER,))
    old_access = row(store)['last_access_at']
    for _ in range(3):
        r.access(OWNER, USER, activity=False)
    assert row(store)['last_access_at'] == old_access
    assert row(store)['policy_started_at'] == original['policy_started_at']
    maintain = r.maintain_owner
    async def empty_archives(owner, action, **kwargs):
        assert action == 'archived-sessions'
        return {'complete': True, 'sessionIds': []}
    async def synthetic_maintenance(owner, user):
        return await maintain(owner, user, empty_archives)
    monkeypatch.setattr(r, 'maintain_owner', synthetic_maintenance)
    assert asyncio.run(r.maintenance_cycle()) == {'cleared': 0, 'maintained': 2, 'deferred': 0, 'failed': 0}
    assert asyncio.run(r.maintenance_cycle()) == {'cleared': 0, 'maintained': 0, 'deferred': 0, 'failed': 0}
    assert row(store)['last_access_at'] == old_access
    assert row(store)['policy_started_at'] == original['policy_started_at']
    r.access(OWNER, USER, activity=True)
    assert row(store)['last_access_at'] > old_access


@pytest.mark.parametrize('baseline_days,access_days', [(0, 90), (31, 29)])
def test_full_30_days_required_for_baseline_and_last_access(store, baseline_days, access_days):
    age(store, baseline_days=baseline_days, access_days=access_days)
    assert r._claim(OWNER, USER) == (None, False)
    assert row(store)['deletion_id'] is None


def test_due_owner_claim_does_not_claim_recent_other_owner(store):
    age(store)
    r.access(OTHER, OTHER_USER, activity=True)
    generation, new = r._claim(OWNER, USER)
    assert generation and new
    assert r._claim(OWNER, USER) == (generation, False)
    assert r._claim(OTHER, OTHER_USER) == (None, False)
    with pytest.raises(IAMFailure) as error:
        r.access(OWNER, USER, activity=True)
    assert error.value.status == 409
    r.access(OTHER, OTHER_USER, activity=True)


def test_wrong_owner_and_department_are_never_admitted(store):
    r.access(OWNER, USER)
    for owner, user in ((OWNER, OTHER_USER), (str(UUID(str(W))).replace('-', ''), USER)):
        with pytest.raises(IAMFailure) as error:
            r.access(owner, user, activity=True)
        assert error.value.status == 403


def _gated_connection(base, marker, entered, release):
    class Cursor:
        def __init__(self, raw): self.raw = raw
        def __enter__(self): self.raw.__enter__(); return self
        def __exit__(self, *args): return self.raw.__exit__(*args)
        def __getattr__(self, key): return getattr(self.raw, key)
        def execute(self, query, params=None):
            result = self.raw.execute(query, params)
            if marker in query:
                entered.set()
                assert release.wait(10), 'synthetic transaction gate timed out'
            return result
    class Connection(base):
        def cursor(self): return Cursor(super().cursor())
    return Connection


@pytest.mark.parametrize('first', ['access', 'claim'])
def test_access_and_claim_serialize_under_same_row_lock(store, monkeypatch, first):
    age(store)
    entered, release = threading.Event(), threading.Event()
    marker = ('SET last_access_at=clock_timestamp()' if first == 'access' else 'SET deletion_id=')
    monkeypatch.setattr(r, '_connect', _gated_connection(store, marker, entered, release))
    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(r.access, OWNER, USER, activity=True) if first == 'access' else pool.submit(r._claim, OWNER, USER)
        assert entered.wait(5)
        second = pool.submit(r._claim, OWNER, USER) if first == 'access' else pool.submit(r.access, OWNER, USER, activity=True)
        try:
            with pytest.raises(FutureTimeout): second.result(timeout=0.1)
        finally:
            release.set()
        initial.result(timeout=5)
        if first == 'access':
            assert second.result(timeout=5) == (None, False)
        else:
            with pytest.raises(IAMFailure) as error: second.result(timeout=5)
            assert error.value.status == 409


@pytest.mark.parametrize('state_deleted,files_deleted', [(False, True), (True, False), (False, False)])
def test_either_volume_incomplete_retains_fence_until_retry(store, state_deleted, files_deleted):
    age(store)
    async def incomplete(owner, action, **kwargs):
        return {'status': 'deleted', 'stateDeleted': state_deleted, 'filesDeleted': files_deleted}
    with pytest.raises(ValueError, match='incomplete'):
        asyncio.run(r.maintain_owner(OWNER, USER, incomplete))
    claimed = row(store)
    assert claimed['deletion_id'] and claimed['last_error_code'] == 'maintenance_incomplete'
    with pytest.raises(IAMFailure): r.access(OWNER, USER, activity=True)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))['status'] == 'cleared'
    assert row(store)['deletion_id'] is None
    assert row(store)['cleared_at']


def test_new_preflight_409_can_release_fence_but_old_partial_409_cannot(store):
    age(store)
    old_access = row(store)['last_access_at']
    async def busy(owner, action, **kwargs): raise IAMFailure(409, 'synthetic-preflight-busy')
    assert asyncio.run(r.maintain_owner(OWNER, USER, busy))['status'] == 'deferred'
    assert row(store)['deletion_id'] is None
    assert row(store)['last_error_code'] == 'active_workspace'
    assert row(store)['last_access_at'] == old_access
    age(store)
    async def partial(owner, action, **kwargs): raise IAMFailure(503, 'synthetic-second-volume-failed')
    with pytest.raises(IAMFailure): asyncio.run(r.maintain_owner(OWNER, USER, partial))
    before = row(store)
    with pytest.raises(IAMFailure) as error: asyncio.run(r.maintain_owner(OWNER, USER, busy))
    assert error.value.status == 409
    after = row(store)
    assert after['deletion_id'] == before['deletion_id']
    assert after['last_access_at'] == before['last_access_at']
    with pytest.raises(IAMFailure): r.access(OWNER, USER)


def test_private_object_partial_pass_keeps_fence_and_exact_owner_scope(store, monkeypatch):
    age(store)
    calls = []
    monkeypatch.setattr(file_store, 'configured', lambda: True)
    class Objects:
        def delete_namespace(self, owner, scope):
            calls.append((owner, scope))
            return len(calls) > 1
    monkeypatch.setattr(file_store, 'FileStore', Objects)
    with pytest.raises(ValueError, match='another pass'):
        asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))
    assert row(store)['deletion_id']
    with pytest.raises(IAMFailure): r.access(OWNER, USER)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))['status'] == 'cleared'
    assert calls == [(USER, str(UUID(hex=OWNER)))] * 2


def test_object_configuration_missing_cannot_finish_whole_workspace_purge(store, monkeypatch, tmp_path):
    workspace = str(UUID(hex=OWNER))
    target = _seed_content(store, workspace, USER, OWNER)
    objects = file_store.FileStore(tmp_path / 'objects')
    key = objects.put(USER, workspace, b'payload')
    with store() as conn, conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_attachments SET content=NULL,object_key=%s WHERE id=%s', (key, target['attachment']))
    assert not file_store.configured()
    age(store)
    with pytest.raises(Exception):
        asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))
    assert row(store)['deletion_id']
    assert (objects.root / key).read_bytes() == b'payload'
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT object_key FROM platform.hybrid_attachments WHERE id=%s', (target['attachment'],))
        assert cur.fetchone()['object_key'] == key
    monkeypatch.setenv('PLATFORM_FILE_ROOT', str(objects.root))
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))['status'] == 'cleared'
    assert not (objects.root / key).exists()


def test_object_configuration_missing_other_employee_objects_are_not_target_scope(store, tmp_path):
    _seed_content(store, str(UUID(hex=OWNER)), USER, OWNER)
    other = _seed_content(store, str(UUID(hex=OTHER)), OTHER_USER, OTHER)
    objects = file_store.FileStore(tmp_path / 'objects')
    key = objects.put(OTHER_USER, str(UUID(hex=OTHER)), b'payload')
    with store() as conn, conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_attachments SET content=NULL,object_key=%s WHERE id=%s', (key, other['attachment']))
    age(store)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))['status'] == 'cleared'
    assert (objects.root / key).read_bytes() == b'payload'
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT object_key FROM platform.hybrid_attachments WHERE id=%s', (other['attachment'],))
        assert cur.fetchone()['object_key'] == key


def test_parallel_maintenance_only_one_rpc_owner(store):
    age(store)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(owner, action, **kwargs):
            entered.set()
            await release.wait()
            return await successful_wipe(owner, action, **kwargs)
        first = asyncio.create_task(r.maintain_owner(OWNER, USER, delayed))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            assert await r.maintain_owner(OWNER, USER, successful_wipe) == {'status': 'busy'}
        finally:
            release.set()
        assert (await first)['status'] == 'cleared'
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['claim', 'objects', 'complete'])
@pytest.mark.parametrize('cancel_count', [1, 2])
def test_cancellation_drains_mutating_thread_before_releasing_owner_lock(store, monkeypatch, stage, cancel_count):
    age(store)
    entered, release = threading.Event(), threading.Event()
    calls_lock, gated_calls = threading.Lock(), []

    def gate(function):
        def run(*args, **kwargs):
            with calls_lock:
                first = not gated_calls
                gated_calls.append(stage)
            if first:
                entered.set()
                assert release.wait(10), 'synthetic cancellation gate timed out'
            return function(*args, **kwargs)
        return run

    if stage == 'objects':
        monkeypatch.setattr(file_store, 'configured', lambda: True)
        class Objects:
            delete_namespace = gate(lambda self, owner, scope: True)
        monkeypatch.setattr(file_store, 'FileStore', Objects)
    else:
        name = '_claim' if stage == 'claim' else '_complete'
        monkeypatch.setattr(r, name, gate(getattr(r, name)))

    async def scenario():
        first = asyncio.create_task(r.maintain_owner(OWNER, USER, successful_wipe))
        assert await asyncio.to_thread(entered.wait, 5)
        second_calls = []
        async def must_not_enter(owner, action, **kwargs):
            second_calls.append(action)
            raise IAMFailure(503, 'synthetic second maintainer escaped owner lock')
        try:
            for _ in range(cancel_count):
                first.cancel()
                await asyncio.sleep(0)
            assert await r.maintain_owner(OWNER, USER, must_not_enter) == {'status': 'busy'}
            assert not first.done()
            assert second_calls == []
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 5)

    asyncio.run(scenario())
    # The thread is settled before the cancellation becomes observable: either
    # completion committed, or its durable intent remains for a later retry.
    assert (row(store)['deletion_id'] is None) is (stage == 'complete')


def _seed_content(store, workspace, user, owner=None, location='cloud', sid=SID):
    conversation, run, attachment, document, request = [uuid4() for _ in range(5)]
    with store() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO platform.hybrid_conversations(id,workspace_id,owner_user_no,operation,title)
            VALUES(%s,%s,%s,'chat','Synthetic private title')''', (conversation, workspace, user))
        cur.execute('''INSERT INTO platform.hybrid_attachments
            (id,workspace_id,owner_user_no,request_id,operation,name,sha256,size_bytes,content)
            VALUES(%s,%s,%s,%s,'chat','synthetic.txt',%s,7,%s)''', (attachment, workspace, user, request, 'd' * 64, b'payload'))
        cur.execute('''INSERT INTO platform.hybrid_runs
            (id,conversation_id,workspace_id,owner_user_no,request_id,request_hash,input_text,attachment_ids,status,result)
            VALUES(%s,%s,%s,%s,%s,%s,'Synthetic prompt',%s,'completed',%s)''',
            (run, conversation, workspace, user, request, 'd' * 64, [attachment], Jsonb({'text': 'Synthetic result'})))
        cur.execute('''INSERT INTO platform.hybrid_documents(id,workspace_id,owner_user_no,name,current_version)
            VALUES(%s,%s,%s,'Synthetic document',1)''', (document, workspace, user))
        cur.execute('''INSERT INTO platform.hybrid_document_versions(document_id,version,name,content,sha256,size_bytes,created_by)
            VALUES(%s,1,'Synthetic version','Synthetic document content',%s,26,%s)''', (document, 'd' * 64, user))
        cur.execute('''INSERT INTO platform.enterprise_native_bindings
            (owner_user_no,request_id,native_session_id,location,request_hash,plugin_key,plugin_version,conversation_id,run_id)
            VALUES(%s,%s,%s,%s,%s,'knowledge-qa','1.0.0',%s,%s)''', (user, request, sid, location, 'd' * 64, conversation, run))
        if owner:
            cur.execute('''INSERT INTO platform.personal_cloud_history
                (owner_key,session_id,owner_user_no,title,native_format,source_digest,last_activity_at,header,event_count)
                VALUES(%s,%s,%s,'Synthetic title','3',%s,now(),%s,1)''', (owner, sid, user, 'd' * 64, Jsonb({'content': 'synthetic header'})))
            cur.execute('INSERT INTO platform.personal_cloud_history_events VALUES(%s,%s,0,%s)', (owner, sid, Jsonb({'content': 'synthetic message'})))
            cur.execute('INSERT INTO platform.personal_cloud_storage_jobs(owner_key,owner_user_no) VALUES(%s,%s) ON CONFLICT DO NOTHING', (owner, user))
    return {'conversation': conversation, 'run': run, 'attachment': attachment, 'document': document, 'request': request}


def test_whole_owner_purges_pg_content_preserving_other_owner_department_and_local_binding(store):
    target = _seed_content(store, str(UUID(hex=OWNER)), USER, OWNER)
    other = _seed_content(store, str(UUID(hex=OTHER)), OTHER_USER, OTHER)
    # Explicit local uploads are still server-side copies in this cloud
    # workspace and expire with it. The employee's local disk is never touched.
    local_cloud_copy = _seed_content(store, str(UUID(hex=OWNER)), USER, location='local')
    department = _seed_content(store, W, USER, location='local')
    age(store)
    with pytest.raises(Exception, match='immutable'):
        with store() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM platform.hybrid_document_versions WHERE document_id=%s', (target['document'],))
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_wipe))['status'] == 'cleared'
    with store() as conn, conn.cursor() as cur:
        for table, id_key in (('hybrid_runs', 'run'), ('hybrid_conversations', 'conversation'), ('hybrid_attachments', 'attachment'), ('hybrid_documents', 'document')):
            cur.execute('SELECT id FROM platform.' + table + ' WHERE id=ANY(%s)', ([target[id_key], local_cloud_copy[id_key], other[id_key], department[id_key]],))
            assert {item['id'] for item in cur.fetchall()} == {other[id_key], department[id_key]}
        cur.execute('SELECT document_id FROM platform.hybrid_document_versions')
        assert {item['document_id'] for item in cur.fetchall()} == {other['document'], department['document']}
        for table in ('personal_cloud_history', 'personal_cloud_history_events', 'personal_cloud_storage_jobs'):
            cur.execute('SELECT owner_key FROM platform.' + table)
            assert {item['owner_key'] for item in cur.fetchall()} == {OTHER}
        cur.execute('SELECT request_id FROM platform.enterprise_native_bindings')
        assert {item['request_id'] for item in cur.fetchall()} == {other['request'], department['request']}
    with pytest.raises(Exception, match='immutable'):
        with store() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM platform.hybrid_document_versions WHERE document_id=%s', (department['document'],))


def test_session_tombstone_is_exact_identity_and_owner_scoped(store):
    r.queue_session_delete(OWNER, USER, SID)
    r.queue_session_delete(OWNER, USER, SID)
    with pytest.raises(IAMFailure) as error:
        r.access(OWNER, USER, session_id=SID)
    assert error.value.status == 410
    r.access(OWNER, USER, session_id=SID.removeprefix('session-'))
    r.access(OTHER, OTHER_USER, session_id=SID)
    async def delete(owner, action, **kwargs):
        if action == 'archived-sessions': return {'complete': True, 'sessionIds': []}
        assert owner == OWNER and action == 'delete-session' and kwargs == {'sessionId': SID}
        return {'status': 'deleted'}
    assert asyncio.run(r.maintain_owner(OWNER, USER, delete))['deleted_sessions'] == 1
    with pytest.raises(IAMFailure) as error:
        r.access(OWNER, USER, session_id=SID)
    assert error.value.status == 410
    assert asyncio.run(r.maintain_owner(OWNER, USER, delete))['deleted_sessions'] == 0


async def successful_session_delete(owner, action, **kwargs):
    assert owner == OWNER
    if action == 'archived-sessions': return {'complete': True, 'sessionIds': []}
    assert action == 'delete-session'
    return {'status': 'deleted'}


def test_single_session_removes_plugin_payload_not_other_session_or_local_binding(store):
    workspace = str(UUID(hex=OWNER))
    target = _seed_content(store, workspace, USER, OWNER)
    retained = _seed_content(store, workspace, USER, OWNER, sid='session-20000000-0000-4000-8000-000000000002')
    local = _seed_content(store, workspace, USER, location='local')
    r.queue_session_delete(OWNER, USER, SID)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    with store() as conn, conn.cursor() as cur:
        for table, key in (('hybrid_runs', 'run'), ('hybrid_conversations', 'conversation'), ('hybrid_attachments', 'attachment')):
            cur.execute('SELECT id FROM platform.' + table + ' WHERE id=ANY(%s)', ([target[key], retained[key], local[key]],))
            assert {item['id'] for item in cur.fetchall()} == {retained[key], local[key]}
        cur.execute('SELECT request_id FROM platform.enterprise_native_bindings WHERE owner_user_no=%s', (USER,))
        assert {item['request_id'] for item in cur.fetchall()} == {retained['request'], local['request']}
        cur.execute('SELECT session_id FROM platform.personal_cloud_history_events WHERE owner_key=%s', (OWNER,))
        assert {item['session_id'] for item in cur.fetchall()} == {'session-20000000-0000-4000-8000-000000000002'}


def test_session_delete_keeps_attachment_referenced_by_retained_run(store):
    workspace = str(UUID(hex=OWNER))
    target = _seed_content(store, workspace, USER, OWNER)
    retained = _seed_content(store, workspace, USER, sid='session-20000000-0000-4000-8000-000000000002')
    with store() as conn, conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_runs SET attachment_ids=%s WHERE id=%s', ([target['attachment']], retained['run']))
    r.queue_session_delete(OWNER, USER, SID)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT content FROM platform.hybrid_attachments WHERE id=%s', (target['attachment'],))
        attachment = cur.fetchone()
        assert attachment is not None and bytes(attachment['content']) == b'payload'
        cur.execute('SELECT attachment_ids FROM platform.hybrid_runs WHERE id=%s', (retained['run'],))
        assert cur.fetchone()['attachment_ids'] == [target['attachment']]


def test_last_session_reference_removes_old_request_attachment_and_physical_object(store, monkeypatch, tmp_path):
    workspace = str(UUID(hex=OWNER))
    target = _seed_content(store, workspace, USER, OWNER)
    last_sid = 'session-20000000-0000-4000-8000-000000000002'
    last = _seed_content(store, workspace, USER, OWNER, sid=last_sid)
    monkeypatch.setenv('PLATFORM_FILE_ROOT', str(tmp_path / 'objects'))
    objects = file_store.FileStore()
    key = objects.put(USER, workspace, b'payload')
    other_key = objects.put(OTHER_USER, str(UUID(hex=OTHER)), b'payload')
    department_key = objects.put(USER, W, b'payload')
    with store() as conn, conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_attachments SET content=NULL,object_key=%s WHERE id=%s', (key, target['attachment']))
        cur.execute('UPDATE platform.hybrid_runs SET attachment_ids=%s WHERE id=%s', ([target['attachment']], last['run']))
    r.queue_session_delete(OWNER, USER, SID)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    assert (objects.root / key).read_bytes() == b'payload'
    r.queue_session_delete(OWNER, USER, last_sid)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT id FROM platform.hybrid_attachments WHERE id=ANY(%s)', ([target['attachment'], last['attachment']],))
        assert cur.fetchall() == []
    assert not (objects.root / key).exists()
    assert (objects.root / other_key).read_bytes() == b'payload'
    assert (objects.root / department_key).read_bytes() == b'payload'


@pytest.mark.parametrize('uploader_location', ['cloud', 'local'])
@pytest.mark.parametrize('uploader_state', ['run', 'binding_only'])
def test_borrower_deletion_preserves_attachment_of_live_upload_request(store, uploader_location, uploader_state):
    workspace = str(UUID(hex=OWNER))
    uploader = _seed_content(store, workspace, USER, OWNER if uploader_location == 'cloud' else None, location=uploader_location)
    borrower_sid = 'session-20000000-0000-4000-8000-000000000002'
    borrower = _seed_content(store, workspace, USER, OWNER, sid=borrower_sid)
    with store() as conn, conn.cursor() as cur:
        # The uploader's attachment remains its own input even if no surviving
        # uploader run currently lists it (e.g. the input was not selected).
        cur.execute('UPDATE platform.hybrid_runs SET attachment_ids=%s WHERE id=%s', ([], uploader['run']))
        if uploader_state == 'binding_only':
            # bind_request commits before prepare_run; retain the upload in
            # that admitted-request window even though no run exists yet.
            cur.execute('DELETE FROM platform.hybrid_runs WHERE id=%s', (uploader['run'],))
            cur.execute('UPDATE platform.enterprise_native_bindings SET run_id=NULL,conversation_id=NULL WHERE owner_user_no=%s AND request_id=%s', (USER, uploader['request']))
        cur.execute('UPDATE platform.hybrid_runs SET attachment_ids=%s WHERE id=%s', ([uploader['attachment']], borrower['run']))
    r.queue_session_delete(OWNER, USER, borrower_sid)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT content FROM platform.hybrid_attachments WHERE id=%s', (uploader['attachment'],))
        assert bytes(cur.fetchone()['content']) == b'payload'
        cur.execute('SELECT request_id FROM platform.enterprise_native_bindings WHERE owner_user_no=%s', (USER,))
        assert [item['request_id'] for item in cur.fetchall()] == [uploader['request']]


def test_attachment_reference_expansion_never_crosses_employee_or_department_scope(store):
    target = _seed_content(store, str(UUID(hex=OWNER)), USER, OWNER)
    other = _seed_content(store, str(UUID(hex=OTHER)), OTHER_USER, OTHER)
    department = _seed_content(store, W, USER, location='local')
    with store() as conn, conn.cursor() as cur:
        # Deliberately inconsistent synthetic references exercise the destructive
        # boundary even though normal admission rejects these attachments.
        cur.execute('UPDATE platform.hybrid_runs SET attachment_ids=%s WHERE id=%s',
                    ([other['attachment'], department['attachment']], target['run']))
    r.queue_session_delete(OWNER, USER, SID)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT id FROM platform.hybrid_attachments WHERE id=ANY(%s)',
                    ([target['attachment'], other['attachment'], department['attachment']],))
        assert {item['id'] for item in cur.fetchall()} == {other['attachment'], department['attachment']}


def test_session_object_erasure_failure_retains_queue_and_retries_without_other_data(store, monkeypatch, tmp_path):
    workspace = str(UUID(hex=OWNER))
    target = _seed_content(store, workspace, USER, OWNER)
    retained = _seed_content(store, workspace, USER, sid='session-20000000-0000-4000-8000-000000000002')
    monkeypatch.setenv('PLATFORM_FILE_ROOT', str(tmp_path / 'objects'))
    objects = file_store.FileStore()
    object_key = objects.put(USER, workspace, b'payload')
    with store() as conn, conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_attachments SET content=NULL,object_key=%s WHERE id=%s', (object_key, target['attachment']))
    r.queue_session_delete(OWNER, USER, SID)
    with monkeypatch.context() as scoped:
        def fail(*args): raise OSError('synthetic object removal failure')
        scoped.setattr(file_store.FileStore, 'delete_object', fail)
        with pytest.raises(OSError, match='synthetic'):
            asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))
    with store() as conn, conn.cursor() as cur:
        cur.execute('SELECT completed_at FROM platform.personal_cloud_session_deletions WHERE owner_key=%s AND session_id=%s', (OWNER, SID))
        assert cur.fetchone()['completed_at'] is None
        cur.execute('SELECT id FROM platform.hybrid_runs WHERE id=ANY(%s)', ([target['run'], retained['run']],))
        assert len(cur.fetchall()) == 2
    with pytest.raises(IAMFailure) as error: r.access(OWNER, USER, session_id=SID)
    assert error.value.status == 410
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    assert not (objects.root / object_key).exists()


def test_session_delete_preserves_deduplicated_object_with_retained_metadata(store, monkeypatch, tmp_path):
    workspace = str(UUID(hex=OWNER))
    target = _seed_content(store, workspace, USER, OWNER)
    retained = _seed_content(store, workspace, USER, sid='session-20000000-0000-4000-8000-000000000002')
    monkeypatch.setenv('PLATFORM_FILE_ROOT', str(tmp_path / 'objects'))
    objects = file_store.FileStore()
    key = objects.put(USER, workspace, b'payload')
    with store() as conn, conn.cursor() as cur:
        cur.execute('UPDATE platform.hybrid_attachments SET content=NULL,object_key=%s WHERE id=ANY(%s)', (key, [target['attachment'], retained['attachment']]))
    r.queue_session_delete(OWNER, USER, SID)
    assert asyncio.run(r.maintain_owner(OWNER, USER, successful_session_delete))['deleted_sessions'] == 1
    assert (objects.root / key).read_bytes() == b'payload'


def test_archived_inventory_migrates_once_and_incomplete_result_never_marks_complete(store):
    r.access(OWNER, USER)
    access_before = row(store)['last_access_at']
    async def incomplete(owner, action, **kwargs):
        assert action == 'archived-sessions'
        return {'complete': False, 'sessionIds': [SID]}
    with pytest.raises(ValueError, match='incomplete'):
        asyncio.run(r.maintain_owner(OWNER, USER, incomplete))
    assert row(store)['archive_migrated_at'] is None
    assert r._pending(OWNER) == []
    calls = []
    async def complete(owner, action, **kwargs):
        calls.append(action)
        if action == 'archived-sessions': return {'complete': True, 'sessionIds': [SID, SID]}
        return {'status': 'deleted'}
    assert asyncio.run(r.maintain_owner(OWNER, USER, complete))['deleted_sessions'] == 1
    assert row(store)['archive_migrated_at']
    assert row(store)['last_access_at'] == access_before
    assert asyncio.run(r.maintain_owner(OWNER, USER, complete))['deleted_sessions'] == 0
    assert calls == ['archived-sessions', 'delete-session']


def test_migration_015_grants_existing_personal_owner_full_baseline_and_queues_old_trash():
    dsn = os.getenv('IAM_TEST_DATABASE_URL')
    if not dsn: pytest.skip('local synthetic PostgreSQL URL not configured')
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    schema = 'retention_upgrade_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            paths = sorted((Path(__file__).parents[3] / 'db/migrations').glob('*.sql'))
            for path in paths:
                if path.name.startswith('015_'): continue
                conn.execute(re.sub(r'\bplatform\b', schema, path.read_text()))
            conn.execute('''INSERT INTO "''' + schema + '''".hybrid_workspaces
                (id,organization_code,name,kind,owner_user_no,created_at)
                VALUES(%s,'LOCAL-TEST','Old personal','personal',%s,now()-interval '500 days')''',
                (str(UUID(hex=OWNER)), USER))
            conn.execute('''INSERT INTO "''' + schema + '''".hybrid_workspaces
                (id,organization_code,name,kind) VALUES(%s,'LOCAL-TEST','Keep department','department')''', (uuid4(),))
            conn.execute('''INSERT INTO "''' + schema + '''".personal_cloud_history
                (owner_key,session_id,owner_user_no,native_format,source_digest,last_activity_at,header,event_count,status)
                VALUES(%s,%s,%s,'3',%s,now()-interval '100 days',%s,0,'quarantined')''',
                (OWNER, SID, USER, 'd' * 64, Jsonb({'synthetic': True})))
            before = conn.execute('SELECT clock_timestamp() AS value').fetchone()['value']
            migration = next(path for path in paths if path.name.startswith('015_'))
            conn.execute(re.sub(r'\bplatform\b', schema, migration.read_text()))
            rows = conn.execute('SELECT * FROM "' + schema + '".personal_cloud_retention').fetchall()
            assert len(rows) == 1 and rows[0]['owner_key'] == OWNER
            assert rows[0]['policy_started_at'] >= before
            assert rows[0]['last_access_at'] >= before
            assert rows[0]['deletion_id'] is None
            deletion = conn.execute('SELECT owner_key,session_id,completed_at FROM "' + schema + '".personal_cloud_session_deletions').fetchone()
            assert deletion == {'owner_key': OWNER, 'session_id': SID, 'completed_at': None}
        finally:
            assert re.fullmatch(r'retention_upgrade_[a-f0-9]{32}', schema)
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
