import asyncio
import json
import logging
import time
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import auth_middleware, iam_oauth as iam, main


class FakeStore:
    def __init__(self):
        self.states = {}
        self.sessions = {}
        self.ttls = {}

    async def state_create(self, state, binding, ttl):
        assert state not in self.states
        self.states[state] = binding

    async def state_consume(self, state, binding):
        if self.states.get(state) != binding:
            return False
        del self.states[state]
        return True

    async def session_create(self, sid, account_no, user_no, ttl, encrypted_token, expires_at):
        self.sessions[sid] = {"account_no": account_no, "user_no": user_no,
                              "iam_token_encrypted": encrypted_token, "expires_at": expires_at}
        self.ttls[sid] = ttl

    async def session_create_personal(self, sid, account_no, user_no, ttl, encrypted_credential, expires_at):
        self.sessions[sid] = {"version": 2, "account_no": account_no, "user_no": user_no,
                              "personal_credential_encrypted": encrypted_credential, "expires_at": expires_at}
        self.ttls[sid] = ttl

    async def session_read(self, sid):
        return self.sessions.get(sid)

    async def session_delete(self, sid):
        self.sessions.pop(sid, None)

    async def session_remaining_ttl(self, sid):
        return min(self.ttls[sid], int(self.sessions[sid]["expires_at"] - time.time())) if sid in self.sessions else -2


@pytest.fixture
def config(tmp_path, monkeypatch):
    secret = tmp_path / "client-secret"
    secret.write_text("synthetic-client-secret\n")
    encryption = tmp_path / "token-encryption"
    encryption.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("IAM_TOKEN_ENCRYPTION_KEY_FILE", str(encryption))
    return iam.IAMConfig(
        base_url="https://iam.test", authorization_base_url="https://iam.test",
        client_id="synthetic-client", client_secret_file=str(secret),
        redirect_uri="https://app.test/v1/iam/callback", public_origin="https://app.test",
        post_login_path="/v1/iam/session", logout_return_uri="https://app.test/v1/iam/session",
        redis_url="redis://redis.test/0",
    )


