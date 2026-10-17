"""Bounded, read-only MES logistics lookup. Credentials and identity are server-owned.

No real provider request is enabled merely by supplying a waybill. Kuaidi100 is
optional and requires explicit server credentials plus a carrier-code mapping.
Classification rules describe the supplied MES workbook, not global numbering.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .kuaidi100_mcp import (
    Kuaidi100MCPClient,
    Kuaidi100MCPConfig as MCPTransportConfig,
    Kuaidi100MCPError,
)

MES_ENDPOINT = "http://api.megmeet.com:9080/api/MesApi/ApiFromObjectValue?sqlid=472E7EFC-26F9-4B1C-B90F-85A054401FA4"
KUAIDI100_ENDPOINT = "https://poll.kuaidi100.com/poll/query.do"
KUAIDI100_CREDENTIAL_FILE = "/run/secrets/maimi-logistics-kuaidi100.json"
KUAIDI100_MCP_CREDENTIAL_FILE = "/run/secrets/maimi-logistics-kuaidi100-mcp.json"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_RESULT_BYTES = 85_000
MAX_TRACKING_REQUESTS = 20
RULE_SOURCE = "MES_WORKBOOK_SNAPSHOT"
Authorize = Callable[[], bool | None | Awaitable[bool | None]]
PhoneResolver = Callable[[str, str, str, str], str | None | Awaitable[str | None]]
_ORDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
_WAYBILL = re.compile(r"[A-Za-z0-9]{6,32}\Z", re.ASCII)
_IDENTITY = re.compile(r"[A-Za-z0-9_.@-]{1,128}\Z", re.ASCII)
_RULES = sorted([
    ("9100", "思邦"), ("8100", "思邦"), ("85", "思邦"),
    ("思邦", "思邦"), ("出账", "思邦"), ("JD", "京东"),
    ("ZY", "中通"), ("中通", "中通"), ("KY", "跨越"),
    ("SF", "顺丰快递"), ("DP", "德邦"),
    ("德邦", "德邦"), ("DHL", "DHL"), ("联邦", "联邦"),
    ("fedex", "飞帆(国外)"), ("8", "优速快递"), ("壹米滴答", "优速快递"),
    ("汇森", "优速快递"), ("z", "自提"), ("普通", "普通"),
    ("客户自提", "客户自提"), ("世邦", "世邦"), ("进代管仓", "进代管仓"),
    ("公司车", "公司车"), ("成达", "成达"),
], key=lambda pair: (-len(pair[0]), pair[0].casefold()))
_NAME_ONLY = {"普通", "客户自提", "世邦", "进代管仓", "公司车", "成达", "飞帆", "德邦专车", "自提"}
_PHONE_CARRIERS = {"shunfeng", "shunfengkuaiyun", "zhongtong"}
_TRACKING_ERRORS = {"PHONE_REQUIRED", "PHONE_INVALID", "PHONE_MISMATCH", "RATE_LIMITED", "PROVIDER_UNAVAILABLE", "PROVIDER_INVALID_RESPONSE", "PROVIDER_AUTH_ERROR", "QUOTA_EXHAUSTED", "CARRIER_UNSUPPORTED", "TRACKING_TIMEOUT", "TRACKING_BUDGET_EXCEEDED"}
_MCP_ERROR_CODES = {
    "MCP_AUTH_REJECTED": "PROVIDER_AUTH_ERROR",
    "MCP_RATE_LIMITED": "RATE_LIMITED",
    "MCP_REQUEST_TIMEOUT": "TRACKING_TIMEOUT",
    "MCP_QUERY_LIMIT": "TRACKING_BUDGET_EXCEEDED",
    "MCP_RESPONSE_INVALID": "PROVIDER_INVALID_RESPONSE",
    "MCP_RESPONSE_TOO_LARGE": "PROVIDER_INVALID_RESPONSE",
    "MCP_SECRET_ECHO": "PROVIDER_INVALID_RESPONSE",
    "MCP_TOOL_RESULT_INVALID": "PROVIDER_INVALID_RESPONSE",
    "MCP_QUERY_CONTRACT_UNAVAILABLE": "PROVIDER_INVALID_RESPONSE",
}


@dataclass
class _MCPQueryContext:
    stack: AsyncExitStack
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Kuaidi100MCPClient | None = None
    unavailable: str | None = None
    phone_parameter_available: bool = False


def _strict_json_object(raw: bytes | str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate")
            value[key] = item
        return value
    result = json.loads(raw, object_pairs_hook=unique_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    if not isinstance(result, dict):
        raise ValueError("object")
    return result


def _mcp_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Accept structured JSON only; tool text is data, never executable advice."""
    if result.get("isError") is True:
        raise LogisticsQueryError("PROVIDER_UNAVAILABLE")
    if "structuredContent" in result:
        payload = result["structuredContent"]
    else:
        content = result.get("content")
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
        try:
            payload = _strict_json_object(content[0]["text"])
        except (ValueError, TypeError, RecursionError):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE") from None
    if not isinstance(payload, dict):
        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
    # The public SDK sets isError=false even for message-only business errors.
    if "message" in payload and "kuaidiNum" not in payload:
        raise LogisticsQueryError("PROVIDER_UNAVAILABLE")
    return payload


