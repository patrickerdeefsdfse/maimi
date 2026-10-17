"""SSO-only company contract: no directory secret, no guessed org privileges."""
import asyncio
import json
import logging
import os
import time
from dataclasses import replace

import httpx
import pytest

from test_iam_oauth import config, env_config, FakeStore, start
from app import iam_oauth as iam
from app import personal_model_proxy as models
from app.desktop_auth import TrustedActor


@pytest.fixture
def sso(monkeypatch, config, tmp_path):
    path = tmp_path / "integration-secret"
    path.write_text("synthetic-integration-secret")
    monkeypatch.setenv("PERSONAL_KEY_ADAPTER", "tokenkey")
    monkeypatch.setenv("PERSONAL_KEY_SERVICE_SECRET_FILE", str(path))
    monkeypatch.setenv("PERSONAL_KEY_TOKENKEY_URL", "https://tokenkey.test/api/external/user-credentials")
    # There is deliberately no directory snapshot, endpoint or secret.
    for name in ("IAM_DIRECTORY_SNAPSHOT_FILE", "IAM_DIRECTORY_API_KEY", "IAM_DIRECTORY_API_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)
    state = {"account": "M00000001", "key_user": "M00000001", "profile_status": 200,
             "key_status": 200, "key_ok": True, "requests": []}

    def handler(request):
        state["requests"].append(request)
        if request.url.path.endswith("/accessToken"):
            return httpx.Response(200, json={"access_token": "synthetic-current-access",
                "token_type": "bearer", "expires_in": 600})
        if request.url.path.endswith("/profile"):
            assert request.url.params["access_token"] == "synthetic-current-access"
            if state.get("on_profile"):
                state["on_profile"]()
            return httpx.Response(state["profile_status"], json={"id": "opaque-id-not-employee",
                "attributes": {"username": state["account"], "organization_codes": ["FORGED-DEPT"], "admin": True}})
        assert request.url.host == "tokenkey.test"
        assert request.url.path == "/api/external/user-credentials"
        assert request.method == "POST" and not request.content
        assert request.headers["X-API-Key"] == "synthetic-integration-secret"
        assert request.headers["X-IAM-Access-Token"] == "synthetic-current-access"
        return httpx.Response(state["key_status"], json={"ok": state["key_ok"],
            "username": state["key_user"], "api_key": "synthetic-private-personal-key",
            "models": [{"model_code": models.DEFAULT_MODEL, "model_name": "Test model"}]})

    async def forbidden_mapper(account):
        pytest.fail("SSO-only must not call the directory")

    transport = httpx.MockTransport(handler)
    service = iam.IAMService(replace(config, identity_mode="sso_tokenkey"), FakeStore(),
                             forbidden_mapper, transport)
    return service, state, transport


async def sign_in(service):
    state, binding = await start(service)
    return await service.exchange("synthetic-code", state, binding)


def test_sso_binds_same_trusted_account_without_directory_or_org_grants(sso, caplog):
    async def scenario():
        service, state, _ = sso
        sid, ttl, actor = await sign_in(service)
        assert actor["user_no"] == actor["account_no"] == "M00000001"
        assert actor["organization_codes"] == [] and actor["organization_code"] == ""
        assert "admin" not in actor
        assert 600 < ttl <= 86400
        stored = json.dumps(service.store.sessions)
        assert "synthetic-current-access" not in stored
        assert "synthetic-private-personal-key" not in stored
        assert "synthetic-integration-secret" not in stored
        assert await service.authenticate(sid) == actor
        assert sum(r.url.path.endswith("/profile") for r in state["requests"]) == 1
        assert sum(r.url.host == "tokenkey.test" for r in state["requests"]) == 1
    with caplog.at_level(logging.INFO, logger="httpx"):
        asyncio.run(scenario())
    for secret in ("synthetic-current-access", "synthetic-integration-secret", "synthetic-private-personal-key"):
        assert secret not in caplog.text


@pytest.mark.parametrize("changes,code", [
    ({"key_user": "M00000002"}, "personal_key_binding_invalid_username_mismatch"),
    ({"key_user": "m00000001"}, "personal_key_binding_invalid_username_case"),
    ({"key_user": " M00000001 "}, "personal_key_binding_invalid_username_whitespace"),
    ({"key_user": " m00000001 "}, "personal_key_binding_invalid_username_case_whitespace"),
    ({"key_ok": False}, "personal_key_binding_invalid_ok_false"),
    ({"key_status": 403}, "personal_key_unavailable"),
    ({"key_status": 500}, "personal_key_service_unavailable"),
    ({"account": "../../another-user"}, "iam_employee_account_invalid"),
    ({"account": "Name With Spaces"}, "iam_employee_account_invalid"),
    ({"account": "M00000001\n"}, "iam_service_unavailable"),
])
def test_inconsistent_or_invalid_identity_never_creates_session(sso, changes, code):
    async def scenario():
        service, state, _ = sso
        state.update(changes)
        with pytest.raises(iam.IAMFailure, match="^" + code + "$"):
            await sign_in(service)
        assert not service.store.sessions
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,code", [
    ({"account": "M00000002"}, "iam_identity_changed"),
    ({"profile_status": 401}, "iam_login_rejected"),
    ({"profile_status": 403}, "iam_login_rejected"),
])
def test_iam_state_is_not_requeried_after_local_session_is_created(sso, changes, code):
    async def scenario():
        service, state, _ = sso
        sid, _, actor = await sign_in(service)
        before = len(state["requests"])
        state.update(changes)
        assert await service.authenticate(sid) == actor
        assert len(state["requests"]) == before
        assert sid in service.store.sessions
    asyncio.run(scenario())


