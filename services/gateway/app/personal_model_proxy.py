"""Personal-model broker: trusted employee -> that login's encrypted key.

SSO exchanges the short-lived IAM access token for a personal key once during
login. Ordinary requests read the encrypted session credential and its model
catalog; they do not renew IAM or call the company credential endpoint again.
No conversation bodies or plaintext credentials are written to Postgres/files.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import struct
import time
import stat
from typing import Protocol
from urllib.parse import urlsplit
import zlib

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .desktop_auth import TrustedActor, require_trusted_actor, validate_trusted_actor
from . import iam_oauth as iam
from . import usage_producer as metering
from .iam_oauth import IAMFailure

HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# A bounded inline image (8 MiB before base64) must fit alongside its prompt.
MAX_BODY = 16 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
DEFAULT_MODEL = "deepseek-v4.1-flash"


@dataclass(frozen=True)
class PersonalModel:
    code: str
    name: str
    input_modalities: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PersonalCredential:
    user_no: str
    api_key: str = field(repr=False)
    expires_at: float
    models: tuple[PersonalModel, ...] = (PersonalModel(DEFAULT_MODEL, "DeepSeek V4.1 Flash"),)


class PersonalKeyProvider(Protocol):
    async def resolve(self, actor: TrustedActor) -> PersonalCredential: ...


class UnconfiguredKeyProvider:
    async def resolve(self, actor: TrustedActor) -> PersonalCredential:
        raise IAMFailure(503, "personal_key_provider_unconfigured")


def _url(value: str, *, synthetic=False) -> str:
    parsed = urlsplit(value)
    dev = os.getenv("IAM_DEV_MODE", "false").lower() == "true"
    local = parsed.hostname in {"127.0.0.1", "localhost", "mock-company"}
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(c in value for c in "\r\n\\\x00")
            or not parsed.hostname or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not (dev and local))
            or (synthetic and not (dev and local))):
        raise IAMFailure(503, "personal_model_configuration_unavailable")
    return value.rstrip("/")


def _secret(path: str) -> str:
    try:
        file = Path(path)
        if not path or not stat.S_ISREG(file.lstat().st_mode) or file.stat().st_size > 8192:
            raise ValueError()
        value = file.read_text(encoding="utf-8").strip()
        if not value or any(c in value for c in "\r\n\x00"):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise IAMFailure(503, "personal_key_configuration_unavailable") from None


class SyntheticKeyProvider:
    """Only for the repository's local mock. This is not a company API contract."""
    def __init__(self, url: str, secret_file: str, transport=None):
        self.url = _url(url, synthetic=True)
        self.secret_file = secret_file
        self.transport = transport

    async def resolve(self, actor: TrustedActor) -> PersonalCredential:
        user_no = actor.user_no
        secret = _secret(self.secret_file)
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False,
                                         transport=self.transport) as client:
                response = await client.post(self.url, headers={"Authorization": "Bearer " + secret},
                                             json={"user_no": user_no})
                if response.status_code in {401, 403, 404}:
                    raise IAMFailure(403, "personal_key_unavailable")
                if response.status_code != 200 or len(response.content) > 16384:
                    raise ValueError()
                value = response.json()
                ttl = value.get("expires_in")
                credential = PersonalCredential(value.get("user_no"), value.get("api_key"),
                                                time.time() + ttl if type(ttl) is int and 0 < ttl <= 3600 else 0)
                validate_credential(credential, user_no)
                return credential
        except IAMFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise IAMFailure(503, "personal_key_service_unavailable") from None


async def server_iam_credential(actor: TrustedActor) -> iam.IAMAccessCredential:
    return await iam._with_service(lambda service: service.personal_access_credential(
        actor.auth_session_id, actor.user_no, actor.account_no))


async def server_personal_credential(actor: TrustedActor, *, legacy_provider) -> PersonalCredential:
    async def read(service):
        if service.config.identity_mode == "directory":
            access = await service.personal_access_credential(
                actor.auth_session_id, actor.user_no, actor.account_no)
            return await legacy_provider.resolve_access(actor.user_no, access)
        return await service.personal_model_credential(
            actor.auth_session_id, actor.user_no, actor.account_no)
    return await iam._with_service(read)