def _event_datetime(value: str) -> datetime | None:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?", value, re.ASCII):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Kuaidi100's local timestamps are Chinese local time, not the gateway
        # host's possibly different timezone. Explicit offsets take priority.
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        return None


def _apply_mcp_trace(row: dict[str, Any], result: dict[str, Any]) -> None:
    payload = _mcp_payload(result)
    if (payload.get("kuaidiNum") != row["waybill"]
            or any(not isinstance(payload.get(key), str) for key in ("kuaidiCom", "kuaidiName", "state", "fromTo"))
            or not isinstance(payload.get("data"), list)
            or len(payload["kuaidiCom"]) > 80 or len(payload["kuaidiName"]) > 80
            or len(payload["state"]) > 80):
        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
    events = payload["data"]
    if len(events) > 2048:
        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
    parsed: list[tuple[datetime | None, dict[str, Any]]] = []
    for event in events:
        if (not isinstance(event, dict)
                or any(not isinstance(event.get(key), str) for key in ("time", "status", "context"))):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
        parsed.append((_event_datetime(event["time"]), event))
    # Keep workbook-derived carrier visible; provider identification is a
    # separate field, especially when the enterprise prefix rule differs.
    row["tracking_carrier_name"] = payload["kuaidiName"]
    row["tracking_carrier_code"] = payload["kuaidiCom"]
    row["tracking_state_text"] = payload["state"]
    if not events:
        row["tracking_status"] = "NO_TRACKING"
        return
    row["tracking_status"] = {
        "在途": "IN_TRANSIT", "揽收": "COLLECTED", "已揽收": "COLLECTED",
        "签收": "DELIVERED", "已签收": "DELIVERED", "派件": "OUT_FOR_DELIVERY",
        "派送中": "OUT_FOR_DELIVERY", "退回": "RETURNED", "拒收": "REFUSED",
        "异常": "EXCEPTION",
    }.get(payload["state"], "TRACKING_AVAILABLE")
    if any(stamp is None for stamp, _ in parsed):
        # An unparseable event might be the latest; do not pick another one and
        # call it latest. Preserve provider state with an explicit limitation.
        row["tracking_time_unverified"] = True
        return
    _, latest = max(parsed, key=lambda pair: pair[0])
    row["latest_event"] = latest["context"][:1024]
    row["event_time"] = latest["time"]
    row["event_truncated"] = len(latest["context"]) > 1024


