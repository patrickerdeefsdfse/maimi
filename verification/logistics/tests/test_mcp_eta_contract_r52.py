"""Observed hosted ETA versus public-package contract; httpx mock only."""
import asyncio
from copy import deepcopy
import json
import unittest

import httpx

from test_kuaidi100_mcp import FakeServer, TEST_KEY, SESSION, client, mod, tool


def eta_tool(contract="hosted", **overrides):
    names = (["kuaidicom", "from", "to", "orderTime", "logistic"] if contract == "hosted"
             else ["kuaidi_com", "from_loc", "to_loc", "order_time", "exp_type", "logistic"])
    required = [x for x in names if contract == "hosted" or x != "order_time"]
    props = {x: {"type": "string"} for x in names}
    props.update(responseFormat={"type": "string"}, unknownOptional={"type": "string"})
    value = {"name": "estimate_time_with_logistic", "inputSchema": {
        "type": "object", "properties": props, "required": required}}
    value.update(overrides)
    return value


def args(**overrides):
    value = dict(kuaidi_com="synthetic", from_loc="合成始发市", to_loc="合成目的市",
                 order_time="2026-10-10 08:00:00", logistic='[{"time":"2026-10-10 08:00:00","context":"合成揽收"}]')
    value.update(overrides)
    return value


class Server(FakeServer):
    def __init__(self, contract="hosted", *, schema=None, sse=False):
        super().__init__(schema=eta_tool(contract) if schema is None else schema, sse=sse)

    @property
    def calls(self):
        return [json.loads(r.content)["params"] for r in self.requests
                if json.loads(r.content)["method"] == "tools/call"]


