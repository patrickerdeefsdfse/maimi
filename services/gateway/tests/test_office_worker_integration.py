"""Actual shared worker/engine behind Gateway; synthetic identities/documents."""
import asyncio
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

from app import office_remote as gateway
from app.iam_oauth import IAMFailure
from app.desktop_auth import TrustedActor


def test_real_worker_gateway_identity_scope_file_roundtrip(tmp_path, monkeypatch):
    async def run():
        system = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location('office_worker_integration', system / 'services/office-service/app.py')
        worker = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = worker
        spec.loader.exec_module(worker)
        secret = b'synthetic-office-worker-integration-secret'
        key = tmp_path / 'key'
        key.write_bytes(secret)
        monkeypatch.setenv('OFFICE_SERVICE_SECRET_FILE', str(key))
        monkeypatch.setenv('OFFICE_SERVICE_URL', 'http://office-service:8093')
        config = worker.Config(root=tmp_path / 'spool', engine=system / 'office-engine/engine.py',
            python=Path(sys.executable), timeout_seconds=5)
        service = worker.create_app(config, secret=secret)
        actor = TrustedActor('TEST-OWNER-A', 'test-a', 'Test', (), 'a' * 43)
        state = {'actor': actor}

        async def identify(request):
            return state['actor']

        monkeypatch.setattr(gateway.cloud, 'actor_for', identify)
        monkeypatch.setattr(gateway, '_transport', httpx.ASGITransport(app=service))
        app = FastAPI()
        app.include_router(gateway.router)

        @app.exception_handler(IAMFailure)
        async def failed(request, exc):
            return JSONResponse({'detail': exc.code}, status_code=exc.status)

        async with service.router.lifespan_context(service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://office.test') as client:
                caps = await client.get('/v1/office/capabilities')
                assert caps.status_code == 200, caps.text
                assert 'ocr' in caps.json()['operations']
                created = await client.post('/v1/office/jobs', json={'protocol':gateway.PROTOCOL,
                    'request':{'operation':'create','output':'report.txt','options':{'text':'共享服务集成测试'}}, 'inputs':[]})
                assert created.status_code == 201, created.text
                job = created.json()['job_id']
                url = '/v1/office/jobs/' + job
                assert (await client.post(url + '/run')).status_code == 202
                for _ in range(100):
                    status = await client.get(url)
                    assert status.status_code == 200, status.text
                    if status.json()['state'] in worker.TERMINAL:
                        break
                    await asyncio.sleep(.03)
                assert status.json()['state'] == 'succeeded', status.text
                response = await client.get(url + '/outputs/0')
                assert response.content == '共享服务集成测试'.encode()
                assert status.json()['outputs'][0]['sha256'] == hashlib.sha256(response.content).hexdigest()
                data = response.content
                read = await client.post('/v1/office/jobs', json={'protocol':gateway.PROTOCOL,
                    'request':{'operation':'read','input':'selected.txt','options':{}},
                    'inputs':[{'path':'selected.txt','size_bytes':len(data)}]})
                read_url = '/v1/office/jobs/' + read.json()['job_id']
                assert (await client.put(read_url + '/inputs/0', content=data)).status_code == 200
                assert (await client.post(read_url + '/run')).status_code == 202
                for _ in range(100):
                    read_status = (await client.get(read_url)).json()
                    if read_status['state'] in worker.TERMINAL:
                        break
                    await asyncio.sleep(.03)
                assert read_status['state'] == 'succeeded', read_status
                assert read_status['result']['text'] == data.decode()
                assert (await client.delete(read_url)).status_code == 200
                state['actor'] = replace(actor, auth_session_id='b'*43)
                assert (await client.get(url)).status_code == 404
                assert (await client.get(url + '/outputs/0')).status_code == 404
                state['actor'] = replace(actor, user_no='TEST-OWNER-B')
                assert (await client.delete(url)).status_code == 404
                state['actor'] = actor
                assert (await client.delete(url)).json()['state'] == 'cancelled'
                assert not service.state.office.jobs[job].root.exists()
                assert (await client.get(url + '/outputs/0')).status_code == 404
    asyncio.run(run())
