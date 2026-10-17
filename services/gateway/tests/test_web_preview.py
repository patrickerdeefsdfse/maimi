"""Synthetic native RPC -> authenticated shared Office preview, without WASM."""
import asyncio
import base64
import hashlib
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import personal_cloud_preview as preview
from app import personal_cloud_web as web
from app.desktop_auth import TrustedActor

SESSION = 'session-12345678-1234-1234-1234-123456789abc'
PATH = '/workspace/合成预览.docx'
SOURCE = b'PK\x03\x04' + bytes(range(256)) * 600
PDF = b'%PDF-1.7\nsynthetic-conversion\n%%EOF'
JOB = 'a' * 32


def envelope(**args):
    return {'type': 'client-request', 'rpcId': 'test-preview', 'method': 'officeToPdf/render',
            'payload': {'args': {'workspaceFileScopeId': SESSION, 'path': PATH, 'priority': 'foreground', **args}}}


@pytest.fixture
def client(monkeypatch):
    actor = TrustedActor('SYNTHETIC-A', 'SYNTHETIC-A', 'A', (), 'SYNTHETIC-LOGIN-A')
    state = {'calls': [], 'auth': 0, 'polls': 0, 'uploaded': b'', 'deleted': False, 'touches': 0, 'activities': []}

    async def auth(request):
        state['auth'] += 1
        if state.get('expire_at', 999999) <= state['auth']:
            raise preview.iam.IAMFailure(401, 'authentication_required')
        return actor

    async def touch(request, bound, **kwargs):
        assert bound is actor and type(kwargs.get('activity')) is bool
        state['touches'] += 1
        state['activities'].append(kwargs['activity'])
        return {'origin': 'http://worker:3084', 'route_token': 'synthetic-route'}

    async def lease(request, bound):
        return 'mcl_synthetic', 9999999999

    def job(status='running'):
        outputs = [] if status != 'succeeded' else [{'id': '0', 'path': 'output.pdf',
            'size_bytes': len(PDF), 'sha256': hashlib.sha256(PDF).hexdigest()}]
        return {'protocol': preview.office.PROTOCOL, 'job_id': JOB, 'state': status, 'outputs': outputs}

    async def transport(request):
        state['calls'].append((request.method, request.url.path))
        if request.url.host == 'worker':
            assert request.headers['authorization'] == 'Bearer synthetic-route'
            body = json.loads(request.content)
            method, args = body['method'], body['payload']['args']
            # No native conversion request is ever made.
            assert method in ('workspaceFiles/stat', 'workspaceFiles/readBytes', 'officeToPdf/generation', 'session/list')
            result = {'absolutePath': PATH, 'bytes': len(SOURCE), 'version': 'source-v1'}
            if method == 'session/list':
                result = {'items': [{'sessionId': SESSION, 'cwd': '/workspace'}]}
            elif method == 'officeToPdf/generation':
                result = 'native-generation-one'
            else:
                assert args['workspaceFileScopeId'] == SESSION and args['path'] == PATH
                if method == 'workspaceFiles/readBytes':
                    offset, length = args['range']['offset'], args['range']['length']
                    result.update(offset=offset, data=base64.b64encode(SOURCE[offset:offset + length]).decode(),
                                  eof=offset + length == len(SOURCE))
                    if state.get('changed'):
                        result['version'] = 'source-v2'
            return httpx.Response(200, json={'type': 'server-response', 'rpcId': body['rpcId'],
                                           'result': {'ok': True, 'value': result}})
        assert request.url.host == 'office-service'
        assert request.headers['x-office-owner'] == preview.office.worker_headers(actor, 'test-secret')['X-Office-Owner']
        assert request.headers['x-office-scope'] == preview.office.worker_headers(actor, 'test-secret')['X-Office-Scope']
        path = request.url.path
        if request.method == 'DELETE':
            state['deleted'] = True
            return httpx.Response(200, json=job('cancelled'))
        if path.endswith('/jobs'):
            value = json.loads(request.content)
            assert value['inputs'] == [{'path': 'input.docx', 'size_bytes': len(SOURCE)}]
            assert value['request'] == {'operation': 'convert', 'input': 'input.docx', 'output': 'output.pdf', 'options': {}}
            return httpx.Response(201, json=job('uploading'))
        if path.endswith('/inputs/0'):
            state['uploaded'] = await request.aread()
            assert state['uploaded'] == SOURCE
            return httpx.Response(200, json=job('uploading'))
        if path.endswith('/run'):
            return httpx.Response(202, json=job())
        if path.endswith('/outputs/0'):
            return httpx.Response(200, content=(b'%PDF-tampered' if state.get('tamper') else PDF))
        state['polls'] += 1
        if state.get('never_finish') or state['polls'] < state.get('needed_polls', 1):
            return httpx.Response(200, json=job())
        return httpx.Response(200, json=job('failed' if state.get('fail') else 'succeeded'))

    original = httpx.AsyncClient
    def factory(**kwargs):
        kwargs['transport'] = httpx.MockTransport(transport)
        return original(**kwargs)
    monkeypatch.setattr(preview.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(preview.cloud, 'actor_for', auth)
    monkeypatch.setattr(preview.cloud, 'touch_host', touch)
    monkeypatch.setattr(preview.cloud, 'issue_lease', lease)
    monkeypatch.setattr(preview.office, 'service_configuration', lambda: ('http://office-service:8093', 'test-secret'))
    monkeypatch.setattr(preview, '_slots', asyncio.Semaphore(2))
    monkeypatch.setattr(preview, '_pending', 0)
    monkeypatch.setattr(preview, 'HEARTBEAT', .01)
    monkeypatch.setattr(preview, 'POLL_INTERVAL', .002)
    app = FastAPI()
    app.include_router(web.router)

    @app.exception_handler(preview.iam.IAMFailure)
    async def exception(request, exc):
        return JSONResponse({'detail': exc.code}, status_code=exc.status)

    with TestClient(app, base_url='https://preview.test') as value:
        value.state = state
        yield value
    assert preview._pending == 0


def render(client, body=None):
    return client.post('/api/officeToPdf/render', json=envelope() if body is None else body)


def test_real_reader_and_office_protocol_preserve_native_preview_contract(client):
    response = render(client)
    value = response.json()
    assert response.status_code == 200 and response.content.startswith(b' \n')
    assert value['rpcId'] == 'test-preview' and value['result']['ok']
    result = value['result']['value']
    assert result == {'absolutePath': PATH, 'version': 'source-v1', 'offset': 0, 'eof': True,
        'bytes': len(PDF), 'data': base64.b64encode(PDF).decode(), 'missingFonts': [], 'generation': 'native-generation-one'}
    assert client.state['deleted'] and client.state['uploaded'] == SOURCE
    assert client.state['touches'] >= 3
    assert response.headers['x-accel-buffering'] == 'no'


@pytest.mark.parametrize('extra', [{'path': '/etc/passwd'}, {'path': '/workspace/../file.docx'},
    {'path': '/workspace/file.exe'}, {'workspaceFileScopeId': 'other-owner'}, {'owner': 'other'}, {'priority': 'unbounded'}])
def test_input_scope_and_file_constraints(client, extra):
    assert render(client, envelope(**extra)).status_code == 400
    assert not client.state['calls']


def test_bearer_is_not_a_web_preview_session(client):
    response = client.post('/api/officeToPdf/render', json=envelope(), headers={'Authorization': 'Bearer fake'})
    assert response.status_code == 403 and not client.state['calls']


@pytest.mark.parametrize('fault', ['tamper', 'fail', 'changed'])
def test_failures_return_actionable_native_error_and_remove_job(client, fault):
    client.state[fault] = True
    result = render(client).json()['result']
    assert result['ok'] is False and set(result['error']) == {'code', 'message', 'details'}
    assert result['error']['details'] == {}
    assert client.state['deleted']
    assert 'value' not in result


def test_slow_conversion_keeps_proxy_alive_and_checks_activity(client):
    client.state['needed_polls'] = 22
    response = render(client)
    assert response.json()['result']['ok']
    assert response.content.count(b' \n') >= 3
    assert client.state['touches'] >= 5


def test_timeout_cancels_and_removes_private_job(client, monkeypatch):
    client.state['never_finish'] = True
    monkeypatch.setattr(preview, 'TIMEOUT', .035)
    result = render(client).json()['result']
    assert not result['ok'] and '超时' in result['error']['message']
    assert client.state['deleted']


def test_expired_session_during_conversion_never_returns_pdf(client):
    client.state.update(needed_polls=30, expire_at=10)
    result = render(client).json()['result']
    assert not result['ok'] and '登录' in result['error']['message']
    assert client.state['deleted']


def test_other_native_calls_still_use_original_proxy(client, monkeypatch):
    seen = []
    async def proxy(request, path):
        seen.append(path)
        return JSONResponse({'unchanged': True})
    monkeypatch.setattr(web.cloud, 'native_http', proxy)
    response = client.post('/api/workspaceFiles/stat', json={})
    assert response.json() == {'unchanged': True} and seen == ['api/workspaceFiles/stat']


def test_native_workspace_relative_preview_path(client):
    response = render(client, envelope(path=PATH[len('/workspace/'):]))
    assert response.json()['result']['value']['absolutePath'] == PATH
    assert ('POST', '/api/session/list') in client.state['calls']


def test_background_prefetch_never_counts_as_user_activity(client):
    client.state['needed_polls'] = 15
    response = render(client, envelope(priority='background'))
    assert response.json()['result']['ok']
    assert client.state['activities'] and not any(client.state['activities'])


def test_real_asgi_disconnect_cancels_office_job_under_cancel_scope(client):
    client.state['never_finish'] = True
    async def scenario():
        sent_request = False
        disconnected = asyncio.Event()
        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {'type': 'http.request', 'body': json.dumps(envelope()).encode(), 'more_body': False}
            await disconnected.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            pass
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'https', 'path': '/api/officeToPdf/render',
            'raw_path': b'/api/officeToPdf/render', 'query_string': b'', 'root_path': '',
            'headers': [(b'content-type', b'application/json')], 'server': ('preview.test', 443), 'client': ('127.0.0.1', 1)}
        task = asyncio.create_task(client.app(scope, receive, send))
        for _ in range(200):
            if client.state['polls']:
                break
            await asyncio.sleep(.005)
        assert client.state['polls'] > 0
        disconnected.set()
        await asyncio.wait_for(task, 2)
        assert client.state['deleted'] and preview._pending == 0
        assert preview._slots._value == 2
    client.portal.call(scenario)
