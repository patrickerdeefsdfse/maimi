"""Shared-office identity, streaming limits and fixed routing; synthetic only."""
import hashlib
import json
from dataclasses import replace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from app import office_remote as office
from app import iam_oauth as iam
from app.desktop_auth import TrustedActor

A = TrustedActor('SYNTHETIC-A', 'account-a', 'A', (), 'a' * 43)
B = TrustedActor('SYNTHETIC-B', 'account-b', 'B', (), 'b' * 43)
JOB = 'a' * 32
KEY = 'synthetic-office-service-key-only-for-tests'


def value(state='uploading', **extras):
    return {'protocol': office.PROTOCOL, 'job_id': JOB, 'state': state, 'outputs': [], **extras}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    secret = tmp_path / 'service-secret'
    secret.write_text(KEY)
    monkeypatch.setenv('OFFICE_SERVICE_SECRET_FILE', str(secret))
    monkeypatch.setenv('OFFICE_SERVICE_URL', 'http://office-service:8093')
    state = {'actor': A, 'calls': [], 'revalidations': 0}

    async def actor(request):
        state['revalidations'] += 1
        if state['actor'] is None:
            raise iam.IAMFailure(401, 'synthetic_expired')
        return state['actor']

    async def upstream(request):
        state['calls'].append(request)
        await request.aread()
        return httpx.Response(200, json=value())

    monkeypatch.setattr(office.cloud, 'actor_for', actor)
    monkeypatch.setattr(office, '_transport', httpx.MockTransport(upstream))
    app = FastAPI()
    app.include_router(office.router)

    @app.exception_handler(iam.IAMFailure)
    async def failed(request, exc):
        return JSONResponse({'detail': exc.code}, status_code=exc.status)

    return TestClient(app, base_url='https://office-app.test'), state


def create(client, headers=None):
    return client.post('/v1/office/jobs', json={'protocol': office.PROTOCOL,
        'request': {'operation': 'read', 'input': 'input.pdf', 'options': {}},
        'inputs': [{'path': 'input.pdf', 'size_bytes': 2}]}, headers=headers)


def test_identity_and_scope_come_only_from_current_server_actor(setup):
    client, state = setup
    response = create(client, {'X-Office-Service-Key': 'foreign', 'X-Office-Owner': 'foreign-owner',
        'X-Office-Scope': 'foreign-scope', 'Authorization': 'Bearer synthetic-desktop-grant',
        'Cookie': 'synthetic-browser-cookie', 'X-IAM-Access-Token': 'synthetic-forbidden'})
    assert response.status_code == 200
    sent = state['calls'][0]
    assert sent.headers['x-office-service-key'] == KEY
    assert sent.headers['x-office-owner'] == hashlib.sha256(('megmeet-office-owner-v1:' + A.user_no).encode()).hexdigest()
    first_scope = sent.headers['x-office-scope']
    assert all(h not in sent.headers for h in ('authorization', 'cookie', 'x-iam-access-token'))
    state['actor'] = replace(A, auth_session_id='c' * 43)
    assert create(client).status_code == 200
    assert state['calls'][1].headers['x-office-owner'] == sent.headers['x-office-owner']
    assert state['calls'][1].headers['x-office-scope'] != first_scope
    state['actor'] = B
    assert create(client).status_code == 200
    assert state['calls'][2].headers['x-office-owner'] != sent.headers['x-office-owner']


@pytest.mark.parametrize('suffix', ['/jobs/../secret', '/jobs/x', '/jobs/' + JOB + '/inputs/20',
    '/jobs/' + JOB + '/outputs/../../etc', '/jobs?owner=foreign', '/jobs/' + JOB + '/run?url=http://foreign'])
def test_unknown_paths_and_query_overrides_never_reach_worker(setup, suffix):
    client, state = setup
    response = client.post('/v1/office' + suffix, content=b'{}')
    assert response.status_code in (404, 405)
    assert not state['calls']


def test_create_rejects_identity_and_unknown_envelope_fields(setup):
    client, state = setup
    response = client.post('/v1/office/jobs', json={'protocol': office.PROTOCOL,
        'request': {}, 'inputs': [], 'owner': 'foreign'})
    assert response.status_code == 400
    assert not state['calls']


