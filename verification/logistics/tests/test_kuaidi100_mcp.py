"""Synthetic protocol tests; no key, waybill or network from the user is used."""
import asyncio
from dataclasses import asdict
import importlib.util
import json
import logging
from pathlib import Path
import sys
import unittest

import httpx

PATH = Path(__file__).resolve().parents[1] / "backend/app/kuaidi100_mcp.py"
SPEC = importlib.util.spec_from_file_location("mcp_transport_test_subject", PATH)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)
TEST_KEY = "SYNTHETIC_KEY_ONLY"
SESSION = "synthetic-session-token"


def tool(**overrides):
    value = {"name": "query_trace", "inputSchema": {"type": "object", "properties": {
        "kuaidi_num": {"type": "string"}, "phone": {"type": "string", "default": ""}},
        "required": ["kuaidi_num"]}}
    value.update(overrides)
    return value


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False
    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk
    async def aclose(self):
        self.closed = True


class FakeServer:
    def __init__(self, *, schema=None, sse=False):
        self.requests = []
        self.schema = tool() if schema is None else schema
        self.sse = sse
        self.alter = None
    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        if self.alter:
            value = self.alter(body, request)
            if value is not None:
                return value
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [self.schema]}
        else:
            result = {"isError": False, "content": [{"type": "text", "text": "{}"}],
                      "structuredContent": {"kuaidiNum": "TEST123456", "data": []}}
        output = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        headers = {"Mcp-Session-Id": SESSION} if method == "initialize" else {}
        if self.sse:
            text = "data: " + json.dumps(output) + "\n\n"
            return httpx.Response(200, content=text, headers={**headers, "Content-Type": "text/event-stream"})
        return httpx.Response(200, json=output, headers=headers)


def client(server, **config):
    return mod.Kuaidi100MCPClient(mod.Kuaidi100MCPConfig(TEST_KEY, **config),
                                transport=httpx.MockTransport(server))


class Configuration(unittest.TestCase):
    def test_secrets_never_in_repr_and_bad_configuration(self):
        self.assertNotIn(TEST_KEY, repr(mod.Kuaidi100MCPConfig(TEST_KEY)))
        self.assertNotIn(TEST_KEY, repr(client(FakeServer())))
        for key in ("", "abc defgh", "abc\nxxxxx", "a" * 513):
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_CONFIGURATION_INVALID"):
                mod.Kuaidi100MCPConfig(key)
        self.assertEqual(str(mod.Kuaidi100MCPError("secret-body")), "MCP_RESPONSE_INVALID")


