"""Synthetic documents only; real subprocesses, no company services or secrets."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('megmeet_office_service', HERE / 'app.py')
office = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = office
spec.loader.exec_module(office)
SECRET = b'synthetic-office-key-never-company-123456'
HEADERS = {'X-Office-Service-Key': SECRET.decode(), 'X-Office-Owner': '1' * 64, 'X-Office-Scope': '2' * 64}


def body(operation='create', output='result.txt', options=None, input=None, inputs=None):
    request = {'operation': operation, 'options': options if options is not None else {'text': '办公内容'}}
    if output is not None:
        request['output'] = output
    if input is not None:
        request['input'] = input
    return {'protocol': office.PROTOCOL, 'request': request, 'inputs': inputs or []}


class OfficeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='megmeet-office-service-test-')
        self.root = Path(self.temp.name)
        self.config = office.Config(root=self.root / 'jobs', engine=HERE.parents[1] / 'office-engine/engine.py',
                                   python=Path(sys.executable), timeout_seconds=3, ttl_seconds=30)

    async def asyncTearDown(self):
        self.temp.cleanup()

    @asynccontextmanager
    async def client(self, config=None, runner=office.run_engine):
        app = office.create_app(config or self.config, secret=SECRET, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://office', headers=HEADERS) as client:
                yield client, app.state.office

    async def create(self, client, payload=None, headers=None):
        response = await client.post('/v1/office/jobs', json=payload or body(), headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['job_id']

    async def wait(self, client, job):
        for _ in range(200):
            response = await client.get('/v1/office/jobs/' + job)
            value = response.json()
            if value['state'] in office.TERMINAL:
                return value
            await asyncio.sleep(.02)
        self.fail('office task did not finish')

    async def test_real_create_download_read_and_capabilities(self):
        async with self.client() as (client, service):
            caps = await client.get('/v1/office/capabilities')
            self.assertEqual(caps.status_code, 200, caps.text)
            self.assertEqual(caps.json()['protocol'], office.ENGINE_PROTOCOL)
            self.assertEqual(set(caps.json()['operations']), office.OPERATIONS)
            job = await self.create(client)
            self.assertEqual((await client.post(f'/v1/office/jobs/{job}/run')).status_code, 202)
            state = await self.wait(client, job)
            self.assertEqual(state['state'], 'succeeded', state)
            output = state['outputs'][0]
            data = await client.get(f'/v1/office/jobs/{job}/outputs/0')
            self.assertEqual(data.content, '办公内容'.encode())
            self.assertEqual(output['sha256'], hashlib.sha256(data.content).hexdigest())
            self.assertEqual(int(data.headers['content-length']), len(data.content))
            original = data.content
            read = await self.create(client, body('read', output=None, input='selected.txt', options={},
                inputs=[{'path': 'selected.txt', 'size_bytes': len(original)}]))
            await client.put(f'/v1/office/jobs/{read}/inputs/0', content=original)
            await client.post(f'/v1/office/jobs/{read}/run')
            status = await self.wait(client, read)
            self.assertEqual(status['state'], 'succeeded', status)
            self.assertEqual(status['result']['text'], original.decode())
            self.assertEqual(status['outputs'], [])
            self.assertFalse((service.jobs[read].root / 'selected.txt').exists())
            self.assertEqual((await client.delete(f'/v1/office/jobs/{job}')).json()['state'], 'cancelled')
            self.assertFalse(service.jobs[job].root.exists())
            self.assertEqual((await client.get(f'/v1/office/jobs/{job}/outputs/0')).status_code, 404)
            self.assertEqual((await client.delete(f'/v1/office/jobs/{job}')).json()['state'], 'cancelled')

    async def test_auth_owner_scope_and_undeclared_paths(self):
        async with self.client() as (client, _):
            self.assertEqual((await client.get('/v1/office/capabilities', headers={'X-Office-Service-Key': 'wrong'})).status_code, 401)
            job = await self.create(client)
            for field in ('X-Office-Owner', 'X-Office-Scope'):
                headers = {field: '3' * 64}
                for method, suffix in [('GET', ''), ('POST', '/run'), ('PUT', '/inputs/0'), ('DELETE', ''), ('GET', '/outputs/0')]:
                    response = await client.request(method, f'/v1/office/jobs/{job}' + suffix, headers=headers)
                    self.assertEqual(response.status_code, 404)
            for value in ('../escape.txt', '/tmp/escape.txt', 'a\\b.txt', '.office-tmp/exploit.txt', 'NUL.txt'):
                response = await client.post('/v1/office/jobs', json=body(output=value))
                self.assertEqual(response.status_code, 400)
            value = body()
            value['request']['workspace'] = '/tmp'
            self.assertEqual((await client.post('/v1/office/jobs', json=value)).status_code, 400)
            value = body('pdf', output='new.pdf', options={'action': 'merge', 'inputs': ['not-uploaded.pdf']})
            self.assertEqual((await client.post('/v1/office/jobs', json=value)).status_code, 400)

    async def test_upload_bounds_incomplete_and_retry(self):
        async with self.client() as (client, service):
            job = await self.create(client, body('inspect', output=None, input='a.txt', options={},
                inputs=[{'path': 'a.txt', 'size_bytes': 3}]))
            self.assertEqual((await client.post(f'/v1/office/jobs/{job}/run')).status_code, 409)
            self.assertEqual((await client.put(f'/v1/office/jobs/{job}/inputs/0', content=b'abcd')).status_code, 413)
            self.assertEqual((await client.put(f'/v1/office/jobs/{job}/inputs/0', content=b'ab')).status_code, 400)
            self.assertFalse(list((service.jobs[job].root / '.office-tmp').iterdir()))
            self.assertEqual((await client.put(f'/v1/office/jobs/{job}/inputs/0', content=b'abc')).status_code, 200)
            self.assertEqual((await client.put(f'/v1/office/jobs/{job}/inputs/0', content=b'abc')).status_code, 409)
            await client.post(f'/v1/office/jobs/{job}/run')
            self.assertEqual((await self.wait(client, job))['state'], 'succeeded')

    async def test_real_invalid_archive_is_refused_without_parser_dependencies(self):
        buffer = io.BytesIO()
        with ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE root [<!ENTITY x SYSTEM "file:///etc/passwd">]><root>&x;</root>')
        content = buffer.getvalue()
        async with self.client() as (client, service):
            job = await self.create(client, body('read', output=None, input='unsafe.docx', options={},
                inputs=[{'path': 'unsafe.docx', 'size_bytes': len(content)}]))
            await client.put(f'/v1/office/jobs/{job}/inputs/0', content=content)
            await client.post(f'/v1/office/jobs/{job}/run')
            result = await self.wait(client, job)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['error'], {'code': 'UNSAFE_XML'})
            self.assertFalse(service.jobs[job].root.exists())
            self.assertNotIn('/etc/passwd', json.dumps(result))

    async def test_output_symlink_or_hash_mismatch_is_never_published(self):
        outside = self.root / 'outside.txt'
        outside.write_text('must stay')

        async def runner(config, request, cancel, temporary):
            target = Path(request['workspace']) / request['output']
            if request['options']['text'] == 'link':
                target.symlink_to(outside)
            else:
                target.write_text('bad hash')
            return {'output': request['output'], 'bytes': 8, 'sha256': '0' * 64}

        async with self.client(runner=runner) as (client, service):
            for text in ('link', 'hash'):
                job = await self.create(client, body(options={'text': text}))
                await client.post(f'/v1/office/jobs/{job}/run')
                result = await self.wait(client, job)
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['outputs'], [])
                self.assertFalse(service.jobs[job].root.exists())
            self.assertEqual(outside.read_text(), 'must stay')

    async def test_queue_fairness_per_owner_concurrency_and_cancellation(self):
        started = []
        gates = {name: asyncio.Event() for name in ('a1', 'a2', 'b1')}

        async def runner(config, request, cancel, temporary):
            name = request['options']['text']
            started.append(name)
            await gates[name].wait()
            raise office.EngineFailure('SYNTHETIC_COMPLETED')

        async with self.client(runner=runner) as (client, service):
            a1 = await self.create(client, body(options={'text': 'a1'}))
            a2 = await self.create(client, body(options={'text': 'a2'}))
            b1 = await self.create(client, body(options={'text': 'b1'}), headers={'X-Office-Owner': '3' * 64})
            await client.post(f'/v1/office/jobs/{a1}/run')
            await client.post(f'/v1/office/jobs/{a2}/run')
            await client.post(f'/v1/office/jobs/{b1}/run', headers={'X-Office-Owner': '3' * 64})
            await asyncio.sleep(.02)
            self.assertEqual(started, ['a1', 'b1'])
            self.assertEqual(len(service.running), 2)
            self.assertEqual((await client.get(f'/v1/office/jobs/{a2}')).json()['state'], 'queued')
            await client.delete(f'/v1/office/jobs/{a2}')
            self.assertNotIn(a2, service.queue)
            gates['a1'].set()
            gates['b1'].set()
            await asyncio.gather(service.jobs[a1].done.wait(), service.jobs[b1].done.wait())
            self.assertNotIn('a2', started)

    def process_alive(self, pid):
        try:
            return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
        except FileNotFoundError:
            return False

    async def test_real_process_group_cancel_and_timeout_kill_grandchild(self):
        engine = self.root / 'slow-engine.py'
        engine.write_text('''import json,sys,subprocess,time
from pathlib import Path
request=json.load(sys.stdin)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path(request['workspace'],'child.pid').write_text(str(child.pid))
time.sleep(60)
''')
        for action in ('cancel', 'timeout'):
            config = replace(self.config, root=self.root / ('jobs-' + action), engine=engine, timeout_seconds=1)
            async with self.client(config=config) as (client, service):
                job = await self.create(client)
                await client.post(f'/v1/office/jobs/{job}/run')
                pid_file = service.jobs[job].root / 'child.pid'
                for _ in range(60):
                    if pid_file.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(pid_file.exists())
                pid = int(pid_file.read_text())
                self.assertTrue(self.process_alive(pid))
                if action == 'cancel':
                    value = (await client.delete(f'/v1/office/jobs/{job}')).json()
                    self.assertEqual(value['state'], 'cancelled')
                else:
                    value = await self.wait(client, job)
                    self.assertEqual(value['error'], {'code': 'OFFICE_TIMEOUT'})
                for _ in range(30):
                    if not self.process_alive(pid):
                        break
                    await asyncio.sleep(.01)
                self.assertFalse(self.process_alive(pid))
                self.assertFalse(service.jobs[job].root.exists())

    async def test_capacity_ttl_and_exclusive_spool_lock(self):
        config = replace(self.config, spool_bytes=office.OUTPUT_LIMIT, max_jobs=2)
        async with self.client(config=config) as (client, service):
            first = await self.create(client)
            self.assertEqual((await client.post('/v1/office/jobs', json=body())).status_code, 429)
            await client.delete(f'/v1/office/jobs/{first}')
            second = await self.create(client)
            await client.post(f'/v1/office/jobs/{second}/run')
            self.assertEqual((await self.wait(client, second))['state'], 'succeeded')
            await service.reap(time.monotonic() + 31)
            self.assertEqual((await client.get(f'/v1/office/jobs/{second}')).status_code, 404)
            third = await self.create(client)
            other = office.OfficeService(config, SECRET)
            with self.assertRaisesRegex(ValueError, 'SINGLE_WORKER'):
                await other.start()
            self.assertTrue(service.jobs[third].root.exists())

    async def test_request_byte_and_declared_size_limits(self):
        async with self.client() as (client, _):
            oversized = body('read', output=None, input='a.txt', options={},
                inputs=[{'path': 'a.txt', 'size_bytes': office.INPUT_LIMIT + 1}])
            self.assertEqual((await client.post('/v1/office/jobs', json=oversized)).status_code, 400)
            self.assertEqual((await client.post('/v1/office/jobs', content=b'x' * (1024**2 + 1))).status_code, 413)
            for number in ('NaN', 'Infinity', '-Infinity'):
                payload = ('{"protocol":"megmeet-office-remote-v1","request":{"operation":"create",'
                           '"output":"a.txt","options":{"text":' + number + '}},"inputs":[]}')
                response = await client.post('/v1/office/jobs', content=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json(), {'detail': 'OFFICE_OPTIONS_INVALID'})

    async def test_real_child_does_not_inherit_secret_proxy_or_model_environment(self):
        script = self.root / 'environment-engine.py'
        script.write_text('''import json,os,sys
request=json.load(sys.stdin)
blocked=['IAM_CLIENT_SECRET','TOKENKEY_API_KEY','HTTP_PROXY','HTTPS_PROXY','OFFICE_SERVICE_KEY','PYTHONPATH']
print(json.dumps({'protocol':'megmeet-office-v1','ok':True,'result':{'leaked':any(k in os.environ for k in blocked)}}))
''')
        config = replace(self.config, engine=script)
        keys = ['IAM_CLIENT_SECRET', 'TOKENKEY_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'OFFICE_SERVICE_KEY', 'PYTHONPATH']
        with patch.dict(os.environ, {key: 'synthetic-secret-canary' for key in keys}):
            async with self.client(config=config) as (client, _):
                job = await self.create(client, body('inspect', output=None, input='input.txt', options={},
                    inputs=[{'path': 'input.txt', 'size_bytes': 0}]))
                await client.put(f'/v1/office/jobs/{job}/inputs/0', content=b'')
                await client.post(f'/v1/office/jobs/{job}/run')
                result = await self.wait(client, job)
                self.assertEqual(result['state'], 'succeeded', result)
                self.assertEqual(result['result'], {'leaked': False})

    async def test_queue_limit_and_owner_pending_reject_without_extra_execution(self):
        gate = asyncio.Event()

        async def runner(config, request, cancel, temporary):
            await gate.wait()
            raise office.EngineFailure('SYNTHETIC_COMPLETED')

        config = replace(self.config, concurrency=1, queue_limit=1, owner_pending=2)
        async with self.client(config=config, runner=runner) as (client, service):
            first = await self.create(client)
            second = await self.create(client)
            self.assertEqual((await client.post('/v1/office/jobs', json=body())).status_code, 429)
            await client.post(f'/v1/office/jobs/{first}/run')
            await client.post(f'/v1/office/jobs/{second}/run')
            other_headers = {'X-Office-Owner': '3' * 64}
            third = await self.create(client, headers=other_headers)
            self.assertEqual((await client.post(f'/v1/office/jobs/{third}/run', headers=other_headers)).status_code, 429)
            self.assertEqual(service.jobs[third].state, 'uploading')
            await client.delete(f'/v1/office/jobs/{second}')
            gate.set()
            await service.jobs[first].done.wait()

    async def test_spool_refuses_unmarked_foreign_uuid_directory(self):
        self.config.root.mkdir()
        foreign = self.config.root / ('f' * 32)
        foreign.mkdir()
        sentinel = foreign / 'keep.txt'
        sentinel.write_text('unrelated directory must survive')
        service = office.OfficeService(self.config, SECRET)
        with self.assertRaisesRegex(ValueError, 'SPOOL_NOT_PRIVATE'):
            await service.start()
        self.assertEqual(sentinel.read_text(), 'unrelated directory must survive')

    async def test_restart_removes_only_owned_orphan_jobs(self):
        original = office.OfficeService(self.config, SECRET)
        await original.start()
        job = await original.create(('1' * 64, '2' * 64), body())
        original.reaper.cancel()
        await asyncio.gather(original.reaper, return_exceptions=True)
        original.spool_lock.close()
        restored = office.OfficeService(self.config, SECRET)
        await restored.start()
        try:
            self.assertFalse(job.root.exists())
            self.assertEqual(restored.jobs, {})
            self.assertTrue((self.config.root / '.office-spool-v1').is_file())
        finally:
            await restored.close()

    async def test_secret_file_bounds_links_and_parent_links(self):
        key = self.root / 'service-key'
        key.write_bytes(SECRET + b'\n')
        config = replace(self.config, secret_file=key)
        app = office.create_app(config)
        async with app.router.lifespan_context(app):
            self.assertEqual(app.state.office.secret, SECRET)
        for content in (b'a' * 31, b'a' * 258):
            key.write_bytes(content)
            app = office.create_app(config)
            with self.assertRaisesRegex(ValueError, 'SERVICE_KEY_INVALID'):
                async with app.router.lifespan_context(app):
                    pass
        key.write_bytes(SECRET)
        link = self.root / 'key-link'
        link.symlink_to(key)
        app = office.create_app(replace(config, secret_file=link))
        with self.assertRaisesRegex(ValueError, 'SERVICE_KEY_INVALID'):
            async with app.router.lifespan_context(app):
                pass
        hardlink = self.root / 'key-hardlink'
        os.link(key, hardlink)
        app = office.create_app(config)
        with self.assertRaisesRegex(ValueError, 'SERVICE_KEY_INVALID'):
            async with app.router.lifespan_context(app):
                pass
        hardlink.unlink()
        parent = self.root / 'directory-link'
        parent.symlink_to(self.root, target_is_directory=True)
        app = office.create_app(replace(config, secret_file=parent / 'service-key'))
        with self.assertRaisesRegex(ValueError, 'SERVICE_KEY_INVALID'):
            async with app.router.lifespan_context(app):
                pass


if __name__ == '__main__':
    unittest.main()