def _model_text(value, limit=200):
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _catalog(value: object) -> tuple[PersonalModel, ...]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("invalid catalog")
    result = []
    codes = set()
    for item in value:
        if (not isinstance(item, dict) or not _model_text(item.get("model_code"))
                or not _model_text(item.get("model_name"), 300)
                or item["model_code"] in codes):
            raise ValueError("invalid catalog")
        codes.add(item["model_code"])
        present = [item[key] for key in ("inputModalities", "input_modalities") if key in item]
        modalities = None
        if present:
            raw = present[0]
            if (any(other != raw for other in present[1:]) or not isinstance(raw, list)
                    or not raw or any(not isinstance(v, str) or v not in {"text", "image"} for v in raw)
                    or len(set(raw)) != len(raw) or "text" not in raw):
                raise ValueError("invalid model modalities")
            modalities = tuple(v for v in ("text", "image") if v in raw)
        result.append(PersonalModel(item["model_code"], item["model_name"], modalities))
    return tuple(result)


def _binding_failure(reason: str, *, detailed=True) -> IAMFailure:
    # Callers supply fixed literals only; never include values from the response.
    suffix = "_" + reason if detailed else ""
    return IAMFailure(503, "personal_key_binding_invalid" + suffix)


def _username_mismatch_reason(actual: str, expected: str) -> str:
    # These comparisons explain a rejected exact match; they never authorize it.
    if not actual:
        return "username_missing"
    if isinstance(expected, str):
        if actual.strip() == expected:
            return "username_whitespace"
        if actual.casefold() == expected.casefold():
            return "username_case"
        if actual.strip().casefold() == expected.casefold():
            return "username_case_whitespace"
    return "username_mismatch"


class TokenKeyProvider:
    """Cached SSO credentials, with a separate login-only company POST contract."""
    def __init__(self, url: str, secret_file: str, transport=None,
                 access_reader=None, *, credential_reader=None):
        self.url = _url(url)
        self.secret_file, self.transport, self.access_reader = secret_file, transport, access_reader
        self.credential_reader = credential_reader

    async def resolve(self, actor: TrustedActor) -> PersonalCredential:
        if not isinstance(actor, TrustedActor):
            raise IAMFailure(401, "trusted_actor_required")
        # Explicit access_reader injection preserves the old provider contract
        # for isolated adapter tests/integrations. Runtime SSO uses only cache.
        if self.access_reader is not None:
            access = await self.access_reader(actor)
            return await self.resolve_access(actor.user_no, access)
        credential = (await self.credential_reader(actor) if self.credential_reader is not None
                      else await server_personal_credential(actor, legacy_provider=self))
        validate_credential(credential, actor.user_no)
        return credential

    async def invalidate(self, actor, credential):
        from . import persistent_login
        await iam._with_service(lambda service: persistent_login.invalidate_credential(service.store, actor.account_no, credential))

    async def resolve_access(self, user_no: str, access: iam.IAMAccessCredential) -> PersonalCredential:
        """Server-only binding check, also usable before an IAM session exists."""
        if (not isinstance(access, iam.IAMAccessCredential) or not iam._valid_access_token(access.access_token)
                or type(access.expires_at) is not int or access.expires_at <= time.time()):
            raise IAMFailure(401, "iam_personal_credentials_require_login")
        secret = _secret(self.secret_file)
        try:
            async with httpx.AsyncClient(timeout=10, verify=True, trust_env=False, follow_redirects=False,
                                         transport=self.transport) as client:
                # No client-provided identity, token, URL or JSON body is accepted.
                async with client.stream("POST", self.url, headers={
                    "X-API-Key": secret, "X-IAM-Access-Token": access.access_token,
                }) as response:
                    if response.status_code in {401, 403, 404}:
                        raise IAMFailure(403, "personal_key_unavailable")
                    if response.status_code != 200:
                        raise ValueError("invalid credential response")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 256 * 1024:
                            raise ValueError("credential response too large")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise _binding_failure("response_shape")
            if value.get("ok") is not True:
                if "ok" not in value:
                    raise _binding_failure("ok_missing")
                if type(value["ok"]) is not bool:
                    raise _binding_failure("ok_type")
                raise _binding_failure("ok_false")
            if not isinstance(value.get("username"), str):
                if value.get("username") is None:
                    raise _binding_failure("username_missing")
                raise _binding_failure("username_type")
            if value["username"] != user_no:
                raise _binding_failure(_username_mismatch_reason(value["username"], user_no))
            credential = PersonalCredential(value["username"], value.get("api_key"),
                                            access.expires_at, _catalog(value.get("models")))
            validate_credential(credential, user_no, binding_diagnostics=True)
            return credential
        except IAMFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            raise IAMFailure(503, "personal_key_service_unavailable") from None


