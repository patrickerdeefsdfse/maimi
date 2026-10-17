"""Synthetic SSO proofs and fixed application-session lifecycle regression tests."""
import asyncio
import copy
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import iam_oauth as iam


class MemoryRedis:
    def __init__(self, now):
        self.now, self.data, self.deadlines = now, {}, {}

    async def set(self, key, value, *, ex, nx):
        if nx and await self.get(key) is not None:
            return False
        self.data[key], self.deadlines[key] = value, self.now() + ex
        return True

    async def get(self, key):
        if key in self.data and self.deadlines[key] <= self.now():
            await self.delete(key)
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)
        self.deadlines.pop(key, None)

    async def ttl(self, key):
        return int(self.deadlines[key] - self.now()) if await self.get(key) is not None else -2


class MemoryStore(iam.RedisIAMStore):
    def __init__(self, now):
        self.redis = MemoryRedis(now)
        self.states = {}

    async def state_create(self, state, binding, ttl):
        self.states[state] = binding

    async def state_consume(self, state, binding):
        if self.states.get(state) != binding:
            return False
        del self.states[state]
        return True


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(iam.time, "time", lambda: clock[0])
    monkeypatch.setattr(iam, "monotonic", lambda: clock[0])
    encryption = tmp_path / "synthetic-encryption"
    encryption.write_bytes(Fernet.generate_key())
    secret = tmp_path / "synthetic-client-secret"
    secret.write_text("synthetic-client-secret")
    integration = tmp_path / "synthetic-integration-secret"
    integration.write_text("synthetic-integration-secret")
    monkeypatch.setenv("IAM_TOKEN_ENCRYPTION_KEY_FILE", str(encryption))
    monkeypatch.setenv("PERSONAL_KEY_SERVICE_SECRET_FILE", str(integration))
    monkeypatch.setenv("PERSONAL_KEY_ADAPTER", "tokenkey")
    monkeypatch.setenv("PERSONAL_KEY_TOKENKEY_URL", "https://tokenkey.test/api/external/user-credentials")
    config = iam.IAMConfig(
        base_url="https://iam.test", authorization_base_url="https://iam.test",
        client_id="synthetic-client", client_secret_file=str(secret),
        redirect_uri="https://app.test/v1/iam/callback", public_origin="https://app.test",
        post_login_path="/maimi/web/", logout_return_uri="https://app.test/maimi/",
        redis_url="redis://unused.test/0", identity_mode="sso_tokenkey")
    seen, controls = [], {"employee": "EMPLOYEE-A", "lookup_elapsed": 0, "expires_in": 60}

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/accessToken"):
            return httpx.Response(200, json={"access_token": "synthetic-short-iam-token",
                "token_type": "Bearer", "expires_in": controls["expires_in"]})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"attributes": {"username": controls["employee"]}})
        assert request.url.path == "/api/external/user-credentials"
        assert request.headers["x-iam-access-token"] == "synthetic-short-iam-token"
        assert request.headers["x-api-key"] == "synthetic-integration-secret"
        clock[0] += controls["lookup_elapsed"]
        return httpx.Response(200, json={"ok": True, "username": controls["employee"],
            "api_key": "synthetic-personal-key-" + controls["employee"],
            "models": controls.get("models", [{"model_code": "model-a", "model_name": "Model A"}])})

    service = iam.IAMService(config, MemoryStore(lambda: clock[0]), transport=httpx.MockTransport(handler))
    return service, clock, seen, controls


async def login(service):
    location, binding = await service.begin()
    state = parse_qs(urlsplit(location).query)["state"][0]
    return await service.exchange("synthetic-code", state, binding)


