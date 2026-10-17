"""Synthetic A/B desktop handoff, actual Redis and personal-model broker smoke.

Loopback-only; never prints bearer credentials, keys, cookies or chat payloads.
Ordinary model requests use the non-persistent broker, not /v1/chat/run.
"""
from __future__ import annotations

import base64
import hashlib
from http.cookiejar import Cookie, CookieJar
import json
import re
import secrets
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

BASE = "http://127.0.0.1:28090"
PREFIX = "/v1/desktop/auth"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def check(value, label):
    if not value:
        raise RuntimeError(label)
    print(label + ": PASS", flush=True)


class Browser:
    def __init__(self, user=None):
        self.jar = CookieJar()
        self.client = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.jar), NoRedirect())
        if user is not None:
            if user not in {"a", "b"}:
                raise ValueError("synthetic user required")
            self.jar.set_cookie(Cookie(0, "local_iam_fixture", user, None, False, "127.0.0.1",
                False, False, "/esc-sso", True, False, None, True, None, None, {"HttpOnly": None}, False))

    def request(self, path, body=None, token=None, extra_headers=None, form=False):
        url = urljoin(BASE + "/", path)
        parsed = urlsplit(url)
        if parsed.scheme + "://" + parsed.netloc != BASE or parsed.username or parsed.password or parsed.fragment:
            raise RuntimeError("DESKTOP_SMOKE_NONLOCAL_URL_DENIED")
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(extra_headers or {})}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = (urlencode(body) if form else json.dumps(body)).encode() if body is not None else None
        request = Request(url, data=data, headers=headers)
        try:
            response = self.client.open(request, timeout=25)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            payload = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, response.headers, payload


def login(user):
    desktop = Browser()
    browser = Browser(user)
    verifier = secrets.token_urlsafe(48)
    proof = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    instance = str(uuid4())
    status, _, flow = desktop.request(PREFIX + "/start", {"instance_id": instance, "code_challenge": proof})
    check(status == 200, "DESKTOP_HANDOFF_START_" + user.upper())
    exchange = {"flow_id": flow["flow_id"], "instance_id": instance, "code_verifier": verifier}
    check(desktop.request(PREFIX + "/exchange", exchange)[0] == 202, "DESKTOP_PENDING_" + user.upper())
    status, headers, _ = browser.request(flow["browser_url"])
    check(status == 303, "DESKTOP_BROWSER_IAM_REDIRECT_" + user.upper())
    status, headers, _ = browser.request(headers["Location"])
    check(status in {302, 303} and urlsplit(headers["Location"]).path == "/v1/iam/callback",
          "DESKTOP_REAL_MOCK_IAM_CALLBACK_" + user.upper())
    callback = headers["Location"]
    status, _, body = browser.request(callback)
    check(status == 200 and isinstance(body, bytes) and b"access_token" not in body,
          "DESKTOP_BROWSER_NO_APP_CREDENTIAL_" + user.upper())
    check(flow["verification_code"].encode() in body, "DESKTOP_BROWSER_MATCHING_DEVICE_CODE_" + user.upper())
    check(desktop.request(PREFIX + "/exchange", exchange)[0] == 202, "DESKTOP_UNCONFIRMED_CANNOT_EXCHANGE_" + user.upper())
    match = re.search(rb'name="confirmation" value="([A-Za-z0-9_-]+)"', body)
    check(match is not None, "DESKTOP_BROWSER_CONFIRMATION_PRESENT_" + user.upper())
    confirmation = {"confirmation": match.group(1).decode()}
    check(desktop.request(PREFIX + "/confirm", confirmation, extra_headers={"Origin": BASE}, form=True)[0] == 400,
          "DESKTOP_FOREIGN_BROWSER_CONFIRMATION_REJECTED_" + user.upper())
    check(browser.request(PREFIX + "/confirm", confirmation, extra_headers={"Origin": BASE}, form=True)[0] == 200,
          "DESKTOP_BROWSER_DEVICE_CONFIRMED_" + user.upper())
    check(not any(cookie.name == "megmeet_iam_session" for cookie in browser.jar),
          "DESKTOP_BROWSER_NO_APP_SESSION_COOKIE_" + user.upper())
    check(desktop.request(PREFIX + "/exchange", {**exchange, "instance_id": str(uuid4())})[0] == 400,
          "DESKTOP_FOREIGN_INSTANCE_REJECTED_" + user.upper())
    check(desktop.request(PREFIX + "/exchange", {**exchange, "code_verifier": "x" * 43})[0] == 400,
          "DESKTOP_WRONG_PROOF_REJECTED_" + user.upper())
    status, _, grant = desktop.request(PREFIX + "/exchange", exchange)
    check(status == 200 and grant["user"]["user_no"] == "LOCAL-FIXTURE-" + user.upper()
          and 0 < grant["expires_in"] <= 900, "DESKTOP_STABLE_EMPLOYEE_GRANT_" + user.upper())
    check(desktop.request(PREFIX + "/exchange", exchange)[0] == 410,
          "DESKTOP_HANDOFF_REPLAY_REJECTED_" + user.upper())
    check(browser.request(callback)[0] == 400, "DESKTOP_IAM_CALLBACK_REPLAY_REJECTED_" + user.upper())
    return desktop, grant