def validate_credential(credential, user_no, *, binding_diagnostics=False):
    # Keep the generic error contract for providers/brokers outside TokenKey.
    def invalid(reason):
        raise _binding_failure(reason, detailed=binding_diagnostics)

    if not isinstance(credential, PersonalCredential):
        invalid("credential_type")
    if credential.user_no != user_no:
        invalid("username_mismatch")
    if not isinstance(credential.api_key, str):
        invalid("key_missing" if credential.api_key is None else "key_type")
    if not credential.api_key:
        invalid("key_empty")
    if len(credential.api_key) > 8192:
        invalid("key_length")
    if credential.api_key != credential.api_key.strip():
        invalid("key_whitespace")
    if any(c in credential.api_key for c in "\r\n\x00"):
        invalid("key_control")
    if type(credential.expires_at) not in {int, float}:
        invalid("lease_type")
    if not math.isfinite(credential.expires_at):
        invalid("lease_nonfinite")
    if credential.expires_at <= time.time():
        invalid("lease_expired")
    if (not isinstance(credential.models, tuple)
            or any(not isinstance(m, PersonalModel) or not _model_text(m.code)
                   or not _model_text(m.name, 300)
                   or (m.input_modalities is not None
                       and m.input_modalities not in (("text",), ("text", "image")))
                   for m in credential.models)
            or len({m.code for m in credential.models}) != len(credential.models)):
        invalid("model_catalog")


def provider_from_env() -> PersonalKeyProvider:
    mode = os.getenv("PERSONAL_KEY_ADAPTER", "tokenkey")
    if mode == "tokenkey":
        return TokenKeyProvider(os.getenv("PERSONAL_KEY_TOKENKEY_URL",
            "https://tokenkey.megmeet.com/api/external/user-credentials"),
            os.getenv("PERSONAL_KEY_SERVICE_SECRET_FILE", ""))
    if mode == "synthetic":
        return SyntheticKeyProvider(os.getenv("PERSONAL_KEY_SYNTHETIC_URL", ""),
                                    os.getenv("PERSONAL_KEY_SERVICE_SECRET_FILE", ""))
    return UnconfiguredKeyProvider()


def allowed_models() -> tuple[str, ...]:
    # Company chat policy fixed by the owner. Employee credentials are still
    # intersected in catalog()/open(); this never creates missing entitlement.
    # Search has its own provider contract and is not changed by this policy.
    return (DEFAULT_MODEL,)


def simulation_mode(base_url: str) -> bool:
    mode = os.getenv("PERSONAL_MODEL_SIMULATION", "false").lower()
    if mode not in {"true", "false"}:
        raise IAMFailure(503, "personal_model_configuration_unavailable")
    if mode == "true":
        # A contract-compatible local mock uses the real TokenKey adapter too.
        # Mark the model service explicitly, never infer reality from the class.
        _url(base_url, synthetic=True)
        return True
    return False


def validate_body(body: object) -> dict:
    allowed = allowed_models()
    if (not isinstance(body, dict) or not _model_text(body.get("model"))
            or (allowed is not None and body["model"] not in allowed)):
        raise IAMFailure(400, "personal_model_not_allowed")
    # These fields must never override the authenticated principal or upstream.
    if set(body) & {"user_no", "actor", "api_key", "base_url", "authorization", "provider_key"}:
        raise IAMFailure(400, "personal_model_identity_field_forbidden")
    messages = body.get("messages")
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 1000
            or any(not isinstance(m, dict) or m.get("role") not in {"system", "developer", "user", "assistant", "tool"}
                   for m in messages)
            or ("stream" in body and type(body["stream"]) is not bool)):
        raise IAMFailure(400, "personal_model_request_invalid")
    if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) > MAX_BODY:
        raise IAMFailure(413, "personal_model_request_too_large")
    result = dict(body)
    # OpenAI's optional attribution field is server-owned as well.
    result.pop("user", None)
    return result


