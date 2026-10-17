"""Boundary checks for the entry; all identities, cookies and streams synthetic."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SOURCE = Path(__file__).parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
SPEC = importlib.util.spec_from_file_location("entry_app", SOURCE / "entry_app.py")
entry = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = entry
SPEC.loader.exec_module(entry)


@pytest.fixture
def fixture():
    users = [str(uuid4()), str(uuid4())]
    data = {
        "public_origin": "https://platform-ai.megmeet.com",
        "sso_url": "https://sso.invalid/v1/auth/query-user",
        "bindings": [
            {"user_id": users[i], "user_no": str(i + 100), "upstream": f"http://personal-{i}:3084",
             "bootstrap": f"/run/megmeet-bootstrap/{i}/ready.json",
             "model_token_file": f"/run/megmeet-entry/{i}-model-token"} for i in range(2)
        ],
        "tokenkey_url": "https://models.invalid/v1",
        "tokenkey_secret_file": "/run/secrets/tokenkey-api-key",
    }
    cfg = entry.Config.parse(data)
    files = {cfg.tokenkey_secret_file: b"company-secret-is-never-browser-visible"}
    for i, binding in enumerate(cfg.bindings):
        files[binding.model_token_file] = (str(i) * 48).encode()
        files[binding.bootstrap] = json.dumps({
            "owner_id": binding.user_id, "generation": str(uuid4()), "ready": True,
            "workspace_id": str(uuid4()), "cookie": f"dsh_session=host-{i}",
        }).encode()
    calls = []

    def read(path, limit):
        body = files[path]
        if len(body) > limit:
            raise ValueError("bounded")
        return body

    def handle(request):
        calls.append(request)
        if request.url.host == "sso.invalid":
            cookie = request.headers.get("cookie", "")
            if cookie == "access_token=alice":
                return httpx.Response(200, json={"code": 0, "data": {"user_no": "100", "organization_code": "dept-current"}},
                                      headers={"set-cookie": "access_token=SHOULD_NOT_LEAK; Path=/"})
            if cookie == "access_token=bob":
                return httpx.Response(200, json={"code": 0, "data": {"userNo": "101"}})
            return httpx.Response(403)
        if request.url.host == "models.invalid":
            return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic answer"}}]},
                                  headers={"set-cookie": "company=NEVER", "authorization": "SECRET"})
        return httpx.Response(200, text="native resource", headers={"set-cookie": "dsh_session=NEVER; Path=/", "x-user-id": "NEVER"})

    return cfg, files, calls, read, handle


def client_for(fixture, handler=None, **kwargs):
    cfg, _, _, reader, handle = fixture
    return TestClient(entry.create_app(cfg, reader=reader, transport=httpx.MockTransport(handler or handle), **kwargs))


def headers(cookie="alice", **extra):
    return {"cookie": "access_token=" + cookie, "origin": "https://platform-ai.megmeet.com", **extra}


def test_no_cookie_unknown_user_and_health(fixture):
    with client_for(fixture) as client:
        assert client.get("/health").json() == {"status": "healthy", "service": "native-entry"}
        assert client.get("/").status_code == 403
        assert client.get("/", headers=headers("unknown")).status_code == 403
        assert client.get("/internal/bootstrap", headers=headers()).status_code == 404
        assert client.get("/__internal/ready", headers=headers()).status_code == 404
    assert not any(r.url.host.startswith("personal") for r in fixture[2])


def test_identity_routes_only_from_sso_and_strips_credentials(fixture):
    with client_for(fixture) as client:
        forged = headers(host="evil.invalid", referer="https://evil.invalid", authorization="Bearer injected",
                         **{"x-user-id": "bob", "forwarded": "host=personal-1", "x-forwarded-for": "injected"})
        forged["cookie"] += "; dsh_session=other-host; arbitrary=VALUE"
        response = client.post("/api/workspace/list", headers=forged, content=b"{}")
        assert response.status_code == 200
        assert "set-cookie" not in response.headers and "x-user-id" not in response.headers
        assert response.headers["cache-control"] == "private, no-store"
        client.get("/", headers=headers("bob"))
    sso = [r for r in fixture[2] if r.url.host == "sso.invalid"]
    native = [r for r in fixture[2] if r.url.host.startswith("personal")]
    assert sso[0].headers["cookie"] == "access_token=alice"
    assert sso[1].headers["cookie"] == "access_token=bob"
    assert [r.url.host for r in native] == ["personal-0", "personal-1"]
    first = native[0].headers
    assert first["cookie"] == "dsh_session=host-0"
    assert first["host"] == "127.0.0.1:3085"
    assert first["origin"] == "http://127.0.0.1:3085"
    for name in ("authorization", "x-user-id", "x-forwarded-for", "forwarded", "referer"):
        assert name not in first


def test_department_change_preserves_owner(fixture):
    calls = []
    base = fixture[4]

    def handler(request):
        if request.url.host == "sso.invalid":
            return httpx.Response(200, json={"code": 0, "data": {"userNo": "100", "organizationCode": str(uuid4())}})
        calls.append(request)
        return base(request)

    with client_for(fixture, handler) as client:
        assert client.get("/", headers=headers()).status_code == 200
        assert client.get("/", headers=headers()).status_code == 200
    assert {r.url.host for r in calls} == {"personal-0"}


@pytest.mark.parametrize("status,body", [(500, b"secret"), (302, b""), (200, b"not JSON"), (200, b'{}'),
                                       (200, b'{"code":0,"data":null}'), (200, b'x' * 65537)],
                         ids=["500", "redirect", "non-json", "no-code", "no-data", "oversize"])
def test_sso_failure_is_closed_and_generic(fixture, status, body):
    def handler(_request):
        return httpx.Response(status, content=body)
    with client_for(fixture, handler) as client:
        response = client.get("/", headers=headers())
        assert response.status_code == 503
        assert response.json() == {"detail": "authentication service unavailable"}


def test_other_valid_employee_cannot_enter(fixture):
    def handler(_request):
        return httpx.Response(200, json={"code": 0, "data": {"user_no": "999"}})
    with client_for(fixture, handler) as client:
        assert client.get("/", headers=headers()).status_code == 403


@pytest.mark.parametrize("change", [
    {"owner_id": str(uuid4())}, {"generation": "stale"}, {"workspace_id": "../foreign"},
    {"ready": False}, {"cookie": "session=x\r\nX-Injection: true"},
    {"cookie": "session=x; Path=/"},
])
def test_invalid_bootstrap_never_falls_back(fixture, change):
    path = fixture[0].bindings[0].bootstrap
    value = json.loads(fixture[1][path])
    value.update(change)
    fixture[1][path] = json.dumps(value).encode()
    with client_for(fixture) as client:
        assert client.get("/", headers=headers()).status_code == 503
    assert not any(r.url.host.startswith("personal") for r in fixture[2])


@pytest.mark.parametrize("method,path,extra,expected", [
    ("POST", "/api/workspace/create", {"origin": "https://evil.invalid"}, 403),
    ("POST", "/api/workspace/create", {"origin": "http://127.0.0.1:3085"}, 403),
    ("GET", "/", {"origin": "null"}, 403),
    ("GET", "/?token=SECRET", {}, 400),
    ("GET", "/?%74oken=SECRET", {}, 400),
    ("GET", "/?refresh_token=SECRET", {}, 400),
])
def test_external_credentials_and_bad_origin_rejected(fixture, method, path, extra, expected):
    with client_for(fixture) as client:
        assert client.request(method, path, headers=headers(**extra)).status_code == expected
    assert fixture[2] == []


def test_unsafe_request_requires_origin_and_size_bounded(fixture):
    with client_for(fixture) as client:
        assert client.post("/api/a", headers={"cookie": "access_token=alice"}, content=b"{}").status_code == 403
        assert client.post("/api/a", headers=headers(**{"content-length": str(entry.MAX_BODY + 1)})).status_code == 413
        assert client.post("/api/a", headers=headers(**{"content-encoding": "gzip"}), content=b"x").status_code == 415


@pytest.mark.parametrize("location", ["https://evil.invalid/path", "//evil.invalid/path", "/?token=secret"])
def test_redirect_cannot_expose_credentials(fixture, location):
    base = fixture[4]
    def handler(request):
        return base(request) if request.url.host == "sso.invalid" else httpx.Response(302, headers={"location": location})
    with client_for(fixture, handler) as client:
        assert client.get("/", headers=headers()).status_code == 502


def test_model_only_scoped_token_and_fixed_model(fixture):
    cfg = fixture[0]
    token = fixture[1][cfg.bindings[0].model_token_file].decode()
    model_headers = {"Authorization": "Bearer " + token}
    with client_for(fixture) as client:
        path = "/internal/model/v1/chat/completions"
        assert client.post(path, headers=headers(), json={"model": entry.MODEL}).status_code == 403
        assert client.post(path, headers={"Authorization": "Bearer bad"}, json={"model": entry.MODEL}).status_code == 403
        assert client.post(path, headers=model_headers, json={"model": "different"}).status_code == 400
        answer = client.post(path, headers=model_headers, json={"model": entry.MODEL, "messages": []})
        assert answer.status_code == 200
        assert "set-cookie" not in answer.headers and "authorization" not in answer.headers
        assert client.get("/internal/model/v1/models", headers=model_headers).json()["data"][0]["id"] == entry.MODEL
    upstream = [r for r in fixture[2] if r.url.host == "models.invalid"]
    assert len(upstream) == 1
    assert upstream[0].headers["authorization"] == "Bearer company-secret-is-never-browser-visible"
    assert "cookie" not in upstream[0].headers


def test_model_error_body_and_headers_not_exposed(fixture):
    def handler(_request):
        return httpx.Response(401, content=b"company-secret-is-never-browser-visible")
    with client_for(fixture, handler) as client:
        response = client.post("/internal/model/v1/chat/completions", headers={"Authorization": "Bearer " + "0" * 48}, json={"model": entry.MODEL})
        assert response.status_code == 503 and "secret" not in response.text


def test_stream_is_closed_on_normal_completion(fixture):
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"one"
            yield b"two"
        async def aclose(self):
            self.closed = True
    stream = Stream()
    base = fixture[4]
    def handler(request):
        return base(request) if request.url.host == "sso.invalid" else httpx.Response(200, stream=stream)
    with client_for(fixture, handler) as client:
        assert client.get("/", headers=headers()).content == b"onetwo"
    assert stream.closed


def test_stream_is_closed_on_cancel():
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"first"
            await asyncio.sleep(100)
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async def run():
        response = entry.ClosingStream(httpx.Response(200, stream=stream), {})
        async def receive():
            await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}
        async def send(_event):
            pass
        await response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}, receive, send)
    asyncio.run(run())
    assert stream.closed


def test_private_file_rejects_symlink_and_oversize(tmp_path):
    file = tmp_path / "data"
    file.write_bytes(b"abcd")
    assert entry.private_file(str(file), 4) == b"abcd"
    with pytest.raises(ValueError):
        entry.private_file(str(file), 3)
    link = tmp_path / "link"
    try:
        link.symlink_to(file)
    except OSError:
        pytest.skip("Windows symlink privilege absent")
    with pytest.raises(ValueError):
        entry.private_file(str(link), 5)


class EchoSocket:
    subprotocol = None
    def __init__(self):
        self.queue = asyncio.Queue()
    async def send(self, message):
        await self.queue.put(message)
    def __aiter__(self):
        return self
    async def __anext__(self):
        return await self.queue.get()


def test_websocket_denied_before_accept(fixture):
    connected = []
    @asynccontextmanager
    async def connect(*args, **kwargs):
        connected.append(args)
        yield EchoSocket()
    with client_for(fixture, websocket_connect=connect) as client:
        for supplied in ({}, headers(origin="https://evil.invalid"),
                         headers(origin="http://127.0.0.1:3085"), headers("unknown"),
                         {"Origin": fixture[0].public_origin, "X-User-ID": fixture[0].bindings[0].user_id}):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/api/remote.mux", headers=supplied):
                    pass
    assert connected == []


def test_websocket_routes_private_cookie_and_reauth_revokes(fixture):
    connected, closed, auth = [], [], []
    @asynccontextmanager
    async def connect(*args, **kwargs):
        connected.append((args, kwargs))
        try:
            yield EchoSocket()
        finally:
            closed.append(True)
    async def resolver(client, cfg, cookie):
        auth.append(cookie)
        if len(auth) > 1:
            raise HTTPException(403, "revoked")
        return cfg.bindings[0]
    with client_for(fixture, resolver=resolver, websocket_connect=connect, reauth_seconds=0.05) as client:
        with client.websocket_connect("/api/remote.mux", headers=headers()) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "hello"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert closed == [True] and len(auth) == 2
    args, kwargs = connected[0]
    assert args[0] == "ws://127.0.0.1:3085/api/remote.mux"
    assert kwargs["host"] == "personal-0" and kwargs["port"] == 3084
    assert kwargs["extra_headers"] == {"Cookie": "dsh_session=host-0"}
    assert kwargs["origin"] == "http://127.0.0.1:3085"
    assert kwargs["subprotocols"] is None


@pytest.mark.parametrize("index,cookie", [(0, "alice"), (1, "bob")])
def test_websocket_private_authority_still_routes_by_verified_sso(fixture, index, cookie):
    cfg = fixture[0]
    connected = []
    @asynccontextmanager
    async def connect(*args, **kwargs):
        connected.append((args, kwargs))
        yield EchoSocket()
    foreign = cfg.bindings[1 - index]
    forged = headers(cookie, **{
        "host": "attacker.invalid",
        "x-user-id": foreign.user_id,
        "x-user-no": foreign.user_no,
        "x-forwarded-host": "attacker.invalid",
        "origin": cfg.public_origin,
    })
    forged["cookie"] += f"; dsh_session=host-{1-index}"
    with client_for(fixture, websocket_connect=connect) as client:
        with client.websocket_connect("/api/remote.mux", headers=forged) as ws:
            ws.send_text("verified-user-only")
            assert ws.receive_text() == "verified-user-only"
    assert len(connected) == 1
    args, kwargs = connected[0]
    assert args == ("ws://127.0.0.1:3085/api/remote.mux",)
    assert kwargs["host"] == f"personal-{index}"
    assert kwargs["port"] == 3084
    assert kwargs["origin"] == "http://127.0.0.1:3085"
    assert kwargs["extra_headers"] == {"Cookie": f"dsh_session=host-{index}"}
    assert kwargs["subprotocols"] is None
    sso = [r for r in fixture[2] if r.url.host == "sso.invalid"]
    assert len(sso) == 1 and sso[0].headers["cookie"] == "access_token=" + cookie


def test_config_rejects_dynamic_destination_and_shared_binding(fixture):
    cfg = fixture[0]
    from dataclasses import asdict
    for url in ("http://user:pass@host:3084", "http://host:3084/path", "https://host:3084", "http://host?url=x"):
        value = asdict(cfg)
        value["bindings"][0]["upstream"] = url
        with pytest.raises(ValueError):
            entry.Config.parse(value)
    value = asdict(cfg)
    value["bindings"][1]["bootstrap"] = value["bindings"][0]["bootstrap"]
    with pytest.raises(ValueError):
        entry.Config.parse(value)


def test_websocket_closes_when_host_generation_changes(fixture):
    ready_path = fixture[0].bindings[0].bootstrap
    checks = []
    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield EchoSocket()
    async def resolver(_client, cfg, _cookie):
        checks.append(True)
        if len(checks) == 2:
            ready = json.loads(fixture[1][ready_path])
            ready["generation"] = str(uuid4())
            fixture[1][ready_path] = json.dumps(ready).encode()
        return cfg.bindings[0]
    with client_for(fixture, resolver=resolver, websocket_connect=connect, reauth_seconds=0.01) as client:
        with client.websocket_connect("/api/remote.mux", headers=headers()) as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert len(checks) == 2


def test_ambiguous_sso_fields_rejected(fixture):
    def handler(_request):
        return httpx.Response(200, json={"code": 0, "data": {"user_no": "100", "userNo": "101"}})
    with client_for(fixture, handler) as client:
        assert client.get("/", headers=headers()).status_code == 503


def test_hop_header_nominations_stripped(fixture):
    base = fixture[4]
    def handler(request):
        if request.url.host == "sso.invalid":
            return base(request)
        assert "range" not in request.headers
        return httpx.Response(200, text="native", headers={"Connection": "Content-Disposition", "Content-Disposition": "unsafe"})
    with client_for(fixture, handler) as client:
        response = client.get("/", headers=headers(connection="Range", range="bytes=1-3"))
        assert response.status_code == 200
        assert "content-disposition" not in response.headers


def test_duplicate_sso_cookie_rejected(fixture):
    with client_for(fixture) as client:
        response = client.get("/", headers={"Cookie": "access_token=alice; access_token=bob"})
        assert response.status_code == 403
    assert fixture[2] == []
