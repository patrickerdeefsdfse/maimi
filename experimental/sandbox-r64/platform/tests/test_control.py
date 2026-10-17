import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maimi_sandbox import Broker, Scope, Profile, Budget, Failure
from maimi_sandbox.capacity import Decision


class SyntheticAdmission:
    """Explicit synthetic trusted port, never a deployment proof."""
    def __init__(self):
        self.active = False
        self.allowed = True
        self.calls = []

    @contextmanager
    def locked(self):
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def check(self, *, reserved, requested):
        assert self.active
        self.calls.append((reserved, requested))
        return Decision(self.allowed, 'SYNTHETIC_TEST')


class FakeProvider:
    """Failure-injection port only; these tests do not certify a real container runtime."""
    def __init__(self):
        self.created = []
        self.destroyed = []
        self.writes = []
        self.executions = []
        self.fail_create = False
        self.fail_destroy = False
        self.wait_execute = None
        self.wait_create = None

    async def create(self, task_id, profile, metadata):
        self.created.append(task_id)
        if self.wait_create:
            await self.wait_create.wait()
        if self.fail_create:
            raise TimeoutError('private provider error must not escape')
        return 'instance-' + task_id

    async def write_input(self, sandbox_id, path, data):
        self.writes.append((sandbox_id, path, data))

    async def execute(self, sandbox_id, command, timeout, max_output):
        self.executions.append(command)
        if self.wait_execute:
            await self.wait_execute.wait()
        return {'exit_code': 0, 'stdout': 'ok', 'stderr': ''}

    async def read_output(self, sandbox_id, path, max_bytes):
        return b'output'

    async def destroy_task(self, task_id, sandbox_id):
        self.destroyed.append(task_id)
        if self.fail_destroy:
            raise TimeoutError('private provider error must not escape')


