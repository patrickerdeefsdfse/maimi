"""Synthetic login/key/desktop/cloud integration; no company network access."""
import asyncio
from dataclasses import replace
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from test_iam_oauth import config, env_config, FakeStore, start
from test_desktop_auth import MemoryDesktopStore, login_desktop
from app import iam_oauth as iam, desktop_auth as desktop, personal_cloud as cloud
from app import personal_model_proxy as model, main


@pytest.fixture
def local_session(config, env_config, monkeypatch, tmp_path):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    secret = tmp_path / "synthetic-tokenkey-integration-secret"
    secret.write_text("synthetic-service-secret")
    monkeypatch.setenv("AUTH_MODE", "iam_direct")
    monkeypatch.setenv("IAM_IDENTITY_MODE", "sso_tokenkey")
    monkeypatch.setenv("PERSONAL_KEY_ADAPTER", "tokenkey")
    monkeypatch.setenv("PERSONAL_KEY_SERVICE_SECRET_FILE", str(secret))
    monkeypatch.setenv("PERSONAL_KEY_TOKENKEY_URL", "https://tokenkey.test/api/external/user-credentials")
    monkeypatch.delenv("PERSONAL_MODEL_ALLOWED_MODELS", raising=False)
    cfg = replace(config, identity_mode="sso_tokenkey")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/accessToken"):
            return httpx.Response(200, json={"access_token": "synthetic-short-iam-token",
                "token_type": "bearer", "expires_in": 60})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"attributes": {"username": "M00000001"}})
        assert request.url.host == "tokenkey.test"
        assert request.headers["X-IAM-Access-Token"] == "synthetic-short-iam-token"
        return httpx.Response(200, json={"ok": True, "username": "M00000001",
            "api_key": "synthetic-personal-model-key", "models": [
                {"model_code": "synthetic-model", "model_name": "Synthetic model"}]})

    async def forbidden_mapper(account):
        pytest.fail("Company SSO does not use directory mapping")
    service = iam.IAMService(cfg, FakeStore(), forbidden_mapper, httpx.MockTransport(handler))
    async def run(action):
        return await action(service)
    monkeypatch.setattr(iam, "_with_service", run)
    monkeypatch.setattr(iam.IAMConfig, "from_env", lambda: cfg)
    return SimpleNamespace(service=service, calls=calls, clock=clock)


async def sign_in(stack):
    state, binding = await start(stack.service)
    sid, ttl, user = await stack.service.exchange("synthetic-code", state, binding)
    return desktop._actor(user, sid), ttl


def test_sixty_second_iam_token_yields_24h_local_model_session_without_requery(local_session, caplog):
    stack = local_session
    submitted = []
    def upstream(request):
        assert request.headers["Authorization"] == "Bearer synthetic-personal-model-key"
        assert "X-IAM-Access-Token" not in request.headers
        submitted.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    async def run():
        actor, ttl = await sign_in(stack)
        assert ttl == 86400
        deadline = stack.clock[0] + 86400
        assert len(stack.calls) == 3
        # A stale IAM token is never revisited by catalog polling or chat.
        stack.clock[0] += 75
        broker = model.PersonalModelBroker(model.provider_from_env(), "https://model.test/v1",
            httpx.MockTransport(upstream))
        for _ in range(2):
            result = await broker.models(actor)
            assert result["default_model"] == "synthetic-model"
        assert (await broker.complete(actor, {"model": "synthetic-model", "messages": [
            {"role": "user", "content": "synthetic message"}]}))["choices"][0]["message"]["content"] == "OK"
        assert len(submitted) == 1 and len(stack.calls) == 3
        assert await stack.service.session_deadline(actor.auth_session_id, actor.user_no, actor.account_no) == deadline
        assert "synthetic-personal-model-key" not in json.dumps(stack.service.store.sessions)
        assert "synthetic-short-iam-token" not in json.dumps(stack.service.store.sessions)
        stack.clock[0] = deadline
        with pytest.raises(iam.IAMFailure):
            await broker.models(actor)
        assert len(stack.calls) == 3
    asyncio.run(run())
    assert "synthetic-personal-model-key" not in caplog.text
    assert "synthetic-short-iam-token" not in caplog.text


