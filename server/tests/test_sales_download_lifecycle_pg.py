"""Real PostgreSQL, Linux blobs, gateway ports and order review/preview.

Run only against a disposable localhost database named maimi_sales_review_qa_*.
The extraction provider is synthetic; Oracle and ECM are never called.
"""
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'server/gateway'), str(ROOT / 'integrations/sales-order-mcp')]
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from cryptography.fernet import Fernet
from openpyxl import Workbook
from app import storage_lifecycle as storage
from app import project_platform as project
from maimi_projects import sales_orders
from maimi_projects.service import ProjectFailure
from order_mcp.config import Settings
from order_mcp.service import OrderService

DSN = os.getenv('MAIMI_SALES_REVIEW_TEST_DSN', '')
if os.getenv('MAIMI_BASELINE_STORAGE_SOURCE'):
    import importlib.util
    baseline_path = Path(os.environ['MAIMI_BASELINE_STORAGE_SOURCE']).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('app.storage_baseline_qa', baseline_path)
    baseline = importlib.util.module_from_spec(spec); sys.modules[spec.name] = baseline; spec.loader.exec_module(baseline)
    baseline.StorageError = storage.StorageError
    storage.Storage.finish_download = baseline.Storage.finish_download

def connect():
    parsed = conninfo_to_dict(DSN)
    assert parsed.get('host') == '127.0.0.1' and parsed.get('dbname', '').startswith('maimi_sales_review_qa_')
    return psycopg.connect(DSN, row_factory=dict_row)

