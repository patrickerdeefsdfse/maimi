"""Discovery-only check of a replacement MCP key in the verified gateway.

The caller owns current-container preflight. This module does not read or change
the active credential and never calls an MES, tracking or forecasting tool.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

TRANSPORT_SHA256 = "771837a2a36ea47fa54d8dfec75ec9d80012ac319cd025ef39ae2d84dade488c"
MAX_CONTROL_REQUESTS = 5
MAX_CHILD_SECONDS = 25
RUNNER_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
              "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
_KEY = re.compile(r"[A-Za-z0-9_-]{1,512}\Z", re.ASCII)
_GATEWAY = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
SAFE_ERRORS = frozenset({
    "PROVIDER_INPUT_INVALID", "PROVIDER_CHECK_FAILED", "PROVIDER_CHECK_TIMEOUT",
    "PROVIDER_OUTPUT_INVALID", "PROVIDER_SOURCE_MISMATCH", "PROVIDER_IDENTITY_INVALID",
    "PROVIDER_CONTROL_LIMIT", "PROVIDER_METHOD_FORBIDDEN", "PROVIDER_PHONE_UNAVAILABLE",
    "MCP_CONFIGURATION_INVALID", "MCP_NOT_OPEN", "MCP_HTTP_ERROR", "MCP_AUTH_REJECTED",
    "MCP_RATE_LIMITED", "MCP_REQUEST_TIMEOUT", "MCP_CONNECTION_FAILED",
    "MCP_RESPONSE_INVALID", "MCP_RESPONSE_TOO_LARGE", "MCP_RPC_ERROR",
    "MCP_PROTOCOL_UNSUPPORTED", "MCP_SESSION_INVALID", "MCP_TOOLS_LIMIT",
    "MCP_QUERY_CONTRACT_UNAVAILABLE", "MCP_SECRET_ECHO",
})

CHILD = r'''
import sys
sys.dont_write_bytecode = True
import asyncio, hashlib, json, logging, os, re, stat, types
logging.disable(logging.CRITICAL)
TRANSPORT_SOURCE = "/app/app/kuaidi100_mcp.py"
TRANSPORT_SHA256 = "771837a2a36ea47fa54d8dfec75ec9d80012ac319cd025ef39ae2d84dade488c"
ENDPOINT = "https://api.kuaidi100.com/mcp/streamable"
SAFE_ERRORS = {
    "PROVIDER_INPUT_INVALID", "PROVIDER_CHECK_FAILED", "PROVIDER_CHECK_TIMEOUT",
    "PROVIDER_SOURCE_MISMATCH", "PROVIDER_IDENTITY_INVALID", "PROVIDER_CONTROL_LIMIT",
    "PROVIDER_METHOD_FORBIDDEN", "PROVIDER_PHONE_UNAVAILABLE",
    "MCP_CONFIGURATION_INVALID", "MCP_NOT_OPEN", "MCP_HTTP_ERROR", "MCP_AUTH_REJECTED",
    "MCP_RATE_LIMITED", "MCP_REQUEST_TIMEOUT", "MCP_CONNECTION_FAILED",
    "MCP_RESPONSE_INVALID", "MCP_RESPONSE_TOO_LARGE", "MCP_RPC_ERROR",
    "MCP_PROTOCOL_UNSUPPORTED", "MCP_SESSION_INVALID", "MCP_TOOLS_LIMIT",
    "MCP_QUERY_CONTRACT_UNAVAILABLE", "MCP_SECRET_ECHO",
}
class CheckError(Exception):
    def __init__(self, code):
        self.code = code if code in SAFE_ERRORS else "PROVIDER_CHECK_FAILED"
        super().__init__(self.code)
def strict_json(raw):
    def pairs(values):
        result = {}
        for k, v in values:
            if k in result: raise CheckError("PROVIDER_INPUT_INVALID")
            result[k] = v
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
def load_transport():
    if os.getuid() != 10001 or os.getgid() != 10001:
        raise CheckError("PROVIDER_IDENTITY_INVALID")
    fd = os.open(TRANSPORT_SOURCE, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 131072:
            raise CheckError("PROVIDER_SOURCE_MISMATCH")
        raw = stream.read(131073)
    if hashlib.sha256(raw).hexdigest() != TRANSPORT_SHA256:
        raise CheckError("PROVIDER_SOURCE_MISMATCH")
    module = types.ModuleType("r53_verified_kuaidi100_transport")
    sys.modules[module.__name__] = module
    exec(compile(raw, "<r53-verified-transport>", "exec"), module.__dict__)
    if module.ENDPOINT != ENDPOINT or module.MAX_LIST_PAGES != 3:
        raise CheckError("PROVIDER_SOURCE_MISMATCH")
    return module
async def discover(module, key):
    class DiscoveryClient(module.Kuaidi100MCPClient):
        control_requests = 0
        async def _request(self, method, params=None, *, notification=False, initialize=False):
            if method not in {"initialize", "notifications/initialized", "tools/list"}:
                raise CheckError("PROVIDER_METHOD_FORBIDDEN")
            if self.control_requests >= 5:
                raise CheckError("PROVIDER_CONTROL_LIMIT")
            self.control_requests += 1
            return await super()._request(method, params, notification=notification, initialize=initialize)
    async with DiscoveryClient(module.Kuaidi100MCPConfig(
            key=key, request_timeout_seconds=8, operation_timeout_seconds=20)) as client:
        found = await client.discover()
        if not found.query_tool_available:
            raise CheckError("MCP_QUERY_CONTRACT_UNAVAILABLE")
        if not found.phone_parameter_available:
            raise CheckError("PROVIDER_PHONE_UNAVAILABLE")
        result = {"status": "VERIFIED", "control_requests": client.control_requests,
                  "discovery": {
                      "protocol_version": found.protocol_version,
                      "tools_seen": found.tools_seen,
                      "query_tool_available": found.query_tool_available,
                      "phone_parameter_available": found.phone_parameter_available,
                      "remote_contract": found.remote_contract,
                      "response_format_parameter_available": found.response_format_parameter_available,
                      "eta_tool_available": found.eta_tool_available,
                      "eta_remote_contract": found.eta_remote_contract,
                      "eta_product_required": found.eta_product_required,
                  }}
        return result
def run(raw):
    try:
        if not isinstance(raw, bytes) or len(raw) > 2048:
            raise CheckError("PROVIDER_INPUT_INVALID")
        value = strict_json(raw)
        if (not isinstance(value, dict) or set(value) != {"key"}
                or not isinstance(value["key"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value["key"], re.ASCII)):
            raise CheckError("PROVIDER_INPUT_INVALID")
        module = load_transport()
        return asyncio.run(asyncio.wait_for(discover(module, value["key"]), 22))
    except BaseException as exc:
        code = getattr(exc, "code", None)
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            code = "PROVIDER_CHECK_TIMEOUT"
        return {"status": "STOP", "reason": code if isinstance(code, str) and code in SAFE_ERRORS else "PROVIDER_CHECK_FAILED"}
if __name__ == "__main__":
    result = run(sys.stdin.buffer.read(2049))
    print(json.dumps(result, separators=(",", ":")), flush=True)
'''


class ProviderCheckError(Exception):
    def __init__(self, code: str):
        self.code = code if code in SAFE_ERRORS else "PROVIDER_CHECK_FAILED"
        super().__init__(self.code)


def _strict_object(raw: bytes) -> dict[str, Any]:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError("object")
    return value


def _project(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("status") == "STOP":
        if set(value) != {"status", "reason"} or not isinstance(value["reason"], str):
            raise ProviderCheckError("PROVIDER_OUTPUT_INVALID")
        raise ProviderCheckError(value["reason"] if value["reason"] in SAFE_ERRORS else "PROVIDER_CHECK_FAILED")
    if set(value) != {"status", "control_requests", "discovery"} or value["status"] != "VERIFIED":
        raise ProviderCheckError("PROVIDER_OUTPUT_INVALID")
    count, found = value["control_requests"], value["discovery"]
    fields = {"protocol_version", "tools_seen", "query_tool_available", "phone_parameter_available",
              "remote_contract", "response_format_parameter_available", "eta_tool_available",
              "eta_remote_contract", "eta_product_required"}
    if (type(count) is not int or not 3 <= count <= MAX_CONTROL_REQUESTS
            or not isinstance(found, dict) or set(found) != fields
            or found["protocol_version"] not in {"2025-03-26", "2025-06-18", "2025-11-25"}
            or type(found["tools_seen"]) is not int or not 1 <= found["tools_seen"] <= 64
            or found["remote_contract"] not in {"kuaidiNum", "kuaidi_num"}
            or found["eta_remote_contract"] not in {None, "hosted-camel", "public-snake"}
            or any(type(found[name]) is not bool for name in {
                "query_tool_available", "phone_parameter_available", "response_format_parameter_available",
                "eta_tool_available", "eta_product_required"})
            or found["query_tool_available"] is not True or found["phone_parameter_available"] is not True
            or found["eta_tool_available"] != (found["eta_remote_contract"] is not None)
            or found["eta_product_required"] != (found["eta_remote_contract"] == "public-snake")):
        raise ProviderCheckError("PROVIDER_OUTPUT_INVALID")
    return {"status": "VERIFIED", "control_requests": count, "discovery": dict(found),
            "maximum_mcp_control_requests": MAX_CONTROL_REQUESTS,
            "mes_requests": 0, "waybill_requests": 0, "eta_requests": 0,
            "transport": "HTTPS_STREAMABLE", "tls_certificate_verification_enabled": True,
            "redirects_followed": False, "proxy_used": False, "automatic_retries": 0,
            "quota_acceptance": "NOT_TESTED", "actual_tracking_acceptance": "NOT_TESTED",
            "actual_eta_acceptance": "NOT_TESTED", "credential_values_or_hash_output": False}


def check_new_key(gateway_id: str, key: str, *, runner=subprocess.run) -> dict[str, Any]:
    """Check discovery using a caller-verified container ID; key only via stdin.

    The returned data proves tool availability, not balance or real query access.
    A provider timeout/error never rotates the current credential automatically.
    """
    if (not isinstance(gateway_id, str) or not _GATEWAY.fullmatch(gateway_id)
            or not isinstance(key, str) or not _KEY.fullmatch(key)):
        raise ProviderCheckError("PROVIDER_INPUT_INVALID")
    command = ["docker", "--host", "unix:///var/run/docker.sock", "exec", "-i",
               gateway_id, "python", "-I", "-B", "-c", CHILD]
    payload = json.dumps({"key": key}, separators=(",", ":")).encode("ascii")
    try:
        result = runner(command, input=payload, capture_output=True, timeout=MAX_CHILD_SECONDS,
                        check=False, env=dict(RUNNER_ENV))
        if result.returncode != 0:
            raise ProviderCheckError("PROVIDER_CHECK_FAILED")
        if (not isinstance(result.stdout, bytes) or not 1 <= len(result.stdout) <= 8192
                or not isinstance(result.stderr, bytes) or result.stderr):
            raise ProviderCheckError("PROVIDER_OUTPUT_INVALID")
        return _project(_strict_object(result.stdout))
    except subprocess.TimeoutExpired:
        raise ProviderCheckError("PROVIDER_CHECK_TIMEOUT") from None
    except ProviderCheckError:
        raise
    except BaseException:
        raise ProviderCheckError("PROVIDER_CHECK_FAILED") from None
