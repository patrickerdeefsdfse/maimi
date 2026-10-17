"""Web-only upload hook integration; synthetic requests, no company endpoints."""
import asyncio
import ast
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import auth_middleware, personal_cloud as cloud, personal_cloud_web as web
from app.desktop_auth import TrustedActor


def test_upload_static_script_is_available_without_exposing_authenticated_routes(monkeypatch):
    monkeypatch.setenv('AUTH_MODE', 'iam_direct')
    checks = []

    async def no_session(request):
        checks.append(request.url.path)
        return None

    monkeypatch.setattr(auth_middleware, 'authenticate_request', no_session)
    app = FastAPI()
    app.add_middleware(auth_middleware.PlatformAuthMiddleware)
    app.include_router(web.router)
    with TestClient(app, base_url='https://upload.test') as client:
        response = client.get('/maimi/web/upload.js')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/javascript')
        assert response.headers['cache-control'] == 'no-store'
        assert '__DSH_FILE_UPLOAD__' in response.text
        assert not checks
        assert client.post('/api/session/uploadFileBinary', content=b'synthetic').status_code == 401
        assert checks == ['/api/session/uploadFileBinary']


def test_web_chat_places_blocking_hook_before_session_and_deferred_dsh_boot(monkeypatch):
    original = ('<!doctype html><html><head><script type="module" src="/assets/client.js"></script>'
                '</head><body><main>synthetic chat</main></body></html>')

    async def native(request, path):
        assert path == ''
        return StreamingResponse(iter([original[:31], original[31:]]), media_type='text/html')

    monkeypatch.setattr(cloud, 'native_http', native)
    app = FastAPI()
    app.include_router(web.router)
    with TestClient(app, base_url='https://upload.test') as client:
        response = client.get('/maimi/web/chat')
    assert response.status_code == 200
    inserted = '<script src="/maimi/web/upload.js"></script><script src="/maimi/web/session.js"></script><script src="/maimi/web/image.js"></script><script src="/maimi/web/files.js"></script>'
    assert response.text == original.replace('</head>', inserted + '</head>')
    assert response.text.count('/maimi/web/upload.js') == 1


def test_upload_and_image_protocol_scripts_remain_unchanged_since_r17():
    repo = Path(__file__).resolve().parents[4]
    frozen = repo / 'output/cloud-access-r15-20260922/release/public'
    original = (repo / 'output/web-image-r17-20260923/release/public/personal_cloud_web.py').read_text(encoding='utf8')
    candidate = Path(web.__file__).read_text(encoding='utf8')
    for name in ('upload_script', 'image_script'):
        pattern = rf"async def {name}\(\):[\s\S]*?return Response\('''([\s\S]*?)'''"
        assert re.search(pattern, original).group(1) == re.search(pattern, candidate).group(1)
    def function_text(source, name):
        node = next(item for item in ast.parse(source).body if isinstance(item, ast.AsyncFunctionDef) and item.name == name)
        return ast.get_source_segment(source, node)
    original_proxy = (frozen / 'personal_cloud.py').read_text(encoding='utf8')
    # Historical r18 kept the byte proxy identical. r19 adds retention gates;
    # its actual transformed-byte transport is covered by the test below and
    # the retention route tests, rather than requiring an obsolete full body.
    candidate_proxy = (repo / 'output/web-files-idle-r18-20260923/release/public/personal_cloud.py').read_text(encoding='utf8')
    assert function_text(original_proxy, 'native_http') == function_text(candidate_proxy, 'native_http')


def test_proxy_accepts_gateway_transformed_length_without_trusting_original_url_size(monkeypatch):
    """A decrypting gateway can shrink 8192 uploaded bytes to a short document."""
    actor = TrustedActor('SYNTHETIC-UPLOAD-A', 'SYNTHETIC-UPLOAD-A', 'A', (), 'session-A')
    plaintext = 'SYNTHETIC_GATEWAY_CLEAR_TEXT\n'.encode()
    filename = '测试 & 库龄 + 表.xlsx'
    query = urlencode({'sessionId': 'session-synthetic', 'name': filename,
                       'filename': filename, 'size': '8192'})
    receipt = {'ok': True, 'value': {'receiptId': 'synthetic-receipt', 'file': {
        'attachmentId': 'sha256:' + hashlib.sha256(plaintext).hexdigest(),
        'name': filename, 'bytes': len(plaintext)}}}
    sent = []

    async def authenticated(request):
        return actor

    async def existing_host(request, bound_actor):
        assert bound_actor is actor
        return {'origin': 'http://synthetic-owned:3084', 'route_token': 'r' * 43}

    async def lease(request, bound_actor):
        assert bound_actor is actor
        return 'mcl_' + 'l' * 43, int(time.time()) + 60

    class Reply(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(receipt).encode()

    async def transport(request):
        sent.append(request)
        assert request.url.host == 'synthetic-owned'
        assert request.url.port == 3084
        assert request.url.path == '/api/session/uploadFileBinary'
        assert request.url.params['filename'] == filename
        assert request.url.params['size'] == '8192'
        assert await request.aread() == plaintext
        assert int(request.headers['content-length']) == len(plaintext)
        assert request.headers['authorization'] == 'Bearer ' + 'r' * 43
        assert request.headers['x-megmeet-client-lease'] == 'mcl_' + 'l' * 43
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Reply())

    real_client = httpx.AsyncClient
    monkeypatch.setattr(cloud, 'actor_for', authenticated)
    monkeypatch.setattr(cloud, 'ensure_personal_workspace', lambda bound_actor: {})
    monkeypatch.setattr(cloud, 'touch_host', existing_host)
    monkeypatch.setattr(cloud, 'issue_lease', lease)
    monkeypatch.setattr(cloud.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))

    async def scenario():
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {'type': 'http.disconnect'}
            delivered = True
            return {'type': 'http.request', 'body': plaintext, 'more_body': False}

        request = Request({'type': 'http', 'method': 'POST', 'scheme': 'https',
                           'server': ('upload.test', 443), 'path': '/api/session/uploadFileBinary',
                           'query_string': query.encode(),
                           'headers': [(b'content-type', b'application/octet-stream'),
                                       (b'origin', b'https://upload.test')]}, receive)
        response = await cloud.native_http(request, 'api/session/uploadFileBinary')
        result = b''.join([chunk async for chunk in response.body_iterator])
        assert response.status_code == 200
        assert json.loads(result) == receipt
        assert len(sent) == 1

    asyncio.run(scenario())