def test_short_iam_proof_yields_fixed_24_hour_local_session_and_no_later_company_requests(scenario):
    async def run():
        service, clock, seen, _ = scenario
        sid, ttl, actor = await login(service)
        assert ttl == 86400
        record = await service.store.session_read(sid)
        assert set(record) == {"version", "account_no", "user_no", "personal_credential_encrypted", "expires_at"}
        assert record["version"] == 2
        encoded = json.dumps(record)
        assert "synthetic-short-iam-token" not in encoded
        assert "synthetic-personal-key" not in encoded
        assert "iam_token_encrypted" not in record
        initial_deadline = record["expires_at"]
        for elapsed in (61, 600, 43199, 86399):
            clock[0] = 2_000_000_000 + elapsed
            assert await service.authenticate(sid) == actor
            credential = await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
            assert credential.api_key == "synthetic-personal-key-EMPLOYEE-A"
            assert credential.models[0].code == "model-a"
            assert credential.expires_at == initial_deadline
            assert await service.session_deadline(sid, "EMPLOYEE-A", "EMPLOYEE-A") == initial_deadline
            assert "synthetic-personal-key" not in repr(credential)
        assert seen == ["/esc-sso/oauth2.0/accessToken", "/esc-sso/oauth2.0/profile", "/api/external/user-credentials"]
        clock[0] = initial_deadline
        assert await service.authenticate(sid) is None
        with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
            await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


def test_explicit_logout_and_missing_records_never_yield_cached_key(scenario):
    async def run():
        service, _, _, _ = scenario
        sid, _, _ = await login(service)
        await service.store.session_delete(sid)
        assert await service.authenticate(sid) is None
        for method in (service.personal_model_credential, service.session_deadline):
            with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
                await method(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


@pytest.mark.parametrize("user_no,account_no", [("EMPLOYEE-B", "EMPLOYEE-A"), ("EMPLOYEE-A", "EMPLOYEE-B")])
def test_callers_cannot_borrow_another_identity_key(scenario, user_no, account_no):
    async def run():
        service, _, _, _ = scenario
        sid, _, _ = await login(service)
        with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
            await service.personal_model_credential(sid, user_no, account_no)
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["same_employee_cipher_swap", "different_employee_cipher_swap", "account", "user", "deadline", "version", "cipher", "extra_field"])
def test_encrypted_record_is_bound_to_session_identity_deadline_and_version(scenario, mutation):
    async def run():
        service, _, _, controls = scenario
        sid, _, _ = await login(service)
        first = await service.store.session_read(sid)
        if mutation == "different_employee_cipher_swap":
            controls["employee"] = "EMPLOYEE-B"
        other_sid, _, _ = await login(service)
        second = await service.store.session_read(other_sid)
        modified = copy.deepcopy(first)
        if "cipher_swap" in mutation:
            modified["personal_credential_encrypted"] = second["personal_credential_encrypted"]
        elif mutation == "account":
            modified["account_no"] = "EMPLOYEE-B"
        elif mutation == "user":
            modified["user_no"] = "EMPLOYEE-B"
        elif mutation == "deadline":
            modified["expires_at"] += 3600
        elif mutation == "version":
            modified["version"] = 1
        elif mutation == "cipher":
            modified["personal_credential_encrypted"] = "invalid-ciphertext"
        elif mutation == "extra_field":
            modified["unexpected"] = True
        service.store.redis.data["megmeet:iam:session:" + iam._digest(sid)] = json.dumps(modified)
        with pytest.raises(iam.IAMFailure):
            await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


def test_explicit_redis_ttl_shortening_cannot_be_extended(scenario):
    async def run():
        service, clock, _, _ = scenario
        sid, _, _ = await login(service)
        key = "megmeet:iam:session:" + iam._digest(sid)
        shortened = clock[0] + 10
        service.store.redis.deadlines[key] = shortened
        assert await service.session_deadline(sid, "EMPLOYEE-A", "EMPLOYEE-A") == shortened
        clock[0] += 11
        with pytest.raises(iam.IAMFailure):
            await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


