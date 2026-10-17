import asyncio
import importlib.util
import json
from pathlib import Path
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
import pytest

from test_desktop_auth import desktop_env, login_desktop
from test_iam_oauth import config, env_config
from app import personal_model_proxy as model, desktop_auth as desktop, iam_oauth as iam


@pytest.fixture
def mock_company():
    file = Path(__file__).parents[3] / "dev-local" / "mock_company.py"
    spec = importlib.util.spec_from_file_location("personal_model_mock", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield module, "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.fixture
def broker_env(monkeypatch, tmp_path, mock_company):
    module, url = mock_company
    file = tmp_path / "personal-key-service"
    file.write_text(module.PERSONAL_KEY_SERVICE_SECRET)
    monkeypatch.setenv("IAM_DEV_MODE", "true")
    monkeypatch.setenv("PERSONAL_KEY_ADAPTER", "synthetic")
    monkeypatch.setenv("PERSONAL_KEY_SYNTHETIC_URL", url + "/__synthetic/personal-key")
    monkeypatch.setenv("PERSONAL_KEY_SERVICE_SECRET_FILE", str(file))
    monkeypatch.setenv("PERSONAL_MODEL_BASE_URL", url + "/personal-model/v1")
    monkeypatch.delenv("PERSONAL_MODEL_ALLOWED_MODELS", raising=False)
    return module, url


async def unchanged(actor):
    return actor


def actor(user):
    return desktop.TrustedActor("LOCAL-FIXTURE-" + user, "app-" + user.lower(), user, ("LOCAL-TEST",), "s" * 43)


def test_real_synthetic_key_service_and_model_use_separate_employee_keys(broker_env, caplog):
    module, url = broker_env
    broker = model.PersonalModelBroker(model.provider_from_env(), url + "/personal-model/v1", actor_validator=unchanged)

    async def scenario():
        replies = await asyncio.gather(*(broker.complete(actor(user), {"model": model.DEFAULT_MODEL,
            "messages": [{"role": "user", "content": "identity-bound-model-check"}]}) for user in ("A", "B")))
        for user, reply in zip(("A", "B"), replies):
            text = reply["choices"][0]["message"]["content"]
            assert text == "PERSONAL_MODEL_OK[LOCAL-FIXTURE-" + user + "]: identity-bound-model-check"
        return replies
    replies = asyncio.run(scenario())
    assert len(set(module.PERSONAL_MODEL_KEYS.values())) == 2
    for key in (*module.PERSONAL_MODEL_KEYS.values(), module.PERSONAL_KEY_SERVICE_SECRET):
        assert key not in json.dumps(replies)
        assert key not in caplog.text


def test_gateway_personal_chat_keeps_identity_and_never_calls_history_db(desktop_env, broker_env, monkeypatch):
    client, service, _ = desktop_env
    module, _ = broker_env

    async def mapping(account):
        return {"account_no": account, "user_no": "LOCAL-FIXTURE-A", "display_name": "A",
                "organization_codes": ["LOCAL-TEST"]}
    service.mapper = mapping
    from app import history_store
    monkeypatch.setattr(history_store, "_connect", lambda: pytest.fail("local model body must never enter history DB"))
    grant, _ = login_desktop(client)
    headers = {"Authorization": "Bearer " + grant["access_token"], "X-Employee-No": "LOCAL-FIXTURE-B"}
    body = {"model": model.DEFAULT_MODEL, "messages": [{"role": "user", "content": "local-only-body"}]}
    response = client.post("/v1/personal-model/chat/completions", json=body, headers=headers)
    assert response.status_code == 200
    assert "PERSONAL_MODEL_OK[LOCAL-FIXTURE-A]" in response.text
    assert "LOCAL-FIXTURE-B" not in response.text
    for key in module.PERSONAL_MODEL_KEYS.values():
        assert key not in response.text
    stream = client.post("/v1/personal-model/chat/completions", json={**body, "stream": True}, headers=headers)
    assert stream.status_code == 200
    assert "text/event-stream" in stream.headers["content-type"]
    assert "PERSONAL_MODEL_OK[LOCAL-FIXTURE-A]" in stream.text and "[DONE]" in stream.text
    assert client.post("/v1/personal-model/chat/completions", json={**body, "user_no": "LOCAL-FIXTURE-B"}, headers=headers).status_code == 400
    client.post(desktop.PREFIX + "/logout", headers=headers)
    assert client.post("/v1/personal-model/chat/completions", json=body, headers=headers).status_code == 401


def test_missing_company_contract_fails_closed_without_shared_key(monkeypatch):
    monkeypatch.setenv("PERSONAL_KEY_ADAPTER", "unconfigured")
    monkeypatch.setenv("TOKENKEY_API_KEY", "synthetic-shared-key-must-not-be-used")
    with pytest.raises(iam.IAMFailure, match="personal_key_provider_unconfigured"):
        asyncio.run(model.provider_from_env().resolve(actor("A")))


def test_synthetic_adapter_cannot_be_enabled_in_production(monkeypatch):
    monkeypatch.delenv("IAM_DEV_MODE", raising=False)
    with pytest.raises(iam.IAMFailure, match="configuration_unavailable"):
        model.SyntheticKeyProvider("http://mock-company:8081/__synthetic/personal-key", "unused")
    monkeypatch.setenv("IAM_DEV_MODE", "true")
    with pytest.raises(iam.IAMFailure, match="configuration_unavailable"):
        model.SyntheticKeyProvider("https://company.example/key", "unused")


@pytest.mark.parametrize("credential", [
    model.PersonalCredential("LOCAL-FIXTURE-B", "synthetic-B", time.time() + 60),
    model.PersonalCredential("LOCAL-FIXTURE-A", "synthetic-A", 0),
    model.PersonalCredential("LOCAL-FIXTURE-A", "bad\nheader", time.time() + 60),
])
def test_wrong_owner_or_invalid_key_never_reaches_model(credential):
    class WrongProvider:
        async def resolve(self, current):
            return credential

    def unexpected(request):
        pytest.fail("invalid employee credential must not reach upstream")
    broker = model.PersonalModelBroker(WrongProvider(), "https://model.test/v1",
                                       httpx.MockTransport(unexpected), unchanged)
    with pytest.raises(iam.IAMFailure, match="personal_key_binding_invalid"):
        asyncio.run(broker.complete(actor("A"), {"model": model.DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": "a"}]}))


def test_revocation_during_key_lookup_prevents_model_call():
    calls = []

    async def checked(value):
        calls.append("validate")
        if len(calls) > 1:
            raise iam.IAMFailure(401, "desktop_session_expired")
        return value
    class Provider:
        async def resolve(self, current):
            return model.PersonalCredential(current.user_no, "synthetic-A", time.time() + 60)
    broker = model.PersonalModelBroker(Provider(), "https://model.test/v1",
        httpx.MockTransport(lambda request: pytest.fail("revoked identity submitted model request")), checked)
    with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
        asyncio.run(broker.complete(actor("A"), {"model": model.DEFAULT_MODEL,
            "messages": [{"role": "user", "content": "a"}]}))


def test_model_failure_never_echoes_upstream_secret_or_retries():
    calls = []
    class Provider:
        async def resolve(self, current):
            return model.PersonalCredential(current.user_no, "synthetic-A", time.time() + 60)
    def fail(request):
        calls.append(request)
        return httpx.Response(500, json={"error": "secret=synthetic-A body=private"})
    broker = model.PersonalModelBroker(Provider(), "https://model.test/v1", httpx.MockTransport(fail), unchanged)
    with pytest.raises(iam.IAMFailure, match="^personal_model_upstream_unavailable$"):
        asyncio.run(broker.complete(actor("A"), {"model": model.DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": "private"}]}))
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_personal_key_rejection_requires_new_login_without_credential_retry(status):
    calls = []
    class Provider:
        async def resolve(self, current):
            return model.PersonalCredential(current.user_no, "synthetic-A", time.time() + 86400)
    def fail(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "synthetic-A-private-upstream-body"})
    broker = model.PersonalModelBroker(Provider(), "https://model.test/v1", httpx.MockTransport(fail), unchanged)
    with pytest.raises(iam.IAMFailure, match="^personal_key_reauthorization_required$") as error:
        asyncio.run(broker.complete(actor("A"), {"model": model.DEFAULT_MODEL,
            "messages": [{"role": "user", "content": "private"}]}))
    assert error.value.status == 401 and len(calls) == 1


def company_value():
    return {"ok": True, "username": "LOCAL-FIXTURE-A", "iam_user_name": "Display name is not identity",
            "api_key": "tk-synthetic-A", "models": [{"model_code": "glm-5.3-flash", "model_name": "GLM-5.3-Flash"}]}


def company_provider(tmp_path, handler):
    secret = tmp_path / "company-integration-secret"
    secret.write_text("synthetic-integration-secret")

    async def current_access(current):
        assert isinstance(current, desktop.TrustedActor)
        assert current.account_no != current.user_no
        return iam.IAMAccessCredential("synthetic-current-iam-access", int(time.time()) + 90)
    return model.TokenKeyProvider("https://tokenkey.test/api/external/user-credentials", str(secret),
                                  httpx.MockTransport(handler), current_access)


def test_default_provider_preserves_explicit_directory_mode_without_sso_cache_fallback(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []
    async def access(sid, user_no, account_no):
        calls.append((sid, user_no, account_no))
        return iam.IAMAccessCredential("synthetic-current-iam-access", int(time.time()) + 90)
    async def forbidden(*args):
        pytest.fail("Legacy directory mode must use its own existing access contract")
    service = SimpleNamespace(config=SimpleNamespace(identity_mode="directory"),
        personal_access_credential=access, personal_model_credential=forbidden)
    async def run(action):
        return await action(service)
    monkeypatch.setattr(iam, "_with_service", run)
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json=company_value()))
    provider.access_reader = None
    credential = asyncio.run(provider.resolve(actor("A")))
    assert credential.user_no == actor("A").user_no
    assert calls == [(actor("A").auth_session_id, actor("A").user_no, actor("A").account_no)]


def test_company_contract_uses_server_headers_no_identity_body_and_returns_only_permitted_models(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("PERSONAL_MODEL_ALLOWED_MODELS", raising=False)
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "POST" and not request.url.query and not request.content
        assert request.headers["X-API-Key"] == "synthetic-integration-secret"
        assert request.headers["X-IAM-Access-Token"] == "synthetic-current-iam-access"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=company_value())
    provider = company_provider(tmp_path, respond)
    broker = model.PersonalModelBroker(provider, "https://model.test/v1", actor_validator=unchanged)
    catalog = asyncio.run(broker.models(actor("A")))
    assert catalog == {"object": "list", "data": [{"id": "glm-5.3-flash", "name": "GLM-5.3-Flash",
        "object": "model", "owned_by": "megmeet"}], "default_model": "glm-5.3-flash"}
    for value in ("tk-synthetic-A", "synthetic-current-iam-access", "synthetic-integration-secret"):
        assert value not in json.dumps(catalog) + caplog.text
    assert len(seen) == 1


@pytest.mark.parametrize("change", [
    {"ok": 1}, {"ok": False}, {"username": "LOCAL-FIXTURE-B"}, {"username": "app-a"},
    {"api_key": ""}, {"api_key": "bad\nheader"}, {"api_key": "  "},
    {"models": None}, {"models": [{"id": "guessed-model"}]},
    {"models": [{"model_code": "glm-5.3-flash", "model_name": ""}]},
    {"models": [{"model_code": "bad\nmodel", "model_name": "Bad"}]},
    {"models": [{"model_code": "duplicate", "model_name": "A"}, {"model_code": "duplicate", "model_name": "B"}]},
])
def test_company_malformed_or_wrong_employee_credentials_fail_closed(tmp_path, change):
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json={**company_value(), **change}))
    with pytest.raises(iam.IAMFailure):
        asyncio.run(provider.resolve(actor("A")))


@pytest.mark.parametrize("change,missing,reason", [
    ({}, "ok", "ok_missing"),
    ({"ok": 1}, None, "ok_type"),
    ({"ok": "private-upstream-sentinel"}, None, "ok_type"),
    ({"ok": False}, None, "ok_false"),
    ({}, "username", "username_missing"),
    ({"username": None}, None, "username_missing"),
    ({"username": ""}, None, "username_missing"),
    ({"username": ["private-upstream-sentinel"]}, None, "username_type"),
    ({"username": "private-upstream-sentinel"}, None, "username_mismatch"),
    ({"username": "local-fixture-a"}, None, "username_case"),
    ({"username": " LOCAL-FIXTURE-A\n"}, None, "username_whitespace"),
    ({"username": " local-fixture-a\t"}, None, "username_case_whitespace"),
    ({}, "api_key", "key_missing"),
    ({"api_key": None}, None, "key_missing"),
    ({"api_key": {"private": "private-upstream-sentinel"}}, None, "key_type"),
    ({"api_key": ""}, None, "key_empty"),
    ({"api_key": "k" * 8193}, None, "key_length"),
    ({"api_key": " private-upstream-sentinel "}, None, "key_whitespace"),
    ({"api_key": "private-upstream\x00sentinel"}, None, "key_control"),
])
def test_company_binding_reasons_are_fixed_and_credential_free(tmp_path, caplog, change, missing, reason):
    value = {**company_value(), **change, "unexpected": "private-upstream-sentinel"}
    if missing:
        del value[missing]
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json=value))
    with pytest.raises(iam.IAMFailure) as caught:
        asyncio.run(provider.resolve(actor("A")))
    assert caught.value.status == 503
    assert caught.value.code == "personal_key_binding_invalid_" + reason
    assert str(caught.value) == caught.value.code
    for secret in ("private-upstream-sentinel", "tk-synthetic-A", "synthetic-current-iam-access",
                   "synthetic-integration-secret", "LOCAL-FIXTURE-A"):
        assert secret not in str(caught.value) + caplog.text


@pytest.mark.parametrize("value,code", [
    (["private-upstream-sentinel"], "personal_key_binding_invalid_response_shape"),
    ({"ok": False, "username": None, "api_key": None, "models": None},
     "personal_key_binding_invalid_ok_false"),
    ({**company_value(), "username": "wrong", "api_key": None, "models": None},
     "personal_key_binding_invalid_username_mismatch"),
    ({**company_value(), "api_key": None, "models": None}, "personal_key_service_unavailable"),
])
def test_company_binding_checks_keep_original_failure_precedence(tmp_path, value, code):
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json=value))
    with pytest.raises(iam.IAMFailure) as caught:
        asyncio.run(provider.resolve(actor("A")))
    assert (caught.value.status, caught.value.code) == (503, code)


