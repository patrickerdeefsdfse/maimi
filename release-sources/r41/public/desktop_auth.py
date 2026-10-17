"""Application-owned desktop handoff. This is not IAM-provided PKCE.

The system browser completes confidential-client IAM OAuth on the server. A
desktop proves possession of its in-memory verifier before receiving a short
application credential. IAM secrets and provider tokens never reach the app.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, parse_qs
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import iam_oauth as iam

PREFIX = "/v1/desktop/auth"
PUBLIC_PATHS = frozenset(PREFIX + suffix for suffix in ("/start", "/authorize", "/confirm", "/exchange", "/session", "/renew", "/logout"))
CONFIRM_COOKIE = "megmeet_desktop_confirm"
BEARER_SCOPES = ("/v1/plugins/", "/v1/personal-cloud/", "/v1/personal-model/", "/v1/connectors/", "/v1/office/", PREFIX + "/")
CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
TOKEN = re.compile(r"^mdt_[A-Za-z0-9_-]{43}$")
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True)
class TrustedActor:
    user_no: str
    account_no: str
    display_name: str
    organization_codes: tuple[str, ...]
    auth_session_id: str = field(repr=False)  # Server-only; never serialize directly.

    def public(self) -> dict[str, Any]:
        return {"user_no": self.user_no, "account_no": self.account_no,
                "display_name": self.display_name, "organization_codes": list(self.organization_codes)}

    def platform(self) -> dict[str, Any]:
        return {**self.public(), "auth_provider": "iam_direct",
                "organization_code": self.organization_codes[0] if len(self.organization_codes) == 1 else ""}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def verification_code(flow: str) -> str:
    code = _hash(flow)[:8].upper()
    return code[:4] + "-" + code[4:]


def proof_challenge(verifier: str) -> str:
    if not VERIFIER.fullmatch(verifier):
        raise iam.IAMFailure(400, "desktop_invalid_proof")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def _instance(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value or uuid.UUID(value).version != 4:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise iam.IAMFailure(400, "desktop_invalid_instance") from None
    return value


def _lifetime() -> int:
    try:
        result = int(os.getenv("DESKTOP_APP_TOKEN_TTL_SECONDS", "900"))
        if not 60 <= result <= 900:
            raise ValueError()
        return result
    except ValueError:
        raise iam.IAMFailure(503, "desktop_configuration_unavailable") from None


class RedisDesktopStore:
    def __init__(self, redis):
        self.redis = redis

    def key(self, kind, token):
        return "megmeet:desktop:" + kind + ":" + _hash(token)

    async def create(self, flow, instance, challenge, ttl=300, remember_device=False):
        record = {"instance": instance, "challenge": challenge, "status": "pending", "remember_device": remember_device}
        if not await self.redis.set(self.key("flow", flow), json.dumps(record), ex=ttl, nx=True):
            raise iam.IAMFailure(503, "desktop_store_unavailable")

    async def browser_claim(self, flow):
        script = """local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
        local d=cjson.decode(raw); if d.status~='pending' then return 0 end
        local ttl=redis.call('TTL',KEYS[1]); if ttl<=0 then return 0 end
        d.status='authorizing'; redis.call('SET',KEYS[1],cjson.encode(d),'EX',ttl); return cjson.encode(d)"""
        result = await self.redis.eval(script, 1, self.key('flow', flow))
        if not result:
            raise iam.IAMFailure(410, 'desktop_flow_unavailable')
        return json.loads(result)

    async def attach(self, state, flow):
        if not await self.redis.set(self.key("oauth", state), flow, ex=300, nx=True):
            raise iam.IAMFailure(503, "desktop_store_unavailable")

    async def pop_oauth(self, state):
        return await self.redis.getdel(self.key("oauth", state))

    async def complete(self, flow, sid, expires_at):
        script = """local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
        local d=cjson.decode(raw); if d.status~='authorizing' then return 0 end
        local ttl=redis.call('TTL',KEYS[1]); if ttl<=0 then return 0 end
        d.status='confirming';d.sid=ARGV[1];d.expires_at=tonumber(ARGV[2]);
        redis.call('SET',KEYS[1],cjson.encode(d),'EX',ttl);return 1"""
        if not await self.redis.eval(script, 1, self.key("flow", flow), sid, expires_at):
            raise iam.IAMFailure(410, "desktop_flow_unavailable")

    async def confirmation_create(self, confirmation, flow, binding):
        record = {"flow": flow, "binding": _hash(binding)}
        if not await self.redis.set(self.key("confirmation", confirmation), json.dumps(record), ex=300, nx=True):
            raise iam.IAMFailure(503, "desktop_store_unavailable")

    async def confirm(self, confirmation, binding):
        key = self.key("confirmation", confirmation)
        raw = await self.redis.get(key)
        if raw is None:
            raise iam.IAMFailure(410, "desktop_confirmation_unavailable")
        record = json.loads(raw)
        if not hmac.compare_digest(record["binding"], _hash(binding)):
            raise iam.IAMFailure(403, "desktop_confirmation_denied")
        script = """if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end
        local raw=redis.call('GET',KEYS[2]);if not raw then return 0 end
        local d=cjson.decode(raw);if d.status~='confirming' then return 0 end
        local ttl=redis.call('TTL',KEYS[2]);if ttl<=0 then return 0 end
        d.status='ready';redis.call('SET',KEYS[2],cjson.encode(d),'EX',ttl)
        redis.call('DEL',KEYS[1]);return 1"""
        if not await self.redis.eval(script, 2, key, self.key("flow", record["flow"]), raw):
            raise iam.IAMFailure(410, "desktop_confirmation_unavailable")

    async def consume(self, flow, instance, challenge):
        # A wrong verifier or instance must neither reveal readiness nor consume
        # the legitimate desktop's flow. Compare constant-size SHA256 strings.
        raw = await self.redis.get(self.key("flow", flow))
        if raw is None:
            raise iam.IAMFailure(410, "desktop_flow_unavailable")
        record = json.loads(raw)
        if not (hmac.compare_digest(record["instance"], instance)
                and hmac.compare_digest(record["challenge"], challenge)):
            raise iam.IAMFailure(400, "desktop_invalid_proof")
        if record["status"] != "ready":
            return None
        script = """if redis.call('GET',KEYS[1])==ARGV[1] then
        redis.call('DEL',KEYS[1]);return ARGV[1] else return false end"""
        taken = await self.redis.eval(script, 1, self.key("flow", flow), raw)
        if not taken:
            raise iam.IAMFailure(410, "desktop_flow_unavailable")
        return json.loads(taken)

    async def grant(self, token, sid, instance, ttl, challenge=None):
        record = {"sid": sid, "instance": instance, "expires_at": int(time.time()) + ttl,
                  "renewal_challenge": challenge}
        if not await self.redis.set(self.key("grant", token), json.dumps(record), ex=ttl, nx=True):
            raise iam.IAMFailure(503, "desktop_store_unavailable")

    async def read_grant(self, token):
        raw = await self.redis.get(self.key("grant", token))
        return json.loads(raw) if raw else None

    async def delete_grant(self, token):
        raw = await self.redis.getdel(self.key("grant", token))
        return json.loads(raw) if raw else None

    async def renew_grant(self, token, record, expires_at):
        # Compare the actual persisted grant, require a live underlying login
        # session, and never resurrect a concurrently logged-out credential.
        script = """local raw=redis.call('GET',KEYS[1]);if not raw then return 0 end
        local d=cjson.decode(raw);local expected=cjson.decode(ARGV[1])
        local now=tonumber(redis.call('TIME')[1])
        if d.sid~=expected.sid or d.instance~=expected.instance or
          d.renewal_challenge~=expected.renewal_challenge or d.expires_at<=now or
          not redis.call('GET',KEYS[2]) then return 0 end
        local remaining=redis.call('TTL',KEYS[2]);local session=cjson.decode(redis.call('GET',KEYS[2]))
        local persistent=session.version==4 and session.expires_at==0 and session.login_policy=='device-r40'
        if remaining==-1 and not persistent then return 0 end
        local expires=persistent and remaining==-1 and tonumber(ARGV[2]) or math.min(tonumber(ARGV[2]),now+remaining)
        if expires<=now then return 0 end
        d.expires_at=math.max(d.expires_at,expires)
        redis.call('SET',KEYS[1],cjson.encode(d),'EX',d.expires_at-now)
        return d.expires_at"""
        result = await self.redis.eval(script, 2, self.key('grant', token),
            'megmeet:iam:session:' + iam._digest(record['sid']), json.dumps(record), expires_at)
        if not result:
            raise iam.IAMFailure(401, 'desktop_session_expired')
        return int(result)


def desktop_store(service):
    injected = getattr(service, "desktop_store", None)
    if injected is not None:
        return injected
    redis = getattr(service.store, "redis", None)
    return RedisDesktopStore(redis) if redis is not None else None


def _actor(user, sid):
    return TrustedActor(user["user_no"], user["account_no"], user.get("display_name", ""),
                        tuple(user["organization_codes"]), sid)


async def complete_desktop_callback(service, state, sid, ttl, user):
    store = desktop_store(service)
    if store is None:
        return None  # Existing cookie-service unit fixtures have no Redis.
    flow = await store.pop_oauth(state)
    if flow is None:
        return None
    try:
        # Keep the absolute provider-bounded IAM session. The desktop receives
        # only a short grant and must prove possession to renew within it.
        await store.complete(flow, sid, int(time.time()) + ttl)
        confirmation, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await store.confirmation_create(confirmation, flow, binding)
    except Exception:
        await service.store.session_delete(sid)
        raise
    response = HTMLResponse(
        '<!doctype html><meta charset="utf-8"><title>MEGMEET AI</title>'
        '<h1>确认登录 MEGMEET AI 桌面</h1><p>请核对桌面应用显示的验证码：</p>'
        '<h2>' + verification_code(flow) + '</h2>'
        '<p>仅在验证码与您正在使用的桌面应用一致时确认。不要确认他人发来的登录链接。</p>'
        '<form method="post" action="' + PREFIX + '/confirm">'
        '<input type="hidden" name="confirmation" value="' + confirmation + '">'
        '<button type="submit">确认登录这台桌面</button></form>',
        # A native form POST under no-referrer sends Origin: null in Chromium.
        # Preserve the same-origin CSRF check without exposing callback code/state
        # in Referer. Other auth responses keep the default no-referrer policy.
        headers={**HEADERS, "Referrer-Policy": "strict-origin",
                 "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'"},
    )
    response.set_cookie(CONFIRM_COOKIE, binding, max_age=300, path=PREFIX,
                        httponly=True, secure=service.config.secure_cookie, samesite="strict")
    response.delete_cookie(iam.BINDING_COOKIE, path="/v1/iam", secure=service.config.secure_cookie,
                           httponly=True, samesite="lax")
    return response


async def validate_trusted_actor(actor: TrustedActor) -> TrustedActor:
    if not isinstance(actor, TrustedActor):
        raise iam.IAMFailure(401, "trusted_actor_required")
    async def run(service):
        current = await service.authenticate(actor.auth_session_id)
        if current is None or current["user_no"] != actor.user_no or current["account_no"] != actor.account_no:
            raise iam.IAMFailure(401, "desktop_session_expired")
        return _actor(current, actor.auth_session_id)
    return await iam._with_service(run)


async def authenticate_desktop_request(request: Request) -> TrustedActor | None:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not TOKEN.fullmatch(token):
        raise iam.IAMFailure(401, "desktop_session_invalid")
    async def run(service):
        store = desktop_store(service)
        record = await store.read_grant(token)
        if record is None or record.get("expires_at", 0) <= time.time():
            raise iam.IAMFailure(401, "desktop_session_expired")
        user = await service.authenticate(record["sid"])
        if user is None:
            raise iam.IAMFailure(401, "desktop_session_expired")
        return _actor(user, record["sid"])
    return await iam._with_service(run)


async def require_trusted_actor(request: Request) -> TrustedActor:
    if os.getenv("AUTH_MODE", "iam_direct").lower() != "iam_direct":
        raise iam.IAMFailure(503, "iam_direct_required")
    cached = getattr(request.state, "trusted_actor", None)
    if isinstance(cached, TrustedActor):
        return cached
    if request.headers.get("authorization"):
        actor = await authenticate_desktop_request(request)
        if actor is None:
            raise iam.IAMFailure(401, "desktop_session_invalid")
        return actor
    user = await iam.authenticate_request(request)
    if user is None:
        raise iam.IAMFailure(401, "authentication_required")
    return _actor(user, request.cookies.get(iam.SESSION_COOKIE, ""))


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(max_length=36)
    code_challenge: str = Field(min_length=43, max_length=43)
    remember_device: StrictBool = False


class ExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow_id: str = Field(min_length=40, max_length=128)
    instance_id: str = Field(max_length=36)
    code_verifier: str = Field(min_length=43, max_length=128)


    device_id: str | None = Field(default=None, max_length=36)
    resume_token: str | None = Field(default=None, max_length=47)
    request_id: str | None = Field(default=None, max_length=36)


class RenewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instance_id: str = Field(max_length=36)
    code_verifier: str = Field(min_length=43, max_length=128)


router = APIRouter(prefix=PREFIX)


def _check_mode(request):
    iam._enabled()
    # Native main-process requests have no Origin; reject cross-site web starts.
    origin = request.headers.get("origin")
    if origin and origin != iam.IAMConfig.from_env().public_origin:
        raise iam.IAMFailure(403, "desktop_origin_denied")


@router.post("/start")
async def start(request: Request, body: StartBody):
    _check_mode(request)
    _instance(body.instance_id)
    if not CHALLENGE.fullmatch(body.code_challenge):
        raise iam.IAMFailure(400, "desktop_invalid_proof")
    async def run(service):
        flow = secrets.token_urlsafe(32)
        if body.remember_device:
            if service.config.identity_mode != 'sso_tokenkey':
                raise iam.IAMFailure(503, 'desktop_remembered_login_unavailable')
            await desktop_store(service).create(flow, body.instance_id, body.code_challenge, remember_device=True)
        else:
            await desktop_store(service).create(flow, body.instance_id, body.code_challenge)
        return JSONResponse({"remembered_login": service.config.identity_mode == 'sso_tokenkey',
                             "remembered_login_protocol": 1,
                             "flow_id": flow, "expires_in": 300, "verification_code": verification_code(flow),
                             "browser_url": service.config.public_origin + PREFIX + "/authorize?" + urlencode({"flow": flow})},
                            headers=HEADERS)
    return await iam._with_service(run)


@router.get("/authorize")
async def authorize(request: Request, flow: str = ""):
    _check_mode(request)
    if not iam.OPAQUE.fullmatch(flow):
        raise iam.IAMFailure(400, "desktop_flow_invalid")
    async def run(service):
        store = desktop_store(service)
        claimed = await store.browser_claim(flow)
        if isinstance(claimed, dict) and claimed.get('remember_device') is True:
            location, binding = await service.begin(policy='device-r40')
        else:
            location, binding = await service.begin()
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(location).query)["state"][0]
        await store.attach(state, flow)
        response = RedirectResponse(location, status_code=303, headers=HEADERS)
        response.set_cookie(iam.BINDING_COOKIE, binding, max_age=service.config.state_ttl,
                            httponly=True, secure=service.config.secure_cookie, samesite="lax", path="/v1/iam")
        return response
    return await iam._with_service(run)


@router.post("/confirm")
async def confirm(request: Request):
    _check_mode(request)
    config = iam.IAMConfig.from_env()
    if request.headers.get("origin") != config.public_origin:
        raise iam.IAMFailure(403, "desktop_confirmation_denied")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 512:
            raise iam.IAMFailure(400, "desktop_confirmation_invalid")
    try:
        values = parse_qs(raw.decode("ascii"), strict_parsing=True)
        if set(values) != {"confirmation"} or len(values["confirmation"]) != 1:
            raise ValueError()
        confirmation = values["confirmation"][0]
        binding = request.cookies.get(CONFIRM_COOKIE, "")
        if not iam.OPAQUE.fullmatch(confirmation) or not iam.OPAQUE.fullmatch(binding):
            raise ValueError()
    except (UnicodeError, ValueError, KeyError):
        raise iam.IAMFailure(400, "desktop_confirmation_invalid") from None
    async def run(service):
        await desktop_store(service).confirm(confirmation, binding)
        response = HTMLResponse('<!doctype html><meta charset="utf-8"><title>MEGMEET AI</title>'
                                '<h1>登录完成</h1><p>请返回 MEGMEET AI 桌面应用。可以关闭此浏览器页面。</p>',
                                headers={**HEADERS, "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
        response.delete_cookie(CONFIRM_COOKIE, path=PREFIX, secure=service.config.secure_cookie,
                               httponly=True, samesite="strict")
        return response
    return await iam._with_service(run)


@router.post("/exchange")
async def exchange(request: Request):
    _check_mode(request)
    from .desktop_device_auth import action_payload, action_body
    body = action_body(await action_payload(request), None, ExchangeBody)
    _instance(body.instance_id)
    if not iam.OPAQUE.fullmatch(body.flow_id):
        raise iam.IAMFailure(400, "desktop_flow_invalid")
    challenge = proof_challenge(body.code_verifier)
    async def run(service):
        store = desktop_store(service)
        if any(value is not None for value in (body.device_id, body.resume_token, body.request_id)):
            from .desktop_device_auth import exchange_remembered
            return await exchange_remembered(service, body, challenge)
        record = await store.consume(body.flow_id, body.instance_id, challenge)
        if record is not None and record.get('remember_device') is True:
            await service.store.session_delete(record['sid'])
            raise iam.IAMFailure(400, 'desktop_device_exchange_required')
        if record is None:
            return JSONResponse({"status": "pending"}, status_code=202, headers=HEADERS)
        user = await service.authenticate(record["sid"])
        ttl = min(_lifetime(), int(record["expires_at"] - time.time()))
        if user is None or ttl < 1:
            raise iam.IAMFailure(401, "desktop_session_expired")
        token = "mdt_" + secrets.token_urlsafe(32)
        await store.grant(token, record["sid"], body.instance_id, ttl, challenge)
        return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": ttl, "renewal_supported": True,
                             "user": _actor(user, record["sid"]).public()}, headers=HEADERS)
    return await iam._with_service(run)


@router.post('/renew')
async def renew(request: Request):
    _check_mode(request)
    from . import desktop_device_auth as device
    payload = await device.action_payload(request)
    if 'kind' in payload:
        body = device.action_body(payload, 'device_resume', device.ResumeBody)
        return await device.resume(request, body)
    body = device.action_body(payload, None, RenewBody)
    _instance(body.instance_id)
    challenge = proof_challenge(body.code_verifier)
    authorization = request.headers.get('authorization', '')
    token = authorization[7:] if authorization.startswith('Bearer ') else ''
    if not TOKEN.fullmatch(token):
        raise iam.IAMFailure(401, 'desktop_session_invalid')
    async def run(service):
        store = desktop_store(service)
        record = await store.read_grant(token)
        if not record or record.get('expires_at', 0) <= time.time():
            raise iam.IAMFailure(401, 'desktop_session_expired')
        if (record.get('instance') != body.instance_id or
                not isinstance(record.get('renewal_challenge'), str) or
                not hmac.compare_digest(record['renewal_challenge'], challenge)):
            raise iam.IAMFailure(403, 'desktop_renewal_proof_denied')
        user = await service.authenticate(record['sid'])
        if user is None:
            raise iam.IAMFailure(401, 'desktop_session_expired')
        session_deadline = await service.session_deadline(record['sid'], user['user_no'], user['account_no'])
        deadline = min(int(time.time()) + _lifetime(), session_deadline)
        expires = await store.renew_grant(token, record, deadline)
        return JSONResponse({'expires_in': max(0, expires - int(time.time())),
            'user': _actor(user, record['sid']).public()}, headers=HEADERS)
    return await iam._with_service(run)


@router.get("/session")
async def session(request: Request):
    _check_mode(request)
    if 'capabilities' in request.query_params:
        if list(request.query_params.multi_items()) != [('capabilities', 'remembered-login-v1')]:
            raise iam.IAMFailure(400, 'desktop_request_invalid')
        from .desktop_device_auth import capabilities
        return await capabilities(request)
    actor = await authenticate_desktop_request(request)
    if actor is None:
        raise iam.IAMFailure(401, "desktop_session_invalid")
    return JSONResponse({"authenticated": True, "user": actor.public()}, headers=HEADERS)


@router.post("/logout")
async def logout(request: Request):
    _check_mode(request)
    from . import desktop_device_auth as device
    payload = await device.action_payload(request, allow_empty=True)
    if 'kind' in payload:
        body = device.action_body(payload, 'device_logout', device.DeviceLogoutBody)
        return await device.device_logout(request, body)
    if payload:
        raise iam.IAMFailure(400, 'desktop_request_invalid')
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not TOKEN.fullmatch(token):
        raise iam.IAMFailure(401, "desktop_session_invalid")
    async def run(service):
        record = await desktop_store(service).delete_grant(token)
        if record is not None:
            await service.store.session_delete(record["sid"])
        return JSONResponse({"revoked": True}, headers=HEADERS)
    return await iam._with_service(run)