def test_logout_during_ttl_lookup_fails_closed(scenario, monkeypatch):
    async def run():
        service, _, _, _ = scenario
        sid, _, _ = await login(service)
        async def revoked(_sid):
            await service.store.session_delete(_sid)
            return 86400
        monkeypatch.setattr(service.store, "session_remaining_ttl", revoked)
        with pytest.raises(iam.IAMFailure, match="iam_session_expired"):
            await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


@pytest.mark.parametrize("remaining", [-2, -1, 0, True, None])
def test_missing_or_unbounded_redis_ttl_cannot_authorize(scenario, monkeypatch, remaining):
    async def run():
        service, _, _, _ = scenario
        sid, _, _ = await login(service)
        async def no_lease(_sid):
            return remaining
        monkeypatch.setattr(service.store, "session_remaining_ttl", no_lease)
        with pytest.raises(iam.IAMFailure, match="iam_session_expired"):
            await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


@pytest.mark.parametrize("lookup_elapsed", [60, 61])
def test_expired_signin_proof_cannot_create_long_local_session(scenario, lookup_elapsed):
    async def run():
        service, _, _, controls = scenario
        controls["lookup_elapsed"] = lookup_elapsed
        with pytest.raises(iam.IAMFailure):
            await login(service)
        assert not service.store.redis.data
    asyncio.run(run())


def test_old_sso_session_requires_new_login_without_automatic_upgrade(scenario):
    async def run():
        service, clock, seen, _ = scenario
        sid = "O" * 43
        encrypted = iam._encrypt_access_token(sid, "EMPLOYEE-A", "EMPLOYEE-A", "synthetic-old-token", int(clock[0]) + 60)
        await service.store.session_create(sid, "EMPLOYEE-A", "EMPLOYEE-A", 60, encrypted, int(clock[0]) + 60)
        with pytest.raises(iam.IAMFailure, match="iam_personal_credentials_require_login"):
            await service.authenticate(sid)
        assert await service.store.session_remaining_ttl(sid) == 60
        assert seen == []
    asyncio.run(run())


def test_new_sso_session_cannot_be_misrepresented_as_iam_access_token(scenario):
    async def run():
        service, _, _, _ = scenario
        sid, _, _ = await login(service)
        with pytest.raises(iam.IAMFailure, match="iam_personal_credentials_require_login"):
            await service.personal_access_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
    asyncio.run(run())


def test_large_valid_catalog_round_trips_in_encrypted_session(scenario):
    async def run():
        service, _, _, controls = scenario
        controls["models"] = [{"model_code": f"model-{i}", "model_name": "模型名称" * 8} for i in range(500)]
        sid, _, _ = await login(service)
        record = await service.store.session_read(sid)
        assert len(record["personal_credential_encrypted"]) > 16384
        credential = await service.personal_model_credential(sid, "EMPLOYEE-A", "EMPLOYEE-A")
        assert len(credential.models) == 500
    asyncio.run(run())


def test_local_lifetime_config_default_and_override(monkeypatch, scenario):
    service, _, _, _ = scenario
    config = service.config
    values = {"IAM_BASE_URL": config.base_url, "IAM_CLIENT_ID": config.client_id,
              "IAM_CLIENT_SECRET_FILE": config.client_secret_file, "IAM_REDIRECT_URI": config.redirect_uri,
              "IAM_PUBLIC_ORIGIN": config.public_origin, "IAM_LOGOUT_RETURN_URI": config.logout_return_uri,
              "REDIS_URL": config.redis_url, "IAM_IDENTITY_MODE": "sso_tokenkey"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MAIMI_SESSION_TTL_SECONDS", raising=False)
    assert iam.IAMConfig.from_env().maimi_session_ttl == 86400
    monkeypatch.setenv("MAIMI_SESSION_TTL_SECONDS", "3600")
    assert iam.IAMConfig.from_env().maimi_session_ttl == 3600
    monkeypatch.setenv("MAIMI_SESSION_TTL_SECONDS", "3599")
    with pytest.raises(iam.IAMFailure, match="iam_configuration_unavailable"):
        iam.IAMConfig.from_env()