def test_company_iam_lease_expiring_during_request_has_fixed_reason(tmp_path, monkeypatch):
    now = [1000]
    monkeypatch.setattr(model.time, "time", lambda: now[0])

    def respond(request):
        now[0] = 1002
        return httpx.Response(200, json=company_value())

    provider = company_provider(tmp_path, respond)
    with pytest.raises(iam.IAMFailure) as caught:
        asyncio.run(provider.resolve_access(actor("A").user_no,
            iam.IAMAccessCredential("synthetic-current-iam-access", 1001)))
    assert (caught.value.status, caught.value.code) == (503, "personal_key_binding_invalid_lease_expired")


@pytest.mark.parametrize("kind,reason", [
    ("wrong_type", "credential_type"), ("wrong_user", "username_mismatch"),
    ("key_and_lease", "key_empty"), ("lease_bool", "lease_type"),
    ("lease_nan", "lease_nonfinite"), ("lease_expired", "lease_expired"),
    ("catalog_list", "model_catalog"), ("catalog_duplicate", "model_catalog"),
])
def test_generic_credential_callers_keep_error_contract_and_order(kind, reason):
    future = time.time() + 60
    credential = {
        "wrong_type": None,
        "wrong_user": model.PersonalCredential("OTHER", "", 0),
        "key_and_lease": model.PersonalCredential("A", "", 0),
        "lease_bool": model.PersonalCredential("A", "synthetic-key", True),
        "lease_nan": model.PersonalCredential("A", "synthetic-key", float("nan")),
        "lease_expired": model.PersonalCredential("A", "synthetic-key", 0),
        "catalog_list": model.PersonalCredential("A", "synthetic-key", future, []),
        "catalog_duplicate": model.PersonalCredential("A", "synthetic-key", future,
            (model.PersonalModel("same", "One"), model.PersonalModel("same", "Two"))),
    }[kind]
    for detailed, suffix in ((False, ""), (True, "_" + reason)):
        with pytest.raises(iam.IAMFailure) as caught:
            model.validate_credential(credential, "A", binding_diagnostics=detailed)
        assert (caught.value.status, caught.value.code) == (503, "personal_key_binding_invalid" + suffix)