@unittest.skipUnless(DSN, 'Requires an explicitly isolated local PostgreSQL database')
class SalesDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as db:
            for file in sorted((ROOT / 'server/gateway/migrations').glob('*.sql')):
                db.execute(file.read_text('utf8'))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='sales-review-blobs-')
        self.order_directory = tempfile.TemporaryDirectory(prefix='sales-review-orders-')
        self.owner, self.sid, self.epoch = uuid4().hex, 'session-' + str(uuid4()), str(uuid4())
        self.user = 'M' + str(uuid4().int % 100000000).zfill(8)
        self.store = storage.Storage(connect=connect, blobs=storage.BlobStore(self.directory.name))
        self.revoked = False
        with connect() as db:
            db.execute("INSERT INTO platform.hybrid_workspaces(id,organization_code,name,kind,owner_user_no) VALUES(%s,'','synthetic','personal',%s)", (str(UUID(hex=self.owner)), self.user))
            # Admission identity is local to this test; no directory/IAM query.
            db.execute('INSERT INTO platform.personal_cloud_retention(owner_key,workspace_id,owner_user_no) VALUES(%s,%s,%s)', (self.owner, str(UUID(hex=self.owner)), self.user))
            db.execute('INSERT INTO platform.project_scopes(owner_key,session_id,epoch,fence) VALUES(%s,%s,%s,1)', (self.owner, self.sid, self.epoch))
        self.cipher = Fernet(Fernet.generate_key())
        case = self
        class Connector:
            def seal(self, user, value):
                return case.cipher.encrypt(json.dumps({'user': user, 'value': value}).encode()).decode()
            def unseal(self, user, value):
                record = json.loads(case.cipher.decrypt(value.encode()))
                assert record['user'] == user
                return record['value']
        self.access = project.Access(actor=SimpleNamespace(user_no=self.user), owner=self.owner, lease='synthetic', session_id=self.sid,
            invocation_id='pi_synthetic', permit={'source': 'model', 'connector': 'sales-orders', 'action': 'recognize'}, worker_hash='synthetic', epoch=self.epoch, fence=1)
        async def authorize(access):
            if self.revoked or access is not self.access:
                raise PermissionError('synthetic authorization revoked')
        self.runtime = SimpleNamespace(db=project.Database(Connector(), connect=connect), authorize=authorize)
        self.ctx = SimpleNamespace(user_no=self.user, native_session_id=self.sid, source='model')
        self.files = project.FilePort(self.runtime, self.access, self.ctx)
        self.state = project.StatePort(self.runtime, self.access, self.ctx)
        self.env = patch.dict(os.environ, {'MAIMI_STORAGE_POLICY': 'managed-cloud-files-r72'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.patcher = patch.object(storage, 'default_store', return_value=self.store)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def tearDown(self):
        self.directory.cleanup(); self.order_directory.cleanup()

    def sql(self, statement, args=()):
        with connect() as db:
            cur = db.execute(statement, args)
            return cur.fetchall() if cur.description else None

    def start_job(self, action, connector='sales-orders'):
        jid = str(uuid4()); self.access.job_id = jid; self.access.permit.update(connector=connector, action=action)
        self.sql('''INSERT INTO platform.project_jobs(owner_key,session_id,job_id,request_id,invocation_id,lease_hash,
          connector,action,input_hash,process_id,status,deadline,content_expires_at)
          VALUES(%s,%s,%s,%s,'pi_synthetic',%s,%s,%s,%s,'local-qa','running',clock_timestamp()+interval '5 minutes',clock_timestamp()+interval '1 hour')''',
          (self.owner, self.sid, jid, str(uuid4()), 'a' * 64, connector, action, 'b' * 64))
        return jid

    def finish_job(self, jid, value):
        encrypted = self.runtime.db.seal(self.access, 'job:' + jid, value)
        self.sql("UPDATE platform.project_jobs SET status='completed',encrypted_result=%s WHERE job_id=%s", (encrypted, jid))
        return encrypted

    def download(self, fid, *, desktop=False):
        lease = self.store.begin_download(self.owner, self.sid, fid)
        data = self.store.read_download(self.owner, self.sid, lease['lease_id'])
        value = self.store.finish_download(self.owner, self.sid, lease['lease_id'], bytes_received=len(data), sha256=hashlib.sha256(data).hexdigest(), desktop_saved_verified=desktop)
        return data, value

    def snapshot(self, fid, jid):
        return self.sql('''SELECT f.expires_at,f.status,f.first_transfer_at,j.encrypted_result,j.content_expires_at
          FROM platform.storage_files f JOIN platform.project_jobs j ON j.job_id=%s WHERE f.file_id=%s''', (jid, fid))[0]

    def test_workbook_download_keeps_real_review_source_and_preview_confirmation(self):
        async def flow():
            case = self
            wb = Workbook(); wb.active.title = '销货订单导入'; wb.active.append(['示例员甲用例'])
            buffer = io.BytesIO(); wb.save(buffer); workbook = buffer.getvalue()
            header = {'业务实体': '示例制造甲有限公司', '客户名称': '示例员甲购方', '客户代号': 'QA123', '客户PO号': 'QA-DOWNLOAD', '币种': 'CNY', '含税单价': 3, '请求日期': '2026-12-30'}
            class Provider:
                async def extract(self, name, raw, employee, display):
                    assert raw == b'synthetic source' and employee == case.user
                    return {'rows': [header | {'订购项目': 'TEST-' + str(i), '订单数量': i} for i in (1, 2, 3)], 'workbook': workbook, 'workbook_kind': 'xlsx'}
            class NoOracle:
                def __getattr__(self, name):
                    raise AssertionError('Oracle must never be called: ' + name)
            settings = Settings(data_dir=Path(self.order_directory.name),
                                master_data=Path(self.order_directory.name) / 'missing',
                                multi_line_confirmed=True)
            orders = OrderService(settings, provider=Provider(), oracle=NoOracle())
            async def order_call(action, args):
                if action == 'recognize':
                    source = Path(self.order_directory.name) / 'source.png'; source.write_bytes(base64.b64decode(args['data']))
                    return await orders.recognize(source, args['name'], case.user, '示例员甲验收')
                if action == 'review': return orders.review(args['order_id'])
                if action == 'prepare':
                    value = orders.prepare_submission(args['order_id'], args['revision'])
                    return value | {
                        'oracle_xml': orders.export(args['order_id'])['xml'],
                        'oracle_target': {'environment': 'test', 'multi_line_confirmed': settings.multi_line_confirmed},
                    }
                raise AssertionError('Unexpected order operation: ' + action)
            async def auth(ctx, key):
                permit = self.access.permit
                if (ctx.user_no != self.access.actor.user_no or ctx.native_session_id != self.access.session_id
                        or ctx.source != permit['source'] or key != permit['connector']):
                    raise PermissionError('Synthetic actor, session or permit does not match')
                await self.runtime.authorize(self.access)
            def request_service(files, state):
                async def save(ctx, card):
                    await auth(ctx, card['connector'])
                    old = await state.get(ctx, 'results') or []
                    await state.put(ctx, 'results', old + [card])
                    return {'status': card['status'], 'project_result': card}
                return SimpleNamespace(files=files, sales_orders=order_call, _authorize=auth, _save_result=save)

            svc = request_service(self.files, self.state)
            recognize_job = self.start_job('recognize')
            original = self.store.put(self.owner, self.sid, request_id='source', kind='ordinary', name='source.png', media_type='image/png', data=b'synthetic source')
            attachment = await self.files.register({'kind': 'file', 'ref': {'attachmentId': 'sha256:' + original['sha256'], 'name': 'source.png', 'bytes': original['expected_bytes']}}, 'native_message', 'image/png')
            recognized = await sales_orders.invoke(svc, self.ctx, 'recognize', {'attachment_id': attachment['attachment_id']})
            self.finish_job(recognize_job, recognized)
            view = recognized['order_result']['orders'][0]; self.assertEqual(len(view['order']['lines']), 3)
            artifact = recognized['project_result']['artifacts'][0]
            output, _ = await self.files.record(artifact['attachment_id']); fid = output['storage_id']
            before = self.snapshot(fid, recognize_job); source_expiry = self.store.inspect(self.owner, self.sid, original['file_id'])['expires_at']
            data, receipt = self.download(fid); self.assertEqual(data, workbook); self.assertFalse(receipt['browser_saved_to_disk_verified'])
            after = self.snapshot(fid, recognize_job)
            self.assertEqual(after['encrypted_result'], before['encrypted_result']); self.assertEqual(after['expires_at'], before['expires_at'])
            self.assertIsNotNone(after['first_transfer_at']); self.assertEqual(after['content_expires_at'], before['content_expires_at'])
            self.assertEqual(self.store.inspect(self.owner, self.sid, original['file_id'])['expires_at'], source_expiry)
            self.assertEqual((await self.files.read(self.ctx, attachment['attachment_id'], 100))['data'], b'synthetic source')
            history = await project.ProjectRuntime.history(self.runtime, self.access, {'history_job_ids': [recognize_job]})
            self.assertEqual(len(history['history']), 1); self.assertEqual(history['history'][0]['result']['project_result']['id'], recognized['project_result']['id'])
            self.assertTrue(await self.state.get(self.ctx, 'results'))
            # Continue through actual gateway review and version-bound preview.
            # A customer-supplied order item needs ERP evidence or an explicit reviewer choice.
            incomplete = orders.review(view['id'])
            self.assertFalse(incomplete['can_prepare'])
            codes = {f'lines.{i}.item_code': f'TEST-{i + 1}' for i in range(3)}
            view = orders.revise(view['id'], incomplete['revision'], codes, 'Synthetic reviewer confirmed internal items', human=True)
            review_job = self.start_job('review')
            reviewed = await sales_orders.invoke(svc, self.ctx, 'review', {'order_id': view['id']}); self.finish_job(review_job, reviewed)
            self.assertEqual(reviewed['order_result']['status'], 'review'); self.assertTrue(reviewed['order_result']['can_prepare'])
            prepare_job = self.start_job('prepare')
            request = {'order_id': view['id'], 'revision': view['revision']}
            with self.assertRaisesRegex(ProjectFailure, 'project_human_authorization_required'):
                await sales_orders.invoke(svc, self.ctx, 'prepare', request)
            reviewer = SimpleNamespace(**{**vars(self.ctx), 'source': 'human'})
            with self.assertRaises(PermissionError):
                await auth(reviewer, 'sales-orders')
            self.access.permit['source'] = 'human'
            for changed in ({'user_no': 'OTHER'}, {'native_session_id': 'another-session'}):
                with self.assertRaises(PermissionError):
                    await auth(SimpleNamespace(**{**vars(reviewer), **changed}), 'sales-orders')
            review_service = request_service(project.FilePort(self.runtime, self.access, reviewer),
                                             project.StatePort(self.runtime, self.access, reviewer))
            prepared = await sales_orders.invoke(review_service, reviewer, 'prepare', request)
            self.finish_job(prepare_job, prepared)
            confirm = prepared['project_result']['actions'][0]
            self.assertEqual(confirm['action'], 'submit'); self.assertTrue(confirm['arguments']['confirmation_token'])
            xml_record, _ = await self.files.record(prepared['project_result']['artifacts'][0]['attachment_id'])
            xml_before = self.snapshot(xml_record['storage_id'], prepare_job); xml, _ = self.download(xml_record['storage_id'], desktop=True)
            self.assertIn(b'QA-DOWNLOAD', xml); self.assertEqual(self.snapshot(xml_record['storage_id'], prepare_job)['encrypted_result'], xml_before['encrypted_result'])
            self.assertEqual(orders.review(view['id'])['status'], 'prepared')
            self.revoked = True
            with self.assertRaises(PermissionError): await self.files.read(self.ctx, attachment['attachment_id'], 100)
            with self.assertRaises(PermissionError): await self.state.get(self.ctx, 'results')
        asyncio.run(flow())

    def make_delivery(self, connector='sales-orders', action='recognize', status='completed', expired=False):
        jid = self.start_job(action, connector)
        source = self.store.put(self.owner, self.sid, request_id=str(uuid4()), kind='ordinary', name='source.txt', media_type='text/plain', data=b'source')
        self.store.bind_task_file(self.owner, self.sid, source['file_id'], task_id=jid, role='input')
        output = self.store.put(self.owner, self.sid, request_id=str(uuid4()), kind='enterprise_output', job_id=jid, name='output.bin', media_type='application/octet-stream', data=b'output')
        self.store.bind_task_file(self.owner, self.sid, output['file_id'], task_id=jid, role='output')
        self.finish_job(jid, {'project_result': {'connector': connector}})
        self.sql('UPDATE platform.project_jobs SET status=%s,content_expires_at=clock_timestamp()+(%s*interval \'1 second\') WHERE job_id=%s', (status, -1 if expired else 3600, jid))
        return jid, source['file_id'], output['file_id']

    def test_legacy_human_recognition_aliases_keep_original_deadlines(self):
        for action in ('recognize_test', 'recognize_production'):
            with self.subTest(action=action):
                jid, source, output = self.make_delivery(action=action)
                before = self.snapshot(output, jid); self.download(output)
                self.assertEqual(self.snapshot(output, jid)['expires_at'], before['expires_at'])
                self.assertEqual(self.snapshot(output, jid)['encrypted_result'], before['encrypted_result'])
                self.assertEqual(self.store.inspect(self.owner, self.sid, source)['status'], 'active')

    def test_other_connectors_terminal_sales_and_expired_jobs_keep_old_delivery_cleanup(self):
        for connector, action, status, expired in [('meeting', 'generate', 'completed', False), ('sales-orders', 'submit', 'completed', False), ('sales-orders', 'recognize', 'failed', False), ('sales-orders', 'recognize', 'completed', True)]:
            with self.subTest(connector=connector, action=action, status=status, expired=expired):
                jid, source, output = self.make_delivery(connector, action, status, expired)
                self.download(output); self.assertIsNone(self.snapshot(output, jid)['encrypted_result'])
                with self.assertRaisesRegex(storage.StorageError, 'expired'): self.store.begin_download(self.owner, self.sid, source)

    def test_receipt_identity_integrity_expiry_and_explicit_cancellation_still_apply(self):
        jid, source, output = self.make_delivery()
        before = self.snapshot(output, jid)
        for owner, sid in [(uuid4().hex, self.sid), (self.owner, 'session-' + str(uuid4()))]:
            with self.assertRaisesRegex(storage.StorageError, 'not_found'): self.store.begin_download(owner, sid, output)
        lease = self.store.begin_download(self.owner, self.sid, output)
        with self.assertRaisesRegex(storage.StorageError, 'mismatch'):
            self.store.finish_download(self.owner, self.sid, lease['lease_id'], bytes_received=6, sha256='0' * 64)
        self.assertEqual(self.snapshot(output, jid)['encrypted_result'], before['encrypted_result'])
        self.store.complete_task(self.owner, self.sid, jid, cancelled=True)
        self.assertIsNone(self.snapshot(output, jid)['encrypted_result'])
        for fid in (source, output):
            with self.assertRaisesRegex(storage.StorageError, 'expired'): self.store.begin_download(self.owner, self.sid, fid)

if __name__ == '__main__': unittest.main(verbosity=2)
