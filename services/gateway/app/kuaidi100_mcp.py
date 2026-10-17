"""Narrow, bounded Kuaidi100 Streamable HTTP client.

The business methods are the documented query_trace and optional in-transit
estimate_time_with_logistic read operations. Tool
discovery verifies that method's remote argument contract before it is used.
Neither callers nor tool arguments can choose an endpoint, identity or method.
Credentials and session identifiers stay in this server-side transport.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any

import httpx

ENDPOINT = "https://api.kuaidi100.com/mcp/streamable"
PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = {PROTOCOL_VERSION, "2025-06-18", "2025-11-25"}
MAX_RESPONSE_BYTES = 512 * 1024
MAX_EVENTS = 64
MAX_TOOLS = 64
MAX_LIST_PAGES = 3
MAX_QUERY_CALLS = 20
MAX_ETA_CALLS = 4
_WAYBILL = re.compile(r"[A-Za-z0-9]{6,32}\Z", re.ASCII)
_PHONE = re.compile(r"[0-9]{4,20}\Z", re.ASCII)
_SAFE_CODES = {
    "MCP_CONFIGURATION_INVALID", "MCP_NOT_OPEN", "MCP_HTTP_ERROR",
    "MCP_AUTH_REJECTED", "MCP_RATE_LIMITED", "MCP_REQUEST_TIMEOUT",
    "MCP_CONNECTION_FAILED", "MCP_RESPONSE_INVALID", "MCP_RESPONSE_TOO_LARGE",
    "MCP_RPC_ERROR", "MCP_PROTOCOL_UNSUPPORTED", "MCP_SESSION_INVALID",
    "MCP_TOOLS_LIMIT", "MCP_QUERY_CONTRACT_UNAVAILABLE", "MCP_ARGUMENTS_INVALID",
    "MCP_QUERY_LIMIT", "MCP_SECRET_ECHO", "MCP_TOOL_RESULT_INVALID",
    "MCP_ETA_CONTRACT_UNAVAILABLE", "MCP_ETA_LIMIT",
}


class Kuaidi100MCPError(Exception):
    """Only a fixed classification leaves the transport; no URL/body/details."""

    def __init__(self, code: str):
        self.code = code if code in _SAFE_CODES else "MCP_RESPONSE_INVALID"
        super().__init__(self.code)


class _CredentialURLFilter(logging.Filter):
    # httpx logs request URLs at INFO. Redact only this fixed provider endpoint,
    # independent of the actual credential, so logs never retain its URL key.
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        if ENDPOINT in rendered:
            record.msg = re.sub(
                r"https://api\.kuaidi100\.com/mcp/streamable[^\s\"']*",
                ENDPOINT + "?[REDACTED]", rendered,
            )
            record.args = ()
        return True


_HTTPX_LOGGER = logging.getLogger("httpx")
if not any(isinstance(value, _CredentialURLFilter) for value in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_CredentialURLFilter())


@dataclass(frozen=True)
class Kuaidi100MCPConfig:
    key: str = field(repr=False)
    request_timeout_seconds: float = 8.0
    operation_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (not isinstance(self.key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", self.key)
                or type(self.request_timeout_seconds) not in {float, int}
                or not 0 < self.request_timeout_seconds <= 15
                or type(self.operation_timeout_seconds) not in {float, int}
                or not 0 < self.operation_timeout_seconds <= 60):
            raise Kuaidi100MCPError("MCP_CONFIGURATION_INVALID")


@dataclass(frozen=True)
class Discovery:
    protocol_version: str
    tools_seen: int
    query_tool_available: bool
    phone_parameter_available: bool
    remote_contract: str | None = None
    response_format_parameter_available: bool = False
    eta_tool_available: bool = False
    eta_remote_contract: str | None = None
    eta_product_required: bool = False


def _load_json(raw: bytes | str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    try:
        data = json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
        if not isinstance(data, dict):
            raise ValueError("object")
        return data
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise Kuaidi100MCPError("MCP_RESPONSE_INVALID") from None


def _response_result(message: dict[str, Any], rpc_id: int) -> dict[str, Any] | None:
    if message.get("jsonrpc") != "2.0":
        raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
    if "method" in message:
        # Clients advertise no sampling/elicitation capabilities and never
        # execute inbound server requests. Progress/log notifications are inert.
        if "id" in message or "result" in message or "error" in message:
            raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
        if not isinstance(message["method"], str):
            raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
        return None
    if type(message.get("id")) is not int or message["id"] != rpc_id:
        raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
    if ("error" in message) == ("result" in message):
        raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
    if "error" in message:
        error = message["error"]
        if (not isinstance(error, dict) or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)):
            raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
        raise Kuaidi100MCPError("MCP_RPC_ERROR")
    if not isinstance(message["result"], dict):
        raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
    return message["result"]


class Kuaidi100MCPClient:
    def __init__(self, config: Kuaidi100MCPConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self._config = config
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._version: str | None = None
        self._discovery: Discovery | None = None
        self._rpc_id = 0
        self._query_calls = 0
        self._eta_calls = 0
        self._eta_response_format = False
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "Kuaidi100MCPClient()"

    async def __aenter__(self) -> "Kuaidi100MCPClient":
        if self._http is not None:
            raise Kuaidi100MCPError("MCP_CONFIGURATION_INVALID")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self._config.request_timeout_seconds),
            follow_redirects=False, trust_env=False, transport=self._transport,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        client, self._http = self._http, None
        self._session = None
        self._version = None
        self._discovery = None
        if client is not None:
            await client.aclose()

    async def discover(self) -> Discovery:
        """Initialize and list tools only. No waybill query is made."""
        return await self._bounded(self._serialized(self._discover))

    async def query_trace(self, kuaidi_num: str, *, phone: str = "") -> dict[str, Any]:
        """Return a validated CallToolResult for the fixed documented read tool.

        Business fields, phone policy, per-waybill rate limits and identity
        checks belong to the caller. phone is a trusted server-resolved suffix
        or full phone number; the transport never derives or truncates it.
        """
        if (not isinstance(kuaidi_num, str) or not _WAYBILL.fullmatch(kuaidi_num)
                or not isinstance(phone, str) or (phone and not _PHONE.fullmatch(phone))):
            raise Kuaidi100MCPError("MCP_ARGUMENTS_INVALID")
        return await self._bounded(self._serialized(self._query, kuaidi_num, phone))

    async def estimate_time_with_logistic(self, *, kuaidi_com: str, from_loc: str,
                                         to_loc: str, order_time: str,
                                         logistic: str, exp_type: str | None = "") -> dict[str, Any]:
        """Only source-complete, existing-waybill forecasts; no endpoint overrides."""
        args = dict(kuaidi_com=kuaidi_com, from_loc=from_loc, to_loc=to_loc,
                    order_time=order_time, exp_type="" if exp_type is None else exp_type, logistic=logistic)
        if (any(not isinstance(v, str) or not v or len(v) > 200 for k, v in args.items()
                if k not in {"logistic", "exp_type"})
                or not isinstance(args["exp_type"], str) or len(args["exp_type"]) > 200
                or not re.fullmatch(r"[a-z0-9_]{1,80}", kuaidi_com, re.ASCII)
                or not isinstance(logistic, str) or not 2 <= len(logistic.encode("utf-8")) <= 64_000):
            raise Kuaidi100MCPError("MCP_ARGUMENTS_INVALID")
        return await self._bounded(self._serialized(self._estimate, args))

    async def _serialized(self, operation: Any, *args: Any) -> Any:
        # Queueing time counts against the public operation timeout too.
        async with self._lock:
            return await operation(*args)

    async def _bounded(self, operation: Any) -> Any:
        try:
            return await asyncio.wait_for(operation, self._config.operation_timeout_seconds)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise Kuaidi100MCPError("MCP_REQUEST_TIMEOUT") from None
        except httpx.HTTPError:
            raise Kuaidi100MCPError("MCP_CONNECTION_FAILED") from None

    async def _discover(self) -> Discovery:
        if self._http is None:
            raise Kuaidi100MCPError("MCP_NOT_OPEN")
        if self._discovery is not None:
            return self._discovery
        initial = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "megmeet-logistics-readonly", "version": "1"},
        }, initialize=True)
        version = initial.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise Kuaidi100MCPError("MCP_PROTOCOL_UNSUPPORTED")
        if (not isinstance(initial.get("capabilities"), dict)
                or not isinstance(initial["capabilities"].get("tools"), dict)):
            raise Kuaidi100MCPError("MCP_QUERY_CONTRACT_UNAVAILABLE")
        self._version = version
        await self._request("notifications/initialized", notification=True)
        count = 0
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = None
        query_available = False
        phone_available = False
        remote_contract = None
        response_format_available = False
        eta_available = False
        eta_remote_contract = None
        eta_product_required = False
        for page in range(MAX_LIST_PAGES):
            result = await self._request("tools/list", {} if cursor is None else {"cursor": cursor})
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
            count += len(tools)
            if count > MAX_TOOLS:
                raise Kuaidi100MCPError("MCP_TOOLS_LIMIT")
            for tool in tools:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not 1 <= len(tool["name"]) <= 128 or tool["name"] in seen_names):
                    raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
                seen_names.add(tool["name"])
                if tool["name"] == "query_trace":
                    query_available, phone_available, remote_contract, response_format_available = self._query_contract(tool)
                elif tool["name"] == "estimate_time_with_logistic":
                    (eta_available, self._eta_response_format, eta_remote_contract,
                     eta_product_required) = self._eta_contract(tool)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or cursor in seen_cursors:
                raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
            seen_cursors.add(cursor)
        else:
            raise Kuaidi100MCPError("MCP_TOOLS_LIMIT")
        self._discovery = Discovery(version, count, query_available, phone_available,
                                    remote_contract, response_format_available, eta_available,
                                    eta_remote_contract, eta_product_required)
        return self._discovery

    @staticmethod
    def _query_contract(tool: dict[str, Any]) -> tuple[bool, bool, str | None, bool]:
        unavailable = (False, False, None, False)
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return unavailable
        props, required = schema.get("properties"), schema.get("required", [])
        if (not isinstance(props, dict) or not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)
                or len(set(required)) != len(required)):
            return unavailable
        # Two separate published/observed contracts are supported. The public
        # Python/Node package uses snake_case; the hosted Streamable service
        # observed on 2026-10-09 uses camelCase. Never guess or send both aliases.
        aliases = [name for name in ("kuaidi_num", "kuaidiNum") if name in props]
        if len(aliases) != 1:
            return unavailable
        waybill_parameter = aliases[0]
        if (waybill_parameter not in required
                or set(required) - {waybill_parameter, "phone", "responseFormat"}
                or not isinstance(props[waybill_parameter], dict)
                or props[waybill_parameter].get("type") != "string"):
            return unavailable
        phone = props.get("phone")
        has_phone = isinstance(phone, dict) and phone.get("type") == "string"
        if "phone" in required and not has_phone:
            return unavailable
        response_format = props.get("responseFormat")
        has_response_format = isinstance(response_format, dict) and response_format.get("type") == "string"
        if "responseFormat" in required and not has_response_format:
            return unavailable
        # A remote server explicitly marking the fixed operation destructive is
        # incompatible with this read-only integration. Missing hints are okay:
        # the official package currently does not publish annotations.
        annotations = tool.get("annotations", {})
        if (not isinstance(annotations, dict) or annotations.get("destructiveHint") is True
                or annotations.get("readOnlyHint") is False):
            return unavailable
        return True, has_phone, waybill_parameter, has_response_format

    @staticmethod
    def _eta_contract(tool: dict[str, Any]) -> tuple[bool, bool, str | None, bool]:
        # Both contracts belong to the same fixed official read operation.
        # The hosted schema was observed on the company path on 2026-10-14;
        # the public Python package uses the separate snake_case contract.
        # Do not mix aliases or assume the hosted method requires exp_type.
        unavailable = (False, False, None, False)
        schema = tool.get("inputSchema", {})
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return unavailable
        props, required = schema.get("properties"), schema.get("required", [])
        if (not isinstance(props, dict) or not isinstance(required, list)
                or any(not isinstance(x, str) for x in required)
                or len(set(required)) != len(required)):
            return unavailable
        hosted = {"kuaidicom", "from", "to", "orderTime", "logistic"}
        public = {"kuaidi_com", "from_loc", "to_loc", "order_time", "exp_type", "logistic"}
        candidates = []
        for contract, names, mandatory, foreign in (
                ("hosted-camel", hosted, hosted, public - {"logistic"}),
                ("public-snake", public, public - {"order_time"}, hosted - {"logistic"})):
            if (not (set(props) & foreign)
                    and mandatory <= set(required) <= names | {"responseFormat"}
                    and all(isinstance(props.get(x), dict) and props[x].get("type") == "string" for x in names)):
                candidates.append(contract)
        if len(candidates) != 1:
            return unavailable
        annotations = tool.get("annotations", {})
        if (not isinstance(annotations, dict) or annotations.get("destructiveHint") is True
                or annotations.get("readOnlyHint") is False
                or any(key in annotations and type(annotations[key]) is not bool
                       for key in ("readOnlyHint", "destructiveHint"))):
            return unavailable
        response_format = isinstance(props.get("responseFormat"), dict) and props["responseFormat"].get("type") == "string"
        if ("responseFormat" in props or "responseFormat" in required) and not response_format:
            return unavailable
        if response_format and "enum" in props["responseFormat"]:
            allowed = props["responseFormat"]["enum"]
            if not isinstance(allowed, list) or "json" not in allowed:
                return unavailable
        contract = candidates[0]
        return True, response_format, contract, contract == "public-snake"

    async def _estimate(self, args: dict[str, str]) -> dict[str, Any]:
        found = await self._discover()
        if not found.eta_tool_available:
            raise Kuaidi100MCPError("MCP_ETA_CONTRACT_UNAVAILABLE")
        if found.eta_product_required and not args["exp_type"]:
            raise Kuaidi100MCPError("MCP_ARGUMENTS_INVALID")
        if self._eta_calls >= MAX_ETA_CALLS:
            raise Kuaidi100MCPError("MCP_ETA_LIMIT")
        if found.eta_remote_contract == "hosted-camel":
            params = {remote: args[canonical] for remote, canonical in (
                ("kuaidicom", "kuaidi_com"), ("from", "from_loc"), ("to", "to_loc"),
                ("orderTime", "order_time"), ("logistic", "logistic"))}
        elif found.eta_remote_contract == "public-snake":
            params = dict(args)
        else:
            raise Kuaidi100MCPError("MCP_ETA_CONTRACT_UNAVAILABLE")
        if self._eta_response_format:
            params["responseFormat"] = "json"
        self._eta_calls += 1
        result = await self._request("tools/call", {"name": "estimate_time_with_logistic", "arguments": params})
        return self._validate_tool_result(result)

    async def _query(self, kuaidi_num: str, phone: str) -> dict[str, Any]:
        found = await self._discover()
        if not found.query_tool_available or (phone and not found.phone_parameter_available):
            raise Kuaidi100MCPError("MCP_QUERY_CONTRACT_UNAVAILABLE")
        if self._query_calls >= MAX_QUERY_CALLS:
            raise Kuaidi100MCPError("MCP_QUERY_LIMIT")
        args = {found.remote_contract: kuaidi_num}
        if found.phone_parameter_available:
            args["phone"] = phone
        if found.response_format_parameter_available:
            args["responseFormat"] = "json"
        self._query_calls += 1
        result = await self._request("tools/call", {"name": "query_trace", "arguments": args})
        return self._validate_tool_result(result)

    def _validate_tool_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if ("isError" in result and type(result["isError"]) is not bool):
            raise Kuaidi100MCPError("MCP_TOOL_RESULT_INVALID")
        content = result.get("content")
        if not isinstance(content, list) or len(content) > 32:
            raise Kuaidi100MCPError("MCP_TOOL_RESULT_INVALID")
        if any(not isinstance(block, dict) or block.get("type") != "text"
               or not isinstance(block.get("text"), str) for block in content):
            raise Kuaidi100MCPError("MCP_TOOL_RESULT_INVALID")
        if "structuredContent" in result and not isinstance(result["structuredContent"], dict):
            raise Kuaidi100MCPError("MCP_TOOL_RESULT_INVALID")
        # Hosted MCP returns JSON inside a text block. The business layer
        # decodes that JSON once, so inspect the same decoded representation:
        # a provider echo encoded as \\uXXXX must not bypass the raw-text guard.
        for block in content:
            try:
                decoded = json.loads(block["text"])
            except (ValueError, TypeError, RecursionError):
                continue
            self._guard_secrets(decoded)
        return result

    def _guard_secrets(self, value: Any) -> None:
        serialized = json.dumps(value, ensure_ascii=False)
        if self._config.key in serialized or (self._session and self._session in serialized):
            raise Kuaidi100MCPError("MCP_SECRET_ECHO")

    async def _request(self, method: str, params: dict[str, Any] | None = None,
                       *, notification: bool = False, initialize: bool = False) -> dict[str, Any]:
        if self._http is None:
            raise Kuaidi100MCPError("MCP_NOT_OPEN")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self._rpc_id += 1
            message["id"] = self._rpc_id
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "Accept-Encoding": "identity", "responseFormat": "json"}
        if self._version is not None:
            headers["MCP-Protocol-Version"] = self._version
        if self._session is not None:
            headers["Mcp-Session-Id"] = self._session
        async with self._http.stream("POST", ENDPOINT, params={"key": self._config.key},
                                     headers=headers, json=message, follow_redirects=False) as response:
            if response.status_code in {401, 403}:
                raise Kuaidi100MCPError("MCP_AUTH_REJECTED")
            if response.status_code == 429:
                raise Kuaidi100MCPError("MCP_RATE_LIMITED")
            if response.status_code not in ({200, 202, 204} if notification else {200}):
                raise Kuaidi100MCPError("MCP_HTTP_ERROR")
            # Do not allow a compressed body to allocate unbounded decoder
            # buffers before our decoded-byte check can run.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
            response_session = response.headers.get("mcp-session-id")
            if response_session is not None:
                if (not re.fullmatch(r"[\x21-\x7e]{1,1024}", response_session)
                        or (not initialize and response_session != self._session)):
                    raise Kuaidi100MCPError("MCP_SESSION_INVALID")
                self._session = response_session
            declared = response.headers.get("content-length")
            if declared is not None:
                if not declared.isdigit():
                    raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
                if int(declared) > MAX_RESPONSE_BYTES:
                    raise Kuaidi100MCPError("MCP_RESPONSE_TOO_LARGE")
            if notification:
                # MCP requires no JSON-RPC response to a notification. Drain a
                # bounded empty body and refuse unexpected protocol payloads.
                body = await self._read_all(response)
                if body.strip():
                    raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
                return {}
            media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media == "application/json":
                data = _load_json(await self._read_all(response))
                result = _response_result(data, self._rpc_id)
                if result is None:
                    raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
            elif media == "text/event-stream":
                result = await self._read_sse(response, self._rpc_id)
            else:
                raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
            self._guard_secrets(result)
            return result

    @staticmethod
    async def _read_all(response: httpx.Response) -> bytes:
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise Kuaidi100MCPError("MCP_RESPONSE_TOO_LARGE")
        return bytes(chunks)

    @staticmethod
    async def _read_sse(response: httpx.Response, rpc_id: int) -> dict[str, Any]:
        pending = bytearray()
        total = events = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise Kuaidi100MCPError("MCP_RESPONSE_TOO_LARGE")
            pending.extend(chunk)
            # Handle LF and CRLF event separators, including split chunks. Bare
            # CR-only streams are rejected rather than silently misparsed.
            while True:
                match = re.search(br"\r?\n\r?\n", pending)
                if match is None:
                    break
                raw = bytes(pending[:match.start()])
                del pending[:match.end()]
                events += 1
                if events > MAX_EVENTS:
                    raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
                try:
                    lines = raw.decode("utf-8").splitlines()
                except UnicodeError:
                    raise Kuaidi100MCPError("MCP_RESPONSE_INVALID") from None
                data_lines = []
                for line in lines:
                    if line.startswith("data:"):
                        value = line[5:]
                        data_lines.append(value[1:] if value.startswith(" ") else value)
                if not data_lines:
                    continue
                parsed = _response_result(_load_json("\n".join(data_lines)), rpc_id)
                if parsed is not None:
                    # Close response as soon as this request's result arrives;
                    # never wait for a provider's indefinite event stream.
                    return parsed
        raise Kuaidi100MCPError("MCP_RESPONSE_INVALID")