def test_diagnostics_do_not_add_key_format_or_catalog_restrictions(tmp_path):
    # Internal tabs and empty entitlements were accepted before this diagnostic change.
    value = {**company_value(), "api_key": "synthetic\tkey", "models": []}
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json=value))
    credential = asyncio.run(provider.resolve(actor("A")))
    assert credential.api_key == value["api_key"] and credential.models == ()
    model.validate_credential(credential, actor("A").user_no)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 429, 500])
def test_company_credential_failure_and_redirect_are_not_followed_or_echoed(tmp_path, status):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": "https://foreign.test/steal"},
                              json={"error": "synthetic-private-credentials"})
    provider = company_provider(tmp_path, respond)
    with pytest.raises(iam.IAMFailure) as caught:
        asyncio.run(provider.resolve(actor("A")))
    assert len(seen) == 1 and "synthetic-private" not in str(caught.value)


def test_company_tls_downgrade_and_missing_secret_are_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("IAM_DEV_MODE", raising=False)
    with pytest.raises(iam.IAMFailure, match="configuration_unavailable"):
        model.TokenKeyProvider("http://tokenkey.megmeet.com/api/external/user-credentials", "unused")
    provider = company_provider(tmp_path, lambda request: pytest.fail("missing secret reached company API"))
    provider.secret_file = str(tmp_path / "missing")
    with pytest.raises(iam.IAMFailure, match="personal_key_configuration_unavailable"):
        asyncio.run(provider.resolve(actor("A")))