class Files:
    def __init__(self):
        self.reads, self.outputs = [], []

    async def read_input(self, scope, reference, maximum):
        self.reads.append((scope, reference))
        if reference != 'allowed_file':
            raise Failure('FILE_NOT_AUTHORIZED')
        return b'input'

    async def publish_output(self, scope, name, data, key):
        self.outputs.append((scope, name, data, key))
        return {'artifact_id': 'opaque-result'}


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'tasks.db')
        self.provider, self.files = FakeProvider(), Files()
        self.admission = SyntheticAdmission()
        self.allowed = True
        self.now = 1000.0
        self.a = Scope('employee_a', 'workspace_a', 'session_a')
        self.b = Scope('employee_b', 'workspace_b', 'session_b')
        self.broker = self.make()
        self.broker.recover()

    async def authorize(self, scope):
        if not self.allowed:
            raise Failure('ACCESS_REVOKED')

    def make(self, **kwargs):
        return Broker(self.path, authorize=self.authorize, files=self.files, provider=self.provider,
            profiles={'files': Profile(1000, 2048)}, budget=Budget(2000, 4096, 2),
            enabled=kwargs.get('enabled', True), clock=lambda: self.now,
            admission=self.admission)

    async def asyncTearDown(self):
        self.broker.close_store()
        self.tmp.cleanup()

    async def task(self, scope=None, key='request_0001', inputs=None):
        return (await self.broker.submit(scope or self.a, profile='files', inputs=inputs or [], request_key=key))['task_id']

    async def ready(self, scope=None, **kwargs):
        task = await self.task(scope, **kwargs)
        await self.broker.dispatch_one()
        return task

    async def test_cross_owner_and_cross_session_are_denied(self):
        task = await self.ready()
        for scope in (self.b, Scope(self.a.owner, self.a.workspace, 'different_session')):
            with self.assertRaisesRegex(Failure, 'TASK_NOT_FOUND'):
                await self.broker.close(scope, task)
        self.assertEqual(self.provider.destroyed, [])

    async def test_host_admission_missing_denies_without_provider_call(self):
        task = await self.task()
        self.broker.admission = None
        with self.assertRaisesRegex(Failure, 'HOST_ADMISSION_NOT_CONFIGURED'):
            await self.broker.dispatch_one()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'QUEUED')
        self.assertEqual(self.provider.created, [])

    async def test_host_denial_preserves_queue_and_budget(self):
        task = await self.task()
        self.admission.allowed = False
        with self.assertRaisesRegex(Failure, 'HOST_CAPACITY_NOT_ADMITTED'):
            await self.broker.dispatch_one()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'QUEUED')
        self.assertEqual(self.provider.created, [])
        self.assertEqual(self.broker.db.execute('SELECT count(*) FROM fairness').fetchone()[0], 0)

    async def test_host_reservation_is_committed_before_provider_creation(self):
        task = await self.task()
        original = self.provider.create
        async def checked_create(*args):
            self.assertFalse(self.admission.active)
            self.assertFalse(self.broker.db.in_transaction)
            self.assertEqual(self.broker.db.execute('SELECT state FROM tasks WHERE id=?', (task,)).fetchone()[0], 'PROVISIONING')
            return await original(*args)
        self.provider.create = checked_create
        await self.broker.dispatch_one()

    async def test_unknown_resources_are_passed_to_host_admission(self):
        self.provider.fail_create = True
        await self.task()
        with self.assertRaises(Failure):
            await self.broker.dispatch_one()
        self.provider.fail_create = False
        await self.task(self.b)
        await self.broker.dispatch_one()
        reserved, requested = self.admission.calls[-1]
        self.assertEqual(len(reserved), 1)
        self.assertEqual((reserved[0].memory_mib, reserved[0].cpu_millis), (2048, 1000))

    async def test_host_probe_failure_is_redacted_and_does_not_create(self):
        task = await self.task()
        def broken(**kwargs):
            raise RuntimeError('private host detail')
        self.admission.check = broken
        with self.assertRaisesRegex(Failure, '^HOST_ADMISSION_UNAVAILABLE$'):
            await self.broker.dispatch_one()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'QUEUED')
        self.assertEqual(self.provider.created, [])

    async def test_admission_lock_acquisition_failure_is_redacted(self):
        task = await self.task()
        @contextmanager
        def broken_lock():
            raise OSError('private lock path')
            yield
        self.admission.locked = broken_lock
        with self.assertRaisesRegex(Failure, '^HOST_ADMISSION_UNAVAILABLE$'):
            await self.broker.dispatch_one()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'QUEUED')
        self.assertEqual(self.provider.created, [])

    async def test_admission_unlock_failure_keeps_committed_reservation(self):
        task = await self.task()
        @contextmanager
        def broken_unlock():
            self.admission.active = True
            yield
            self.admission.active = False
            raise OSError('private lock path')
        self.admission.locked = broken_unlock
        with self.assertRaisesRegex(Failure, '^HOST_ADMISSION_UNAVAILABLE$'):
            await self.broker.dispatch_one()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'PROVISIONING')
        self.assertFalse(self.broker.db.in_transaction)
        self.assertEqual(self.provider.created, [])

    async def test_submission_idempotency_and_conflict(self):
        task = await self.task()
        self.assertEqual(task, await self.task())
        with self.assertRaisesRegex(Failure, 'REQUEST_KEY_CONFLICT'):
            await self.task(inputs=['allowed_file'])

    async def test_per_owner_admission_preserves_other_employee_access(self):
        first = await self.ready()
        waiting = await self.task(key='request_0002')
        other = await self.task(self.b)
        self.assertEqual((await self.broker.dispatch_one())['task_id'], other)
        self.assertIsNone(await self.broker.dispatch_one())
        self.assertEqual((await self.broker.status(self.a, waiting))['state'], 'QUEUED')

    async def test_unknown_create_and_destroy_retain_budget(self):
        self.provider.fail_create = True
        task = await self.task()
        with self.assertRaisesRegex(Failure, 'PROVISIONING_UNCERTAIN'):
            await self.broker.dispatch_one()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'UNCERTAIN')
        self.provider.fail_destroy = True
        with self.assertRaisesRegex(Failure, 'STOP_UNCONFIRMED'):
            await self.broker.close(self.a, task)
        await self.task(key='request_0002')
        self.assertIsNone(await self.broker.dispatch_one())
        self.provider.fail_destroy = False
        self.assertEqual((await self.broker.close(self.a, task))['state'], 'CLOSED')

    async def test_no_automatic_command_replay(self):
        task = await self.ready()
        await self.broker.execute(self.a, task, command='test command', request_key='command_0001')
        with self.assertRaisesRegex(Failure, 'COMMAND_ALREADY_ATTEMPTED'):
            await self.broker.execute(self.a, task, command='test command', request_key='command_0001')
        self.assertEqual(len(self.provider.executions), 1)

    async def test_cancellation_during_execution_never_returns_late_success(self):
        task = await self.ready()
        self.provider.wait_execute = asyncio.Event()
        running = asyncio.create_task(self.broker.execute(self.a, task, command='test', request_key='command_0001'))
        await asyncio.sleep(0)
        await self.broker.close(self.a, task)
        self.provider.wait_execute.set()
        with self.assertRaisesRegex(Failure, 'COMMAND_OUTCOME_UNKNOWN'):
            await running
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'CLOSED')

    async def test_cancel_provisioning_waits_for_destroy(self):
        task = await self.task(inputs=['allowed_file'])
        self.provider.wait_create = asyncio.Event()
        dispatch = asyncio.create_task(self.broker.dispatch_one())
        await asyncio.sleep(0)
        self.assertEqual((await self.broker.close(self.a, task))['state'], 'STOPPING')
        self.provider.wait_create.set()
        self.assertEqual((await dispatch)['state'], 'CLOSED')
        self.assertEqual(self.files.reads, [])

    async def test_authorized_inputs_and_artifact_export(self):
        task = await self.ready(inputs=['allowed_file'])
        self.assertEqual(self.provider.writes[0][1], '/workspace/inputs/input-000')
        result = await self.broker.export(self.a, task, path='report.pptx')
        self.assertEqual(result['artifact_id'], 'opaque-result')
        self.assertEqual(self.files.outputs[0][0], self.a)
        for path in ('../secret', '/etc/passwd', 'foo/../bar', 'foo\\bar', 'foo//bar'):
            with self.assertRaises(Failure):
                await self.broker.export(self.a, task, path=path)

    async def test_revocation_blocks_execute_export_and_status(self):
        task = await self.ready()
        self.allowed = False
        with self.assertRaisesRegex(Failure, 'ACCESS_REVOKED'):
            await self.broker.execute(self.a, task, command='test', request_key='command_0001')
        with self.assertRaisesRegex(Failure, 'ACCESS_REVOKED'):
            await self.broker.export(self.a, task, path='report.txt')
        self.assertEqual(self.provider.executions, [])

    async def test_snapshot_failure_never_reopens_task_for_execution(self):
        task = await self.ready()
        async def broken_snapshot(*args):
            raise TimeoutError('resume acknowledgement lost')
        self.provider.read_output = broken_snapshot
        with self.assertRaisesRegex(Failure, 'EXPORT_OUTCOME_UNKNOWN'):
            await self.broker.export(self.a, task, path='report.txt')
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'UNCERTAIN')
        with self.assertRaisesRegex(Failure, 'TASK_NOT_READY'):
            await self.broker.execute(self.a, task, command='test', request_key='command_0001')
        self.assertEqual(self.files.outputs, [])

    async def test_late_export_after_close_is_not_delivered_as_success(self):
        task = await self.ready()
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_publish(*args):
            started.set()
            await release.wait()
            return {'artifact_id': 'opaque-result'}
        self.files.publish_output = slow_publish
        work = asyncio.create_task(self.broker.export(self.a, task, path='report.txt'))
        await started.wait()
        await self.broker.close(self.a, task)
        release.set()
        with self.assertRaisesRegex(Failure, 'EXPORT_OUTCOME_UNKNOWN'):
            await work
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'CLOSED')

    async def test_restart_marks_inflight_unknown_without_replaying(self):
        task = await self.ready()
        self.broker.db.execute("UPDATE tasks SET state='BUSY' WHERE id=?", (task,))
        self.broker.close_store()
        self.broker = self.make()
        self.broker.recover()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'UNCERTAIN')
        self.assertEqual(self.provider.executions, [])

    async def test_idle_reaper_saves_reservation_on_uncertain_stop(self):
        task = await self.ready()
        self.now += 601
        self.provider.fail_destroy = True
        with self.assertRaisesRegex(Failure, 'STOP_UNCONFIRMED'):
            await self.broker.reap()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'UNCERTAIN')

    async def test_second_controller_cannot_admit_against_same_budget(self):
        with self.assertRaisesRegex(Failure, 'CONTROLLER_LOCK_UNAVAILABLE'):
            self.make()
        task = await self.ready()
        self.broker.close_store()
        self.broker = self.make()
        self.broker.recover()
        self.assertEqual((await self.broker.status(self.a, task))['state'], 'UNCERTAIN')
        with self.assertRaisesRegex(Failure, 'TASK_NOT_READY'):
            await self.broker.execute(self.a, task, command='test', request_key='command_0001')

    async def test_disabled_by_default_and_unhashable_input_rejected(self):
        self.broker.enabled = False
        with self.assertRaisesRegex(Failure, 'SANDBOX_DISABLED'):
            await self.task()
        self.broker.enabled = True
        with self.assertRaisesRegex(Failure, 'INVALID_INPUT_REFERENCE'):
            await self.broker.submit(self.a, profile='files', inputs=[{}], request_key='request_0001')


if __name__ == '__main__':
    unittest.main()
