"""Fixed two-account native DSH pilot: server-owned identity, cookies and routes.

Run via uvicorn entry_app:app --no-access-log. Container bindings and bootstrap
mounts must be private; this application never provisions Docker containers.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.legacy.client import connect as ws_connect

from entry_login import LOGIN_HEADERS, SCRIPT_HEADERS, login_page, login_script

MAX_BODY = 8 * 1024 * 1024
MAX_WS_MESSAGE = MAX_BODY
MODEL = "deepseek-flash-v4.1"
# The current DSH runtime accepts the private cookie with its loopback web
# authority; the verified public-authority probe is rejected. After browser
# SSO/public-Origin checks, use the fixed inner authority. It never selects a
# user or a TCP destination: Binding.upstream does.
NATIVE_AUTHORITY = "127.0.0.1:3085"
NATIVE_ORIGIN = "http://" + NATIVE_AUTHORITY
NATIVE_WEBSOCKET_ORIGIN = "ws://" + NATIVE_AUTHORITY
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SSO_COOKIE_NAMES = {"access_token", "refresh_token"}
REQUEST_HEADERS = {"accept", "accept-language", "content-type", "range", "if-range"}
RESPONSE_HEADERS = {
    "content-type", "content-disposition", "content-range", "accept-ranges",
    "content-security-policy", "x-content-type-options", "referrer-policy",
}
COOKIE_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+=[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+(?:; [!#$%&'*+.^_`|~0-9A-Za-z-]+=[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+)*$")
DEFAULT_LOGGER = logging.getLogger("megmeet.native-entry.websocket")
# WebSocket DEBUG logging includes handshake headers. Never enable that logger.
DEFAULT_LOGGER.addHandler(logging.NullHandler())
DEFAULT_LOGGER.propagate = False
DEFAULT_LOGGER.setLevel(logging.CRITICAL)

def _accepts_html(value: str) -> bool:
    """Only an explicit, enabled text/html media range requests the login page."""
    for item in value.split(","):
        media, *parameters = item.split(";")
        if media.strip().lower() != "text/html":
            continue
        quality = 1.0
        for parameter in parameters:
            name, separator, raw = parameter.strip().partition("=")
            if separator and name.lower() == "q":
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
        if 0 < quality <= 1:
            return True
    return False


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid UUID")
    return value


def _url(value: Any, *, https: bool, authority_only: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError("invalid fixed endpoint")
    p = urlsplit(value)
    if p.scheme != ("https" if https else "http") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("invalid fixed endpoint")
    _ = p.port
    if authority_only and p.path not in ("", "/"):
        raise ValueError("fixed authority required")
    return value.rstrip("/")


def _contained_path(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + "/"):
        raise ValueError("private mount path required")
    if any(part in (".", "..") for part in value.split("/")) or "\\" in value:
        raise ValueError("invalid private mount path")
    return value


def private_file(path: str, limit: int) -> bytes:
    """Read a bounded regular file without following final or parent symlinks.

    All ancestors are deployment-owned, read-only mounts in the entry container;
    the Host may atomically replace ready.json, never its parent directory.
    """
    p = Path(path)
    for ancestor in (p, *p.parents):
        if ancestor.is_symlink():
            raise ValueError("symlink private file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(p, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid private file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            result = source.read(limit + 1)
        if len(result) > limit:
            raise ValueError("oversized private file")
        return result
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Binding:
    user_id: str
    user_no: str
    upstream: str
    bootstrap: str
    model_token_file: str


@dataclass(frozen=True)
class Config:
    public_origin: str
    sso_url: str
    bindings: tuple[Binding, ...]
    tokenkey_url: str
    tokenkey_secret_file: str

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "Config":
        origin = _url(value["public_origin"], https=True, authority_only=True)
        sso = _url(value["sso_url"], https=True)
        model_url = _url(value["tokenkey_url"], https=True)
        bindings = []
        for item in value["bindings"]:
            employee = item["user_no"]
            if not isinstance(employee, str) or not employee or len(employee) > 128 or employee.strip() != employee or any(ord(c) < 33 for c in employee):
                raise ValueError("invalid employee identity")
            bindings.append(Binding(
                _uuid(item["user_id"]), employee,
                _url(item["upstream"], https=False, authority_only=True),
                _contained_path(item["bootstrap"], "/run/megmeet-bootstrap"),
                _contained_path(item["model_token_file"], "/run/megmeet-entry"),
            ))
        if len(bindings) != 2:
            raise ValueError("pilot requires exactly two bindings")
        for field in ("user_id", "user_no", "upstream", "bootstrap", "model_token_file"):
            if len({getattr(b, field) for b in bindings}) != 2:
                raise ValueError("bindings must be distinct")
        return cls(origin, sso, tuple(bindings), model_url,
                   _contained_path(value["tokenkey_secret_file"], "/run/secrets"))


@dataclass(frozen=True)
class Bootstrap:
    generation: str
    workspace_id: str
    cookie: str


def read_bootstrap(binding: Binding, reader: Callable = private_file) -> Bootstrap:
    try:
        value = json.loads(reader(binding.bootstrap, 16384))
        if value.get("ready") is not True or value.get("owner_id") != binding.user_id:
            raise ValueError("owner mismatch or not ready")
        generation = _uuid(value["generation"])
        workspace = value["workspace_id"]
        cookie = value["cookie"]
        if not isinstance(workspace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace):
            raise ValueError("invalid workspace")
        if not isinstance(cookie, str) or len(cookie) > 4096 or not COOKIE_RE.fullmatch(cookie):
            raise ValueError("invalid host cookie")
        if {part.split("=", 1)[0].lower() for part in cookie.split("; ")} & {"path", "domain", "expires", "samesite", "max-age"}:
            raise ValueError("cookie attributes forbidden")
        return Bootstrap(generation, workspace, cookie)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, "personal workspace unavailable") from None


def _sso_cookies(raw: str) -> str:
    if len(raw) > 16384 or "\r" in raw or "\n" in raw:
        raise HTTPException(403, "authentication required")
    found = {}
    for pair in raw.split(";"):
        name, sep, value = pair.strip().partition("=")
        if name not in SSO_COOKIE_NAMES:
            continue
        if name in found or not sep or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise HTTPException(403, "authentication required")
        found[name] = value
    if not found:
        raise HTTPException(403, "authentication required")
    return "; ".join(f"{name}={value}" for name, value in sorted(found.items()))


async def _resolve_identity(client: httpx.AsyncClient, config: Config, cookie: str) -> Binding:
    # Construct Request directly: never merge a shared client's response cookie jar.
    request = httpx.Request("POST", config.sso_url, headers={"Cookie": cookie, "Accept": "application/json"},
                           extensions={"timeout": {name: 5 for name in ("connect", "read", "write", "pool")}})
    try:
        response = await client.send(request, stream=True)
        try:
            if response.status_code in (401, 403):
                raise HTTPException(403, "authentication required")
            if response.status_code != 200:
                raise HTTPException(503, "authentication service unavailable")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 65536:
                    raise HTTPException(503, "authentication service unavailable")
            payload = json.loads(data)
        finally:
            await response.aclose()
        if not isinstance(payload, dict) or "code" not in payload:
            raise ValueError("invalid SSO response")
        if payload["code"] != 0:
            raise HTTPException(403, "authentication required")
        user = payload["data"]
        if not isinstance(user, dict):
            raise ValueError("invalid SSO user")
        user_no = user.get("user_no", user.get("userNo"))
        if not isinstance(user_no, str) or not user_no:
            raise ValueError("invalid SSO identity")
        if "user_no" in user and "userNo" in user and user["user_no"] != user["userNo"]:
            raise ValueError("ambiguous SSO identity")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "authentication service unavailable") from None
    for binding in config.bindings:
        if binding.user_no == user_no:
            return binding
    raise HTTPException(403, "account not enabled for personal pilot")


async def resolve_identity(client: httpx.AsyncClient, config: Config, cookie: str) -> Binding:
    try:
        async with asyncio.timeout(10):
            return await _resolve_identity(client, config, cookie)
    except TimeoutError:
        raise HTTPException(503, "authentication service unavailable") from None


def browser_request(scope: Any, config: Config, *, websocket: bool = False) -> None:
    origin = scope.headers.get("origin")
    if origin is not None and origin != config.public_origin:
        raise HTTPException(403, "same-origin access required")
    if (websocket or scope.method not in SAFE_METHODS) and origin != config.public_origin:
        raise HTTPException(403, "same-origin access required")
    # Host and Referer never select identity or destination. Origin is checked above.
    query = scope.scope.get("query_string", b"").decode("ascii", "strict")
    if len(query) > 16384:
        raise HTTPException(400, "invalid request")
    if any(name.lower() in {"token", "api_key", "access_token", "refresh_token"} for name, _ in parse_qsl(query)):
        raise HTTPException(400, "external credential exchange is disabled")
    path = scope.scope.get("path", "/")
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(c) < 32 for c in path):
        raise HTTPException(400, "invalid path")
    if any(segment == ".." for segment in unquote(path).split("/")):
        raise HTTPException(400, "invalid path")


async def bounded_body(request: Request) -> bytes:
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "encoded request body is unsupported")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > MAX_BODY:
                raise ValueError()
        except ValueError:
            raise HTTPException(413, "request too large") from None
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > MAX_BODY:
            raise HTTPException(413, "request too large")
    return bytes(result)


class ClosingStream(StreamingResponse):
    def __init__(self, upstream: httpx.Response, headers: dict[str, str]):
        self.upstream = upstream
        super().__init__(upstream.aiter_bytes(), status_code=upstream.status_code, headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.upstream.aclose()


def native_headers(request: Request, config: Config, bootstrap: Bootstrap) -> dict[str, str]:
    # Allowlist prevents forged identity, forwarding, auth, Cookie and hop headers.
    hop = {name.strip().lower() for name in request.headers.get("connection", "").split(",")}
    headers = {name: value for name, value in request.headers.items() if name in REQUEST_HEADERS and name not in hop}
    headers.update({"Host": NATIVE_AUTHORITY, "Origin": NATIVE_ORIGIN,
                    "Cookie": bootstrap.cookie, "Accept-Encoding": "identity"})
    return headers


def response_headers(response: httpx.Response) -> dict[str, str]:
    hop = {name.strip().lower() for name in response.headers.get("connection", "").split(",")}
    headers = {name: value for name, value in response.headers.items() if name in RESPONSE_HEADERS and name not in hop}
    headers["cache-control"] = "private, no-store"
    headers["vary"] = "Cookie"
    if "location" in response.headers:
        location = response.headers["location"]
        parsed = urlsplit(location)
        if parsed.scheme or parsed.netloc or not location.startswith("/") or location.startswith("//") or "\\" in location or parsed.fragment or any(ord(c) < 32 for c in location):
            raise HTTPException(502, "upstream redirect refused")
        if any(name.lower() in {"token", "access_token", "refresh_token", "api_key"} for name, _ in parse_qsl(parsed.query)):
            raise HTTPException(502, "upstream redirect refused")
        headers["location"] = location
    return headers


Resolver = Callable[[httpx.AsyncClient, Config, str], Awaitable[Binding]]


def create_app(config: Config | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
               reader: Callable = private_file, resolver: Resolver = resolve_identity,
               websocket_connect: Callable = ws_connect, reauth_seconds: float = 30) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        current = config
        if current is None:
            path = _contained_path(os.environ.get("MEGMEET_ENTRY_CONFIG", "/run/megmeet-entry/config.json"), "/run/megmeet-entry")
            current = Config.parse(json.loads(reader(path, 65536)))
        application.state.config = current
        application.state.client = httpx.AsyncClient(
            verify=True, trust_env=False, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(connect=5, read=120, write=30, pool=5),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=16),
        )
        # Only bounded, high-entropy scoped tokens are stored; company key is read
        # per model request and never given to a Host or browser.
        tokens = []
        for binding in current.bindings:
            token = reader(binding.model_token_file, 512).decode().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{40,256}", token):
                raise ValueError("invalid scoped model token")
            tokens.append((binding, hashlib.sha256(token.encode()).digest()))
        if tokens[0][1] == tokens[1][1]:
            raise ValueError("scoped tokens must differ")
        application.state.model_tokens = tokens
        try:
            yield
        finally:
            await application.state.client.aclose()

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.exception_handler(HTTPException)
    async def controlled_error(request: Request, exc: HTTPException):
        if (request.method == "GET" and request.scope.get("path") == "/"
                and exc.status_code == 403 and exc.detail == "authentication required"
                and _accepts_html(request.headers.get("accept", ""))):
            # Cold-entry recovery only. Authenticated native pages and API/WS
            # denials keep their existing behavior; no chat RPC is replayed.
            return HTMLResponse(login_page(application.state.config.public_origin),
                                status_code=403, headers=LOGIN_HEADERS)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers={"Cache-Control": "private, no-store", "Vary": "Cookie, Accept"})

    @application.get("/health")
    async def health():
        return {"status": "healthy", "service": "native-entry"}

    @application.get("/__megmeet/login.js")
    async def cold_login_script(request: Request):
        cfg = application.state.config
        try:
            browser_request(request, cfg)
        except UnicodeError:
            raise HTTPException(400, "invalid request") from None
        return Response(login_script(cfg.public_origin), media_type="application/javascript",
                        headers=SCRIPT_HEADERS)

    @application.get("/__megmeet/identity")
    async def identity_status(request: Request):
        cfg = application.state.config
        try:
            browser_request(request, cfg)
        except UnicodeError:
            raise HTTPException(400, "invalid request") from None
        cookie = _sso_cookies(request.headers.get("cookie", ""))
        binding = await resolver(application.state.client, cfg, cookie)
        bootstrap = read_bootstrap(binding, reader)
        return JSONResponse({
            "authenticated": True,
            "user_id": binding.user_id,
            "workspace_id": bootstrap.workspace_id,
        }, headers={"Cache-Control": "private, no-store", "Vary": "Cookie"})

    @application.api_route("/internal/model/v1/{endpoint:path}", methods=["GET", "POST"])
    async def model_proxy(endpoint: str, request: Request):
        cfg = application.state.config
        if request.headers.get("origin") or request.headers.get("cookie"):
            raise HTTPException(403, "internal model access required")
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 300:
            raise HTTPException(403, "internal model access required")
        digest = hashlib.sha256(authorization[7:].encode()).digest()
        authorized = False
        for _binding, expected in application.state.model_tokens:
            authorized = hmac.compare_digest(digest, expected) or authorized
        if not authorized:
            raise HTTPException(403, "internal model access required")
        if request.url.query:
            raise HTTPException(400, "query is unsupported")
        if endpoint == "models" and request.method == "GET":
            return JSONResponse({"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "megmeet"}]},
                                headers={"Cache-Control": "no-store"})
        if endpoint != "chat/completions" or request.method != "POST":
            raise HTTPException(404, "not found")
        body = await bounded_body(request)
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "invalid model request") from None
        if not isinstance(payload, dict) or payload.get("model") != MODEL:
            raise HTTPException(400, "model not enabled for personal pilot")
        try:
            secret = reader(cfg.tokenkey_secret_file, 4096).decode().strip()
            if not secret or any(ord(c) < 33 or ord(c) > 126 for c in secret):
                raise ValueError()
            upstream = await application.state.client.send(httpx.Request(
                "POST", cfg.tokenkey_url + "/chat/completions", content=body,
                headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json", "Accept-Encoding": "identity"},
            ), stream=True)
        except (OSError, ValueError, httpx.HTTPError):
            raise HTTPException(503, "model service unavailable") from None
        if upstream.status_code != 200:
            await upstream.aclose()
            raise HTTPException(503, "model service unavailable")
        return ClosingStream(upstream, {"content-type": upstream.headers.get("content-type", "application/json"), "cache-control": "no-store"})

    @application.websocket("/api/remote.mux")
    async def websocket_proxy(browser: WebSocket):
        cfg = application.state.config
        try:
            browser_request(browser, cfg, websocket=True)
            cookie = _sso_cookies(browser.headers.get("cookie", ""))
            binding = await resolver(application.state.client, cfg, cookie)
            bootstrap = read_bootstrap(binding, reader)
        except (HTTPException, UnicodeError):
            await browser.close(code=1008)
            return
        target = urlsplit(binding.upstream)
        query = browser.scope.get("query_string", b"").decode("ascii")
        protocols = browser.scope.get("subprotocols", [])
        if len(protocols) > 8 or any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", item) for item in protocols):
            await browser.close(code=1008)
            return
        # URI provides the private DSH handshake authority, not the TCP target.
        # Always override host/port with the selected binding's network address.
        uri = NATIVE_WEBSOCKET_ORIGIN + "/api/remote.mux" + ("?" + query if query else "")
        try:
            async with websocket_connect(
                uri, host=target.hostname, port=target.port or 80, origin=NATIVE_ORIGIN,
                extra_headers={"Cookie": bootstrap.cookie}, subprotocols=protocols or None,
                open_timeout=5, close_timeout=3, ping_interval=20, ping_timeout=20,
                max_size=MAX_WS_MESSAGE, max_queue=8, compression=None, logger=DEFAULT_LOGGER,
            ) as upstream:
                await browser.accept(subprotocol=upstream.subprotocol)

                async def from_browser():
                    while True:
                        event = await browser.receive()
                        if event["type"] == "websocket.disconnect":
                            return
                        message = event.get("bytes") if event.get("bytes") is not None else event.get("text")
                        if message is None or len(message if isinstance(message, bytes) else message.encode()) > MAX_WS_MESSAGE:
                            return
                        await upstream.send(message)

                async def from_host():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await browser.send_bytes(message)
                        else:
                            await browser.send_text(message)

                async def refresh_auth():
                    while True:
                        await asyncio.sleep(reauth_seconds)
                        renewed = await resolver(application.state.client, cfg, cookie)
                        ready = read_bootstrap(binding, reader)
                        if renewed != binding or ready.generation != bootstrap.generation or ready.cookie != bootstrap.cookie:
                            return

                tasks = [asyncio.create_task(fn()) for fn in (from_browser, from_host, refresh_auth)]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    # Consume exceptions without logging exception strings/secrets.
                    for task in done:
                        with suppress(Exception):
                            task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        finally:
            with suppress(RuntimeError, WebSocketDisconnect):
                await browser.close(code=1008)

    @application.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def native_proxy(path: str, request: Request):
        cfg = application.state.config
        if path.startswith(("internal/", "__internal", "__megmeet/")) or path == "__megmeet":
            raise HTTPException(404, "not found")
        try:
            browser_request(request, cfg)
        except UnicodeError:
            raise HTTPException(400, "invalid request") from None
        cookie = _sso_cookies(request.headers.get("cookie", ""))
        binding = await resolver(application.state.client, cfg, cookie)
        bootstrap = read_bootstrap(binding, reader)
        body = await bounded_body(request)
        # Copy path/query onto a fixed URL, never urljoin with client input.
        url = httpx.URL(binding.upstream).copy_with(path=request.scope["path"], query=request.scope.get("query_string", b""))
        try:
            upstream = await application.state.client.send(httpx.Request(
                request.method, url, headers=native_headers(request, cfg, bootstrap), content=body,
            ), stream=True)
        except httpx.HTTPError:
            raise HTTPException(503, "personal workspace unavailable") from None
        try:
            headers = response_headers(upstream)
        except HTTPException:
            await upstream.aclose()
            raise
        return ClosingStream(upstream, headers)

    return application


app = create_app()
