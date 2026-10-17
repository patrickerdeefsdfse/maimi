"""Synthetic downloads only: account binding, file identity and binary bytes."""
import base64
import asyncio
import json

import httpx
import anyio
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import personal_cloud_files as files
from app.desktop_auth import TrustedActor

SESSION = 'session-12345678-1234-1234-1234-123456789abc'
PATH = '/workspace/合成 & 结果.docx'
DATA = b'PK\x03\x04' + bytes(range(256)) * 3


@pytest.fixture
def client(monkeypatch):
    actor = TrustedActor('SYNTHETIC-FILE-A', 'SYNTHETIC-FILE-A', 'A', (), 'session-A')
    calls = []
    config = {'data': DATA, 'version': 'version-one', 'path': PATH, 'auth_count': 0, 'activity': []}

    async def auth(request):
        config['auth_count'] += 1
        if config.get('expire_after', 999) < config['auth_count']:
            raise files.iam.IAMFailure(401, 'authentication_required')
        return actor

    async def touch(request, bound, **kwargs):
        assert bound is actor and set(kwargs) == {'activity'}
        config['activity'].append(kwargs['activity'])
        return {'origin': 'http://worker.test:3084', 'route_token': 'synthetic-route'}

    async def lease(request, bound):
        assert bound is actor
        return 'mcl_synthetic', 9999999999

    async def upstream(request):
        assert request.headers['authorization'] == 'Bearer synthetic-route'
        assert request.headers['x-megmeet-client-lease'] == 'mcl_synthetic'
        body = json.loads(request.content)
        calls.append(body)
        method, args = body['method'], body['payload']['args']
        if method == 'officeToPdf/generation':
            assert args == {}
            result = 'synthetic-office-generation'
        elif method == 'session/list':
            assert args == {'_request': {}}
            result = {'items': [] if config.get('no_scope') else [{'sessionId': SESSION, 'cwd': '/workspace'}]}
        else:
            assert args['workspaceFileScopeId'] == SESSION and args['path'] == PATH
            result = {'absolutePath': config['path'], 'version': config['version'], 'bytes': len(config['data'])}
            if method == 'workspaceFiles/readBytes':
                start, length = args['range']['offset'], args['range']['length']
                result.update(offset=start, data=base64.b64encode(config['data'][start:start + length]).decode(),
                              eof=start + length == len(config['data']))
                if config.get('changed'):
                    result['version'] = 'changed'
                if config.get('malformed'):
                    result['data'] = '%BAD%'
                if config.get('change_after_read'):
                    config['version'] = 'changed-after-read'
        return httpx.Response(200, json={'type': 'server-response', 'rpcId': body['rpcId'],
                                        'result': {'ok': True, 'value': result}})

    original = httpx.AsyncClient
    monkeypatch.setattr(files.cloud, 'actor_for', auth)
    monkeypatch.setattr(files.cloud, 'touch_host', touch)
    monkeypatch.setattr(files.cloud, 'issue_lease', lease)
    monkeypatch.setattr(files.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    monkeypatch.setattr(files, 'CHUNK_BYTES', 100)
    app = FastAPI()
    app.include_router(files.router)

    @app.exception_handler(files.iam.IAMFailure)
    async def failure(request, exc):
        return JSONResponse({'detail': exc.code}, status_code=exc.status)

    with TestClient(app, base_url='https://file.test') as value:
        value.test_config, value.rpc_calls = config, calls
        yield value


def download(client, path=PATH, **kwargs):
    return client.get('/v1/personal-cloud/web/file', params=kwargs.pop('params', {'path': path}), **kwargs)


def test_binary_download_is_independent_of_preview_and_chunked(client):
    response = download(client)
    assert response.status_code == 200 and response.content == DATA
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-length'] == str(len(DATA))
    assert response.headers['content-disposition'].startswith('attachment;')
    assert '%E5%90%88%E6%88%90%20%26%20' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store'
    methods = [call['method'] for call in client.rpc_calls]
    assert methods[:2] == ['session/list', 'workspaceFiles/stat']
    assert methods[2:] == ['workspaceFiles/readBytes'] * 8 + ['workspaceFiles/stat']
    assert client.test_config['auth_count'] >= 18


@pytest.mark.parametrize('path', ['/etc/passwd', '/workspace/../etc/passwd', '/workspace//a', '/workspace/a\\b',
                                '/state/dsh/settings.json', '/workspace/a\nb', 'relative.docx', '/workspace/'])
def test_download_refuses_outside_and_ambiguous_paths(client, path):
    assert download(client, path).status_code == 400
    assert not client.rpc_calls


def test_native_resolved_symlink_cannot_escape_download_roots(client):
    client.test_config['path'] = '/etc/hosts'
    assert download(client).status_code == 400
    assert [x['method'] for x in client.rpc_calls] == ['session/list', 'workspaceFiles/stat']


def test_changed_file_fails_before_browser_gets_content(client):
    client.test_config['changed'] = True
    response = download(client)
    assert response.status_code == 409
    assert response.json()['detail'] == 'personal_cloud_file_changed'


def test_change_during_final_native_read_is_caught(client, monkeypatch):
    monkeypatch.setattr(files, 'CHUNK_BYTES', 1024)
    client.test_config['change_after_read'] = True
    response = download(client)
    assert response.status_code == 409
    assert response.json()['detail'] == 'personal_cloud_file_changed'


def test_malformed_native_data_is_not_downloaded(client):
    client.test_config['malformed'] = True
    assert download(client).status_code == 502


def test_expired_login_is_checked_after_stat_before_bytes(client):
    client.test_config['expire_after'] = 1
    assert download(client).status_code == 401
    assert not any(x['method'] == 'workspaceFiles/readBytes' for x in client.rpc_calls)


def test_no_existing_native_session_is_not_created(client):
    client.test_config['no_scope'] = True
    assert download(client).status_code == 404
    assert [x['method'] for x in client.rpc_calls] == ['session/list']


def test_bearer_and_duplicate_fields_are_not_browser_download_requests(client):
    assert download(client, headers={'Authorization': 'Bearer synthetic'}).status_code == 403
    assert download(client, params=[('path', PATH), ('path', PATH)]).status_code == 400
    assert download(client, params={'path': PATH, 'owner': 'someone-else'}).status_code == 400
    assert not client.rpc_calls


def test_empty_file_remains_valid_download(client):
    client.test_config['data'] = b''
    response = download(client)
    assert response.status_code == 200 and response.content == b''
    assert response.headers['content-length'] == '0'


def test_download_script_has_separate_link_and_no_office_dependency(client):
    response = client.get('/maimi/web/files.js')
    assert response.status_code == 200
    assert '[data-presented-file]' in response.text and "createElement('a')" in response.text
    assert 'officeToPdf' not in response.text


def test_preview_generation_preserves_native_string_contract(client):
    async def run():
        reader = files.NativeFileReader(None, None, {'origin': 'http://worker.test:3084', 'route_token': 'synthetic-route'},
                                        'mcl_synthetic', httpx.AsyncClient(), PATH, SESSION, files.MAX_FILE_BYTES)
        try:
            assert await reader.rpc('officeToPdf/generation', {}) == 'synthetic-office-generation'
        finally:
            await reader.close()
    asyncio.run(run())


def test_explicit_scope_resolves_native_relative_path_inside_real_workspace(client):
    async def run():
        reader = await files.NativeFileReader.open(object(), PATH.removeprefix('/workspace/'), session_id=SESSION)
        try:
            assert reader.path == PATH and reader.session_id == SESSION
            assert b''.join([part async for part in reader.chunks()]) == DATA
        finally:
            await reader.close()
    asyncio.run(run())
    assert client.rpc_calls[0]['method'] == 'session/list'


def test_relative_preview_requires_existing_matching_native_scope(client):
    async def run():
        with pytest.raises(files.iam.IAMFailure) as caught:
            await files.NativeFileReader.open(object(), 'relative.docx', session_id=SESSION.replace('abc', 'def'))
        assert caught.value.status == 403
    asyncio.run(run())
    assert [call['method'] for call in client.rpc_calls] == ['session/list']


@pytest.mark.parametrize('path', ['../file.docx', './file.docx', 'a//file.docx', 'a/../file.docx',
                                'C:/file.docx', 'https://example/file.docx', 'a\\file.docx'])
def test_relative_preview_paths_do_not_escape_or_become_urls(path):
    with pytest.raises(files.iam.IAMFailure):
        files.file_path(path, allow_relative=True)


def test_background_preview_does_not_renew_human_activity(client):
    async def run():
        reader = await files.NativeFileReader.open(object(), PATH, session_id=SESSION, activity=False)
        try:
            assert b''.join([part async for part in reader.chunks()]) == DATA
        finally:
            await reader.close()
    asyncio.run(run())
    assert client.test_config['activity'] == [False] * 9


def test_native_connection_cleanup_survives_browser_cancellation_scope():
    closed = []
    class Client:
        async def aclose(self):
            await anyio.sleep(.01)
            closed.append(True)
    async def run():
        reader = files.NativeFileReader(None, None, {}, '', Client(), PATH, SESSION, files.MAX_FILE_BYTES)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await reader.close()
    anyio.run(run)
    assert closed == [True]
