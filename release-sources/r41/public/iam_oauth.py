"""Direct IAM OAuth from 1.docx (2026-09-17), with application-owned sessions.

Company SSO uses IAM only during sign-in, including the initial TokenKey
lookup. The resulting personal credential is encrypted in an application-owned
remembered session until logout or administrative revocation after r41 activation.
Legacy directory sessions remain provider-bound.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

if TYPE_CHECKING:
    from .personal_model_proxy import PersonalCredential


SESSION_COOKIE = "megmeet_iam_session"
BINDING_COOKIE = "megmeet_iam_oauth_bind"
WEB_DESTINATION_COOKIE = "megmeet_iam_web_destination"
OPAQUE = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
LOGIN_PATH = "/v1/iam/login"
CALLBACK_PATH = "/v1/iam/callback"
SESSION_PATH = "/v1/iam/session"
LOGOUT_PATH = "/v1/iam/logout"
PUBLIC_PATHS = frozenset({LOGIN_PATH, CALLBACK_PATH, SESSION_PATH, LOGOUT_PATH})
PERSONAL_SESSION_VERSION = 2
REMEMBERED_SESSION_VERSION = 3
WEB_SIGNED_OUT_COOKIE = "megmeet_web_signed_out"
LOGIN_POLICIES = {'web-r40': 0, 'device-r40': 0, 'legacy-r40': 86400}
from . import persistent_login as persistent
MAX_PERSONAL_CIPHERTEXT = 512 * 1024


class _IAMLogFilter(logging.Filter):
    def filter(self, record):
        # HTTPX's INFO logger normally includes request query strings. The IAM
        # contract puts credentials in those queries, so discard that log row.
        message = record.getMessage()
        return not any(path in message for path in (
            "/esc-sso/oauth2.0/", "/v1/iam/callback", "/v1/desktop/auth/authorize"))


logging.getLogger("httpx").addFilter(_IAMLogFilter())
logging.getLogger("uvicorn.access").addFilter(_IAMLogFilter())


class IAMFailure(Exception):
    """Only a fixed, credential-free public message is attached to a failure."""

    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def _url(value: str, *, dev: bool, origin_only: bool = False) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid IAM URL")
    if not parsed.hostname or any(c in value for c in "\r\n\\"):
        raise ValueError("invalid IAM URL")
    local = parsed.hostname in {"127.0.0.1", "localhost", "mock-company"}
    if parsed.scheme != "https" and not (dev and parsed.scheme == "http" and local):
        raise ValueError("IAM requires HTTPS")
    if origin_only and parsed.path not in {"", "/"}:
        raise ValueError("IAM base must be an origin")
    return value.rstrip("/") if origin_only else value


def _integer(name: str, default: int, low: int, high: int) -> int:
    result = int(os.getenv(name, str(default)))
    if not low <= result <= high:
        raise ValueError("invalid IAM lifetime")
    return result


@dataclass(frozen=True)
class IAMConfig:
    base_url: str
    authorization_base_url: str
    client_id: str
    client_secret_file: str
    redirect_uri: str
    public_origin: str
    post_login_path: str
    logout_return_uri: str
    state_ttl: int = 300
    session_ttl: int = 7200
    secure_cookie: bool = True
    redis_url: str = ""
    identity_mode: str = "directory"
    maimi_session_ttl: int = 86400

    @classmethod
    def from_env(cls) -> "IAMConfig":
        try:
            dev = os.getenv("IAM_DEV_MODE", "false").lower() == "true"
            base = _url(os.environ["IAM_BASE_URL"], dev=dev, origin_only=True)
            public = _url(os.environ["IAM_PUBLIC_ORIGIN"], dev=dev, origin_only=True)
            authorization = _url(os.getenv("IAM_AUTHORIZATION_BASE_URL", base), dev=dev, origin_only=True)
            redirect = _url(os.environ["IAM_REDIRECT_URI"], dev=dev)
            logout = _url(os.environ["IAM_LOGOUT_RETURN_URI"], dev=dev)
            for target in (redirect, logout):
                p = urlsplit(target)
                if f"{p.scheme}://{p.netloc}" != public:
                    raise ValueError("IAM callback/return must use public origin")
            if urlsplit(redirect).path != CALLBACK_PATH:
                raise ValueError("IAM callback path mismatch")
            path = os.getenv("IAM_POST_LOGIN_PATH", "/")
            if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n\\?#"):
                raise ValueError("invalid post-login path")
            client_id = os.environ["IAM_CLIENT_ID"].strip()
            secret_file = os.environ["IAM_CLIENT_SECRET_FILE"].strip()
            redis_url = os.environ["REDIS_URL"].strip()
            if not client_id or len(client_id) > 200 or not secret_file or not redis_url:
                raise ValueError("missing IAM configuration")
            if urlsplit(redis_url).scheme not in {"redis", "rediss", "unix"}:
                raise ValueError("invalid IAM session store")
            identity_mode = os.getenv("IAM_IDENTITY_MODE", "directory")
            if identity_mode not in {"directory", "sso_tokenkey"}:
                raise ValueError("invalid identity mode")
            return cls(base, authorization, client_id, secret_file, redirect, public,
                       path, logout, _integer("IAM_STATE_TTL_SECONDS", 300, 60, 600),
                       _integer("IAM_SESSION_TTL_SECONDS", 7200, 60, 86400),
                       not (dev and public.startswith("http://")), redis_url, identity_mode,
                       _integer("MAIMI_SESSION_TTL_SECONDS", 86400, 3600, 2592000))
        except (KeyError, ValueError) as exc:
            raise IAMFailure(503, "iam_configuration_unavailable") from exc

    def secret(self) -> str:
        try:
            path = Path(self.client_secret_file)
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError("invalid client secret file")
            value = path.read_text(encoding="utf-8").strip()
            if not value or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
                raise ValueError("invalid client secret value")
            return value
        except (OSError, UnicodeError, ValueError) as exc:
            raise IAMFailure(503, "iam_configuration_unavailable") from exc


def account_revocation_key(account: str) -> str:
    return 'megmeet:iam:account-revocation:' + hashlib.sha256(account.encode('utf-8')).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class RedisIAMStore:
    """State is atomically consumed only by its browser; sessions are opaque."""

    def __init__(self, redis_url: str):
        from redis.asyncio import Redis
        self.redis = Redis.from_url(redis_url, decode_responses=True,
                                    socket_connect_timeout=3, socket_timeout=3)

    async def close(self):
        await self.redis.aclose()

    async def state_create(self, state: str, binding: str, ttl: int):
        ok = await self.redis.set("megmeet:iam:state:" + _digest(state), _digest(binding), ex=ttl, nx=True)
        if not ok:
            raise IAMFailure(503, "iam_session_store_unavailable")

    async def state_create_policy(self, state, binding, ttl, policy):
        if policy not in LOGIN_POLICIES:
            raise IAMFailure(503, 'iam_configuration_unavailable')
        script = """if redis.call('EXISTS',KEYS[1])==1 then return 0 end
        redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[3])
        redis.call('SET',KEYS[2],ARGV[2],'EX',ARGV[3]);return 1"""
        if not await self.redis.eval(script, 2, 'megmeet:iam:state:'+_digest(state),
                'megmeet:iam:state-policy:'+_digest(state), _digest(binding), policy, ttl):
            raise IAMFailure(503, 'iam_session_store_unavailable')

    async def state_consume_policy(self, state, binding):
        script = """if redis.call('GET',KEYS[1])~=ARGV[1] then return false end
        local policy=redis.call('GET',KEYS[2]) or 'legacy'
        redis.call('DEL',KEYS[1],KEYS[2]);return policy"""
        return await self.redis.eval(script, 2, 'megmeet:iam:state:'+_digest(state),
            'megmeet:iam:state-policy:'+_digest(state), _digest(binding))

    async def account_revocation_version(self, account):
        raw = await self.redis.get(account_revocation_key(account))
        if raw is None:
            return 0
        if not isinstance(raw, str) or not re.fullmatch(r'0|[1-9][0-9]{0,14}', raw):
            raise IAMFailure(503, 'iam_session_store_unavailable')
        return int(raw)

    async def state_consume(self, state: str, binding: str) -> bool:
        script = """if redis.call('GET', KEYS[1]) == ARGV[1] then
          redis.call('DEL', KEYS[1]); return 1 else return 0 end"""
        return bool(await self.redis.eval(script, 1, "megmeet:iam:state:" + _digest(state), _digest(binding)))

    async def session_create(self, sid: str, account_no: str, user_no: str, ttl: int,
                             encrypted_token: str, expires_at: int):
        data = json.dumps({"account_no": account_no, "user_no": user_no,
                           "iam_token_encrypted": encrypted_token,
                           "expires_at": expires_at}, separators=(",", ":"))
        ok = await self.redis.set("megmeet:iam:session:" + _digest(sid), data, ex=ttl, nx=True)
        if not ok:
            raise IAMFailure(503, "iam_session_store_unavailable")

    async def session_read(self, sid: str) -> dict | None:
        raw = await self.redis.get("megmeet:iam:session:" + _digest(sid))
        if raw is None:
            return None
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or set(result) not in (
                    {"account_no", "user_no"},
                    {"account_no", "user_no", "iam_token_encrypted", "expires_at"},
                    {"version", "account_no", "user_no", "personal_credential_encrypted", "expires_at"},
                    {"version", "account_no", "user_no", "personal_credential_encrypted", "expires_at", "login_policy", "revocation_version"}):
                raise ValueError("invalid session")
            if any(not isinstance(result[k], str) or not result[k] for k in ("account_no", "user_no")):
                raise ValueError("invalid session")
            if "iam_token_encrypted" in result:
                if (not isinstance(result["iam_token_encrypted"], str)
                        or not 1 <= len(result["iam_token_encrypted"]) <= 16384
                        or type(result["expires_at"]) is not int):
                    raise ValueError("invalid session")
                if result["expires_at"] <= time.time():
                    await self.session_delete(sid)
                    return None
            if "personal_credential_encrypted" in result:
                if (type(result["version"]) is not int or result["version"] not in (PERSONAL_SESSION_VERSION, REMEMBERED_SESSION_VERSION, persistent.VERSION)
                        or not isinstance(result["personal_credential_encrypted"], str)
                        or not 1 <= len(result["personal_credential_encrypted"]) <= MAX_PERSONAL_CIPHERTEXT
                        or type(result["expires_at"]) is not int):
                    raise ValueError("invalid session")
                if not persistent.expiry_valid(result):
                    await self.session_delete(sid)
                    return None
            return result
        except (ValueError, TypeError) as exc:
            raise IAMFailure(503, "iam_session_store_unavailable") from exc

    async def session_create_personal(self, sid: str, account_no: str, user_no: str, ttl: int,
                                      encrypted_credential: str, expires_at: int):
        data = json.dumps({"version": PERSONAL_SESSION_VERSION, "account_no": account_no,
                           "user_no": user_no, "personal_credential_encrypted": encrypted_credential,
                           "expires_at": expires_at}, separators=(",", ":"))
        ok = await self.redis.set("megmeet:iam:session:" + _digest(sid), data, ex=ttl, nx=True)
        if not ok:
            raise IAMFailure(503, "iam_session_store_unavailable")

    async def session_create_personal_policy(self, sid, account_no, user_no, ttl,
            encrypted_credential, expires_at, policy, revocation_version):
        if (policy not in LOGIN_POLICIES or type(ttl) is not int
                or (not 3600 <= ttl <= 2592000 if policy == 'legacy-r40' else ttl != persistent.policy_ttl(policy))):
            raise IAMFailure(503, 'iam_configuration_unavailable')
        data = json.dumps({'version': persistent.VERSION if ttl == 0 else REMEMBERED_SESSION_VERSION, 'account_no': account_no,
            'user_no': user_no, 'personal_credential_encrypted': encrypted_credential,
            'expires_at': expires_at, 'login_policy': policy, 'revocation_version': revocation_version},
            separators=(',', ':'))
        script = """local epoch=redis.call('GET',KEYS[2]) or '0'
        if epoch~=ARGV[3] then return 0 end
        if ARGV[2]=='0' then return redis.call('SET',KEYS[1],ARGV[1],'NX') and 1 or 0 end
        return redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2],'NX') and 1 or 0"""
        if not await self.redis.eval(script, 2, 'megmeet:iam:session:'+_digest(sid),
                account_revocation_key(account_no), data, ttl, revocation_version):
            raise IAMFailure(401, 'iam_session_revoked')

    async def session_delete(self, sid: str):
        if OPAQUE.fullmatch(sid):
            await self.redis.delete("megmeet:iam:session:" + _digest(sid))

    async def session_remaining_ttl(self, sid: str) -> int:
        return await self.redis.ttl("megmeet:iam:session:" + _digest(sid))


def _valid_access_token(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 8192
            and value == value.strip() and not any(c in value for c in "\r\n\x00"))


@dataclass(frozen=True)
class IAMAccessCredential:
    access_token: str = field(repr=False)
    expires_at: int


def _token_cipher() -> Fernet:
    """Independent server secret; stored credentials stay encrypted in Redis AOF."""
    try:
        path = Path(os.environ["IAM_TOKEN_ENCRYPTION_KEY_FILE"])
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 128:
            raise ValueError()
        return Fernet(path.read_bytes().strip())
    except (KeyError, OSError, ValueError, TypeError):
        raise IAMFailure(503, "iam_token_encryption_unavailable") from None


def _encrypt_access_token(sid: str, account_no: str, user_no: str,
                          token: str, expires_at: int) -> str:
    payload = {"session": _digest(sid), "account_no": account_no, "user_no": user_no,
               "access_token": token, "expires_at": expires_at}
    return _token_cipher().encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")


def _decrypt_access_token(record: dict, sid: str) -> IAMAccessCredential:
    encrypted = record.get("iam_token_encrypted")
    if not isinstance(encrypted, str) or not 1 <= len(encrypted) <= 16384:
        raise IAMFailure(401, "iam_personal_credentials_require_login")
    try:
        payload = json.loads(_token_cipher().decrypt(encrypted.encode("ascii")))
        if (not isinstance(payload, dict) or payload.get("session") != _digest(sid)
                or payload.get("account_no") != record["account_no"]
                or payload.get("user_no") != record["user_no"]
                or type(payload.get("expires_at")) is not int
                or payload["expires_at"] != record.get("expires_at")
                or payload["expires_at"] <= time.time()
                or not _valid_access_token(payload.get("access_token"))):
            raise ValueError()
        return IAMAccessCredential(payload["access_token"], payload["expires_at"])
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise IAMFailure(401, "iam_personal_credentials_require_login") from None


def _encrypt_personal_credential(sid: str, account_no: str, user_no: str,
                                 credential: Any, expires_at: int, *, policy=None, revocation_version=0) -> str:
    from .personal_model_proxy import validate_credential
    validate_credential(credential, user_no)
    if len(credential.models) > 1000:
        raise IAMFailure(503, "personal_key_binding_invalid")
    payload = {"version": PERSONAL_SESSION_VERSION, "session": _digest(sid),
               "account_no": account_no, "user_no": user_no, "expires_at": expires_at,
               "api_key": credential.api_key,
               "models": [{"model_code": model.code, "model_name": model.name,
                           **({"inputModalities": list(model.input_modalities)}
                              if model.input_modalities is not None else {})}
                          for model in credential.models]}
    if policy is not None:
        if policy not in LOGIN_POLICIES or type(revocation_version) is not int or revocation_version < 0:
            raise IAMFailure(503, 'iam_configuration_unavailable')
        payload.update(version=persistent.VERSION if expires_at == 0 and policy in persistent.POLICIES else REMEMBERED_SESSION_VERSION, login_policy=policy,
                       revocation_version=revocation_version)
    encrypted = _token_cipher().encrypt(json.dumps(payload, separators=(",", ":"),
                                                   ensure_ascii=False).encode()).decode("ascii")
    if len(encrypted) > MAX_PERSONAL_CIPHERTEXT:
        raise IAMFailure(503, "personal_key_binding_invalid")
    return encrypted


def _decrypt_personal_credential(record: dict, sid: str) -> PersonalCredential:
    from .personal_model_proxy import PersonalCredential, _catalog, validate_credential
    encrypted = record.get("personal_credential_encrypted")
    remembered = record.get('version') in (REMEMBERED_SESSION_VERSION, persistent.VERSION)
    extra = {'login_policy', 'revocation_version'} if remembered else set()
    if (set(record) != {'version', 'account_no', 'user_no', 'expires_at', 'personal_credential_encrypted'} | extra
            or type(record.get('version')) is not int or record['version'] not in (PERSONAL_SESSION_VERSION, REMEMBERED_SESSION_VERSION, persistent.VERSION)
            or remembered and (record.get('login_policy') not in LOGIN_POLICIES
                or type(record.get('revocation_version')) is not int or record['revocation_version'] < 0)
            or type(record.get("expires_at")) is not int
            or not isinstance(encrypted, str) or not 1 <= len(encrypted) <= MAX_PERSONAL_CIPHERTEXT):
        raise IAMFailure(401, "iam_personal_credentials_require_login")
    cipher = _token_cipher()
    try:
        payload = json.loads(cipher.decrypt(encrypted.encode("ascii")))
        if (not isinstance(payload, dict)
                or set(payload) != {"version", "session", "account_no", "user_no", "expires_at", "api_key", "models"} | extra
                or type(payload["version"]) is not int or payload["version"] != record["version"]
                or any(payload.get(key) != record.get(key) for key in extra)
                or payload["session"] != _digest(sid)
                or payload["account_no"] != record["account_no"]
                or payload["user_no"] != record["user_no"]
                or type(payload["expires_at"]) is not int
                or payload["expires_at"] != record.get("expires_at")
                or not persistent.expiry_valid(record)):
            raise ValueError()
        credential = PersonalCredential(payload["user_no"], payload["api_key"],
                                        persistent.deadline(record), _catalog(payload["models"]))
        validate_credential(credential, record["user_no"])
        return credential
    except IAMFailure:
        raise IAMFailure(401, "iam_personal_credentials_require_login") from None
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise IAMFailure(401, "iam_personal_credentials_require_login") from None


async def directory_actor(account_no: str) -> dict[str, Any]:
    # Separate synchronization module owns the schema, freshness and enablement.
    from .iam_directory import resolve_account, DirectoryUnavailable, DirectoryError
    try:
        actor = await resolve_account(account_no)
    except DirectoryUnavailable as exc:
        raise IAMFailure(503, "iam_directory_unavailable") from exc
    except DirectoryError as exc:
        raise IAMFailure(403, "iam_account_not_authorized") from exc
    return actor


def safe_actor(actor: dict[str, Any], account_no: str) -> dict[str, Any]:
    if not isinstance(actor, dict) or actor.get("account_no") != account_no:
        raise IAMFailure(403, "iam_account_not_authorized")
    user_no = actor.get("user_no")
    orgs = actor.get("organization_codes")
    if not isinstance(user_no, str) or not user_no or len(user_no) > 100:
        raise IAMFailure(403, "iam_account_not_authorized")
    if not isinstance(orgs, list) or any(not isinstance(v, str) or not v for v in orgs):
        raise IAMFailure(403, "iam_account_not_authorized")
    # Keep all departments. Legacy single-department APIs fail closed for multi-
    # department users until their enterprise authorization layer supports lists.
    return {"user_no": user_no, "account_no": account_no,
            "display_name": str(actor.get("display_name") or ""),
            "organization_codes": list(dict.fromkeys(orgs)),
            "organization_code": orgs[0] if len(set(orgs)) == 1 else "",
            "auth_provider": "iam_direct"}


class IAMService:
    def __init__(self, config: IAMConfig, store: Any,
                 mapper: Callable[[str], Awaitable[dict]] = directory_actor,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config, self.store, self.mapper, self.transport = config, store, mapper, transport

    async def profile(self, token: str) -> str:
        """Read the application account from IAM, never from browser claims."""
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False,
                                         verify=True, transport=self.transport) as client:
                response = await client.get(self.config.base_url + "/esc-sso/oauth2.0/profile",
                                            params={"access_token": token})
                if response.status_code in {400, 401, 403}:
                    raise IAMFailure(401, "iam_login_rejected")
                if response.status_code != 200 or len(response.content) > 65536:
                    raise ValueError("invalid profile response")
                profile = response.json()
                attributes = profile.get("attributes") if isinstance(profile, dict) else None
                account = attributes.get("username") if isinstance(attributes, dict) else None
                if (not isinstance(account, str) or not account or len(account) > 200
                        or account != account.strip() or any(ord(c) < 32 or ord(c) == 127 for c in account)):
                    raise ValueError("missing application account")
                return account
        except IAMFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise IAMFailure(503, "iam_service_unavailable") from None

    def sso_actor(self, account: str) -> dict:
        # This explicit company mode requires profile.username == TokenKey's
        # employee username. It never guesses from IAM's opaque id or a name.
        # No synchronized department information means no department grants.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", account):
            raise IAMFailure(403, "iam_employee_account_invalid")
        return safe_actor({"account_no": account, "user_no": account,
                           "display_name": account, "organization_codes": []}, account)

    async def bind_sso_identity(self, account: str, token: str, expires_at: int) -> tuple[dict, PersonalCredential]:
        from .personal_model_proxy import TokenKeyProvider
        if os.getenv("PERSONAL_KEY_ADAPTER", "tokenkey") != "tokenkey":
            raise IAMFailure(503, "iam_configuration_unavailable")
        actor = self.sso_actor(account)
        epoch = await self.store.account_revocation_version(account) if hasattr(self.store,"account_revocation_version") else 0
        self._personal_credential_epoch = epoch
        cached = await persistent.cached_credential(self.store, account, epoch)
        if cached is not None:
            return actor, cached
        provider = TokenKeyProvider(os.getenv("PERSONAL_KEY_TOKENKEY_URL",
            "https://tokenkey.megmeet.com/api/external/user-credentials"),
            os.getenv("PERSONAL_KEY_SERVICE_SECRET_FILE", ""), transport=self.transport)
        credential = await provider.resolve_access(account, IAMAccessCredential(token, expires_at))
        await persistent.save_credential(self.store, account, credential, epoch)
        return actor, credential

    async def begin(self, policy=None) -> tuple[str, str]:
        state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if policy is None:
            await self.store.state_create(state, binding, self.config.state_ttl)
        else:
            await self.store.state_create_policy(state, binding, self.config.state_ttl, policy)
        query = urlencode({"client_id": self.config.client_id, "response_type": "code",
                           "redirect_uri": self.config.redirect_uri, "state": state,
                           "oauth_timestamp": str(int(time.time() * 1000))})
        return self.config.authorization_base_url + "/esc-sso/oauth2.0/authorize?" + query, binding

    async def exchange(self, code: str, state: str, binding: str) -> tuple[str, int, dict]:
        if not OPAQUE.fullmatch(state) or not OPAQUE.fullmatch(binding):
            raise IAMFailure(400, "iam_invalid_login_state")
        if not code or len(code) > 2048 or any(c in code for c in "\r\n\x00"):
            raise IAMFailure(400, "iam_invalid_login_code")
        # Consume before exchanging. Even on a timeout, code cannot be retried.
        consume = getattr(self.store, 'state_consume_policy', None)
        policy = await consume(state, binding) if consume else ('legacy' if await self.store.state_consume(state, binding) else None)
        if policy not in {'legacy', *LOGIN_POLICIES}:
            raise IAMFailure(400, "iam_invalid_login_state")
        self.login_policy = policy
        params = {"grant_type": "authorization_code", "oauth_timestamp": str(int(time.time() * 1000)),
                  "client_id": self.config.client_id, "client_secret": self.config.secret(),
                  "code": code, "redirect_uri": self.config.redirect_uri}
        exchange_started = monotonic()
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False,
                                         verify=True, transport=self.transport) as client:
                # User-confirmed contract: URL-encoded form body, not query.
                # HTTPX encodes redirect_uri once from its exact registered value.
                response = await client.post(self.config.base_url + "/esc-sso/oauth2.0/accessToken", data=params)
                if response.status_code in {400, 401, 403}:
                    raise IAMFailure(401, "iam_login_rejected")
                if response.status_code != 200 or len(response.content) > 65536:
                    raise IAMFailure(503, "iam_service_unavailable")
                token_data = response.json()
                if not isinstance(token_data, dict):
                    raise ValueError("invalid token envelope")
                token = token_data.get("access_token")
                token_type = token_data.get("token_type")
                expiry = token_data.get("expires_in")
                if (not _valid_access_token(token)
                        or not isinstance(token_type, str) or token_type.lower() != "bearer"
                        or isinstance(expiry, bool) or not str(expiry).isdigit() or int(expiry) <= 0):
                    raise ValueError("invalid token response")
                ttl = int(expiry) if self.config.identity_mode == "sso_tokenkey" else min(self.config.session_ttl, int(expiry))
                account = await self.profile(token)
        except IAMFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise IAMFailure(503, "iam_service_unavailable") from exc
        if self.config.identity_mode == "sso_tokenkey":
            actor, personal_credential = await self.bind_sso_identity(account, token,
                int(time.time()) + int(ttl - (monotonic() - exchange_started)))
        elif self.config.identity_mode == "directory":
            actor = safe_actor(await self.mapper(account), account)
        else:
            raise IAMFailure(503, "iam_configuration_unavailable")
        # The IAM proof must remain valid until identity and personal-key lookup
        # finish. Company sign-in then starts its application authorization;
        # it never reuses the short IAM token after this exchange.
        ttl = int(ttl - (monotonic() - exchange_started))
        if ttl <= 0:
            raise IAMFailure(401, "iam_login_expired")
        sid = secrets.token_urlsafe(32)
        if self.config.identity_mode == "sso_tokenkey":
            ttl = persistent.policy_ttl(policy) if policy in LOGIN_POLICIES else self.config.maimi_session_ttl
            if type(ttl) is not int or not (ttl == 0 and policy in persistent.POLICIES or 3600 <= ttl <= 2592000):
                raise IAMFailure(503, "iam_configuration_unavailable")
            expires_at = 0 if ttl == 0 else int(time.time()) + ttl
            if policy == 'legacy' and not hasattr(self.store, 'account_revocation_version'):
                encrypted = _encrypt_personal_credential(sid, account, actor['user_no'], personal_credential, expires_at)
                await self.store.session_create_personal(sid, account, actor['user_no'], ttl, encrypted, expires_at)
            else:
                epoch = getattr(self,"_personal_credential_epoch",None)
                if epoch is None:
                    epoch = await self.store.account_revocation_version(account)
                stored_policy = 'legacy-r40' if policy == 'legacy' else policy
                encrypted = _encrypt_personal_credential(sid, account, actor['user_no'], personal_credential,
                    expires_at, policy=stored_policy, revocation_version=epoch)
                await self.store.session_create_personal_policy(sid, account, actor['user_no'], ttl,
                    encrypted, expires_at, stored_policy, epoch)
            return sid, ttl or persistent.LEASE_SECONDS, actor
        expires_at = int(time.time()) + ttl
        encrypted = _encrypt_access_token(sid, account, actor["user_no"], token, expires_at)
        await self.store.session_create(sid, account, actor["user_no"], ttl, encrypted, expires_at)
        return sid, ttl, actor

    async def authenticate(self, sid: str) -> dict | None:
        if not OPAQUE.fullmatch(sid):
            return None
        record = await self.store.session_read(sid)
        if record is None:
            return None
        if self.config.identity_mode == "sso_tokenkey":
            # v1 IAM-token sessions cannot be upgraded into a longer-lived local
            # authorization. They require one fresh SSO exchange.
            _decrypt_personal_credential(record, sid)
            record = await persistent.promote_session(self, sid, record)
            actor = self.sso_actor(record["account_no"])
            await self._live_personal_deadline(sid, record)
        elif self.config.identity_mode == "directory":
            if "version" in record:
                raise IAMFailure(401, "iam_personal_credentials_require_login")
            actor = safe_actor(await self.mapper(record["account_no"]), record["account_no"])
        else:
            raise IAMFailure(503, "iam_configuration_unavailable")
        if not hmac.compare_digest(actor["user_no"].encode(), record["user_no"].encode()):
            await self.store.session_delete(sid)
            raise IAMFailure(403, "iam_identity_changed")
        await self._check_account_revocation(record)
        return actor

    async def _check_account_revocation(self, record):
        # No IAM refresh/introspection contract is assumed. An explicit local
        # operator epoch bump revokes existing sessions without a public API.
        method = getattr(self.store, 'account_revocation_version', None)
        if method is not None:
            actual = await method(record['account_no'])
            if actual != record.get('revocation_version', 0):
                raise IAMFailure(401, 'iam_session_revoked')

    async def _live_personal_deadline(self, sid: str, record: dict) -> int:
        remaining = await self.store.session_remaining_ttl(sid)
        if (type(remaining) is not int or (remaining <= 0 and not (remaining == -1 and persistent.persistent(record)))
                or not persistent.expiry_valid(record)):
            raise IAMFailure(401, "iam_session_expired")
        deadline = persistent.deadline(record)
        if remaining > 0:
            deadline = min(deadline, int(time.time()) + remaining)
        # Read again after TTL I/O. An intervening logout or replacement must not
        # produce a credential or revive the application session.
        if await self.store.session_read(sid) != record or deadline <= time.time():
            raise IAMFailure(401, "iam_session_expired")
        await self._check_account_revocation(record)
        return deadline

    async def personal_model_credential(self, sid: str, user_no: str, account_no: str) -> PersonalCredential:
        from .personal_model_proxy import PersonalCredential
        if self.config.identity_mode != "sso_tokenkey":
            raise IAMFailure(401, "iam_personal_credentials_require_login")
        current = await self.authenticate(sid)
        if current is None or current["user_no"] != user_no or current["account_no"] != account_no:
            raise IAMFailure(401, "desktop_session_expired")
        record = await self.store.session_read(sid)
        if record is None:
            raise IAMFailure(401, "desktop_session_expired")
        if record.get("user_no") != user_no or record.get("account_no") != account_no:
            raise IAMFailure(401, "iam_personal_credentials_require_login")
        credential = _decrypt_personal_credential(record, sid)
        deadline = await self._live_personal_deadline(sid, record)
        return PersonalCredential(credential.user_no, credential.api_key, deadline, credential.models)

    async def session_deadline(self, sid: str, user_no: str, account_no: str) -> int:
        if self.config.identity_mode == "sso_tokenkey":
            return int((await self.personal_model_credential(sid, user_no, account_no)).expires_at)
        return (await self.personal_access_credential(sid, user_no, account_no)).expires_at

    async def personal_access_credential(self, sid: str, user_no: str, account_no: str) -> IAMAccessCredential:
        if self.config.identity_mode == "sso_tokenkey":
            raise IAMFailure(401, "iam_personal_credentials_require_login")
        current = await self.authenticate(sid)
        if current is None or current["user_no"] != user_no or current["account_no"] != account_no:
            raise IAMFailure(401, "desktop_session_expired")
        # Read after directory revalidation, so a concurrent logout cannot yield
        # a credential from a previously cached actor.
        record = await self.store.session_read(sid)
        if record is None:
            raise IAMFailure(401, "desktop_session_expired")
        if (record.get("user_no") != user_no or record.get("account_no") != account_no
                or type(record.get("expires_at")) is not int
                or record["expires_at"] <= time.time()):
            # Older tokenless sessions need a fresh login; never borrow a key.
            raise IAMFailure(401, "iam_personal_credentials_require_login")
        credential = _decrypt_access_token(record, sid)
        remaining = await self.store.session_remaining_ttl(sid)
        if type(remaining) is not int or remaining <= 0:
            raise IAMFailure(401, "desktop_session_expired")
        # Administrative revocation/expiry may shorten Redis TTL after the
        # OAuth exchange. No model or application lease can extend it.
        return IAMAccessCredential(credential.access_token,
                                   min(credential.expires_at, int(time.time()) + remaining))


def _headers():
    return {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


async def _with_service(action: Callable[[IAMService], Awaitable[Any]]):
    config = IAMConfig.from_env()
    try:
        store = RedisIAMStore(config.redis_url)
    except Exception as exc:
        raise IAMFailure(503, "iam_session_store_unavailable") from exc
    try:
        return await action(IAMService(config, store))
    except IAMFailure:
        raise
    except Exception as exc:
        raise IAMFailure(503, "iam_session_store_unavailable") from exc
    finally:
        try:
            await store.close()
        except Exception:
            pass


async def authenticate_request(request: Request) -> dict | None:
    return await _with_service(lambda service: service.authenticate(request.cookies.get(SESSION_COOKIE, "")))


def _enabled():
    if os.getenv("AUTH_MODE", "iam_direct").lower() != "iam_direct":
        raise IAMFailure(404, "iam_direct_not_enabled")


router = APIRouter()


@router.get(LOGIN_PATH)
async def login(request: Request):
    async def run(service):
        destination=request.query_params.get('return_to')
        if destination is not None and (destination!='/maimi/web/' or len(request.query_params.getlist('return_to'))!=1):
            raise IAMFailure(400,'iam_return_path_invalid')
        location, binding = await service.begin(policy='web-r40')
        response = RedirectResponse(location, status_code=303, headers=_headers())
        response.set_cookie(BINDING_COOKIE, binding, max_age=service.config.state_ttl,
                            httponly=True, secure=service.config.secure_cookie,
                            samesite="lax", path="/v1/iam")
        # Only a fixed local product destination is supported. Binding the
        # marker to this OAuth flow prevents stale/cross-flow redirect changes.
        if destination:
            response.set_cookie(WEB_DESTINATION_COOKIE,_digest(binding),max_age=service.config.state_ttl,
                httponly=True,secure=service.config.secure_cookie,samesite='lax',path='/v1/iam')
        else:
            response.delete_cookie(WEB_DESTINATION_COOKIE,path='/v1/iam',secure=service.config.secure_cookie,
                httponly=True,samesite='lax')
        return response
    try:
        _enabled()
        return await _with_service(run)
    except IAMFailure as exc:
        return JSONResponse({"detail": exc.code}, status_code=exc.status, headers=_headers())


@router.get(CALLBACK_PATH)
async def callback(request: Request):
    async def run(service):
        if len(request.query_params.getlist("code")) != 1 or len(request.query_params.getlist("state")) != 1:
            raise IAMFailure(400, "iam_invalid_callback")
        sid, ttl, _actor = await service.exchange(request.query_params["code"], request.query_params["state"],
                                                 request.cookies.get(BINDING_COOKIE, ""))
        from .desktop_auth import complete_desktop_callback
        desktop_response = await complete_desktop_callback(
            service, request.query_params["state"], sid, ttl, _actor)
        if desktop_response is not None:
            return desktop_response
        if getattr(service, 'login_policy', 'legacy') == 'device-r40':
            await service.store.session_delete(sid)
            raise IAMFailure(410, 'desktop_flow_unavailable')
        old = request.cookies.get(SESSION_COOKIE, "")
        if old:
            await service.store.session_delete(old)
        web_marker=request.cookies.get(WEB_DESTINATION_COOKIE,'')
        binding=request.cookies.get(BINDING_COOKIE,'')
        destination='/maimi/web/' if binding and hmac.compare_digest(web_marker,_digest(binding)) else service.config.post_login_path
        response = RedirectResponse(service.config.public_origin + destination,
                                    status_code=303, headers=_headers())
        response.delete_cookie(WEB_SIGNED_OUT_COOKIE, path='/', secure=service.config.secure_cookie,
                               httponly=True, samesite='lax')
        response.set_cookie(SESSION_COOKIE, sid, max_age=persistent.COOKIE_SECONDS if getattr(service,"login_policy",None) in persistent.POLICIES else ttl, httponly=True,
                            secure=service.config.secure_cookie, samesite="lax", path="/")
        response.delete_cookie(BINDING_COOKIE, path="/v1/iam", secure=service.config.secure_cookie,
                               httponly=True, samesite="lax")
        response.delete_cookie(WEB_DESTINATION_COOKIE,path='/v1/iam',secure=service.config.secure_cookie,
            httponly=True,samesite='lax')
        return response
    try:
        _enabled()
        return await _with_service(run)
    except IAMFailure as exc:
        return JSONResponse({"detail": exc.code}, status_code=exc.status, headers=_headers())


@router.get(SESSION_PATH)
async def session_status(request: Request):
    try:
        _enabled()
        actor = await authenticate_request(request)
        response = JSONResponse({"authenticated": actor is not None, "user": actor},
                            status_code=200 if actor else 401, headers=_headers())
        return persistent.refresh_cookie(response,request,IAMConfig.from_env().secure_cookie) if actor else response
    except IAMFailure as exc:
        return JSONResponse({"authenticated": False, "detail": exc.code}, status_code=exc.status, headers=_headers())


@router.post(LOGOUT_PATH)
async def logout(request: Request):
    async def run(service):
        # The browser sends Origin on same-origin POST. No arbitrary return URL.
        if request.headers.get("origin") != service.config.public_origin:
            raise IAMFailure(403, "iam_origin_denied")
        await service.store.session_delete(request.cookies.get(SESSION_COOKIE, ""))
        location = service.config.authorization_base_url + "/esc-sso/logout?" + urlencode({"service": service.config.logout_return_uri})
        response = JSONResponse({"logout_url": location}, headers=_headers())
        response.set_cookie(WEB_SIGNED_OUT_COOKIE, '1', path='/', max_age=604800,
                            secure=service.config.secure_cookie, httponly=True, samesite='lax')
        response.delete_cookie(SESSION_COOKIE, path="/", secure=service.config.secure_cookie,
                               httponly=True, samesite="lax")
        response.delete_cookie(BINDING_COOKIE, path="/v1/iam", secure=service.config.secure_cookie,
                               httponly=True, samesite="lax")
        return response
    try:
        _enabled()
        return await _with_service(run)
    except IAMFailure as exc:
        return JSONResponse({"detail": exc.code}, status_code=exc.status, headers=_headers())