def test_iam_outage_does_not_interrupt_established_local_session(sso):
    async def scenario():
        service, state, _ = sso
        sid, _, actor = await sign_in(service)
        state["profile_status"] = 503
        before = len(state["requests"])
        assert await service.authenticate(sid) == actor
        assert len(state["requests"]) == before
        assert sid in service.store.sessions
        state["profile_status"] = 200
        assert await service.authenticate(sid)
    asyncio.run(scenario())


def test_logout_revokes_local_authentication_and_cached_personal_credential(sso):
    async def scenario():
        service, state, _ = sso
        sid, _, actor = await sign_in(service)
        await service.store.session_delete(sid)
        before = len(state["requests"])
        assert await service.authenticate(sid) is None
        with pytest.raises(iam.IAMFailure):
            await service.personal_model_credential(sid, actor["user_no"], actor["account_no"])
        assert len(state["requests"]) == before
        assert not service.store.sessions
    asyncio.run(scenario())


def test_personal_provider_uses_sso_session_without_recursing_and_preserves_binding(sso):
    async def scenario():
        service, state, transport = sso
        sid, _, actor = await sign_in(service)
        trusted = TrustedActor(actor["user_no"], actor["account_no"], "", (), sid)
        async def credential_reader(a):
            return await service.personal_model_credential(a.auth_session_id, a.user_no, a.account_no)
        provider = models.TokenKeyProvider("https://tokenkey.test/api/external/user-credentials",
            os.environ["PERSONAL_KEY_SERVICE_SECRET_FILE"],
            transport=transport, credential_reader=credential_reader)
        credential = await provider.resolve(trusted)
        assert credential.user_no == trusted.user_no
        assert credential.models[0].code == models.DEFAULT_MODEL
        before = len(state["requests"])
        state["key_user"] = "M00000002"
        assert await provider.resolve(trusted) == credential
        assert len(state["requests"]) == before
        wrong_actor = TrustedActor("M00000002", "M00000002", "", (), sid)
        with pytest.raises(iam.IAMFailure):
            await provider.resolve(wrong_actor)
        await service.store.session_delete(sid)
        with pytest.raises(iam.IAMFailure):
            await provider.resolve(trusted)
    asyncio.run(scenario())


def test_expired_or_tampered_local_deadline_cannot_extend_session(sso):
    async def scenario():
        service, state, _ = sso
        sid, _, _ = await sign_in(service)
        service.store.sessions[sid]["expires_at"] = int(time.time()) - 1
        before = len(state["requests"])
        try:
            assert await service.authenticate(sid) is None
        except iam.IAMFailure as exc:
            assert exc.status == 401
        assert len(state["requests"]) == before
    asyncio.run(scenario())


def test_mode_requires_explicit_known_value(env_config, monkeypatch):
    assert iam.IAMConfig.from_env().identity_mode == "directory"
    monkeypatch.setenv("IAM_IDENTITY_MODE", "sso_tokenkey")
    assert iam.IAMConfig.from_env().identity_mode == "sso_tokenkey"
    monkeypatch.setenv("IAM_IDENTITY_MODE", "anything")
    with pytest.raises(iam.IAMFailure, match="iam_configuration_unavailable"):
        iam.IAMConfig.from_env()