def model_check(client, grant, user, *, stream=False):
    status, _, result = client.request("/v1/personal-model/chat/completions", {
        "model": "deepseek-flash-v4.1", "messages": [{"role": "user", "content": "HYBRID_SYNTHETIC_CHECK"}],
        "stream": stream,
    }, grant["access_token"], {"X-Employee-No": "LOCAL-FIXTURE-B" if user == "a" else "LOCAL-FIXTURE-A"})
    marker = "PERSONAL_MODEL_OK[LOCAL-FIXTURE-" + user.upper() + "]"
    text = result.decode() if isinstance(result, bytes) else result.get("choices", [{}])[0].get("message", {}).get("content", "")
    check(status == 200 and marker in text and "synthetic-personal-model-key" not in text,
          "PERSONAL_KEY_BINDING_" + user.upper() + ("_SSE" if stream else "_JSON"))


def run():
    check(Browser().request("/v1/personal-model/models")[0] == 401, "PERSONAL_MODEL_ANONYMOUS_REJECTED")
    client_a, grant_a = login("a")
    client_b, grant_b = login("b")
    check(grant_a["access_token"] != grant_b["access_token"], "DESKTOP_TWO_DISTINCT_APP_CREDENTIALS")
    for user, client, grant, expected in (
        ("A", client_a, grant_a, {"deepseek-flash-v4.1", "glm-5.3-flash"}),
        ("B", client_b, grant_b, {"deepseek-flash-v4.1"}),
    ):
        status, _, catalog = client.request("/v1/personal-model/models", token=grant["access_token"])
        check(status == 200 and {row["id"] for row in catalog["data"]} == expected
              and catalog["default_model"] == "deepseek-flash-v4.1"
              and "api_key" not in json.dumps(catalog), "EMPLOYEE_MODEL_CATALOG_" + user)
        status, _, _ = client.request("/v1/personal-model/chat/completions", {
            "model": "glm-5.3-flash", "messages": [{"role": "user", "content": "CATALOG_CHECK"}],
        }, grant["access_token"])
        check(status == (200 if user == "A" else 403), "EMPLOYEE_MODEL_ENFORCEMENT_" + user)
    model_check(client_a, grant_a, "a")
    model_check(client_b, grant_b, "b", stream=True)
    check(client_a.request(PREFIX + "/logout", {}, grant_a["access_token"])[0] == 200, "DESKTOP_LOGOUT_A")
    check(client_a.request(PREFIX + "/session", token=grant_a["access_token"])[0] == 401, "DESKTOP_REVOKED_A_REJECTED")
    check(client_b.request(PREFIX + "/session", token=grant_b["access_token"])[0] == 200, "DESKTOP_LOGOUT_A_DOES_NOT_REVOKE_B")
    model_check(client_b, grant_b, "b")
    check(client_b.request(PREFIX + "/logout", {}, grant_b["access_token"])[0] == 200, "DESKTOP_LOGOUT_B")
    print("DESKTOP_PERSONAL_MODEL_REAL_LOCAL_CHAIN: PASS", flush=True)


if __name__ == "__main__":
    run()
