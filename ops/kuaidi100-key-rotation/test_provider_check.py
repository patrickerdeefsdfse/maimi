"""Offline tests; the only HTTP transport here is httpx.MockTransport."""
import ast
import asyncio
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys
import types

import httpx
import pytest

import provider_check as p

KEY = "R53_SYNTHETIC_TEST_KEY_ONLY"
GATEWAY = "b" * 64
SOURCE = Path(__file__).parents[1] / "logistics-eta-compat-r52-20261014/backend/app/kuaidi100_mcp.py"


def valid_result():
    return {"status": "VERIFIED", "control_requests": 3,
            "discovery": {"protocol_version": "2025-03-26", "tools_seen": 2,
                          "query_tool_available": True, "phone_parameter_available": True,
                          "remote_contract": "kuaidiNum", "response_format_parameter_available": True,
                          "eta_tool_available": True, "eta_remote_contract": "hosted-camel",
                          "eta_product_required": False}}


def completion(value=None, *, stdout=None, stderr=b"", code=0):
    return subprocess.CompletedProcess([], code,
                                      json.dumps(value or valid_result()).encode() if stdout is None else stdout,
                                      stderr)


def test_host_secret_only_stdin_fixed_docker_endpoint_and_environment():
    calls = []
    def runner(args, **kw):
        calls.append((args, kw))
        return completion()
    result = p.check_new_key(GATEWAY, KEY, runner=runner)
    args, kw = calls[0]
    assert args == ["docker", "--host", "unix:///var/run/docker.sock", "exec", "-i",
                    GATEWAY, "python", "-I", "-B", "-c", p.CHILD]
    assert KEY not in repr(args) and KEY not in repr(kw["env"])
    assert json.loads(kw["input"]) == {"key": KEY}
    assert kw["timeout"] == 25 and kw["capture_output"] and not kw["check"]
    assert set(kw["env"]) == {"PATH", "LANG", "LC_ALL"}
    assert result["status"] == "VERIFIED" and result["waybill_requests"] == result["eta_requests"] == 0
    assert result["actual_tracking_acceptance"] == result["quota_acceptance"] == "NOT_TESTED"
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("gateway,key", [("name", KEY), ("a" * 63, KEY), ("B" * 64, KEY),
                                         (GATEWAY, ""), (GATEWAY, "bad\nkey"),
                                         (GATEWAY, "a" * 513), (GATEWAY, None)])
def test_host_invalid_input_no_process(gateway, key):
    def runner(*args, **kw):
        pytest.fail("must not start")
    with pytest.raises(p.ProviderCheckError, match="PROVIDER_INPUT_INVALID"):
        p.check_new_key(gateway, key, runner=runner)


@pytest.mark.parametrize("kind", ["timeout", "exception", "nonzero", "stderr", "raw", "oversize", "duplicate"])
def test_host_process_failures_never_return_sensitive_data(kind):
    def runner(*args, **kw):
        if kind == "timeout": raise subprocess.TimeoutExpired(KEY, 25, output=KEY, stderr=KEY)
        if kind == "exception": raise ValueError(KEY)
        if kind == "nonzero": return completion(stdout=KEY.encode(), stderr=KEY.encode(), code=1)
        if kind == "stderr": return completion(stderr=KEY.encode())
        if kind == "raw": return completion(stdout=KEY.encode())
        if kind == "oversize": return completion(stdout=b"x" * 8193)
        return completion(stdout=b'{"status":"VERIFIED","status":"' + KEY.encode() + b'"}')
    with pytest.raises(p.ProviderCheckError) as error:
        p.check_new_key(GATEWAY, KEY, runner=runner)
    assert KEY not in str(error.value) and error.value.code in p.SAFE_ERRORS


@pytest.mark.parametrize("change", ["extra", "private_protocol", "private_contract", "invalid_count", "bool_count",
                                    "missing_phone", "missing_query", "eta_inconsistent", "product_inconsistent"])
def test_strict_metadata_projection(change):
    value = valid_result()
    if change == "extra": value["raw"] = KEY
    if change == "private_protocol": value["discovery"]["protocol_version"] = KEY
    if change == "private_contract": value["discovery"]["remote_contract"] = KEY
    if change == "invalid_count": value["control_requests"] = 6
    if change == "bool_count": value["control_requests"] = True
    if change == "missing_phone": value["discovery"]["phone_parameter_available"] = False
    if change == "missing_query": value["discovery"]["query_tool_available"] = False
    if change == "eta_inconsistent": value["discovery"]["eta_tool_available"] = False
    if change == "product_inconsistent": value["discovery"]["eta_product_required"] = True
    with pytest.raises(p.ProviderCheckError) as error:
        p.check_new_key(GATEWAY, KEY, runner=lambda *a, **k: completion(value))
    assert KEY not in str(error.value)


@pytest.mark.parametrize("reason", ["MCP_AUTH_REJECTED", "MCP_RATE_LIMITED", "MCP_QUERY_CONTRACT_UNAVAILABLE", KEY])
def test_child_stop_reason_whitelist(reason):
    with pytest.raises(p.ProviderCheckError) as error:
        p.check_new_key(GATEWAY, KEY, runner=lambda *a, **k: completion({"status": "STOP", "reason": reason}))
    assert error.value.code == (reason if reason in p.SAFE_ERRORS else "PROVIDER_CHECK_FAILED")