@pytest.fixture
def env_config(monkeypatch, config):
    values = {
        "IAM_BASE_URL": config.base_url,
        "IAM_CLIENT_ID": config.client_id,
        "IAM_CLIENT_SECRET_FILE": config.client_secret_file,
        "IAM_REDIRECT_URI": config.redirect_uri,
        "IAM_PUBLIC_ORIGIN": config.public_origin,
        "IAM_POST_LOGIN_PATH": config.post_login_path,
        "IAM_LOGOUT_RETURN_URI": config.logout_return_uri,
        "REDIS_URL": config.redis_url,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("IAM_DEV_MODE", raising=False)
    monkeypatch.delenv("IAM_AUTHORIZATION_BASE_URL", raising=False)
    return values


async def account_mapper(account):
    assert account == "app-account-a"
    return {"account_no": account, "user_no": "EMPLOYEE-A", "display_name": "Synthetic A",
            "organization_codes": ["DEPT-A"]}


def make_service(config, handler=None, mapper=account_mapper):
    seen = []

    def transport(request):
        seen.append(request)
        if handler:
            return handler(request)
        if request.url.path.endswith("/accessToken"):
            assert request.method == "POST"
            assert not request.url.query
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            assert form["client_secret"] == ["synthetic-client-secret"]
            assert form["grant_type"] == ["authorization_code"]
            assert form["redirect_uri"] == [config.redirect_uri]
            assert form["oauth_timestamp"][0].isdigit() and len(form["oauth_timestamp"][0]) == 13
            return httpx.Response(200, json={"access_token": "synthetic-oauth-access",
                                            "token_type": "bearer", "expires_in": 600,
                                            "refresh_token": "unused-synthetic-refresh"})
        assert request.url.path.endswith("/profile")
        assert request.method == "GET"
        assert request.url.params["access_token"] == "synthetic-oauth-access"
        return httpx.Response(200, json={"id": "not-an-employee-number", "attributes": {
            "username": "app-account-a", "id": "also-not-an-employee-number"}})

    return iam.IAMService(config, FakeStore(), mapper, httpx.MockTransport(transport)), seen


async def start(service):
    url, binding = await service.begin()
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [service.config.redirect_uri]
    assert query["response_type"] == ["code"]
    return query["state"][0], binding


def test_code_exchange_stores_encrypted_provider_token_only_in_bound_server_session(config, caplog):
    async def scenario():
        service, seen = make_service(config)
        state, binding = await start(service)
        sid, ttl, actor = await service.exchange("synthetic-code", state, binding)
        assert iam.OPAQUE.fullmatch(sid)
        assert actor["user_no"] == "EMPLOYEE-A"
        assert actor["account_no"] == "app-account-a"
        assert actor["auth_provider"] == "iam_direct"
        assert 0 < ttl <= 600
        record = service.store.sessions[sid]
        assert record["account_no"] == "app-account-a" and record["user_no"] == "EMPLOYEE-A"
        assert "synthetic-oauth-access" not in json.dumps(record)
        credential = await service.personal_access_credential(sid, "EMPLOYEE-A", "app-account-a")
        assert credential.access_token == "synthetic-oauth-access"
        assert time.time() < credential.expires_at <= time.time() + ttl
        assert "synthetic-oauth-access" not in repr(credential)
        assert await service.authenticate(sid) == actor
        assert len(seen) == 2
        return service

    with caplog.at_level(logging.INFO, logger="httpx"):
        asyncio.run(scenario())
    for value in ("synthetic-code", "synthetic-client-secret", "synthetic-oauth-access", "unused-synthetic-refresh"):
        assert value not in caplog.text


def test_wrong_browser_binding_cannot_consume_state_and_callback_is_single_use(config):
    async def scenario():
        service, seen = make_service(config)
        state, binding = await start(service)
        with pytest.raises(iam.IAMFailure, match="iam_invalid_login_state"):
            await service.exchange("synthetic-code", state, "X" * 43)
        assert not seen
        assert state in service.store.states
        await service.exchange("synthetic-code", state, binding)
        with pytest.raises(iam.IAMFailure, match="iam_invalid_login_state"):
            await service.exchange("synthetic-code", state, binding)
        assert len(seen) == 2
    asyncio.run(scenario())


def test_exchange_timeout_does_not_retry_or_reuse_authorization_code(config):
    def fail(request):
        raise httpx.ReadTimeout("synthetic-token-in-exception", request=request)

    async def scenario():
        service, seen = make_service(config, fail)
        state, binding = await start(service)
        with pytest.raises(iam.IAMFailure, match="^iam_service_unavailable$"):
            await service.exchange("synthetic-code", state, binding)
        with pytest.raises(iam.IAMFailure, match="iam_invalid_login_state"):
            await service.exchange("synthetic-code", state, binding)
        assert len(seen) == 1
        assert not service.store.sessions
    asyncio.run(scenario())


@pytest.mark.parametrize("token", [
    {"token": "synthetic-only-undocumented-envelope"},
    {"access_token": "x", "token_type": "bearer", "expires_in": True},
    {"access_token": "x", "token_type": "bearer", "expires_in": 0},
    {"access_token": "x", "token_type": "mac", "expires_in": 600},
    {"access_token": "x", "token_type": "bearer", "expires_in": "bad"},
])
def test_invalid_token_envelope_fails_closed(config, token):
    async def scenario():
        service, seen = make_service(config, lambda request: httpx.Response(200, json=token))
        state, binding = await start(service)
        with pytest.raises(iam.IAMFailure, match="iam_service_unavailable"):
            await service.exchange("synthetic-code", state, binding)
        assert len(seen) == 1
        assert not service.store.sessions
    asyncio.run(scenario())


def test_profile_id_is_never_used_as_employee_or_application_account(config):
    def handler(request):
        if request.url.path.endswith("/accessToken"):
            return httpx.Response(200, json={"access_token": "x", "token_type": "bearer", "expires_in": 600})
        return httpx.Response(200, json={"id": "EMPLOYEE-A", "attributes": {"id": "app-account-a"}})

    async def unexpected(_):
        pytest.fail("missing attributes.username must not reach directory mapper")

    async def scenario():
        service, _ = make_service(config, handler, unexpected)
        state, binding = await start(service)
        with pytest.raises(iam.IAMFailure, match="iam_service_unavailable"):
            await service.exchange("synthetic-code", state, binding)
    asyncio.run(scenario())


def test_session_rechecks_directory_and_employee_reassignment_revokes_session(config):
    async def scenario():
        service, _ = make_service(config)
        state, binding = await start(service)
        sid, _, _ = await service.exchange("synthetic-code", state, binding)

        async def denied(_):
            raise iam.IAMFailure(403, "iam_account_not_authorized")

        service.mapper = denied
        with pytest.raises(iam.IAMFailure, match="iam_account_not_authorized"):
            await service.authenticate(sid)

        async def reassigned(account):
            return {"account_no": account, "user_no": "EMPLOYEE-B", "organization_codes": ["DEPT-A"]}

        service.mapper = reassigned
        with pytest.raises(iam.IAMFailure, match="iam_identity_changed"):
            await service.authenticate(sid)
        assert sid not in service.store.sessions
        assert await service.authenticate(sid) is None
    asyncio.run(scenario())


def test_multi_department_actor_preserves_all_departments_without_guessing_primary():
    actor = iam.safe_actor({"account_no": "app-account-a", "user_no": "EMPLOYEE-A",
                           "organization_codes": ["DEPT-A", "DEPT-B", "DEPT-A"]}, "app-account-a")
    assert actor["organization_codes"] == ["DEPT-A", "DEPT-B"]
    assert actor["organization_code"] == ""
    before = SimpleNamespace(state=SimpleNamespace(user=actor))
    after = SimpleNamespace(state=SimpleNamespace(user={**actor, "organization_codes": ["DEPT-C"],
                                                       "organization_code": "DEPT-C"}))
    assert main._user_workspace_key(before) == main._user_workspace_key(after)
    other = SimpleNamespace(state=SimpleNamespace(user={**actor, "user_no": "EMPLOYEE-B"}))
    assert main._user_workspace_key(before) != main._user_workspace_key(other)


@pytest.mark.parametrize("name,value", [
    ("IAM_BASE_URL", "http://iam.megmeet.com"),
    ("IAM_BASE_URL", "https://user:password@iam.test"),
    ("IAM_BASE_URL", "https://iam.test?secret=x"),
    ("IAM_REDIRECT_URI", "https://foreign.test/v1/iam/callback"),
    ("IAM_REDIRECT_URI", "https://app.test/wrong-path"),
    ("IAM_LOGOUT_RETURN_URI", "https://foreign.test/"),
    ("IAM_POST_LOGIN_PATH", "//foreign.test/"),
    ("IAM_SESSION_TTL_SECONDS", "0"),
])
def test_configuration_rejects_unsafe_urls_and_lifetimes(env_config, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(iam.IAMFailure, match="iam_configuration_unavailable"):
        iam.IAMConfig.from_env()


def test_development_http_requires_explicit_flag_and_local_host(env_config, monkeypatch):
    monkeypatch.setenv("IAM_BASE_URL", "http://mock-company:8081")
    with pytest.raises(iam.IAMFailure):
        iam.IAMConfig.from_env()
    monkeypatch.setenv("IAM_DEV_MODE", "true")
    assert iam.IAMConfig.from_env().base_url == "http://mock-company:8081"
    monkeypatch.setenv("IAM_BASE_URL", "http://company.test")
    with pytest.raises(iam.IAMFailure):
        iam.IAMConfig.from_env()


def test_secret_requires_nonempty_regular_file_and_rejects_symlink(config, tmp_path):
    path = Path(config.client_secret_file)
    path.write_text("")
    with pytest.raises(iam.IAMFailure):
        config.secret()
    path.write_text("synthetic-secret")
    link = tmp_path / "secret-link"
    link.symlink_to(path)
    with pytest.raises(iam.IAMFailure):
        replace(config, client_secret_file=str(link)).secret()


@pytest.fixture
def client(config, monkeypatch):
    service, seen = make_service(config)

    async def with_service(action):
        return await action(service)

    async def no_legacy_call(_):
        pytest.fail("direct IAM must never fall back to legacy SSO")

    monkeypatch.setenv("AUTH_MODE", "iam_direct")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setattr(iam, "_with_service", with_service)
    monkeypatch.setattr(auth_middleware, "query_user", no_legacy_call)
    app = FastAPI()
    app.include_router(iam.router)
    app.add_middleware(auth_middleware.PlatformAuthMiddleware)

    @app.get("/protected")
    async def protected(request: Request):
        return {"user_no": request.state.user["user_no"]}

    with TestClient(app, base_url="https://app.test", follow_redirects=False) as instance:
        yield instance, service, seen


def login_browser(client):
    browser, service, seen = client
    result = browser.get(iam.LOGIN_PATH)
    assert result.status_code == 303
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "Secure" in result.headers["set-cookie"]
    state = parse_qs(urlsplit(result.headers["location"]).query)["state"][0]
    result = browser.get(iam.CALLBACK_PATH, params={"code": "synthetic-code", "state": state})
    assert result.status_code == 303
    assert result.headers["location"] == "https://app.test/v1/iam/session"
    return result


def test_browser_round_trip_enforces_auth_and_has_no_token_exposure(client):
    browser, service, _ = client
    assert browser.get("/protected").status_code == 401
    assert browser.get(iam.SESSION_PATH).json() == {"authenticated": False, "user": None}
    result = login_browser(client)
    serialized = str(result.headers) + result.text + json.dumps(service.store.sessions)
    for value in ("synthetic-code", "synthetic-client-secret", "synthetic-oauth-access", "unused-synthetic-refresh"):
        assert value not in serialized
    assert browser.get("/protected").json() == {"user_no": "EMPLOYEE-A"}
    session = browser.get(iam.SESSION_PATH)
    assert session.json()["user"]["account_no"] == "app-account-a"
    assert session.headers["cache-control"] == "no-store"


def test_logout_requires_same_origin_and_deletes_local_session(client):
    browser, service, _ = client
    login_browser(client)
    assert browser.post(iam.LOGOUT_PATH).status_code == 403
    assert browser.post(iam.LOGOUT_PATH, headers={"Origin": "https://foreign.test"}).status_code == 403
    assert len(service.store.sessions) == 1
    result = browser.post(iam.LOGOUT_PATH, headers={"Origin": "https://app.test"})
    assert result.status_code == 200
    target = urlsplit(result.json()["logout_url"])
    assert target.netloc == "iam.test"
    assert target.path == "/esc-sso/logout"
    assert parse_qs(target.query) == {"service": ["https://app.test/v1/iam/session"]}
    assert not service.store.sessions
    assert browser.get("/protected").status_code == 401


def test_callback_duplicate_state_rejected_before_code_exchange(client):
    browser, service, seen = client
    result = browser.get(iam.LOGIN_PATH)
    state = parse_qs(urlsplit(result.headers["location"]).query)["state"][0]
    result = browser.get(iam.CALLBACK_PATH, params=[("code", "synthetic-code"), ("state", state), ("state", state)])
    assert result.status_code == 400
    assert not seen
    assert not service.store.sessions


def test_unknown_auth_mode_is_closed(client, monkeypatch):
    browser, _, _ = client
    monkeypatch.setenv("AUTH_MODE", "misspelled-provider")
    assert browser.get("/protected").status_code == 503


def test_unset_mode_defaults_to_direct_iam_and_rejects_legacy_cookie(client, monkeypatch):
    browser, _, seen = client
    monkeypatch.delenv("AUTH_MODE", raising=False)
    # The fixture's legacy query_user raises if invoked. An unrelated old cookie
    # must neither call that provider nor disable the default IAM requirement.
    browser.cookies.set("synthetic_legacy_sso", "synthetic-old-cookie")
    assert browser.get("/protected").status_code == 401
    assert browser.get(iam.SESSION_PATH).json() == {"authenticated": False, "user": None}
    assert browser.get(iam.LOGIN_PATH).status_code == 303
    assert not seen


@pytest.mark.parametrize("error_class,status,code", [
    ("DirectoryUnavailable", 503, "iam_directory_unavailable"),
    ("AccountUnknown", 403, "iam_account_not_authorized"),
    ("AccountDisabled", 403, "iam_account_not_authorized"),
    ("AccountAmbiguous", 403, "iam_account_not_authorized"),
])
def test_directory_failures_deny_session_without_exposing_error(monkeypatch, error_class, status, code):
    from app import iam_directory

    async def failed(_):
        raise getattr(iam_directory, error_class)("synthetic-private-directory-detail")

    monkeypatch.setattr(iam_directory, "resolve_account", failed)
    with pytest.raises(iam.IAMFailure) as caught:
        asyncio.run(iam.directory_actor("app-account-a"))
    assert caught.value.status == status
    assert str(caught.value) == code


def test_unavailable_session_store_is_sanitized_and_has_no_auth_fallback(env_config, monkeypatch):
    def failed(_):
        raise ConnectionError("synthetic-private-redis-detail")

    monkeypatch.setenv("AUTH_MODE", "iam_direct")
    monkeypatch.setattr(iam, "RedisIAMStore", failed)
    app = FastAPI()
    app.include_router(iam.router)
    with TestClient(app) as browser:
        response = browser.get(iam.LOGIN_PATH)
    assert response.status_code == 503
    assert response.json() == {"detail": "iam_session_store_unavailable"}
    assert "synthetic-private" not in response.text


def test_second_login_rotates_prior_session(client):
    browser, service, _ = client
    login_browser(client)
    previous = next(iter(service.store.sessions))
    login_browser(client)
    assert previous not in service.store.sessions
    assert len(service.store.sessions) == 1


def test_expired_provider_lease_cannot_create_session(config, monkeypatch):
    # The profile/directory lookup must not extend the documented token lease.
    times = iter([100.0, 701.0])
    monkeypatch.setattr(iam, "monotonic", lambda: next(times))
    async def scenario():
        service, _ = make_service(config)
        state, binding = await start(service)
        with pytest.raises(iam.IAMFailure, match="iam_login_expired"):
            await service.exchange("synthetic-code", state, binding)
        assert not service.store.sessions
    asyncio.run(scenario())


def test_current_iam_credential_is_bound_encrypted_revocable_and_capped_by_session_ttl(config):
    async def scenario():
        service, _ = make_service(config)
        state, binding = await start(service)
        sid, _, _ = await service.exchange("synthetic-code", state, binding)
        record = service.store.sessions[sid]
        assert "synthetic-oauth-access" not in json.dumps(record)
        service.store.ttls[sid] = 30
        credential = await service.personal_access_credential(sid, "EMPLOYEE-A", "app-account-a")
        assert time.time() < credential.expires_at <= time.time() + 30
        with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
            await service.personal_access_credential(sid, "EMPLOYEE-B", "app-account-a")
        # Copying a valid cipher to another session is not a token transfer.
        other_sid = "o" * 43
        service.store.sessions[other_sid] = dict(record)
        service.store.ttls[other_sid] = 30
        with pytest.raises(iam.IAMFailure, match="iam_personal_credentials_require_login"):
            await service.personal_access_credential(other_sid, "EMPLOYEE-A", "app-account-a")
        # A Redis deletion (including logout) makes the server-held token unusable.
        await service.store.session_delete(sid)
        with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
            await service.personal_access_credential(sid, "EMPLOYEE-A", "app-account-a")
    asyncio.run(scenario())


@pytest.mark.parametrize("mutate", ["old", "tampered", "expired"])
def test_missing_tampered_or_expired_iam_credential_requires_login(config, mutate):
    async def scenario():
        service, _ = make_service(config)
        state, binding = await start(service)
        sid, _, _ = await service.exchange("synthetic-code", state, binding)
        record = service.store.sessions[sid]
        if mutate == "old":
            record.pop("iam_token_encrypted")
            record.pop("expires_at")
        elif mutate == "tampered":
            record["iam_token_encrypted"] = "not-a-fernet-cipher"
        else:
            record["expires_at"] = 1
        with pytest.raises(iam.IAMFailure, match="iam_personal_credentials_require_login"):
            await service.personal_access_credential(sid, "EMPLOYEE-A", "app-account-a")
    asyncio.run(scenario())


def test_encryption_secret_is_mandatory_and_no_plaintext_session_fallback(config, monkeypatch):
    monkeypatch.delenv("IAM_TOKEN_ENCRYPTION_KEY_FILE")
    async def scenario():
        service, _ = make_service(config)
        state, binding = await start(service)
        with pytest.raises(iam.IAMFailure, match="iam_token_encryption_unavailable"):
            await service.exchange("synthetic-code", state, binding)
        assert not service.store.sessions
    asyncio.run(scenario())