def test_upload_stream_is_bounded_without_forwarding_user_headers(setup, monkeypatch):
    client, state = setup
    monkeypatch.setattr(office, 'MAX_INPUT', 8)
    path = '/v1/office/jobs/' + JOB + '/inputs/0'
    assert client.put(path, content=b'abc', headers={'X-Office-Owner': 'foreign'}).status_code == 200
    assert state['calls'][0].content == b'abc'
    assert state['calls'][0].headers['content-type'] == 'application/octet-stream'
    assert client.put(path, content=b'abcdefghi').status_code == 413
    assert len(state['calls']) == 1


def test_logout_during_worker_request_hides_result(setup, monkeypatch):
    client, state = setup

    async def upstream(request):
        state['actor'] = None
        return httpx.Response(200, json=value(state='succeeded', result={'text': 'synthetic private result'}))

    monkeypatch.setattr(office, '_transport', httpx.MockTransport(upstream))
    response = client.get('/v1/office/jobs/' + JOB)
    assert response.status_code == 401
    assert 'private result' not in response.text


@pytest.mark.parametrize('status,expected', [(302, 503), (401, 503), (403, 503), (404, 404), (409, 409), (429, 429), (500, 503)])
def test_worker_failure_body_never_reaches_client(setup, monkeypatch, status, expected):
    client, _ = setup
    monkeypatch.setattr(office, '_transport', httpx.MockTransport(lambda req:
        httpx.Response(status, headers={'Location': 'https://foreign.test/'}, text='synthetic-secret-error-body')))
    response = client.get('/v1/office/jobs/' + JOB)
    assert response.status_code == expected
    assert 'synthetic-secret' not in response.text


@pytest.mark.parametrize('bad', [value(job_id='b' * 32), value(state='unknown'), value(outputs=[{
    'id': '0', 'path': '../foreign', 'size_bytes': 2, 'sha256': 'a' * 64}])])
def test_worker_job_identity_and_output_paths_checked(setup, monkeypatch, bad):
    client, _ = setup
    monkeypatch.setattr(office, '_transport', httpx.MockTransport(lambda req: httpx.Response(200, json=bad)))
    assert client.get('/v1/office/jobs/' + JOB).status_code == 502


def test_output_is_streamed_without_worker_identity_headers(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(office, '_transport', httpx.MockTransport(lambda req:
        httpx.Response(200, content=b'PDF-test', headers={'X-Office-Service-Key': KEY,
            'Set-Cookie': 'synthetic-foreign-cookie', 'Content-Disposition': 'evil'})))
    response = client.get('/v1/office/jobs/' + JOB + '/outputs/0')
    assert response.status_code == 200 and response.content == b'PDF-test'
    assert 'set-cookie' not in response.headers and 'x-office-service-key' not in response.headers


def test_cloud_route_uses_current_lease_not_desktop_identity(setup, monkeypatch):
    client, state = setup
    leases = []

    async def lease_actor(token):
        leases.append(token)
        if token != 'mcl_' + 'z' * 43:
            raise iam.IAMFailure(401, 'personal_cloud_lease_invalid')
        return B

    monkeypatch.setattr(office.cloud, 'lease_actor', lease_actor)
    path = '/internal/personal-cloud/office/jobs/' + JOB
    assert client.get(path).status_code == 401
    response = client.get(path, headers={'Authorization': 'Bearer mcl_' + 'z' * 43})
    assert response.status_code == 200
    assert state['revalidations'] == 0
    assert len(leases) >= 2
    assert state['calls'][0].headers['x-office-owner'] == office.worker_headers(B, KEY)['X-Office-Owner']


def test_config_disallows_external_worker_url(setup, monkeypatch):
    client, state = setup
    monkeypatch.setenv('OFFICE_SERVICE_URL', 'https://foreign.test/office')
    assert create(client).status_code == 503
    assert not state['calls']


def test_nonfinite_json_and_oversize_service_secret_fail_before_worker(setup, monkeypatch, tmp_path):
    client, state = setup
    response = client.post('/v1/office/jobs', content=b'{"protocol":"megmeet-office-remote-v1","request":{"value":NaN},"inputs":[]}',
                           headers={'Content-Type':'application/json'})
    assert response.status_code == 400
    bad = tmp_path / 'oversize'
    bad.write_text('s' * 257)
    monkeypatch.setenv('OFFICE_SERVICE_SECRET_FILE', str(bad))
    assert create(client).status_code == 503
    assert not state['calls']