def test_company_empty_catalog_is_no_entitlement_and_never_defaults_to_shared_model(tmp_path, monkeypatch):
    monkeypatch.delenv("PERSONAL_MODEL_ALLOWED_MODELS", raising=False)
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json={**company_value(), "models": []}))
    broker = model.PersonalModelBroker(provider, "https://model.test/v1",
        httpx.MockTransport(lambda request: pytest.fail("empty catalog must never submit model call")), unchanged)
    assert asyncio.run(broker.models(actor("A"))) == {"object": "list", "data": [], "default_model": None}
    with pytest.raises(iam.IAMFailure, match="personal_model_not_authorized"):
        asyncio.run(broker.complete(actor("A"), {"model": model.DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": "hello"}]}))


def test_global_model_allowlist_intersects_personal_catalog_and_both_paths_enforce_it(tmp_path, monkeypatch):
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json=company_value()))
    model_calls = []
    def reply(request):
        model_calls.append(request)
        assert request.headers["Authorization"] == "Bearer tk-synthetic-A"
        assert json.loads(request.content)["model"] == "glm-5.3-flash"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    broker = model.PersonalModelBroker(provider, "https://model.test/v1", httpx.MockTransport(reply), unchanged)
    monkeypatch.setattr(model, "get_broker", lambda: broker)
    monkeypatch.delenv("PERSONAL_MODEL_ALLOWED_MODELS", raising=False)
    body = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "private"}]}
    asyncio.run(broker.complete(actor("A"), body))
    assert asyncio.run(model.cloud_chat_complete(actor("A"), body["messages"], "glm-5.3-flash", "request"))["text"] == "ok"
    for execute in (lambda: broker.complete(actor("A"), {**body, "model": model.DEFAULT_MODEL}),
                    lambda: model.cloud_chat_complete(actor("A"), body["messages"], model.DEFAULT_MODEL, "request")):
        with pytest.raises(iam.IAMFailure, match="personal_model_not_authorized"):
            asyncio.run(execute())
    assert len(model_calls) == 2
    monkeypatch.setenv("PERSONAL_MODEL_ALLOWED_MODELS", model.DEFAULT_MODEL)
    assert asyncio.run(broker.models(actor("A"))) == {"object": "list", "data": [], "default_model": None}
    with pytest.raises(iam.IAMFailure, match="personal_model_not_allowed"):
        asyncio.run(broker.complete(actor("A"), body))
    assert len(model_calls) == 2