class HostedETA(unittest.IsolatedAsyncioTestCase):
    async def test_observed_seven_string_hosted_schema_maps_without_product_or_annotations(self):
        for sse in (False, True):
            server = Server(sse=sse)
            self.assertEqual(len(server.schema["inputSchema"]["properties"]), 7)
            self.assertNotIn("annotations", server.schema)
            async with client(server) as instance:
                found = await instance.discover()
                self.assertTrue(found.eta_tool_available)
                self.assertEqual(found.eta_remote_contract, "hosted-camel")
                self.assertFalse(found.eta_product_required)
                await instance.estimate_time_with_logistic(**args())
            self.assertEqual(server.calls, [{"name": "estimate_time_with_logistic", "arguments": {
                "kuaidicom": "synthetic", "from": "合成始发市", "to": "合成目的市",
                "orderTime": "2026-10-10 08:00:00", "logistic": args()["logistic"], "responseFormat": "json"}}])
            self.assertEqual(len(server.requests), 4)
            for request in server.requests:
                self.assertEqual(str(request.url.copy_with(query=None)), mod.ENDPOINT)
                self.assertEqual(dict(request.url.params), {"key": TEST_KEY})
                self.assertNotIn(TEST_KEY, request.content.decode())

    async def test_hosted_never_sends_unknown_optional_or_unadvertised_product(self):
        server = Server()
        async with client(server) as instance:
            await instance.estimate_time_with_logistic(**args(exp_type="来源中真实产品"))
        self.assertNotIn("exp_type", server.calls[0]["arguments"])
        self.assertNotIn("expType", server.calls[0]["arguments"])
        self.assertNotIn("unknownOptional", server.calls[0]["arguments"])

    async def test_hosted_omits_response_format_argument_unless_advertised(self):
        schema = eta_tool()
        del schema["inputSchema"]["properties"]["responseFormat"]
        server = Server(schema=schema)
        async with client(server) as instance:
            await instance.estimate_time_with_logistic(**args(exp_type=None))
        self.assertNotIn("responseFormat", server.calls[0]["arguments"])

    async def test_public_snake_contract_retains_source_product_requirement(self):
        server = Server("public")
        async with client(server) as instance:
            found = await instance.discover()
            self.assertTrue(found.eta_tool_available)
            self.assertTrue(found.eta_product_required)
            self.assertEqual(found.eta_remote_contract, "public-snake")
            for empty in (None, ""):
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ARGUMENTS_INVALID"):
                    await instance.estimate_time_with_logistic(**args(exp_type=empty))
            self.assertEqual(server.calls, [])
            await instance.estimate_time_with_logistic(**args(exp_type="真实产品"))
        self.assertEqual(server.calls[0]["arguments"], {**args(exp_type="真实产品"), "responseFormat": "json"})

    async def test_contracts_reject_unknown_required_missing_required_bad_types_and_alias_mix(self):
        cases = []
        for contract in ("hosted", "public"):
            original = eta_tool(contract)
            for required in original["inputSchema"]["required"]:
                bad = deepcopy(original)
                bad["inputSchema"]["properties"][required]["type"] = "integer"
                cases.append(bad)
                bad = deepcopy(original)
                bad["inputSchema"]["required"].remove(required)
                cases.append(bad)
            for mutation in ("unknown", "duplicate", "nonstr", "response-type", "response-enum", "mixed"):
                bad = deepcopy(original)
                if mutation == "unknown": bad["inputSchema"]["required"].append("unknownOptional")
                if mutation == "duplicate": bad["inputSchema"]["required"].append("logistic")
                if mutation == "nonstr": bad["inputSchema"]["required"].append(4)
                if mutation == "response-type": bad["inputSchema"]["properties"]["responseFormat"]["type"] = "integer"
                if mutation == "response-enum": bad["inputSchema"]["properties"]["responseFormat"]["enum"] = ["xml"]
                if mutation == "mixed": bad["inputSchema"]["properties"]["kuaidi_com" if contract == "hosted" else "kuaidicom"] = {"type": "string"}
                cases.append(bad)
        for schema in cases:
            with self.subTest(schema=schema):
                server = Server(schema=schema)
                async with client(server) as instance:
                    found = await instance.discover()
                    self.assertFalse(found.eta_tool_available)
                    with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ETA_CONTRACT_UNAVAILABLE"):
                        await instance.estimate_time_with_logistic(**args(exp_type="真实产品"))
                self.assertEqual(server.calls, [])
                self.assertEqual(len(server.requests), 3)

    async def test_explicit_mutating_or_malformed_annotations_rejected_missing_allowed(self):
        for annotation in ({"destructiveHint": True}, {"readOnlyHint": False}, None,
                           {"readOnlyHint": "true"}, {"destructiveHint": "false"}):
            server = Server(schema=eta_tool(annotations=annotation))
            async with client(server) as instance:
                self.assertFalse((await instance.discover()).eta_tool_available)
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ETA_CONTRACT_UNAVAILABLE"):
                    await instance.estimate_time_with_logistic(**args())
            self.assertEqual(server.calls, [])
        server = Server(schema=eta_tool(annotations={"readOnlyHint": True, "destructiveHint": False}))
        async with client(server) as instance:
            await instance.estimate_time_with_logistic(**args())
        self.assertEqual(len(server.calls), 1)

    async def test_preshipment_estimate_time_is_not_discovered_or_called(self):
        server = Server(schema=eta_tool(name="estimate_time"))
        async with client(server) as instance:
            self.assertFalse((await instance.discover()).eta_tool_available)
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ETA_CONTRACT_UNAVAILABLE"):
                await instance.estimate_time_with_logistic(**args())
        self.assertEqual(server.calls, [])

    async def test_forecast_budget_four_and_no_automatic_retry(self):
        server = Server()
        async with client(server) as instance:
            for _ in range(mod.MAX_ETA_CALLS):
                await instance.estimate_time_with_logistic(**args())
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ETA_LIMIT"):
                await instance.estimate_time_with_logistic(**args())
        self.assertEqual(len(server.calls), 4)
        server = Server()
        server.alter = lambda body, _: httpx.Response(502, content=TEST_KEY) if body["method"] == "tools/call" else None
        async with client(server) as instance:
            with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_HTTP_ERROR") as caught:
                await instance.estimate_time_with_logistic(**args())
            self.assertNotIn(TEST_KEY, str(caught.exception))
        self.assertEqual(len(server.calls), 1)

    async def test_forecast_secret_echo_and_encoded_secret_never_leave_transport(self):
        for secret in (TEST_KEY, SESSION):
            encoded = "".join("\\u%04x" % ord(c) for c in secret)
            server = Server()
            server.alter = lambda body, _, encoded=encoded: httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text",
                "text": '{"arrivalTime":"2026-10-12 09:00","extra":"' + encoded + '"}'}]}}) if body["method"] == "tools/call" else None
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_SECRET_ECHO"):
                    await instance.estimate_time_with_logistic(**args())

    async def test_invalid_arguments_do_not_start_discovery(self):
        for patch in ({"kuaidi_com": "https://invalid"}, {"order_time": ""}, {"logistic": ""},
                      {"exp_type": 42}, {"from_loc": None}, {"logistic": "字" * 64001}):
            server = Server()
            async with client(server) as instance:
                with self.assertRaisesRegex(mod.Kuaidi100MCPError, "MCP_ARGUMENTS_INVALID"):
                    await instance.estimate_time_with_logistic(**args(**patch))
            self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
