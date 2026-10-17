"""Synthetic cold-entry HTML and browser recovery; no company network calls."""
import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
from entry_app import Config, create_app  # noqa: E402
from entry_login import RELEASE_MARKER, login_script  # noqa: E402

PUBLIC = "https://platform-ai.megmeet.com"


@pytest.fixture
def browser_factory():
    cfg = Config.parse({
        "public_origin": PUBLIC, "sso_url": "https://synthetic.invalid/query-user",
        "tokenkey_url": "https://synthetic-model.invalid/v1",
        "tokenkey_secret_file": "/run/secrets/synthetic",
        "bindings": [{
            "user_id": str(uuid4()), "user_no": str(100 + i),
            "upstream": f"http://host-{i}:3084",
            "bootstrap": f"/run/megmeet-bootstrap/{i}/ready.json",
            "model_token_file": f"/run/megmeet-entry/token-{i}",
        } for i in range(2)],
    })
    files, calls = {}, []
    for i, binding in enumerate(cfg.bindings):
        files[binding.model_token_file] = (str(i) * 48).encode()
        files[binding.bootstrap] = json.dumps({
            "ready": True, "owner_id": binding.user_id, "generation": str(uuid4()),
            "workspace_id": str(uuid4()), "cookie": "dsh_session=synthetic-private",
        }).encode()

    def factory(sso_status=200, employee="100"):
        def transport(request):
            calls.append(request)
            if request.url.host == "synthetic.invalid":
                return httpx.Response(sso_status, json={"code": 0, "data": {"user_no": employee}})
            return httpx.Response(200, content=b"EXACT_NATIVE_PAGE", headers={"Content-Type": "text/html"})
        return TestClient(create_app(cfg, reader=lambda path, limit: files[path],
                                     transport=httpx.MockTransport(transport)))
    return factory, calls


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, name, attrs):
        self.tags.append((name, dict(attrs)))


def test_denied_html_uses_only_configured_urls_and_external_script(browser_factory):
    factory, calls = browser_factory
    with factory() as browser:
        response = browser.get("/?return_url=https%3A%2F%2Fevil.invalid%2F", headers={
            "Accept": "text/html", "Host": "evil.invalid", "X-Forwarded-Host": "evil.invalid",
        })
    assert response.status_code == 403 and calls == []
    assert RELEASE_MARKER in response.text and "ENTRY_LOGIN_ONLY" in response.text
    assert "evil.invalid" not in response.text
    tags = Tags(response.text).tags
    scripts = [attrs for name, attrs in tags if name == "script"]
    assert scripts == [{"type": "module", "src": PUBLIC + "/megmeet-ai-native/__megmeet/login.js"}]
    link = next(attrs for name, attrs in tags if name == "a")
    parsed = urlsplit(link["href"])
    assert parsed.scheme == "https" and parsed.netloc == "platform-ai.megmeet.com"
    assert parsed.path == "/v1/oauth/login"
    assert parse_qs(parsed.query) == {"redirect_uri": [PUBLIC + "/megmeet-ai-native/"], "client_type": ["[web]"]}
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "connect-src 'self'",
                      "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "set-cookie" not in response.headers and "location" not in response.headers
    assert response.headers["cache-control"] == "private, no-store"


def test_script_public_bounded_and_does_not_use_identity_inputs(browser_factory):
    factory, calls = browser_factory
    with factory() as browser:
        response = browser.get("/__megmeet/login.js?return_url=https://evil.invalid/", headers={"Host": "evil.invalid"})
        cross_origin = browser.get("/__megmeet/login.js", headers={"Origin": "https://evil.invalid"})
        token = browser.get("/__megmeet/login.js?access_token=synthetic")
        method = browser.post("/__megmeet/login.js")
    assert response.status_code == 200 and calls == []
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "evil.invalid" not in response.text and RELEASE_MARKER in response.text
    assert all(word not in response.text for word in ("document.cookie", "localStorage", "sessionStorage", "console.", "remote.mux"))
    assert cross_origin.status_code == 403 and token.status_code == 400
    assert method.status_code == 404


@pytest.mark.parametrize("sso_status,employee,expected", [(200, "999", 403), (500, "100", 503)])
def test_account_denial_and_outage_do_not_become_login_html(browser_factory, sso_status, employee, expected):
    factory, _calls = browser_factory
    with factory(sso_status, employee) as browser:
        response = browser.get("/", headers={"Accept": "text/html", "Cookie": "access_token=synthetic"})
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("application/json")
    assert RELEASE_MARKER not in response.text


def test_authenticated_native_page_and_api_denials_are_not_replaced(browser_factory):
    factory, calls = browser_factory
    with factory() as browser:
        page = browser.get("/", headers={"Accept": "text/html", "Cookie": "access_token=synthetic"})
        assert page.status_code == 200 and page.content == b"EXACT_NATIVE_PAGE"
        before = len(calls)
        denied = browser.post("/api/workspace/create", headers={"Origin": PUBLIC, "Accept": "text/html"})
        assert denied.status_code == 403 and denied.json() == {"detail": "authentication required"}
        assert len(calls) == before


