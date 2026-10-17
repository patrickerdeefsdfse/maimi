"""Offline contracts and user boundaries; no real provider credentials/calls."""
import asyncio
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app import enterprise_connectors as c
from app import connector_web_reader as reader


A = {"user_no": "M00000001", "organization_codes": []}
B = {"user_no": "M00000002", "organization_codes": []}


class Store:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    async def get(self, category, owner, key):
        index = (category, owner, key)
        if self.expiry.get(index, 0) <= time.time():
            return None
        return self.values.get(index)

    async def put(self, category, owner, key, value, ttl, nx=False):
        if nx and await self.get(category, owner, key) is not None:
            return False
        index = (category, owner, key)
        self.values[index] = value
        self.expiry[index] = time.time() + ttl
        return True

    async def pop(self, category, owner, key):
        value = await self.get(category, owner, key)
        self.values.pop((category, owner, key), None)
        return value

    async def delete(self, category, owner, key):
        await self.pop(category, owner, key)

    async def replace_if_equal(self, category, owner, key, old, new, ttl=30):
        if await self.get(category, owner, key) != old:
            return False
        if new is None:
            await self.pop(category, owner, key)
        else:
            await self.put(category, owner, key, new, ttl)
        return True

    async def budget(self, owner):
        pass


@pytest.fixture
def system(tmp_path):
    secret = tmp_path / "dingtalk-secret"
    secret.write_text("synthetic-dingtalk-secret")
    brave = tmp_path / "brave-secret"
    brave.write_text("synthetic-brave-secret")
    mapping = tmp_path / "accounts.json"
    mapping.write_text(json.dumps({"version": 1, "accounts": {
        A["user_no"]: {"union_id": "union-A", "user_id": "user-A", "display_name": "员工甲", "enabled": True,
                       "allowed_recipients": [B["user_no"]]},
        B["user_no"]: {"union_id": "union-B", "user_id": "user-B", "display_name": "员工乙", "enabled": True,
                       "allowed_recipients": []}}}), encoding="utf-8")
    cfg = c.ConnectorConfig("synthetic-app", str(secret), "corp-test", str(mapping), "robot-test",
                            "https://assistant.example.com", str(brave))
    state = {"requests": [], "union": "union-A", "corp": "corp-test", "write_status": 200}

    def handler(request):
        state["requests"].append(request)
        path = request.url.path
        if path == "/v1.0/oauth2/userAccessToken":
            payload = json.loads(request.content)
            assert payload["clientSecret"] == "synthetic-dingtalk-secret"
            if state.get("on_token"):
                state["on_token"]()
            return httpx.Response(200, json={"accessToken": "synthetic-user-token", "expireIn": 7200,
                                            "refreshToken": "synthetic-refresh-token", "corpId": state["corp"]})
        if path == "/v1.0/contact/users/me":
            assert request.headers["x-acs-dingtalk-access-token"] == "synthetic-user-token"
            return httpx.Response(200, json={"unionId": state["union"], "mobile": "private-not-returned"})
        if path == "/v1.0/calendar/me/calendars/primary/eventsview":
            assert request.method == "GET"
            return httpx.Response(200, json={"events": [{"id": "event-1", "summary": "My meeting", "description": "<b>body</b>",
                "start": {"dateTime": "2026-10-01T09:00:00+08:00"}, "end": {}, "accessToken": "never-return-me"}],
                "nextToken": "next-private"})
        if path in ("/v1.0/todo/users/me/personalTasks", "/v1.0/calendar/me/calendars/primary/events"):
            if state.get("raise_write"):
                raise httpx.ReadTimeout("upstream timeout synthetic-secret")
            return httpx.Response(state["write_status"], json=state.get("write_result", {"id": "created-test", "taskId": "created-test", "accessToken": "never-return-me"}))
        if path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "synthetic-app-token", "expireIn": 7200})
        if path == "/v1.0/robot/oToMessages/batchSend":
            assert request.headers["x-acs-dingtalk-access-token"] == "synthetic-app-token"
            return httpx.Response(200, json=state.get("message_result", {"processQueryKey": "private-process"}))
        if path == "/res/v1/web/search":
            assert request.url.host == "api.search.brave.com"
            assert request.headers["X-Subscription-Token"] == "synthetic-brave-secret"
            return httpx.Response(200, json={"web": {"results": [{"title": "<b>Source</b>", "description": "<script>bad()</script>Useful", "url": "https://example.com/article"},
                                                                {"title": "private", "url": "http://127.0.0.1/"}]}})
        raise AssertionError(f"unexpected provider route {path}")

    async def page_reader(url):
        return {"url": url, "text": "synthetic public page", "untrusted_external_content": True}

    service = c.ConnectorService(cfg, Store(), Fernet(Fernet.generate_key()), httpx.MockTransport(handler), page_reader=page_reader)
    return service, state