def child_namespace():
    module = types.ModuleType("r53_provider_child_offline_test")
    before = logging.root.manager.disable
    try:
        exec(compile(p.CHILD, "<r53-child-offline>", "exec"), module.__dict__)
    finally:
        logging.disable(before)
    return module


def exact_transport():
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == p.TRANSPORT_SHA256
    name = "r53_exact_transport_offline_test"
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def query_tool(phone=True, available=True):
    return {"name": "query_trace", "inputSchema": {"type": "object",
            "properties": {"kuaidiNum": {"type": "string"}, "responseFormat": {"type": "string"},
                           **({"phone": {"type": "string"}} if phone else {})},
            "required": ["kuaidiNum"] + ([] if available else ["unknownRequired"])}}


def eta_tool():
    required = ["kuaidicom", "from", "to", "orderTime", "logistic"]
    return {"name": "estimate_time_with_logistic", "inputSchema": {"type": "object",
            "properties": {k: {"type": "string"} for k in required + ["responseFormat"]}, "required": required}}


def mock_provider(monkeypatch, *, scenario="ok"):
    module = exact_transport()
    child = child_namespace()
    methods, client_options = [], []
    original_client = httpx.AsyncClient
    def handler(request):
        assert request.url.scheme == "https" and request.url.host == "api.kuaidi100.com"
        assert request.url.path == "/mcp/streamable" and request.url.params["key"] == KEY
        message = json.loads(request.content)
        methods.append(message["method"])
        assert message["method"] in {"initialize", "notifications/initialized", "tools/list"}
        if scenario == "timeout": raise httpx.ReadTimeout(KEY, request=request)
        if scenario == "auth": return httpx.Response(401, content=KEY.encode())
        if scenario == "redirect": return httpx.Response(302, headers={"location": "https://untrusted.invalid/" + KEY})
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
            if scenario == "secret_echo": result["extra"] = KEY
        elif message["method"] == "notifications/initialized":
            return httpx.Response(204)
        else:
            tools = [query_tool(phone=scenario != "no_phone", available=scenario != "bad_query")]
            if scenario == "ok": tools.append(eta_tool())
            if scenario == "pagination":
                page = methods.count("tools/list")
                tools = [query_tool()] if page == 1 else [{"name": "ignored_" + str(page)}]
                result = {"tools": tools, "nextCursor": "page" + str(page)}
            else:
                result = {"tools": tools}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})
    def factory(*args, **kwargs):
        client_options.append(dict(kwargs))
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        assert kwargs["transport"] is None
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)
    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    child.load_transport = lambda: module
    return child, methods, client_options


def test_actual_transport_discovery_success_only_three_control_requests(monkeypatch):
    child, methods, options = mock_provider(monkeypatch)
    value = child.run(json.dumps({"key": KEY}).encode())
    projected = p._project(value)
    assert projected["discovery"]["eta_remote_contract"] == "hosted-camel"
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert len(options) == 1 and value["control_requests"] == 3
    assert KEY not in json.dumps(value)


@pytest.mark.parametrize("scenario,reason,count", [("auth", "MCP_AUTH_REJECTED", 1),
    ("redirect", "MCP_HTTP_ERROR", 1), ("timeout", "MCP_REQUEST_TIMEOUT", 1),
    ("secret_echo", "MCP_SECRET_ECHO", 1), ("no_phone", "PROVIDER_PHONE_UNAVAILABLE", 3),
    ("bad_query", "MCP_QUERY_CONTRACT_UNAVAILABLE", 3), ("pagination", "MCP_TOOLS_LIMIT", 5)])
def test_actual_transport_failures_bounded_no_retries_or_business(monkeypatch, scenario, reason, count):
    child, methods, _ = mock_provider(monkeypatch, scenario=scenario)
    value = child.run(json.dumps({"key": KEY}).encode())
    assert value == {"status": "STOP", "reason": reason}
    assert len(methods) == count and "tools/call" not in methods
    assert KEY not in json.dumps(value)


@pytest.mark.parametrize("raw", [b"[]", b'{"key":"abc","key":"def"}', b'{"key":true}',
                                  b'{"key":"valid","extra":"value"}', b"x" * 2049])
def test_child_invalid_input_never_loads_source(raw):
    child = child_namespace()
    child.load_transport = lambda: pytest.fail("source must not be loaded")
    result = child.run(raw)
    assert result["status"] == "STOP"


def test_child_identity_guard_before_source_open():
    child = child_namespace()
    child.os = types.SimpleNamespace(getuid=lambda: 0, getgid=lambda: 0)
    assert child.run(json.dumps({"key": KEY}).encode()) == {
        "status": "STOP", "reason": "PROVIDER_IDENTITY_INVALID"}


def test_child_source_guard_and_no_relative_imports():
    child = child_namespace()
    assert child.TRANSPORT_SHA256 == p.TRANSPORT_SHA256
    assert child.TRANSPORT_SOURCE == "/app/app/kuaidi100_mcp.py"
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    assert all(node.level == 0 for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    assert "os.O_NOFOLLOW" in p.CHILD and "meta.st_nlink != 1" in p.CHILD
    assert "logging.disable(logging.CRITICAL)" in p.CHILD
    assert "asyncio.wait_for(discover(module, value[\"key\"]), 22)" in p.CHILD


def test_child_unexpected_failure_sanitized():
    child = child_namespace()
    def fail(): raise RuntimeError(KEY)
    child.load_transport = fail
    assert child.run(json.dumps({"key": KEY}).encode()) == {"status": "STOP", "reason": "PROVIDER_CHECK_FAILED"}