class Protocol(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_discovery_uses_only_initialization_and_list(self):
        server = FakeServer()
        async with client(server) as instance:
            found = await instance.discover()
            cached = await instance.discover()
            self.assertEqual(found, cached)
            self.assertEqual(asdict(found), {"protocol_version": "2025-03-26", "tools_seen": 1,
                "query_tool_available": True, "phone_parameter_available": True,
                "remote_contract": "kuaidi_num", "response_format_parameter_available": False,
                "eta_tool_available": False, "eta_remote_contract": None, "eta_product_required": False})
            self.assertNotIn(SESSION, repr(instance))
            self.assertFalse(instance._http.trust_env)
            self.assertFalse(instance._http.follow_redirects)
        bodies = [json.loads(r.content) for r in server.requests]
        self.assertEqual([b["method"] for b in bodies], ["initialize", "notifications/initialized", "tools/list"])
        self.assertNotIn("id", bodies[1])
        for request in server.requests:
            self.assertEqual(str(request.url.copy_with(query=None)), mod.ENDPOINT)
            self.assertEqual(dict(request.url.params), {"key": TEST_KEY})
            self.assertEqual(request.headers["responseFormat"], "json")
        self.assertNotIn("Mcp-Session-Id", server.requests[0].headers)
        self.assertEqual(server.requests[1].headers["Mcp-Session-Id"], SESSION)
        self.assertEqual(server.requests[2].headers["MCP-Protocol-Version"], "2025-03-26")

    async def test_fixed_query_json_and_sse(self):
        for sse in (False, True):
            server = FakeServer(sse=sse)
            async with client(server) as instance:
                result = await instance.query_trace("TEST123456", phone="1234")
            self.assertEqual(result["structuredContent"]["kuaidiNum"], "TEST123456")
            self.assertEqual(json.loads(server.requests[-1].content)["params"],
                {"name": "query_trace", "arguments": {"kuaidi_num": "TEST123456", "phone": "1234"}})

    async def test_observed_hosted_contract_uses_camelcase_and_json_without_tool_key(self):
        schema = {"type": "object", "required": ["kuaidiNum"], "properties": {
            "kuaidiNum": {"type": "string"}, "phone": {"type": "string"},
            "key": {"type": "string"}, "responseFormat": {"type": "string"}}}
        server = FakeServer(schema=tool(inputSchema=schema))
        async with client(server) as instance:
            found = await instance.discover()
            self.assertEqual(found.remote_contract, "kuaidiNum")
            self.assertTrue(found.response_format_parameter_available)
            await instance.query_trace("TEST123456", phone="13900000000")
        call = json.loads(server.requests[-1].content)
        self.assertEqual(call["params"], {"name": "query_trace", "arguments": {
            "kuaidiNum": "TEST123456", "phone": "13900000000", "responseFormat": "json"}})
        self.assertNotIn(TEST_KEY, server.requests[-1].content.decode())
        self.assertEqual(server.requests[-1].url.params["key"], TEST_KEY)

    async def test_conflicting_aliases_and_required_tool_key_rejected_without_query(self):
        for props, required in [
            ({"kuaidi_num": {"type": "string"}, "kuaidiNum": {"type": "string"}}, ["kuaidiNum"]),
            ({"kuaidiNum": {"type": "string"}, "key": {"type": "string"}}, ["kuaidiNum", "key"]),
            ({"kuaidiNum": {"type": "integer"}}, ["kuaidiNum"]),
            ({"kuaidiNum": {"type": "string"}, "responseFormat": {"type": "integer"}}, ["kuaidiNum", "responseFormat"]),
        ]:
            server = FakeServer(schema=tool(inputSchema={"type": "object", "properties": props, "required": required}))
            async with client(server) as instance:
                found = await instance.discover()
                self.assertFalse(found.query_tool_available)
                self.assertIsNone(found.remote_contract)
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_QUERY_CONTRACT_UNAVAILABLE"):
                    await instance.query_trace("TEST123456")
            self.assertEqual(len(server.requests), 3)

    async def test_invalid_arguments_cause_no_requests(self):
        server = FakeServer()
        async with client(server) as instance:
            for num, phone in [("short", ""), ("TEST 123456", ""), ("TEST123456", "1" * 21),
                               ("https://example.com", ""), (None, ""), ("TEST123456", 1234)]:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ARGUMENTS_INVALID"):
                    await instance.query_trace(num, phone=phone)
        self.assertEqual(server.requests, [])

    async def test_full_phone_is_passed_without_truncation(self):
        server = FakeServer()
        async with client(server) as instance:
            await instance.query_trace("TEST123456", phone="13900000000")
        self.assertEqual(json.loads(server.requests[-1].content)["params"]["arguments"]["phone"], "13900000000")

    async def test_unavailable_or_mutating_contract_never_called(self):
        cases = [tool(name="send_order"), tool(annotations={"readOnlyHint": False}),
                 tool(annotations={"destructiveHint": True}),
                 tool(inputSchema={"type": "object", "properties": {"kuaidi_num": {"type": "string"}},
                                   "required": ["kuaidi_num", "customer"]})]
        for schema in cases:
            server = FakeServer(schema=schema)
            async with client(server) as instance:
                found = await instance.discover()
                self.assertFalse(found.query_tool_available)
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_QUERY_CONTRACT_UNAVAILABLE"):
                    await instance.query_trace("TEST123456")
            self.assertEqual(len(server.requests), 3)

    async def test_http_errors_are_sanitized_without_retry_or_redirect(self):
        for status, code in [(302, "MCP_HTTP_ERROR"), (401, "MCP_AUTH_REJECTED"),
                             (403, "MCP_AUTH_REJECTED"), (429, "MCP_RATE_LIMITED"), (502, "MCP_HTTP_ERROR")]:
            server = FakeServer()
            server.alter = lambda *_: httpx.Response(status, content=TEST_KEY,
                                                       headers={"location": "https://other.invalid"})
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, code) as error:
                    await instance.discover()
                self.assertNotIn(TEST_KEY, repr(error.exception))
            self.assertEqual(len(server.requests), 1)

    async def test_rpc_id_error_duplicate_and_nonobject_rejected(self):
        invalid = [
            {"jsonrpc": "2.0", "id": True, "result": {}},
            {"jsonrpc": "2.0", "id": "1", "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 1, "result": []},
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
            {"jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage", "params": {}},
            {"jsonrpc": "1.0", "id": 1, "result": {}},
        ]
        for data in invalid:
            server = FakeServer()
            server.alter = lambda *_, data=data: httpx.Response(200, json=data)
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_RESPONSE_INVALID"):
                    await instance.discover()
        server = FakeServer()
        server.alter = lambda *_: httpx.Response(200,
            content='{"jsonrpc":"2.0","id":1,"id":1,"result":{}}', headers={"content-type": "application/json"})
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_RESPONSE_INVALID"):
                await instance.discover()

    async def test_rpc_error_never_outputs_server_message(self):
        server = FakeServer()
        server.alter = lambda *_: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
            "error": {"code": -32000, "message": TEST_KEY + " private server URL"}})
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_RPC_ERROR") as error:
                await instance.discover()
            self.assertNotIn(TEST_KEY, repr(error.exception))

    async def test_session_rotation_rejected_and_secret_echo_dropped(self):
        server = FakeServer()
        server.alter = lambda body, _: httpx.Response(202, headers={"Mcp-Session-Id": "changed"}) if body["method"] == "notifications/initialized" else None
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_SESSION_INVALID"):
                await instance.discover()
        server = FakeServer()
        server.alter = lambda body, _: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
            "result": {"tools": [], "secret": TEST_KEY}}) if body["method"] == "tools/list" else None
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_SECRET_ECHO"):
                await instance.discover()

    async def test_nested_json_unicode_escaped_key_and_session_never_leave_transport(self):
        for secret in (TEST_KEY, SESSION):
            encoded = "".join("\\u%04x" % ord(char) for char in secret)
            text = '{"kuaidiNum":"TEST123456","data":[{"context":"' + encoded + '"}]}'
            self.assertNotIn(secret, text)
            server = FakeServer()
            server.alter = lambda body, _, text=text: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                "result": {"isError": False, "content": [{"type": "text", "text": text}]}}) if body["method"] == "tools/call" else None
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_SECRET_ECHO") as error:
                    await instance.query_trace("TEST123456")
                self.assertNotIn(secret, str(error.exception))
            self.assertEqual(len(server.requests), 4)

    async def test_response_size_declared_and_decoded_are_bounded(self):
        for response in [httpx.Response(200, headers={"content-length": str(mod.MAX_RESPONSE_BYTES + 1)}),
                         httpx.Response(200, stream=BytesStream([b"x" * (mod.MAX_RESPONSE_BYTES + 1)]),
                                        headers={"content-type": "application/json"})]:
            server = FakeServer()
            server.alter = lambda *_, response=response: response
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_RESPONSE_TOO_LARGE"):
                    await instance.discover()

    async def test_sse_split_chunks_notifications_and_result_closes_stream(self):
        server = FakeServer()
        response = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}}
        data = b': ping\r\n\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\r\n\r\n'
        data += ("data: " + json.dumps(response) + "\r\n\r\n").encode()
        stream = BytesStream([data[index:index + 7] for index in range(0, len(data), 7)] + [b"never-read"])
        server.alter = lambda body, _: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"}) if body["method"] == "initialize" else None
        async with client(server) as instance:
            self.assertTrue((await instance.discover()).query_tool_available)
        self.assertTrue(stream.closed)

    async def test_sse_notification_flood_bounded(self):
        server = FakeServer()
        server.alter = lambda *_: httpx.Response(200, content=": ping\n\n" * (mod.MAX_EVENTS + 1),
            headers={"content-type": "text/event-stream"})
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_RESPONSE_INVALID"):
                await instance.discover()

    async def test_total_timeout_is_independent_of_chunk_arrival(self):
        server = FakeServer()
        server.alter = lambda *_: httpx.Response(200, stream=BytesStream([b": ping\n\n"] * 50, delay=.02),
            headers={"content-type": "text/event-stream"})
        async with client(server, operation_timeout_seconds=.035) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_REQUEST_TIMEOUT"):
                await instance.discover()
        self.assertEqual(len(server.requests), 1)

    async def test_page_and_tool_count_limits(self):
        server = FakeServer()
        server.alter = lambda body, _: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
            "result": {"tools": [], "nextCursor": str(body["id"])}}) if body["method"] == "tools/list" else None
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_TOOLS_LIMIT"):
                await instance.discover()
        self.assertEqual(len(server.requests), 5)
        server = FakeServer()
        server.alter = lambda body, _: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
            "result": {"tools": [tool(name=f"t{i}") for i in range(65)]}}) if body["method"] == "tools/list" else None
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_TOOLS_LIMIT"):
                await instance.discover()

    async def test_query_count_limit_and_tool_error_preserved_for_business_parser(self):
        server = FakeServer()
        server.alter = lambda body, _: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
            "result": {"isError": True, "content": [{"type": "text", "text": "not available"}]}}) if body["method"] == "tools/call" else None
        async with client(server) as instance:
            for _ in range(20):
                self.assertTrue((await instance.query_trace("TEST123456"))["isError"])
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_QUERY_LIMIT"):
                await instance.query_trace("TEST123456")
        self.assertEqual(len(server.requests), 23)

    async def test_httpx_info_url_does_not_disclose_key(self):
        with self.assertLogs("httpx", logging.INFO) as captured:
            async with client(FakeServer()) as instance:
                await instance.discover()
        self.assertTrue(captured.output)
        self.assertNotIn(TEST_KEY, "\n".join(captured.output))
        self.assertIn("REDACTED", "\n".join(captured.output))

    async def test_connect_exception_is_sanitized_and_not_retried(self):
        server = FakeServer()
        def fail(*_):
            raise httpx.ConnectError("https://example.invalid/?key=" + TEST_KEY)
        server.alter = fail
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_CONNECTION_FAILED") as error:
                await instance.discover()
            self.assertNotIn(TEST_KEY, repr(error.exception))
        self.assertEqual(len(server.requests), 1)

    async def test_unexpected_mime_protocol_and_tool_result_rejected(self):
        for body in [
            {"protocolVersion": ["2025-03-26"], "capabilities": {"tools": {}}},
            {"protocolVersion": "2099-01-01", "capabilities": {"tools": {}}},
        ]:
            server = FakeServer()
            server.alter = lambda *_, body=body: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": body})
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_PROTOCOL_UNSUPPORTED"):
                    await instance.discover()
        for result in [{"content": [{"type": "image", "data": "abc"}]},
                       {"content": [], "isError": "false"},
                       {"content": [], "structuredContent": []}]:
            server = FakeServer()
            server.alter = lambda body, _, result=result: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                "result": result}) if body["method"] == "tools/call" else None
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_TOOL_RESULT_INVALID"):
                    await instance.query_trace("TEST123456")

    async def test_compressed_or_non_json_reply_rejected_before_body(self):
        for headers in [{"content-type": "text/html"},
                        {"content-type": "application/json", "content-encoding": "gzip"}]:
            server = FakeServer()
            server.alter = lambda *_, headers=headers: httpx.Response(200, stream=BytesStream([b"not a protocol result"]), headers=headers)
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_RESPONSE_INVALID"):
                    await instance.discover()

    async def test_empty_session_identifier_rejected(self):
        server = FakeServer()
        server.alter = lambda *_: httpx.Response(200, headers={"mcp-session-id": ""}, json={})
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_SESSION_INVALID"):
                await instance.discover()

    async def test_lock_serializes_calls_and_cancel_closes_response(self):
        server = FakeServer()
        async with client(server) as instance:
            results = await asyncio.gather(instance.query_trace("TEST123456"), instance.query_trace("TEST123457"))
        self.assertEqual(len(results), 2)
        self.assertEqual([json.loads(req.content)["method"] for req in server.requests],
                         ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"])
        self.assertEqual([json.loads(req.content)["id"] for req in server.requests if "id" in json.loads(req.content)], [1, 2, 3, 4])
        stream = BytesStream([b": ping\n\n"] * 20, delay=.05)
        server = FakeServer()
        server.alter = lambda *_: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        async with client(server) as instance:
            task = asyncio.create_task(instance.discover())
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