def _vision_challenge() -> tuple[str, str]:
    """An in-memory synthetic image; its random answer never enters the prompt.

    This verifies that the selected endpoint actually reads image pixels. A 200
    response, a model name, or an assertion that it supports vision is not proof.
    PNG has only IHDR/IDAT/IEND, with no text chunk or answer-bearing filename.
    """
    digits = (
        ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
        ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
        ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
        ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
        ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
        ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
        ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
        ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
        ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
        ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    )
    answer = "".join(str(secrets.randbelow(10)) for _ in range(8))
    scale, margin, advance = 8, 16, 56
    width, height = margin * 2 + advance * 8 - 16, margin * 2 + 7 * scale
    pixels = bytearray(b"\xff" * (width * height))
    for index, character in enumerate(answer):
        for row, bits in enumerate(digits[int(character)]):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    offset = (margin + row * scale + dy) * width + margin + index * advance + column * scale
                    pixels[offset:offset + scale] = b"\x00" * scale

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    scanlines = b"".join(b"\x00" + pixels[y * width:(y + 1) * width] for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))
    return answer, "data:image/png;base64," + base64.b64encode(png).decode("ascii")


class _VisionState:
    """Small process-local negotiation cache; never holds a key or user image."""
    def __init__(self):
        self.results = {}
        self.pending = {}
        self.attempts = {}

    def prune(self):
        now = time.monotonic()
        self.results = {key: result for key, result in self.results.items() if result[1] > now}
        self.attempts = {key: [stamp for stamp in stamps if stamp > now - 60]
                         for key, stamps in self.attempts.items() if stamps and stamps[-1] > now - 60}

    def result(self, key):
        result = self.results.get(key)
        return result[0] if result is not None and result[1] > time.monotonic() else None

    def store(self, key, success):
        self.prune()
        if len(self.results) >= 512:
            self.results.pop(min(self.results, key=lambda entry: self.results[entry][1]))
        self.results[key] = success, time.monotonic() + (600 if success else 30)


_VISION_STATE = _VisionState()