class LeaseRedis:
    def __init__(self):
        self.data = {}
        self.deadlines = {}
    async def get(self, key):
        if self.deadlines.get(key, 0) <= time.time():
            self.data.pop(key, None)
        return self.data.get(key)
    async def set(self, key, value, ex=None, nx=False):
        if nx and await self.get(key) is not None:
            return False
        self.data[key] = value
        self.deadlines[key] = time.time() + ex
        return True


def cloud_request(token=None):
    headers = [(b"authorization", ("Bearer " + token).encode())] if token else []
    return Request({"type": "http", "method": "GET", "headers": headers})


def test_web_and_desktop_cloud_leases_survive_iam_expiry_but_remain_independently_revocable(local_session):
    stack = local_session
    redis = LeaseRedis()
    stack.service.store.redis = redis
    async def run():
        web_actor, _ = await sign_in(stack)
        desktop_actor, _ = await sign_in(stack)
        deadline = stack.clock[0] + 86400
        token = "mdt_" + "a" * 43
        grant_store = desktop.RedisDesktopStore(redis)
        await grant_store.grant(token, desktop_actor.auth_session_id, "synthetic-instance", 900)
        stack.clock[0] += 75
        web_lease, web_host_deadline = await cloud.issue_lease(cloud_request(), web_actor)
        desktop_lease, desktop_host_deadline = await cloud.issue_lease(cloud_request(token), desktop_actor)
        assert web_lease != desktop_lease
        assert cloud.owner_key(web_actor) == cloud.owner_key(desktop_actor)
        assert web_host_deadline <= stack.clock[0] + 900
        assert desktop_host_deadline <= deadline
        for lease in (web_lease, desktop_lease):
            key = "megmeet:personal-cloud:lease:" + hashlib.sha256(lease.encode()).hexdigest()
            assert json.loads(redis.data[key])["expires_at"] == deadline
            assert redis.deadlines[key] == deadline
        assert (await cloud.lease_actor(web_lease)).auth_session_id == web_actor.auth_session_id
        assert (await cloud.lease_actor(desktop_lease)).auth_session_id == desktop_actor.auth_session_id
        await stack.service.store.session_delete(web_actor.auth_session_id)
        with pytest.raises(iam.IAMFailure):
            await cloud.lease_actor(web_lease)
        assert (await cloud.lease_actor(desktop_lease)).auth_session_id == desktop_actor.auth_session_id
        assert len(stack.calls) == 6
        stack.clock[0] = deadline
        with pytest.raises(iam.IAMFailure):
            await cloud.lease_actor(desktop_lease)
    asyncio.run(run())


def test_desktop_renewal_after_short_iam_expiry_obeys_fixed_local_login_deadline(local_session):
    stack = local_session
    stack.service.desktop_store = MemoryDesktopStore()
    with TestClient(main.app, base_url="https://app.test") as client:
        result, proof = login_desktop(client)
        assert 800 < result["expires_in"] <= 900
        token = result["access_token"]
        record = stack.service.desktop_store.grants[token]
        sid = record["sid"]
        deadline = stack.service.store.sessions[sid]["expires_at"]
        stack.clock[0] += 75
        record["expires_at"] = stack.clock[0] + 80
        headers = {"Authorization": "Bearer " + token}
        body = {k: proof[k] for k in ("instance_id", "code_verifier")}
        response = client.post(desktop.PREFIX + "/renew", headers=headers, json=body)
        assert response.status_code == 200 and response.json()["expires_in"] == 900
        assert stack.service.store.sessions[sid]["expires_at"] == deadline
        assert len(stack.calls) == 3
        # Close to the absolute boundary renewal yields only the remaining time.
        stack.clock[0] = deadline - 30
        record["expires_at"] = deadline - 10
        response = client.post(desktop.PREFIX + "/renew", headers=headers, json=body)
        assert response.status_code == 200 and response.json()["expires_in"] == 30
        assert record["expires_at"] == deadline
        stack.clock[0] = deadline
        assert client.post(desktop.PREFIX + "/renew", headers=headers, json=body).status_code == 401
        assert len(stack.calls) == 3


def test_default_cached_provider_never_falls_back_after_local_session_loss(local_session):
    async def run():
        actor, _ = await sign_in(local_session)
        await local_session.service.store.session_delete(actor.auth_session_id)
        with pytest.raises(iam.IAMFailure):
            await model.provider_from_env().resolve(actor)
        assert len(local_session.calls) == 3
    asyncio.run(run())