class LogisticsQueryError(Exception):
    """Stable error code only; never includes provider bodies or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Kuaidi100Config:
    customer: str = field(repr=False)
    key: str = field(repr=False)
    carrier_codes: Mapping[str, str] = field(default_factory=dict)
    phone_resolver: PhoneResolver | None = field(default=None, repr=False)
    single_process_rate_guard: bool = False


@dataclass(frozen=True)
class Kuaidi100MCPConfig:
    key: str = field(repr=False)
    single_process_rate_guard: bool = False
    phone_resolver: PhoneResolver | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LogisticsConfig:
    enabled: bool = False
    mes_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    kuaidi100: Kuaidi100Config | None = field(default=None, repr=False)
    kuaidi100_mcp: Kuaidi100MCPConfig | None = field(default=None, repr=False)
    mes_user_code: str | None = field(default=None, repr=False)
    request_timeout_seconds: float = 8.0
    total_timeout_seconds: float = 45.0
    concurrency: int = 4
    max_source_records_per_order: int = 32
    max_rows: int = 200
    max_text_characters: int = 2048

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LogisticsConfig":
        env = os.environ if environ is None else environ
        def flag(name: str) -> bool:
            value = env.get(name, "false").lower()
            if value not in {"true", "false"}:
                raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
            return value == "true"
        enabled = flag("MAIMI_LOGISTICS_ENABLED")
        provider_enabled = flag("MAIMI_LOGISTICS_KUAIDI100_ENABLED")
        mcp_enabled = flag("MAIMI_LOGISTICS_KUAIDI100_MCP_ENABLED")
        mes_user_code = env.get("MAIMI_LOGISTICS_MES_USER_CODE") or None
        if mes_user_code is not None and mes_user_code != "M00001402":
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if provider_enabled and mcp_enabled:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if not provider_enabled and not mcp_enabled:
            return cls(enabled=enabled, mes_user_code=mes_user_code)
        if not enabled:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if (not flag("MAIMI_LOGISTICS_KUAIDI100_SINGLE_PROCESS")
                or env.get("WEB_CONCURRENCY", "1") != "1"
                or env.get("UVICORN_WORKERS", "1") != "1"):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        try:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "geteuid"):
                raise ValueError("platform")
            fd = os.open(KUAIDI100_MCP_CREDENTIAL_FILE if mcp_enabled else KUAIDI100_CREDENTIAL_FILE, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.geteuid() or info.st_size > 16_384):
                    raise ValueError("permissions")
                data = _strict_json_object(stream.read(16_385))
            if mcp_enabled:
                if (info.st_size > 8192 or not isinstance(data, dict) or set(data) != {"key"}
                        or not isinstance(data["key"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", data["key"], re.ASCII)):
                    raise ValueError("shape")
                return cls(enabled=True, mes_user_code=mes_user_code,
                           kuaidi100_mcp=Kuaidi100MCPConfig(key=data["key"], single_process_rate_guard=True))
            if (not isinstance(data, dict) or set(data) != {"customer", "key", "carrier_codes"}
                    or not isinstance(data["customer"], str) or not isinstance(data["key"], str)
                    or not isinstance(data["carrier_codes"], dict)
                    or not 1 <= len(data["customer"]) <= 256 or not 1 <= len(data["key"]) <= 512):
                raise ValueError("shape")
            return cls(enabled=True, mes_user_code=mes_user_code, kuaidi100=Kuaidi100Config(**data, single_process_rate_guard=True))
        except Exception:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION") from None

    def require_ready(self) -> None:
        if not self.enabled:
            raise LogisticsQueryError("NOT_CONFIGURED")

    def availability(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "mes_ready": self.enabled,
                "tracking_ready": bool(self.enabled and (self.kuaidi100_mcp or self.kuaidi100 and self.kuaidi100.carrier_codes)),
                "provider": ("kuaidi100-mcp" if self.kuaidi100_mcp else "kuaidi100") if self.enabled and (self.kuaidi100 or self.kuaidi100_mcp) else None}


def validate_arguments(args: Mapping[str, Any]) -> tuple[list[str], bool]:
    if not isinstance(args, Mapping) or set(args) - {"order_codes", "include_tracking"}:
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    codes = args.get("order_codes")
    tracking = args.get("include_tracking", True)
    if not isinstance(codes, list) or not 1 <= len(codes) <= 20 or type(tracking) is not bool:
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    if any(not isinstance(code, str) or not _ORDER.fullmatch(code) for code in codes):
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    return list(dict.fromkeys(code.upper() for code in codes)), tracking


def _carrier(text: str) -> str | None:
    folded = text.casefold()
    for prefix, carrier in _RULES:
        if folded.startswith(prefix.casefold()):
            return carrier
    return None


def classify_references(text: str) -> list[tuple[str | None, str | None]]:
    """Parse only complete delimited references; ambiguous prose stays intact."""
    stripped = text.strip()
    if not stripped or stripped in _NAME_ONLY:
        return [(None, _carrier(stripped))]
    parts = re.split(r"[,，;；|\r\n]+", stripped)
    parsed: list[tuple[str | None, str | None]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        carrier = _carrier(part)
        candidate = part
        grouped = re.fullmatch(r"(?:[A-Za-z]{1,5}[0-9]{0,8}|[0-9]{3,4})[ \t]+[0-9]{3,4}(?:[ \t]+[0-9]{3,4})+", part)
        if grouped and carrier and carrier != "自提":
            candidate = re.sub(r"[ \t]", "", part)
        # Explicit carrier label with a separator is safe to remove. Arbitrary
        # surrounding prose is not; do not extract substrings from notes.
        labelled = re.fullmatch(r"([^:：\s]+)\s*[:：]\s*([A-Za-z0-9]{6,32})", part)
        if labelled:
            label_carrier = "顺丰快递" if labelled[1] == "顺丰" else _carrier(labelled[1])
            token_carrier = _carrier(labelled[2])
            if not label_carrier or (token_carrier and token_carrier != label_carrier):
                parsed.append((None, None))
                continue
            carrier, candidate = label_carrier, labelled[2]
        if (carrier and carrier != "自提" and _WAYBILL.fullmatch(candidate)
                and any(c.isdigit() for c in candidate)
                and not candidate.upper().startswith(("SAC", "SDXM"))):
            parsed.append((candidate, carrier))
        else:
            parsed.append((None, carrier))
    # More than eight separately written references require clarification; do
    # not fan out hundreds of external queries from a single free-text field.
    return (list(dict.fromkeys(parsed)) if len(parsed) <= 8 else [(None, None)]) or [(None, None)]


def _row(order: str, text: str | None, query_status: str,
         waybill: str | None = None, carrier: str | None = None,
         tracking_status: str = "NOT_APPLICABLE") -> dict[str, Any]:
    return {"order_code": order, "mes_logistics_text": text, "waybill": waybill,
            "carrier": carrier, "rule_source": RULE_SOURCE if carrier else None,
            "tracking_status": tracking_status, "latest_event": None,
            "event_time": None, "query_status": query_status}


class LogisticsQueryService:
    """Reuse one instance per process to preserve its Kuaidi100 rate guard.

    The optional adapter's half-hour guard is in-process. A multi-worker deployment
    must provide a shared guard before enabling it. This candidate is disabled by
    default and performs no account registration, subscription or retries.
    """
    def __init__(self, config: LogisticsConfig | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or LogisticsConfig()
        c = self.config
        if not (type(c.enabled) is bool and 1 <= c.concurrency <= 4 and 0 < c.request_timeout_seconds <= 30
                and 0 < c.total_timeout_seconds <= 45 and 1 <= c.max_rows <= 200
                and 1 <= c.max_source_records_per_order <= 32
                and 1 <= c.max_text_characters <= 2048):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        provider = c.kuaidi100
        mcp = c.kuaidi100_mcp
        if (c.mes_user_code not in {None, "M00001402"} or (provider and mcp)
                or mcp and (not isinstance(mcp.key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", mcp.key, re.ASCII)
                            or mcp.single_process_rate_guard is not True)):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if provider and (not provider.customer or not provider.key or provider.single_process_rate_guard is not True
                         or any(not isinstance(k, str) or not isinstance(v, str)
                                or not re.fullmatch(r"[a-z0-9_]{1,40}", v)
                                for k, v in provider.carrier_codes.items())):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        self._transport = transport
        self._semaphore = asyncio.Semaphore(c.concurrency)
        self._provider_last_attempt: dict[str, float] = {}

    async def _authorized(self, authorize: Authorize) -> None:
        try:
            decision = authorize()
            if inspect.isawaitable(decision):
                decision = await asyncio.wait_for(decision, timeout=3.0)
            if decision is not None and decision is not True:
                raise LogisticsQueryError("AUTHORIZATION_LOST")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LogisticsQueryError("AUTHORIZATION_LOST") from None

    async def _read_json(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with client.stream("POST", url, **kwargs) as response:
                if response.status_code != 200:
                    raise LogisticsQueryError("PROVIDER_UNAVAILABLE")
                raw_length = response.headers.get("content-length", "")
                if raw_length.isdigit() and int(raw_length) > MAX_RESPONSE_BYTES:
                    raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
                    data.extend(chunk)
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("shape")
                return result
        except asyncio.CancelledError:
            raise
        except LogisticsQueryError:
            raise
        except httpx.TimeoutException:
            raise LogisticsQueryError("PROVIDER_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise LogisticsQueryError("PROVIDER_UNAVAILABLE") from None
        except (ValueError, TypeError, RecursionError):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE") from None

    async def _tracking_mcp(self, context: _MCPQueryContext, row: dict[str, Any],
                            user_no: str, authorize: Authorize, budget: dict[str, int]) -> None:
        config = self.config.kuaidi100_mcp
        if config is None:
            row["tracking_status"] = "NOT_CONFIGURED"
            return
        phone = ""
        if row["carrier"] in {"顺丰快递", "顺丰快运", "中通"}:
            resolved = None
            if config.phone_resolver:
                try:
                    resolved = config.phone_resolver(user_no, row["order_code"], row["carrier"], row["waybill"])
                    if inspect.isawaitable(resolved):
                        resolved = await asyncio.wait_for(resolved, self.config.request_timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    resolved = None
            if not resolved:
                row["tracking_status"] = "PHONE_REQUIRED"
                return
            if not isinstance(resolved, str) or not re.fullmatch(r"[0-9]{4,20}", resolved, re.ASCII):
                row["tracking_status"] = "PHONE_INVALID"
                return
            phone = resolved
        # Serialize this query's client so authority is rechecked after waiting
        # for the connection, rather than before another row's slow request.
        async with context.lock:
            await self._authorized(authorize)
            rate_key = hashlib.sha256(("mcp:" + row["waybill"].upper()).encode()).hexdigest()
            now = time.monotonic()
            self._provider_last_attempt = {k: v for k, v in self._provider_last_attempt.items() if now - v < 1800}
            if rate_key in self._provider_last_attempt or len(self._provider_last_attempt) >= 2048:
                row["tracking_status"] = "RATE_LIMITED"
                return
            if budget["requests"] >= MAX_TRACKING_REQUESTS:
                row["tracking_status"] = "TRACKING_BUDGET_EXCEEDED"
                return
            if context.unavailable:
                row["tracking_status"] = context.unavailable
                return
            if context.client is None:
                try:
                    context.client = await context.stack.enter_async_context(Kuaidi100MCPClient(
                        MCPTransportConfig(key=config.key,
                            request_timeout_seconds=min(15.0, self.config.request_timeout_seconds),
                            operation_timeout_seconds=min(30.0, self.config.total_timeout_seconds)),
                        transport=self._transport))
                    found = await context.client.discover()
                    if not found.query_tool_available:
                        raise Kuaidi100MCPError("MCP_QUERY_CONTRACT_UNAVAILABLE")
                    context.phone_parameter_available = found.phone_parameter_available
                except asyncio.CancelledError:
                    raise
                except Kuaidi100MCPError as exc:
                    context.unavailable = _MCP_ERROR_CODES.get(exc.code, "PROVIDER_UNAVAILABLE")
                except Exception:
                    context.unavailable = "PROVIDER_UNAVAILABLE"
                if context.unavailable:
                    row["tracking_status"] = context.unavailable
                    return
            if phone and not context.phone_parameter_available:
                row["tracking_status"] = "PROVIDER_INVALID_RESPONSE"
                return
            await self._authorized(authorize)
            # Other concurrently running employee queries share this guard.
            # Discovery awaited network I/O, so repeat the claim check here.
            now = time.monotonic()
            if rate_key in self._provider_last_attempt or len(self._provider_last_attempt) >= 2048:
                row["tracking_status"] = "RATE_LIMITED"
                return
            budget["requests"] += 1
            self._provider_last_attempt[rate_key] = now
            try:
                result = await context.client.query_trace(row["waybill"], phone=phone)
                await self._authorized(authorize)
                _apply_mcp_trace(row, result)
            except asyncio.CancelledError:
                raise
            except LogisticsQueryError as exc:
                if exc.code == "AUTHORIZATION_LOST":
                    raise
                row["tracking_status"] = exc.code if exc.code in _TRACKING_ERRORS else "PROVIDER_UNAVAILABLE"
            except Kuaidi100MCPError as exc:
                row["tracking_status"] = _MCP_ERROR_CODES.get(exc.code, "PROVIDER_UNAVAILABLE")
            except Exception:
                row["tracking_status"] = "PROVIDER_UNAVAILABLE"

    async def _tracking(self, client: httpx.AsyncClient, row: dict[str, Any],
                        user_no: str, authorize: Authorize, budget: dict[str, int],
                        mcp_context: _MCPQueryContext) -> None:
        if self.config.kuaidi100_mcp:
            await self._tracking_mcp(mcp_context, row, user_no, authorize, budget)
            return
        config = self.config.kuaidi100
        if not config:
            row["tracking_status"] = "NOT_CONFIGURED"
            return
        company = config.carrier_codes.get(row["carrier"])
        if not company:
            row["tracking_status"] = "CARRIER_UNSUPPORTED"
            return
        params = {"com": company, "num": row["waybill"], "show": "0", "order": "desc", "lang": "zh"}
        if company in _PHONE_CARRIERS:
            phone = None
            if config.phone_resolver:
                try:
                    phone = config.phone_resolver(user_no, row["order_code"], row["carrier"], row["waybill"])
                    if inspect.isawaitable(phone):
                        phone = await asyncio.wait_for(phone, self.config.request_timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    phone = None
            if not phone:
                row["tracking_status"] = "PHONE_REQUIRED"
                return
            if not isinstance(phone, str) or not re.fullmatch(r"[0-9]{4,20}", phone):
                row["tracking_status"] = "PHONE_INVALID"
                return
            params["phone"] = phone
        await self._authorized(authorize)
        rate_key = hashlib.sha256((company + ":" + row["waybill"].upper()).encode()).hexdigest()
        now = time.monotonic()
        self._provider_last_attempt = {k: v for k, v in self._provider_last_attempt.items() if now - v < 1800}
        if rate_key in self._provider_last_attempt or len(self._provider_last_attempt) >= 2048:
            row["tracking_status"] = "RATE_LIMITED"
            return
        param = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        sign = hashlib.md5((param + config.key + config.customer).encode("utf-8")).hexdigest().upper()
        # One shared per-query counter bounds optional paid/provider work even
        # when MES returns many records and several references in each record.
        # No await separates the check, claim and rate-key claim. A budget-
        # rejected reference never consumes its 30-minute per-waybill guard.
        if budget["requests"] >= MAX_TRACKING_REQUESTS:
            row["tracking_status"] = "TRACKING_BUDGET_EXCEEDED"
            return
        budget["requests"] += 1
        self._provider_last_attempt[rate_key] = now
        try:
            result = await self._read_json(client, KUAIDI100_ENDPOINT,
                                          data={"customer": config.customer, "sign": sign, "param": param})
        except LogisticsQueryError as exc:
            row["tracking_status"] = "TRACKING_TIMEOUT" if exc.code == "PROVIDER_TIMEOUT" else exc.code
            return
        code = str(result.get("returnCode", result.get("status", "")))
        if code != "200" or result.get("result") is False:
            row["tracking_status"] = {"408": "PHONE_MISMATCH", "500": "NO_TRACKING",
                "503": "PROVIDER_AUTH_ERROR", "504": "RATE_LIMITED", "601": "QUOTA_EXHAUSTED",
                "401": "CARRIER_UNSUPPORTED"}.get(code, "PROVIDER_UNAVAILABLE")
            return
        if (str(result.get("nu", "")).upper() != row["waybill"].upper()
                or result.get("com") != company or not isinstance(result.get("data"), list)):
            row["tracking_status"] = "PROVIDER_INVALID_RESPONSE"
            return
        events = result["data"]
        if not events:
            row["tracking_status"] = "NO_TRACKING"
            return
        event = events[0]
        if (not isinstance(event, dict) or not isinstance(event.get("context"), str)
                or not isinstance(event.get("ftime", event.get("time")), str)):
            row["tracking_status"] = "PROVIDER_INVALID_RESPONSE"
            return
        row["latest_event"] = event["context"][:1024]
        row["event_time"] = event.get("ftime", event.get("time"))[:64]
        row["event_truncated"] = len(event["context"]) > 1024
        row["tracking_status"] = {"0": "IN_TRANSIT", "1": "COLLECTED", "2": "EXCEPTION",
            "3": "DELIVERED", "4": "RETURNED", "5": "OUT_FOR_DELIVERY", "8": "CUSTOMS",
            "14": "REFUSED"}.get(str(result.get("state", "")), "TRACKING_AVAILABLE")

    async def _order(self, client: httpx.AsyncClient, order: str, user_no: str,
                     include_tracking: bool, authorize: Authorize,
                     progress: dict[str, tuple[list[dict[str, Any]], int, int]],
                     budget: dict[str, int], mcp_context: _MCPQueryContext) -> tuple[list[dict[str, Any]], int, int]:
        async with self._semaphore:
            await self._authorized(authorize)
            try:
                result = await self._read_json(client, MES_ENDPOINT, headers=dict(self.config.mes_headers), json={
                    "module": "query_sa_record", "user_code": self.config.mes_user_code or user_no,
                    "filters": [{"field": "sa_code", "operator": "eq", "value": order}]})
            except LogisticsQueryError as exc:
                state = {"PROVIDER_TIMEOUT": "MES_TIMEOUT", "PROVIDER_INVALID_RESPONSE": "MES_INVALID_RESPONSE"}.get(exc.code, "MES_UNAVAILABLE")
                return [_row(order, None, state)], 0, 0
            if type(result.get("code")) is not int:
                return [_row(order, None, "MES_INVALID_RESPONSE")], 0, 0
            if result["code"] != 0:
                return [_row(order, None, "MES_BUSINESS_REJECTED")], 0, 0
            source = result.get("ObjectData")
            if not isinstance(source, list):
                return [_row(order, None, "MES_INVALID_RESPONSE")], 0, 0
            if not source:
                return [_row(order, None, "MES_NO_RECORD")], 0, 0
            rows: list[dict[str, Any]] = []
            for record in source[:self.config.max_source_records_per_order]:
                if not isinstance(record, dict) or "物流单号" not in record or (record["物流单号"] is not None and not isinstance(record["物流单号"], str)):
                    rows.append(_row(order, None, "MES_INVALID_RESPONSE"))
                    continue
                text = record["物流单号"]
                if text is None or not text.strip():
                    rows.append(_row(order, text, "MES_EMPTY"))
                    continue
                truncated = len(text) > self.config.max_text_characters
                bounded_text = text[:self.config.max_text_characters]
                # Never query a reference obtained from cutting off its suffix.
                refs = [(None, None)] if truncated else classify_references(text)
                for waybill, carrier in refs:
                    row = _row(order, bounded_text, "MES_FOUND", waybill, carrier)
                    if truncated:
                        row["mes_text_truncated"] = True
                        row["original_text_characters"] = len(text)
                    if not include_tracking:
                        row["tracking_status"] = "NOT_REQUESTED"
                    elif waybill:
                        row["tracking_status"] = "TRACKING_PENDING"
                    rows.append(row)
            snapshot = rows, len(source), max(0, len(source) - self.config.max_source_records_per_order)
            # Publish the complete MES-only snapshot before the first carrier
            # await, so a timeout preserves every already-received MES value.
            progress[order] = snapshot
            for row in rows:
                if row["tracking_status"] == "TRACKING_PENDING":
                    await self._tracking(client, row, user_no, authorize, budget, mcp_context)
            return snapshot

    async def query(self, args: Mapping[str, Any], *, trusted_user_no: str,
                    authorize: Authorize) -> dict[str, Any]:
        orders, tracking = validate_arguments(args)
        self.config.require_ready()
        if not isinstance(trusted_user_no, str) or not _IDENTITY.fullmatch(trusted_user_no):
            raise LogisticsQueryError("AUTHORIZATION_LOST")
        await self._authorized(authorize)
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        async with AsyncExitStack() as stack, httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                                               transport=self._transport) as client:
            progress: dict[str, tuple[list[dict[str, Any]], int, int]] = {}
            budget = {"requests": 0}
            mcp_context = _MCPQueryContext(stack)
            tasks = [asyncio.create_task(self._order(client, code, trusted_user_no, tracking, authorize, progress, budget, mcp_context)) for code in orders]
            try:
                done, pending = await asyncio.wait(tasks, timeout=self.config.total_timeout_seconds)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                batches = []
                for code, task in zip(orders, tasks):
                    if task in done:
                        batches.append(task.result())
                    elif code in progress:
                        snapshot = progress[code]
                        for row in snapshot[0]:
                            if row["tracking_status"] == "TRACKING_PENDING":
                                row["tracking_status"] = "TRACKING_TIMEOUT"
                        batches.append(snapshot)
                    else:
                        batches.append(([_row(code, None, "MES_TIMEOUT")], 0, 0))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        await self._authorized(authorize)
        rows = [row for batch, _, _ in batches for row in batch]
        budget_exceeded_rows = sum(row["tracking_status"] == "TRACKING_BUDGET_EXCEEDED" for row in rows)
        source_records = sum(count for _, count, _ in batches)
        skipped_records = sum(count for _, _, count in batches)
        omitted_rows = max(0, len(rows) - self.config.max_rows)
        rows = rows[:self.config.max_rows]
        result = {"schema": "logistics-query-result-v1", "status": "completed",
                  "queried_at": datetime.now(timezone.utc).isoformat(), "rows": rows,
                  "counts": {"requested_orders": len(args["order_codes"]), "queried_orders": len(orders),
                             "source_records": source_records, "returned_rows": len(rows),
                             "tracking_requests": budget["requests"], "tracking_budget_exceeded_rows": budget_exceeded_rows},
                  "truncation": {"truncated": False, "omitted_source_records": skipped_records,
                                 "omitted_rows": omitted_rows, "text_truncated_rows": 0}}
        def refresh() -> None:
            result["counts"]["returned_rows"] = len(rows)
            trunc = result["truncation"]
            trunc["text_truncated_rows"] = sum(bool(r.get("mes_text_truncated") or r.get("event_truncated")) for r in rows)
            trunc["truncated"] = bool(trunc["omitted_source_records"] or trunc["omitted_rows"] or trunc["text_truncated_rows"])
            result["status"] = "partial" if (trunc["truncated"] or any(r.get("tracking_time_unverified") is True for r in rows) or any(
                r["query_status"] in {"MES_UNAVAILABLE", "MES_INVALID_RESPONSE", "MES_BUSINESS_REJECTED", "MES_TIMEOUT"}
                or r["tracking_status"] in _TRACKING_ERRORS for r in rows)) else "completed"
        refresh()
        # ASCII escaping plus default JSON whitespace is the larger encoding;
        # the 85 KB ceiling also leaves space for the native 90 KB wrapper.
        while len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_RESULT_BYTES and rows:
            rows.pop()
            result["truncation"]["omitted_rows"] += 1
            refresh()
        # A final check covers authorization revoked while constructing a result.
        await self._authorized(authorize)
        return result
