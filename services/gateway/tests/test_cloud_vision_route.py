"""Synthetic current-task lease checks for the private vision negotiation route."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import personal_cloud as cloud
from app.iam_oauth import IAMFailure


def request(body, lease='synthetic-lease'):
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'scheme': 'http',
        'server': ('local', 80), 'path': '/internal/personal-cloud/model/v1/vision-capability',
        'query_string': b'', 'headers': [(b'authorization', ('Bearer ' + lease).encode())]}, receive)


def broker(monkeypatch):
    actor = SimpleNamespace(user_no='SYNTHETIC-EMPLOYEE')
    calls = []
    async def lease_actor(value):
        assert value == 'synthetic-lease'
        return actor
    current = SimpleNamespace()
    async def ensure_vision(principal, model):
        assert principal is actor
        assert await current.actor_validator(principal) is actor
        calls.append(model)
        return {'data': [{'id': model, 'inputModalities': ['text', 'image']}], 'default_model': model}
    current.ensure_vision = ensure_vision
    monkeypatch.setattr(cloud, 'lease_actor', lease_actor)
    monkeypatch.setattr(cloud.model, 'get_broker', lambda: current)
    return actor, current, calls


def test_vision_route_uses_current_task_principal(monkeypatch):
    _, _, calls = broker(monkeypatch)
    result = asyncio.run(cloud.worker_model(request(b'{"model":"synthetic-vision"}'), 'vision-capability'))
    assert result.status_code == 200
    assert json.loads(result.body)['data'][0]['inputModalities'] == ['text', 'image']
    assert calls == ['synthetic-vision']


@pytest.mark.parametrize('body,status', [(b'{"model":"x","user_no":"other"}', 400),
    (b'[]', 400), (b'{"model":null}', 400), (b'x' * 1025, 413)])
def test_vision_route_rejects_extra_identity_and_unbounded_body(monkeypatch, body, status):
    _, _, calls = broker(monkeypatch)
    with pytest.raises(IAMFailure) as error:
        asyncio.run(cloud.worker_model(request(body), 'vision-capability'))
    assert error.value.status == status
    assert calls == []


def test_revoked_task_lease_rejected_before_body(monkeypatch):
    async def denied(value):
        raise IAMFailure(401, 'personal_cloud_lease_expired')
    async def unread():
        raise AssertionError('must authenticate before reading image capability request')
    monkeypatch.setattr(cloud, 'lease_actor', denied)
    value = request(b'')
    value._receive = unread
    with pytest.raises(IAMFailure) as error:
        asyncio.run(cloud.worker_model(value, 'vision-capability'))
    assert error.value.status == 401