async def connect(service, actor=A):
    binding = "b" * 43
    url = await service.authorize(actor, binding)
    state = parse_qs(urlsplit(url).query)["state"][0]
    await service.finish_authorize(actor, state, binding, "synthetic-code")


def call(connector, action, **arguments):
    return c.InvokeRequest(connector=connector, action=action, arguments=arguments, request_id=uuid4())


def run(coro):
    return asyncio.run(coro)


def test_catalog_visible_to_all_authenticated_employees_without_config():
    async def scenario():
        svc = c.ConnectorService(c.ConnectorConfig(), Store(), Fernet(Fernet.generate_key()))
        for actor in (A, B):
            result = await svc.catalog(actor)
            assert result["protocol"] == c.PROTOCOL
            items = {i["plugin_key"]: i for i in result["items"]}
            assert items["dingtalk"]["status"] == "unavailable"
            assert items["web-search"]["status"] == "limited"
            tools = {t["name"]: t for t in items["web-search"]["tools"]}
            assert tools["web_read"]["available"] and not tools["web_search"]["available"]
    run(scenario())


def test_oauth_binding_corp_and_employee_identity(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        raw = await svc.store.get("grant", A["user_no"], "dingtalk")
        assert "synthetic-user-token" not in raw and "synthetic-refresh-token" not in raw
        data = await svc.catalog(A)
        assert data["items"][0]["status"] == "ready"
        assert "token" not in json.dumps(data)
        assert (await svc.catalog(B))["items"][0]["status"] == "authorization_required"
        with pytest.raises(c.ConnectorFailure, match="authorization_required"):
            await svc.invoke(B, call("dingtalk", "message_recipients"))
    run(scenario())


@pytest.mark.parametrize("field,value,expected", [("corp", "foreign", "corporation_mismatch"), ("union", "union-B", "employee_mismatch")])
def test_oauth_wrong_company_or_employee_never_creates_grant(system, field, value, expected):
    async def scenario():
        svc, state = system
        state[field] = value
        with pytest.raises(c.ConnectorFailure, match=expected):
            await connect(svc)
        assert await svc.store.get("grant", A["user_no"], "dingtalk") is None
    run(scenario())


def test_oauth_state_browser_binding_and_replay(system):
    async def scenario():
        svc, state = system
        url = await svc.authorize(A, "b" * 43)
        nonce = parse_qs(urlsplit(url).query)["state"][0]
        for actor, cookie in ((B, "b" * 43), (A, "x" * 43)):
            with pytest.raises(c.ConnectorFailure, match="invalid_callback"):
                await svc.finish_authorize(actor, nonce, cookie, "synthetic-code")
        assert not state["requests"]
        await svc.finish_authorize(A, nonce, "b" * 43, "synthetic-code")
        with pytest.raises(c.ConnectorFailure, match="invalid_callback"):
            await svc.finish_authorize(A, nonce, "b" * 43, "synthetic-code")
    run(scenario())


def test_own_calendar_fixed_me_endpoint_and_sanitized_projection(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        result = await svc.invoke(A, call("dingtalk", "calendar_list", start="2026-10-01T00:00:00+08:00", end="2026-10-02T00:00:00+08:00"))
        assert result["result"]["items"][0]["description"] == "body"
        assert result["result"]["more_available"]
        assert "never-return-me" not in json.dumps(result) and "next-private" not in json.dumps(result)
        assert state["requests"][-1].url.params["maxResults"] == "20"
    run(scenario())


@pytest.mark.parametrize("args", [
    {"title": "x", "userId": "foreign"}, {"title": "x", "due_time_ms": "10"},
    {"title": "x", "access_token": "forged"}, {"title": "x", "extra": "hidden"},
])
def test_tool_arguments_cannot_set_identity_credentials_or_extra_fields(system, args):
    async def scenario():
        svc, state = system
        await connect(svc)
        before = len(state["requests"])
        with pytest.raises(c.ConnectorFailure, match="arguments_invalid"):
            await svc.invoke(A, call("dingtalk", "todo_create", **args))
        assert len(state["requests"]) == before
    run(scenario())


def test_write_requires_human_confirm_owner_bound_once_only(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        request = call("dingtalk", "todo_create", title="Synthetic task")
        proposal = await svc.invoke(A, request)
        assert proposal["status"] == "requires_confirmation"
        assert not any("personalTasks" in r.url.path for r in state["requests"])
        assert await svc.invoke(A, request) == proposal
        state["union"] = "union-B"
        await connect(svc, B)
        with pytest.raises(c.ConnectorFailure, match="consumed_or_expired"):
            await svc.confirm(B, proposal["confirmation_id"])
        result = await svc.confirm(A, proposal["confirmation_id"])
        assert result["result"]["created"]
        with pytest.raises(c.ConnectorFailure, match="consumed_or_expired"):
            await svc.confirm(A, proposal["confirmation_id"])
        with pytest.raises(c.ConnectorFailure, match="consumed_or_expired"):
            await svc.invoke(A, request)
        assert sum("personalTasks" in r.url.path for r in state["requests"]) == 1
    run(scenario())


def test_confirmation_expiry_and_reauthorization_invalidate_old_proposal(system):
    async def scenario():
        svc, _ = system
        await connect(svc)
        first = await svc.invoke(A, call("dingtalk", "todo_create", title="First"))
        await connect(svc)
        with pytest.raises(c.ConnectorFailure, match="scope_changed"):
            await svc.confirm(A, first["confirmation_id"])
        second = await svc.invoke(A, call("dingtalk", "todo_create", title="Second"))
        key = ("pending", A["user_no"], second["confirmation_id"])
        svc.store.expiry[key] = time.time() - 1
        with pytest.raises(c.ConnectorFailure, match="consumed_or_expired"):
            await svc.confirm(A, second["confirmation_id"])
    run(scenario())


def test_request_id_cannot_change_action_or_arguments(system):
    async def scenario():
        svc, _ = system
        await connect(svc)
        req = call("dingtalk", "todo_create", title="Original")
        await svc.invoke(A, req)
        changed = req.model_copy(update={"arguments": {"title": "Changed"}})
        with pytest.raises(c.ConnectorFailure, match="request_immutable"):
            await svc.invoke(A, changed)
    run(scenario())


def test_message_allowlist_preview_send_and_partial_feedback(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        with pytest.raises(c.ConnectorFailure, match="recipient_denied"):
            await svc.invoke(A, call("dingtalk", "message_send", recipients=["M00000003"], text="No"))
        proposal = await svc.invoke(A, call("dingtalk", "message_send", recipients=[B["user_no"]], text="Synthetic test"))
        assert proposal["preview"]["recipients"] == [{"employee_no": B["user_no"], "name": "员工乙"}]
        state["message_result"] = {"invalidStaffIdList": ["user-B"]}
        result = await svc.confirm(A, proposal["confirmation_id"])
        assert result["result"]["status"] == "partial_or_rejected"
        last = json.loads(state["requests"][-1].content)
        assert last["userIds"] == ["user-B"] and last["msgKey"] == "sampleText"
        assert "corp" not in last and "user-B" not in json.dumps(result)
    run(scenario())


def test_recipient_revocation_blocks_existing_confirmation(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        proposal = await svc.invoke(A, call("dingtalk", "message_send", recipients=[B["user_no"]], text="No longer allowed"))
        data = json.loads(Path(svc.config.dingtalk_accounts_file).read_text())
        data["accounts"][B["user_no"]]["enabled"] = False
        Path(svc.config.dingtalk_accounts_file).write_text(json.dumps(data))
        with pytest.raises(c.ConnectorFailure, match="not_provisioned"):
            await svc.confirm(A, proposal["confirmation_id"])
        assert not any("batchSend" in r.url.path for r in state["requests"])
    from pathlib import Path
    run(scenario())


def test_ambiguous_write_is_consumed_not_retried(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        proposal = await svc.invoke(A, call("dingtalk", "todo_create", title="Maybe sent"))
        state["raise_write"] = True
        with pytest.raises(c.ConnectorFailure, match="write_result_unknown"):
            await svc.confirm(A, proposal["confirmation_id"])
        with pytest.raises(c.ConnectorFailure, match="consumed_or_expired"):
            await svc.confirm(A, proposal["confirmation_id"])
    run(scenario())


def test_refresh_cannot_resurrect_disconnected_grant(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        key = ("grant", A["user_no"], "dingtalk")
        grant = svc.unseal(A["user_no"], svc.store.values[key])
        grant["expires_at"] = time.time() - 1
        svc.store.values[key] = svc.seal(A["user_no"], grant)
        state["on_token"] = lambda: svc.store.values.pop(key, None)
        with pytest.raises(c.ConnectorFailure, match="authorization_changed"):
            await svc.grant(A["user_no"])
        assert await svc.store.get(*key) is None
    run(scenario())


def test_search_only_sends_explicit_query_returns_safe_sources(system):
    async def scenario():
        svc, state = system
        result = await svc.invoke(A, call("web-search", "web_search", query="test public docs", count=3))
        req = state["requests"][-1]
        assert req.url.params["q"] == "test public docs"
        assert "M00000001" not in str(req.url) and not req.content
        assert result["result"]["sources"] == [{"title": "Source", "url": "https://example.com/article", "snippet": "Useful"}]
        assert "synthetic-brave" not in json.dumps(result)
    run(scenario())


def test_unconfigured_search_fails_clearly_but_public_reader_available(system):
    async def scenario():
        svc, state = system
        svc.config = replace(svc.config, brave_secret_file="")
        with pytest.raises(c.ConnectorFailure, match="web_search_not_configured"):
            await svc.invoke(A, call("web-search", "web_search", query="query"))
        result = await svc.invoke(A, call("web-search", "web_read", url="https://example.com/article"))
        assert result["result"]["untrusted_external_content"]
        assert not state["requests"]
    run(scenario())


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1/", "https://169.254.169.254/latest", "https://10.4.1.112/",
                                "https://localhost/", "https://machine.local/", "https://[::1]/", "https://user:secret@example.com/",
                                "https://example.com:8443/", "https://example.com\\@127.0.0.1/", "https://example.com/#fragment"])
def test_ssrf_url_guards(url):
    with pytest.raises(reader.WebReadFailure, match="denied"):
        reader.public_url(url)


def test_mixed_dns_answers_are_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443)), (None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(reader.WebReadFailure, match="address_denied"):
        reader.resolve_public("example.com")


def test_reader_pins_ip_and_original_hostname_for_tls(monkeypatch):
    seen = {}
    class Context:
        def wrap_socket(self, sock, server_hostname):
            seen["sni"] = server_hostname
            return "tls-socket"
    monkeypatch.setattr(reader.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(reader.socket, "create_connection", lambda addr, timeout: seen.update(address=addr) or "tcp-socket")
    conn = reader.PinnedHTTPSConnection("example.com", "93.184.216.34")
    conn.connect()
    assert seen == {"sni": "example.com", "address": ("93.184.216.34", 443)}


def test_redirect_to_private_url_denied_and_initial_connection_closed(monkeypatch):
    seen = []
    class Response:
        status = 302
        def getheader(self, name, default=None):
            return "https://127.0.0.1/" if name == "Location" else default
    class Connection:
        def __init__(self, host, address):
            seen.append((host, address))
        def request(self, *a, **k): pass
        def getresponse(self): return Response()
        def close(self): seen.append("closed")
    monkeypatch.setattr(reader, "resolve_public", lambda host: "93.184.216.34")
    monkeypatch.setattr(reader, "PinnedHTTPSConnection", Connection)
    with pytest.raises(reader.WebReadFailure, match="url_denied"):
        reader.read_public_page("https://example.com/")
    assert seen == [("example.com", "93.184.216.34"), "closed"]


def test_catalog_does_not_offer_model_confirmation_tool(system):
    async def scenario():
        svc, _ = system
        await connect(svc)
        catalog = await svc.catalog(A)
        tools = [t for i in catalog["items"] for t in i["tools"]]
        assert not any("confirm" in t["name"] for t in tools)
        assert next(t for t in tools if t["name"] == "message_send")["requires_confirmation"] is True
    run(scenario())


def test_routes_still_enforce_browser_origin_and_iam_identity(system, monkeypatch):
    async def scenario():
        svc, state = system
        monkeypatch.setattr(c, "_service", svc)
        monkeypatch.setenv("IAM_PUBLIC_ORIGIN", "https://assistant.example.com")
        from app import desktop_auth
        async def trusted(request):
            return A
        monkeypatch.setattr(desktop_auth, "require_trusted_actor", trusted)
        app = FastAPI()
        app.include_router(c.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://assistant.example.com") as client:
            body = call("web-search", "web_search", query="public docs").model_dump(mode="json")
            denied = await client.post("/v1/connectors/invoke", json=body, headers={"Origin": "https://evil.example.com"})
            assert denied.status_code == 403 and not state["requests"]
            allowed = await client.post("/v1/connectors/invoke", json=body, headers={"Origin": "https://assistant.example.com"})
            assert allowed.status_code == 200 and allowed.headers["Cache-Control"] == "no-store"
            denied_confirm = await client.post("/v1/connectors/confirm", json={"confirmation_id": "x" * 43, "confirmed": False}, headers={"Origin": "https://assistant.example.com"})
            assert denied_confirm.status_code == 422
    run(scenario())


def test_calendar_attendees_use_union_ids_and_self_todo_has_no_foreign_executors(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        proposal = await svc.invoke(A, call("dingtalk", "calendar_create", title="Meeting", start="2026-10-01T09:00:00+08:00",
                                           end="2026-10-01T10:00:00+08:00", attendees=[B["user_no"]]))
        await svc.confirm(A, proposal["confirmation_id"])
        payload = json.loads(state["requests"][-1].content)
        assert payload["attendees"] == [{"id": "union-B"}]
        proposal = await svc.invoke(A, call("dingtalk", "todo_create", title="My own task", due_time_ms=1790800000000))
        await svc.confirm(A, proposal["confirmation_id"])
        payload = json.loads(state["requests"][-1].content)
        assert payload == {"subject": "My own task", "description": "", "dueTime": 1790800000000}
    run(scenario())


def test_broken_search_secret_does_not_disable_public_reader(system):
    async def scenario():
        svc, _ = system
        svc.config = replace(svc.config, brave_secret_file="missing-secret-file")
        catalog = await svc.catalog(A)
        web = next(x for x in catalog["items"] if x["plugin_key"] == "web-search")
        assert web["status"] == "limited"
        assert {t["name"]: t["available"] for t in web["tools"]} == {"web_search": False, "web_read": True}
    run(scenario())


def test_revoked_user_delegation_cannot_use_app_bot_token(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        proposal = await svc.invoke(A, call("dingtalk", "message_send", recipients=[B["user_no"]], text="Revoked"))
        state["union"] = "union-B"
        with pytest.raises(c.ConnectorFailure, match="employee_mismatch"):
            await svc.confirm(A, proposal["confirmation_id"])
        assert not any(r.url.path == "/v1.0/oauth2/accessToken" for r in state["requests"])
    run(scenario())


@pytest.mark.parametrize("status,code", [(401, "authorization_denied"), (403, "authorization_denied"), (429, "rate_limit"), (503, "provider_unavailable"), (302, "provider_unavailable")])
def test_upstream_errors_never_reflect_raw_secrets(system, status, code):
    async def scenario():
        svc, _ = system
        svc.transport = httpx.MockTransport(lambda request: httpx.Response(status, text="private-token-and-server-detail"))
        with pytest.raises(c.ConnectorFailure, match=code) as failure:
            await svc.invoke(A, call("web-search", "web_search", query="public"))
        assert "private" not in str(failure.value)
    run(scenario())


def test_concurrent_confirmation_has_exactly_one_upstream_write(system):
    async def scenario():
        svc, state = system
        await connect(svc)
        proposal = await svc.invoke(A, call("dingtalk", "todo_create", title="Once"))
        results = await asyncio.gather(*(svc.confirm(A, proposal["confirmation_id"]) for _ in range(10)), return_exceptions=True)
        assert sum(isinstance(x, dict) and x.get("status") == "completed" for x in results) == 1
        assert sum("personalTasks" in r.url.path for r in state["requests"]) == 1
    run(scenario())


@pytest.mark.parametrize("field", ["filteredStaffIdList", "invalidStaffIdList", "flowControlledStaffIdList"])
def test_robot_partial_response_is_not_reported_as_success(system, field):
    async def scenario():
        svc, state = system
        await connect(svc)
        state["message_result"] = {field: ["user-B"], "processQueryKey": "private-process"}
        proposal = await svc.invoke(A, call("dingtalk", "message_send", recipients=[B["user_no"]], text="Only after confirmation"))
        result = await svc.confirm(A, proposal["confirmation_id"])
        assert result["result"] == {"status": "partial_or_rejected", "check_dingtalk_before_retry": True}
        assert "user-B" not in json.dumps(result)
    run(scenario())


@pytest.mark.parametrize("action,args", [
    ("todo_create", {"title": "Task"}),
    ("calendar_create", {"title": "Meeting", "start": "2026-10-01T09:00:00+08:00", "end": "2026-10-01T10:00:00+08:00"}),
    ("message_send", {"recipients": [B["user_no"]], "text": "Message"}),
])
def test_empty_provider_success_response_is_unknown_and_not_replayed(system, action, args):
    async def scenario():
        svc, state = system
        await connect(svc)
        state["message_result"] = state["write_result"] = {}
        proposal = await svc.invoke(A, call("dingtalk", action, **args))
        with pytest.raises(c.ConnectorFailure, match="write_result_unknown"):
            await svc.confirm(A, proposal["confirmation_id"])
        with pytest.raises(c.ConnectorFailure, match="consumed_or_expired"):
            await svc.confirm(A, proposal["confirmation_id"])
    run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_reader_child_is_killed_and_reaped_on_timeout_or_cancel(monkeypatch, cancel):
    async def scenario():
        started = asyncio.Event()
        class Child:
            returncode = None
            killed = False
            reaped = False
            async def communicate(self, data):
                assert data == b"https://example.com/"
                started.set()
                await asyncio.Future()
            def kill(self): self.killed = True
            async def wait(self):
                self.reaped = True
                self.returncode = -9
        child = Child()
        async def spawn(*args, **kwargs):
            assert args[-3:-1] == ("-I", "-B") and args[-1].endswith("connector_web_reader.py")
            return child
        monkeypatch.setattr(reader.asyncio, "create_subprocess_exec", spawn)
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(reader, "_read_slots", semaphore)
        timeout = asyncio.timeout
        monkeypatch.setattr(reader.asyncio, "timeout", lambda seconds: timeout(.03))
        task = asyncio.create_task(reader.read_public_page_async("https://example.com/"))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            with pytest.raises(reader.WebReadFailure, match="web_page_timeout"): await task
        assert child.killed and child.reaped and not semaphore.locked()
    run(scenario())


def test_reader_child_cli_validates_input_without_network():
    async def scenario():
        import sys
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", reader.__file__,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await process.communicate(b"https://127.0.0.1/")
        assert process.returncode == 0 and not error
        assert json.loads(output) == {"error": "web_url_denied"}
    run(scenario())
