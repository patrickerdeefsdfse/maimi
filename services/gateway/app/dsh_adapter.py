from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class DshConversationRequest:
    conversation_id: str
    input_text: str
    provider_key: str = "tokenkey"
    # The model field is the platform model route key, not an upstream name.
    model: str = "deepseek-flash-v4.1"
    workspace_key: str | None = None


@dataclass(frozen=True)
class DshConversationResult:
    status: str
    conversation_id: str
    text: str
    events: list[dict[str, Any]]


class DshBackend(Protocol):
    async def run(self, request: DshConversationRequest) -> DshConversationResult: ...


class DshAgentAdapter:
    """Single integration seam for DSH SDK/runtime version changes.

    The SDK implementation is kept behind this seam so DSH upgrades do not
    leak into Gateway routes. The Gateway never exposes the SDK object.
    """

    def __init__(
        self,
        backend: DshBackend | None = None,
        unavailable_reason: str | None = None,
    ) -> None:
        self.backend = backend
        self.unavailable_reason = unavailable_reason

    @classmethod
    def from_environment(cls) -> "DshAgentAdapter":
        """Use the DSH SDK when enabled; otherwise use the TokenKey REST seam."""
        if os.getenv("DSH_ENABLED", "false").lower() == "true":
            try:
                return cls(DshSdkBackend())
            except RuntimeError as exc:
                return cls(unavailable_reason=str(exc))
        if os.getenv("TOKENKEY_ENABLED", "false").lower() == "true":
            try:
                return cls(TokenKeyBackend())
            except RuntimeError as exc:
                return cls(unavailable_reason=str(exc))
        return cls()

    async def run(self, request: DshConversationRequest) -> DshConversationResult:
        if self.backend is None:
            raise RuntimeError(self.unavailable_reason or "DSH backend is not configured")
        return await self.backend.run(request)


class DshSdkBackend:
    """Thin adapter around the DSH Python SDK.

    The import remains optional for the core image, while the DSH-enabled image
    pins a tested SDK/runtime wheel. The gateway never exposes the SDK object or
    API key to callers.
    """

    def __init__(self) -> None:
        try:
            from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
        except ImportError as exc:
            raise RuntimeError("DSH SDK is not installed in this image") from exc
        self._harness_type = DeepSeekHarness
        self._config_type = DeepSeekHarnessConfig

    async def run(self, request: DshConversationRequest) -> DshConversationResult:
        return await asyncio.to_thread(self._run_sync, request)

    def _run_sync(self, request: DshConversationRequest) -> DshConversationResult:
        workspace_root = Path(os.getenv("DSH_WORKSPACE_ROOT", "/var/lib/megmeet/workspaces"))
        dsh_home_root = Path(os.getenv("DSH_HOME_ROOT", "/var/lib/megmeet/dsh"))
        workspace_key = request.workspace_key or request.conversation_id
        workspace = workspace_root / workspace_key
        dsh_home = dsh_home_root / workspace_key / request.conversation_id
        workspace.mkdir(parents=True, exist_ok=True)
        dsh_home.mkdir(parents=True, exist_ok=True)
        cache_root = dsh_home / ".cache"
        cache_root.mkdir(parents=True, exist_ok=True)

        api_key = os.getenv("MODEL_GATEWAY_API_KEY", "")
        api_key_file = os.getenv("MODEL_GATEWAY_API_KEY_FILE", "").strip()
        if not api_key_file and os.getenv("TOKENKEY_ENABLED", "false").lower() == "true":
            api_key_file = os.getenv("TOKENKEY_API_KEY_FILE", "/run/secrets/tokenkey-api-key").strip()
        if api_key_file:
            key_path = Path(api_key_file)
            if key_path.is_file() and not key_path.is_symlink():
                api_key = key_path.read_text(encoding="utf-8").strip()

        provider = request.provider_key
        try:
            provider_map = json.loads(os.getenv("DSH_PROVIDER_MAP_JSON", ""))
            if isinstance(provider_map, dict):
                provider = str(provider_map.get(provider, provider))
        except (TypeError, ValueError):
            pass

        if provider == "tokenkey":
            provider = "deepseek-official"

        base_url = os.getenv("MODEL_GATEWAY_BASE_URL", "").strip()
        if not base_url and os.getenv("TOKENKEY_ENABLED", "false").lower() == "true":
            base_url = os.getenv("TOKENKEY_BASE_URL", "https://tokenkey.megmeet.com/v1").strip()

        config = self._config_type(
            provider=provider,
            model=request.model,
            cwd=str(workspace),
            runtime_cwd=str(workspace),
            dsh_home=str(dsh_home),
            profile=os.getenv("DSH_PROFILE", "sdk"),
            base_url=base_url,
            api_key=api_key,
            max_tokens=int(os.getenv("DSH_MAX_TOKENS", "4096")),
            env={
                "HOME": str(dsh_home),
                "XDG_CACHE_HOME": str(cache_root),
            },
        )
        harness = self._harness_type(config)
        result = harness.run(request.input_text, session_id=request.conversation_id)
        text = getattr(result, "final_response", None) or getattr(result, "text", None) or str(result)
        return DshConversationResult(
            status="completed",
            conversation_id=request.conversation_id,
            text=text,
            events=[],
        )


class TokenKeyBackend:
    """OpenAI-compatible client for the internal TokenKey service."""

    def __init__(self) -> None:
        self.base_url = os.getenv(
            "TOKENKEY_BASE_URL", "https://tokenkey.megmeet.com/v1"
        ).rstrip("/")
        key_file = os.getenv(
            "TOKENKEY_API_KEY_FILE", "/run/secrets/tokenkey-api-key"
        ).strip()
        if not key_file:
            raise RuntimeError("TOKENKEY_API_KEY_FILE is not configured")
        path = Path(key_file)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"TokenKey secret is not a regular file: {path}")
        self.api_key = path.read_text(encoding="utf-8").strip()
        if not self.api_key:
            raise RuntimeError("TokenKey secret is empty")

    async def run(self, request: DshConversationRequest) -> DshConversationResult:
        return await asyncio.to_thread(self._run_sync, request)

    def _run_sync(self, request: DshConversationRequest) -> DshConversationResult:
        payload = {
            "model": request.model,
            "messages": [{"role": "user", "content": request.input_text}],
        }
        try:
            tls_verify = os.getenv("TOKENKEY_TLS_VERIFY", "true").lower() not in {
                "0",
                "false",
                "no",
            }
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=float(os.getenv("TOKENKEY_TIMEOUT_SECONDS", "120")),
                verify=tls_verify,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("TokenKey request failed") from exc

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("TokenKey response did not contain choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise RuntimeError("TokenKey response did not contain message content")
        return DshConversationResult(
            status="completed",
            conversation_id=request.conversation_id,
            text=text,
            events=[],
        )
