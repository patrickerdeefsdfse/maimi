"""Read-only identity status and denied browser entry, with synthetic SSO only."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
from entry_app import Config, create_app  # noqa: E402


PUBLIC = "https://platform-ai.megmeet.com"


@pytest.fixture
def pilot():
    config = Config.parse({
        "public_origin": PUBLIC,
        "sso_url": "https://sso.identity-test.invalid/query-user",
        "tokenkey_url": "https://model.identity-test.invalid/v1",
        "tokenkey_secret_file": "/run/secrets/not-supplied",
        "bindings": [{
            "user_id": str(uuid4()), "user_no": f"PRIVATE-EMPLOYEE-{index}",
            "upstream": f"http://personal-{index}:3084",
            "bootstrap": f"/run/megmeet-bootstrap/{index}/ready.json",
            "model_token_file": f"/run/megmeet-entry/{index}-model-token",
        } for index in range(2)],
    })
    files, calls = {}, []
    for index, binding in enumerate(config.bindings):
        files[binding.model_token_file] = (str(index) * 48).encode()
        files[binding.bootstrap] = json.dumps({
            "ready": True, "owner_id": binding.user_id, "generation": str(uuid4()),
            "workspace_id": str(uuid4()), "cookie": f"dsh_session=PRIVATE-HOST-COOKIE-{index}",
        }).encode()

    def read(path, limit):
        body = files[path]
        assert len(body) <= limit
        return body

    def handle(request):
        calls.append(request)
        if str(request.url) == config.sso_url:
            for index, binding in enumerate(config.bindings):
                if request.headers.get("Cookie") == f"access_token=PRIVATE-SSO-COOKIE-{index}":
                    return httpx.Response(200, json={"code": 0, "data": {"user_no": binding.user_no}})
            return httpx.Response(403)
        return httpx.Response(200, content=b"native page unchanged")

    return config, files, calls, read, handle


def client(pilot, **kwargs):
    config, _files, _calls, read, handle = pilot
    return TestClient(create_app(config, reader=read, transport=httpx.MockTransport(handle), **kwargs))


@pytest.mark.parametrize("index", [0, 1])
def test_identity_returns_only_authenticated_opaque_owner_and_default_workspace(pilot, index):
    config, files, calls, _read, _handle = pilot
    binding, other = config.bindings[index], config.bindings[1-index]
    bootstrap = json.loads(files[binding.bootstrap])
    with client(pilot) as browser:
        result = browser.get("/__megmeet/identity?owner_id=" + other.user_id, headers={
            "Cookie": f"access_token=PRIVATE-SSO-COOKIE-{index}; dsh_session=FOREIGN",
            "X-User-ID": other.user_id, "X-User-No": other.user_no, "Origin": PUBLIC,
        })
    assert result.status_code == 200
    assert result.json() == {"authenticated": True, "user_id": binding.user_id,
                             "workspace_id": bootstrap["workspace_id"]}
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["vary"] == "Cookie"
    assert "set-cookie" not in result.headers
    assert len(calls) == 1 and str(calls[0].url) == config.sso_url
    for private in (binding.user_no, bootstrap["cookie"], "PRIVATE-SSO-COOKIE", binding.upstream,
                    binding.bootstrap, bootstrap["generation"]):
        assert private not in result.text


def test_identity_requires_auth_even_with_html_accept_and_refuses_foreign_origin(pilot):
    with client(pilot) as browser:
        anonymous = browser.get("/__megmeet/identity", headers={"Accept": "text/html"})
        assert anonymous.status_code == 403 and anonymous.headers["content-type"].startswith("application/json")
        forged = browser.get("/__megmeet/identity", headers={
            "Cookie": "access_token=PRIVATE-SSO-COOKIE-0", "Origin": "https://foreign.invalid",
        })
        assert forged.status_code == 403
        assert pilot[2] == []


def test_identity_does_not_report_foreign_or_unready_workspace(pilot):
    binding, other = pilot[0].bindings
    value = json.loads(pilot[1][binding.bootstrap])
    value["owner_id"] = other.user_id
    pilot[1][binding.bootstrap] = json.dumps(value).encode()
    with client(pilot) as browser:
        result = browser.get("/__megmeet/identity", headers={"Cookie": "access_token=PRIVATE-SSO-COOKIE-0"})
    assert result.status_code == 503
    assert "user_id" not in result.text and "workspace_id" not in result.text


def test_identity_sso_outage_is_generic_503(pilot):
    config, _files, _calls, read, _handle = pilot
    transport = httpx.MockTransport(lambda _: httpx.Response(500, text="PRIVATE-SSO-ERROR"))
    with TestClient(create_app(config, reader=read, transport=transport)) as browser:
        result = browser.get("/__megmeet/identity", headers={"Cookie": "access_token=PRIVATE-SSO-COOKIE-0"})
    assert result.status_code == 503
    assert result.json() == {"detail": "authentication service unavailable"}


def test_cold_html_navigation_gets_actionable_denied_page_with_fixed_company_login(pilot):
    with client(pilot) as browser:
        result = browser.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert result.status_code == 403
    assert result.headers["content-type"].startswith("text/html")
    assert "请先登录公司门户" in result.text and "重试登录检查" in result.text
    assert "MEGMEET_ENTRY_LOGIN_ONLY_V1" in result.text
    assert '<script type="module" src="https://platform-ai.megmeet.com/megmeet-ai-native/__megmeet/login.js"></script>' in result.text
    assert "access_token" not in result.text
    assert "location" not in result.headers and "set-cookie" not in result.headers
    assert result.headers["cache-control"] == "private, no-store"
    assert "Accept" in result.headers["vary"]
    assert pilot[2] == []


@pytest.mark.parametrize("accept", ["application/json", "*/*", "text/html;q=0", "text/html;q=invalid"])
def test_root_without_explicit_html_accept_keeps_json_denial(pilot, accept):
    with client(pilot) as browser:
        result = browser.get("/", headers={"Accept": accept})
    assert result.status_code == 403
    assert result.json() == {"detail": "authentication required"}


def test_api_and_websocket_anonymous_denials_stay_protocol_correct(pilot):
    connected = []
    @asynccontextmanager
    async def never_connect(*args, **kwargs):
        connected.append((args, kwargs))
        pytest.fail("anonymous WebSocket reached upstream")
        yield
    with client(pilot, websocket_connect=never_connect) as browser:
        response = browser.post("/api/workspace/create", headers={"Origin": PUBLIC, "Accept": "text/html"}, json={})
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("application/json")
        with pytest.raises(WebSocketDisconnect):
            with browser.websocket_connect("/api/remote.mux", headers={"Origin": PUBLIC, "Accept": "text/html"}):
                pass
    assert connected == [] and pilot[2] == []


def test_authenticated_native_page_unchanged_and_unknown_private_route_not_proxied(pilot):
    with client(pilot) as browser:
        page = browser.get("/", headers={"Cookie": "access_token=PRIVATE-SSO-COOKIE-0", "Accept": "text/html"})
        assert page.status_code == 200 and page.content == b"native page unchanged"
        before = len(pilot[2])
        response = browser.get("/__megmeet/bootstrap", headers={"Cookie": "access_token=PRIVATE-SSO-COOKIE-0"})
        assert response.status_code == 404 and len(pilot[2]) == before
