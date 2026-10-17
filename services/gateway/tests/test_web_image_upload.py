"""Synthetic receiving endpoint tests; never send company images or credentials."""
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import personal_cloud_images as images
from app.desktop_auth import TrustedActor

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII=')


@pytest.fixture
def client(monkeypatch):
    actor = TrustedActor('SYNTHETIC-IMAGE-A', 'SYNTHETIC-IMAGE-A', 'A', (), 'session-A')
    calls = []

    async def auth(request):
        calls.append('auth')
        return actor

    async def touch(request, bound, **kwargs):
        assert bound is actor and kwargs == {'activity': True}
        calls.append('touch')
        return {}

    monkeypatch.setattr(images.cloud, 'actor_for', auth)
    monkeypatch.setattr(images.cloud, 'touch_host', touch)
    monkeypatch.setattr(images.iam.IAMConfig, 'from_env', lambda: SimpleNamespace(public_origin='https://image.test'))
    app = FastAPI()
    app.include_router(images.router)

    @app.exception_handler(images.iam.IAMFailure)
    async def failure(request, exc):
        return JSONResponse({'detail': exc.code}, status_code=exc.status)

    with TestClient(app, base_url='https://image.test') as value:
        value.auth_calls = calls
        yield value


def post(client, data=PNG, **kwargs):
    headers = {'Origin': 'https://image.test', 'Content-Type': 'application/octet-stream'}
    headers.update(kwargs.pop('headers', {}))
    return client.post('/v1/personal-cloud/web/image', params=kwargs.pop('params', {'filename': '测试 & 图.png', 'size': '8192'}),
                       content=data, headers=headers, **kwargs)


def test_decrypted_image_can_be_smaller_than_original_upload(client):
    response = post(client)
    assert response.status_code == 200
    assert response.json() == {'mediaType': 'image/png', 'name': '测试 & 图.png', 'data': base64.b64encode(PNG).decode()}
    assert response.headers['cache-control'] == 'no-store'
    assert client.auth_calls == ['auth', 'touch', 'auth']


@pytest.mark.parametrize(('data', 'error'), [
    (b'%TSD-Header-###%' + bytes(20), 'personal_cloud_image_still_encrypted'),
    (b'<svg>synthetic</svg>', 'personal_cloud_image_type_unsupported'),
    (b'', 'personal_cloud_image_invalid'),
])
def test_non_images_do_not_become_image_prompt_content(client, data, error):
    response = post(client, data)
    assert response.status_code == 422
    assert response.json()['detail'] == error


@pytest.mark.parametrize('params', [
    {'filename': 'a.png'}, {'filename': 'a.png', 'size': '0'},
    [('filename', 'a.png'), ('filename', 'b.png'), ('size', '10')],
    {'filename': 'bad\nname', 'size': '10'},
])
def test_invalid_gateway_fields_refused(client, params):
    assert post(client, params=params).status_code == 400
    assert 'touch' not in client.auth_calls


def test_both_declared_and_received_size_are_bounded(client, monkeypatch):
    monkeypatch.setattr(images, 'MAX_IMAGE_BYTES', 64)
    assert post(client, params={'filename': 'a.png', 'size': '65'}).status_code == 413
    assert post(client, data=PNG + bytes(100), params={'filename': 'a.png', 'size': '10'}).status_code == 413


def test_cross_origin_and_bearer_do_not_use_browser_image_route(client):
    assert post(client, headers={'Origin': 'https://foreign.test'}).status_code == 403
    assert post(client, headers={'Authorization': 'Bearer synthetic'}).status_code == 403


def test_expired_login_after_upload_does_not_return_image(client, monkeypatch):
    previous = images.cloud.actor_for
    count = 0

    async def expired(request):
        nonlocal count
        count += 1
        if count == 2:
            raise images.iam.IAMFailure(401, 'unauthorized')
        return await previous(request)

    monkeypatch.setattr(images.cloud, 'actor_for', expired)
    response = post(client)
    assert response.status_code == 401
    assert 'data' not in response.json()