def run_js(tmp_path, body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable; synthetic browser tests require it")
    module = tmp_path / "login.mjs"
    module.write_text(login_script(PUBLIC), encoding="utf-8")
    code = "import assert from 'node:assert/strict';\n" + (
        "const moduleUrl = " + json.dumps(module.as_uri()) + ";\n"
        "const { createRecovery } = await import(moduleUrl);\n"
        "const identity = { authenticated: true, user_id: '11111111-1111-4111-8111-111111111111', workspace_id: 'workspace-synthetic' };\n"
        "function json(value, status = 200) { return new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}}); }\n"
        "const states = [], navigations = [], calls = [];\n"
    ) + body
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True,
                            text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr[-4000:]


def test_js_refresh_cookie_roundtrip_is_verified_before_native_navigation(tmp_path):
    run_js(tmp_path, """
let cookieApplied = false;
const recover = createRecovery({fetcher: async (url, options) => {
  calls.push({url, options});
  assert.equal(options.credentials, 'include');
  assert.equal(options.mode, 'same-origin');
  assert.equal(options.redirect, 'error');
  assert.equal(options.cache, 'no-store');
  assert.equal(Object.hasOwn(options, 'body'), false);
  if (calls.length === 1) {
    assert.equal(options.method, 'POST');
    assert.equal(url, 'https://platform-ai.megmeet.com/v1/auth/refresh-token');
    cookieApplied = true;
    return new Response(null, {status: 204});
  }
  assert.equal(cookieApplied, true);
  assert.equal(options.method, 'GET');
  assert.equal(url, 'https://platform-ai.megmeet.com/megmeet-ai-native/__megmeet/identity');
  return json(identity);
}, navigate: url => navigations.push(url), show: value => states.push(value)});
await recover();
assert.equal(calls.length, 2);
assert.deepEqual(states.map(x => x.code), ['checking', 'ready']);
assert.deepEqual(navigations, ['https://platform-ai.megmeet.com/megmeet-ai-native/']);
""")


@pytest.mark.parametrize("status,code", [(401, "login"), (403, "login"), (500, "temporary"), (503, "temporary"), (400, "unexpected"), (302, "unexpected")])
def test_js_refresh_failure_never_automatically_enters_company_login(tmp_path, status, code):
    run_js(tmp_path, f"""
const recover = createRecovery({{fetcher: async (url, options) => {{
  calls.push(url); return new Response('synthetic', {{status: {status}}});
}}, navigate: url => navigations.push(url), show: value => states.push(value)}});
await recover();
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls.length, 1);
assert.deepEqual(navigations, []);
assert.equal(states.at(-1).code, {json.dumps(code)});
assert.equal(states.at(-1).canLogin, true);
assert.equal(states.at(-1).canRetry, true);
""")


@pytest.mark.parametrize("status,payload,code", [
    (401, {"detail": "authentication required"}, "login"),
    (403, {"detail": "authentication required"}, "login"),
    (403, {"detail": "account not enabled for personal pilot"}, "denied"),
    (503, {"detail": "authentication service unavailable"}, "temporary"),
    (403, {"detail": "unknown error"}, "unexpected"),
    (200, {"authenticated": "true"}, "unexpected"),
    (200, {"authenticated": True}, "unexpected"),
    (200, [], "unexpected"),
])
def test_js_identity_denial_unknown_or_unavailable_never_enters_native(tmp_path, status, payload, code):
    run_js(tmp_path, f"""
const recover = createRecovery({{fetcher: async (url, options) => {{
  calls.push(url); return calls.length === 1 ? new Response(null, {{status: 204}}) : json({json.dumps(payload)}, {status});
}}, navigate: url => navigations.push(url), show: value => states.push(value)}});
await recover();
assert.equal(calls.length, 2);
assert.deepEqual(navigations, []);
assert.equal(states.at(-1).code, {json.dumps(code)});
if ({json.dumps(code)} === 'denied') {{
  assert.equal(states.at(-1).canLogin, false);
  assert.equal(states.at(-1).canRetry, false);
}}
""")


def test_js_network_failure_allows_only_manual_retry_and_login(tmp_path):
    run_js(tmp_path, """
const recover = createRecovery({fetcher: async (url, options) => {
  calls.push(url);
  if (calls.length === 1) throw new Error('SYNTHETIC PRIVATE ERROR');
  return calls.length === 2 ? new Response(null, {status: 204}) : json(identity);
}, navigate: url => navigations.push(url), show: value => states.push(value)});
await recover();
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls.length, 1);
assert.equal(states.at(-1).code, 'temporary');
assert.equal(states.at(-1).canLogin, true);
assert.equal(states.at(-1).canRetry, true);
assert.equal(JSON.stringify(states).includes('SYNTHETIC PRIVATE ERROR'), false);
assert.deepEqual(navigations, []);
await recover();
assert.equal(calls.length, 3);
assert.equal(navigations.length, 1);
""")


def test_js_parallel_retry_clicks_do_not_duplicate_refresh(tmp_path):
    run_js(tmp_path, """
let release;
const pending = new Promise(resolve => { release = resolve; });
const recover = createRecovery({fetcher: async (url, options) => {
  calls.push(url);
  return calls.length === 1 ? pending : json(identity);
}, navigate: url => navigations.push(url), show: value => states.push(value)});
const first = recover();
await recover();
assert.equal(calls.length, 1);
release(new Response(null, {status: 204}));
await first;
assert.equal(calls.length, 2);
assert.equal(navigations.length, 1);
""")


def test_js_dom_entry_makes_one_automatic_attempt_and_no_company_redirect(tmp_path):
    run_js(tmp_path, """
const status = {textContent: ''}, login = {hidden: false};
const retry = {disabled: true, addEventListener: (name, callback) => { retry.click = callback; }};
globalThis.document = {getElementById: id => ({'megmeet-login-status': status, 'megmeet-login-link': login, 'megmeet-login-retry': retry}[id])};
globalThis.window = {location: {replace: url => navigations.push(url)}};
globalThis.fetch = async (url, options) => { calls.push({url, options}); return new Response(null, {status: 403}); };
await import(moduleUrl + '?browser');
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls.length, 1);
assert.equal(login.hidden, false);
assert.equal(retry.disabled, false);
assert.deepEqual(navigations, []);
assert.equal(status.textContent.includes('公司登录'), true);
retry.click();
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls.length, 2);
""")