def test_company_provider_uses_only_current_bound_iam_session(desktop_env, tmp_path):
    client, service, _ = desktop_env
    grant, _ = login_desktop(client)
    sid = next(iter(service.store.sessions))
    current = desktop.TrustedActor("EMPLOYEE-A", "app-account-a", "A", ("DEPT-A",), sid)
    secret = tmp_path / "company-secret"
    secret.write_text("synthetic-integration-secret")
    seen = []
    def respond(request):
        seen.append(request)
        assert request.headers["X-IAM-Access-Token"] == "synthetic-oauth-access"
        return httpx.Response(200, json={**company_value(), "username": "EMPLOYEE-A"})
    provider = model.TokenKeyProvider("https://tokenkey.test/api/external/user-credentials", str(secret),
                                      httpx.MockTransport(respond))
    credential = asyncio.run(provider.resolve(current))
    assert credential.user_no == "EMPLOYEE-A" and credential.expires_at <= service.store.sessions[sid]["expires_at"]
    client.post(desktop.PREFIX + "/logout", headers={"Authorization": "Bearer " + grant["access_token"]})
    with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
        asyncio.run(provider.resolve(current))
    assert len(seen) == 1


def test_contract_compatible_mock_cloud_response_is_explicitly_simulated(tmp_path, monkeypatch):
    monkeypatch.setenv("IAM_DEV_MODE", "true")
    monkeypatch.setenv("PERSONAL_MODEL_SIMULATION", "true")
    monkeypatch.delenv("PERSONAL_MODEL_ALLOWED_MODELS", raising=False)
    provider = company_provider(tmp_path, lambda request: httpx.Response(200, json=company_value()))
    broker = model.PersonalModelBroker(provider, "http://mock-company:8081/personal-model/v1",
        httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "mock"}}]})), unchanged)
    monkeypatch.setattr(model, "get_broker", lambda: broker)
    value = asyncio.run(model.cloud_chat_complete(actor("A"), [{"role": "user", "content": "a"}], "glm-5.3-flash", "r"))
    assert value["simulation"] is True


@pytest.mark.parametrize("dev,url", [
    ("false", "https://tokenkey.megmeet.com/v1"),
    ("false", "https://localhost/v1"),
    ("true", "https://tokenkey.megmeet.com/v1"),
])
def test_simulation_flag_cannot_mark_company_or_production_service_as_mock(monkeypatch, dev, url):
    monkeypatch.setenv("IAM_DEV_MODE", dev)
    monkeypatch.setenv("PERSONAL_MODEL_SIMULATION", "true")
    with pytest.raises(iam.IAMFailure, match="personal_model_configuration_unavailable"):
        model.PersonalModelBroker(model.UnconfiguredKeyProvider(), url)


def test_production_simulation_flag_defaults_false(monkeypatch):
    monkeypatch.delenv("PERSONAL_MODEL_SIMULATION", raising=False)
    monkeypatch.delenv("IAM_DEV_MODE", raising=False)
    broker = model.PersonalModelBroker(model.UnconfiguredKeyProvider(), "https://tokenkey.megmeet.com/v1")
    assert broker.simulation is False