class PersonalModelBroker:
    def __init__(self, provider: PersonalKeyProvider, base_url: str, transport=None,
                 actor_validator=validate_trusted_actor, *, vision_state=None):
        self.provider = provider
        self.base_url = _url(base_url)
        self.simulation = simulation_mode(self.base_url)
        self.transport = transport
        self.actor_validator = actor_validator
        self.vision_state = vision_state if vision_state is not None else _VISION_STATE

    async def credential(self, actor: TrustedActor):
        current = await self.actor_validator(actor)
        credential = await self.provider.resolve(current)
        validate_credential(credential, current.user_no)
        # Check again after credential lookup so a logout during decryption
        # cannot authorize a newly submitted model call.
        await self.actor_validator(current)
        validate_credential(credential, current.user_no)
        return current, credential

    def catalog(self, credential: PersonalCredential):
        allowed = allowed_models()
        return tuple(m for m in credential.models if allowed is None or m.code in allowed)

    def _vision_key(self, actor, credential, model=None):
        # The digest binds successful negotiation to this login/key/upstream,
        # preventing another employee or a rotated key from inheriting it.
        return hashlib.sha256(json.dumps([self.base_url, actor.user_no, actor.auth_session_id,
            credential.api_key, model], separators=(",", ":")).encode()).hexdigest()

    def _models(self, actor, credential):
        models = self.catalog(credential)
        data = []
        for model in models:
            item = {"id": model.code, "name": model.name, "object": "model", "owned_by": "megmeet"}
            modalities = model.input_modalities
            if (modalities != ("text",)
                    and self.vision_state.result(self._vision_key(actor, credential, model.code)) is True):
                modalities = ("text", "image")
            if modalities is not None:
                item["inputModalities"] = list(modalities)
            data.append(item)
        return {"object": "list", "data": data,
            "default_model": DEFAULT_MODEL if any(m.code == DEFAULT_MODEL for m in models)
                else models[0].code if models else None}

    async def models(self, actor: TrustedActor):
        current, credential = await self.credential(actor)
        return self._models(current, credential)

    async def _probe_vision(self, actor, credential, model):
        answer, image = _vision_challenge()
        body = {"model": model, "stream": False, "max_tokens": 256, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Read the eight digits shown in the image, left to right. Reply with exactly the eight digits, including any leading zero, and no other text."},
            {"type": "image_url", "image_url": {"url": image}},
        ]}]}
        await self.actor_validator(actor)
        validate_credential(credential, actor.user_no)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), verify=True,
                    trust_env=False, follow_redirects=False, transport=self.transport) as client:
                async with client.stream("POST", self.base_url + "/chat/completions",
                        headers={"Authorization": "Bearer " + credential.api_key}, json=body) as response:
                    if response.status_code in {401, 403}:
                        raise IAMFailure(401, "personal_key_reauthorization_required")
                    if response.status_code == 429:
                        raise IAMFailure(429, "personal_model_vision_busy")
                    if response.status_code in {400, 404, 415, 422}:
                        raise IAMFailure(422, "personal_model_vision_unverified")
                    if response.status_code != 200:
                        raise IAMFailure(502, "personal_model_upstream_unavailable")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise IAMFailure(422, "personal_model_vision_unverified")
            await self.actor_validator(actor)
            validate_credential(credential, actor.user_no)
            content = json.loads(raw)["choices"][0]["message"]["content"]
            # Exact recognition is required; no model-provided support claims.
            if not isinstance(content, str) or content.strip() != answer:
                raise IAMFailure(422, "personal_model_vision_unverified")
        except IAMFailure:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise IAMFailure(502, "personal_model_vision_check_unavailable") from None

    async def ensure_vision(self, actor: TrustedActor, model: str):
        """Negotiate only the employee's selected, authorized model on demand.

        The caller must already hold that employee's current task lease. Probe
        uses a synthetic image only; the real attachment is sent after success.
        """
        if not _model_text(model):
            raise IAMFailure(400, "personal_model_not_allowed")
        current, credential = await self.credential(actor)
        selected = next((item for item in self.catalog(credential) if item.code == model), None)
        if selected is None:
            raise IAMFailure(403, "personal_model_not_authorized")
        if selected.input_modalities == ("text",):
            raise IAMFailure(422, "personal_model_image_not_supported")
        key = self._vision_key(current, credential, model)
        state = self.vision_state
        state.prune()
        cached = state.result(key)
        if cached is False:
            raise IAMFailure(422, "personal_model_vision_unverified")
        if cached is not True:
            task = state.pending.get(key)
            if task is None:
                principal = self._vision_key(current, credential)
                attempts = state.attempts.get(principal, [])
                if (len(state.pending) >= 8 or len(attempts) >= 4
                        or (principal not in state.attempts and len(state.attempts) >= 512)):
                    raise IAMFailure(429, "personal_model_vision_busy")
                state.attempts[principal] = [*attempts, time.monotonic()]

                async def negotiate():
                    try:
                        try:
                            await asyncio.wait_for(self._probe_vision(current, credential, model), timeout=35)
                        except asyncio.TimeoutError:
                            raise IAMFailure(502, "personal_model_vision_check_unavailable") from None
                        state.store(key, True)
                    except IAMFailure as error:
                        if str(error) == "personal_model_vision_unverified":
                            state.store(key, False)
                        raise
                    finally:
                        state.pending.pop(key, None)

                task = asyncio.create_task(negotiate())
                state.pending[key] = task
                # A disconnected waiter cannot cancel a concurrent waiter's
                # bounded synthetic check. Consume its exception if all leave.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            await asyncio.shield(task)
        # Re-resolve after the wait: revocation or key/catalog rotation must not
        # publish capability based on an obsolete authorization snapshot.
        current, credential = await self.credential(current)
        selected = next((item for item in self.catalog(credential) if item.code == model), None)
        if selected is None:
            raise IAMFailure(403, "personal_model_not_authorized")
        if selected.input_modalities == ("text",):
            raise IAMFailure(422, "personal_model_image_not_supported")
        if self._vision_key(current, credential, model) != key:
            raise IAMFailure(401, "personal_key_reauthorization_required")
        return self._models(current, credential)

    async def open(self, actor: TrustedActor, body: dict):
        body = validate_body(body)
        current, credential = await self.credential(actor)
        if body["model"] not in {m.code for m in self.catalog(credential)}:
            raise IAMFailure(403, "personal_model_not_authorized")
        client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), verify=True,
                                   trust_env=False, follow_redirects=False, transport=self.transport)
        meter = metering.model(current)
        try:
            request = client.build_request("POST", self.base_url + "/chat/completions",
                                           headers={"Authorization": "Bearer " + credential.api_key}, json=body)
            response = await client.send(request, stream=True)
            if response.status_code != 200:
                if response.status_code in {401, 403}:
                    await response.aclose()
                    invalidate = getattr(self.provider, "invalidate", None)
                    if invalidate is not None:
                        await invalidate(current, credential)
                    raise IAMFailure(401, "personal_key_reauthorization_required")
                status = 429 if response.status_code == 429 else 502
                await response.aclose()
                raise IAMFailure(status, "personal_model_upstream_unavailable")
            response.extensions["maimi_usage_meter"] = meter
            return client, response, current, credential.expires_at
        except BaseException as exc:
            if meter:
                meter.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
            await client.aclose()
            if isinstance(exc, (IAMFailure, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise IAMFailure(502, "personal_model_upstream_unavailable") from None

    async def complete(self, actor: TrustedActor, body: dict) -> dict:
        client, response, current, expires = await self.open(actor, {**body, "stream": False})
        meter = response.extensions.get("maimi_usage_meter")
        try:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise IAMFailure(502, "personal_model_response_too_large")
            await self.actor_validator(current)
            if time.time() >= expires:
                raise IAMFailure(401, "iam_personal_credentials_require_login")
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
                raise ValueError()
            if meter:
                meter.finish("failed" if result.get("error") is not None else "succeeded", result.get("usage"))
            return result
        except asyncio.CancelledError:
            if meter:
                meter.finish("cancelled")
            raise
        except IAMFailure:
            if meter:
                meter.finish("failed")
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            if meter:
                meter.finish("failed")
            raise IAMFailure(502, "personal_model_response_invalid") from None
        finally:
            if meter:
                meter.finish("unknown")
            await response.aclose()
            await client.aclose()

    async def streaming(self, actor: TrustedActor, body: dict):
        client, response, current, expires = await self.open(actor, {**body, "stream": True})
        meter = response.extensions.get("maimi_usage_meter")
        if "text/event-stream" not in response.headers.get("content-type", ""):
            if meter:
                meter.finish("failed")
            await response.aclose()
            await client.aclose()
            raise IAMFailure(502, "personal_model_response_invalid")

        async def chunks():
            checked = time.monotonic()
            total = 0
            try:
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE or time.time() >= expires:
                        raise IAMFailure(502, "personal_model_stream_ended")
                    if time.monotonic() - checked >= 5:
                        await self.actor_validator(current)
                        checked = time.monotonic()
                    yield chunk
                    # Tap only after forwarding: no metric store await or I/O
                    # is added before the first yielded upstream byte.
                    if meter:
                        meter.feed(chunk)
                if meter:
                    meter.finish()
            except (asyncio.CancelledError, GeneratorExit):
                if meter:
                    meter.finish("cancelled")
                raise
            except (IAMFailure, httpx.HTTPError):
                if meter:
                    meter.finish("failed")
                # Headers have already been sent. Emit a fixed SSE failure,
                # without echoing upstream errors, request data or credentials.
                yield b'data: {"error":{"message":"personal_model_stream_ended"}}\n\n'
            finally:
                if meter:
                    meter.finish("unknown")
                await response.aclose()
                await client.aclose()
        return StreamingResponse(chunks(), media_type="text/event-stream", headers=HEADERS)


def get_broker() -> PersonalModelBroker:
    # No implicit fallback to the shared TokenKey adapter or environment key.
    return PersonalModelBroker(provider_from_env(), os.getenv("PERSONAL_MODEL_BASE_URL", "https://tokenkey.megmeet.com/v1"))


async def cloud_chat_complete(actor: TrustedActor, messages: list[dict], model: str, request_id: str) -> dict:
    if not request_id or len(request_id) > 200:
        raise IAMFailure(400, "cloud_request_id_invalid")
    broker = get_broker()
    response = await broker.complete(actor, {"model": model, "messages": messages})
    try:
        text = response["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError()
        return {"text": text, "model": response.get("model", model), "usage": response.get("usage", {}),
                "simulation": broker.simulation, "model_called": True}
    except (KeyError, IndexError, TypeError, ValueError):
        raise IAMFailure(502, "personal_model_response_invalid") from None


router = APIRouter(prefix="/v1/personal-model")


@router.get("/models")
async def models(request: Request):
    actor = await require_trusted_actor(request)
    return JSONResponse(await get_broker().models(actor), headers=HEADERS)


@router.post("/chat/completions")
async def chat(request: Request):
    actor = await require_trusted_actor(request)
    if request.headers.get("origin") and request.headers["origin"] != os.getenv("IAM_PUBLIC_ORIGIN"):
        raise IAMFailure(403, "personal_model_origin_denied")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise IAMFailure(413, "personal_model_request_too_large")
    try:
        body = validate_body(json.loads(raw))
    except (ValueError, TypeError):
        raise IAMFailure(400, "personal_model_request_invalid") from None
    broker = get_broker()
    if body.get("stream") is True:
        return await broker.streaming(actor, body)
    return JSONResponse(await broker.complete(actor, body), headers=HEADERS)
