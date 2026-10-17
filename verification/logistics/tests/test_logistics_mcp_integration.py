"""Real logistics service + real MCP transport; synthetic HTTP only."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx

PATH = Path(__file__).resolve().parents[1] / "backend/app/logistics_query.py"
package = types.ModuleType("logistics_mcp_business_subject")
package.__path__ = [str(PATH.parent)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".logistics_query", PATH)
q = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = q
spec.loader.exec_module(q)

KEY = "SYNTHETIC_CARRIER_KEY"
NUMBER = "DPK9277547890"
ACTOR = "SYNTHETIC_EMPLOYEE"


def trace(number):
    return {"kuaidiCom": "debangkuaidi", "kuaidiName": "德邦快递官方", "kuaidiNum": number,
            "state": "已签收", "fromTo": "合成起点 -> 合成终点", "data": [
                {"time": "2026-10-08 09:00:00", "status": "在途", "context": "合成旧事件"},
                {"time": "2026-10-09 15:00:00", "status": "签收", "context": "合成最新事件"},
                {"time": "2026-10-09 10:00:00", "status": "派送", "context": "合成中间事件"}]}


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, records=None):
        self.records = records if records is not None else {"SAC1": [NUMBER]}
        self.methods = []
        self.mes_users = []
        self.trace_arguments = []
        self.closed = 0
        self.on_rpc = None
        self.alter_result = None
        self.sse = False
        self.call_started = asyncio.Event()

    async def aclose(self):
        self.closed += 1

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        if str(request.url) == q.MES_ENDPOINT:
            self.mes_users.append(body["user_code"])
            self.assert_mes(body)
            rows = self.records[body["filters"][0]["value"]]
            if isinstance(rows, dict):
                return httpx.Response(200, json=rows)
            return httpx.Response(200, json={"code": 0, "ObjectData": [{"物流单号": row} for row in rows]})
        if str(request.url.copy_with(query=None)) != "https://api.kuaidi100.com/mcp/streamable":
            raise AssertionError("unexpected target")
        if request.url.params.get("key") != KEY:
            raise AssertionError("synthetic key missing")
        method = body["method"]
        self.methods.append(method)
        if self.on_rpc:
            response = await self.on_rpc(body)
            if response is not None:
                return response
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            value = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                     "serverInfo": {"name": "synthetic", "version": "1"}}
        elif method == "tools/list":
            value = {"tools": [{"name": "query_trace", "inputSchema": {"type": "object",
                "properties": {"kuaidi_num": {"type": "string"}, "phone": {"type": "string"}},
                "required": ["kuaidi_num"]}}]}
        elif method == "tools/call":
            if body["params"]["name"] != "query_trace":
                raise AssertionError("unexpected business tool")
            self.trace_arguments.append(body["params"]["arguments"])
            self.call_started.set()
            value = {"isError": False, "content": [], "structuredContent": trace(body["params"]["arguments"]["kuaidi_num"])}
        else:
            raise AssertionError("unexpected RPC")
        if self.alter_result:
            value = self.alter_result(method, value)
        result = {"jsonrpc": "2.0", "id": body["id"], "result": value}
        if self.sse:
            return httpx.Response(200, text="data: " + json.dumps(result) + "\n\n", headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=result)

    @staticmethod
    def assert_mes(body):
        if body["module"] != "query_sa_record" or body["filters"][0]["field"] != "sa_code":
            raise AssertionError("MES contract changed")


def service(transport, *, phone_resolver=None, **options):
    config = q.LogisticsConfig(enabled=True, kuaidi100_mcp=q.Kuaidi100MCPConfig(
        KEY, single_process_rate_guard=True, phone_resolver=phone_resolver), **options)
    return q.LogisticsQueryService(config, transport=transport)


async def query(instance, orders=None, authorize=lambda: True, **arguments):
    return await instance.query({"order_codes": orders or ["SAC1"], **arguments},
                                trusted_user_no=ACTOR, authorize=authorize)


class MCPBusiness(unittest.IsolatedAsyncioTestCase):
    async def test_server_credential_json_duplicate_fields_rejected(self):
        environment = {"MAIMI_LOGISTICS_ENABLED": "true", "MAIMI_LOGISTICS_KUAIDI100_MCP_ENABLED": "true",
                       "MAIMI_LOGISTICS_KUAIDI100_SINGLE_PROCESS": "true", "MAIMI_LOGISTICS_MES_USER_CODE": "M00001402"}
        for raw, succeeds in [('{"key":"SYNTHETIC_FIRST","key":"SYNTHETIC_SECOND"}', False),
                               ('{"key":"SYNTHETIC_ONLY"}', True)]:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic-credential.json"
                path.write_text(raw, encoding="utf-8")
                metadata = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_size=path.stat().st_size)
                with patch.object(q, "KUAIDI100_MCP_CREDENTIAL_FILE", str(path)), \
                     patch.object(q.os, "O_NOFOLLOW", 0, create=True), \
                     patch.object(q.os, "geteuid", lambda: 0, create=True), \
                     patch.object(q.os, "fstat", return_value=metadata):
                    if succeeds:
                        config = q.LogisticsConfig.from_env(environment)
                        self.assertEqual(config.kuaidi100_mcp.key, "SYNTHETIC_ONLY")
                        self.assertEqual(config.mes_user_code, "M00001402")
                    else:
                        with self.assertRaisesRegex(q.LogisticsQueryError, "INVALID_SERVER_CONFIGURATION"):
                            q.LogisticsConfig.from_env(environment)

    async def test_lazy_one_connection_multiple_orders_structured_latest_and_separate_carrier(self):
        transport = Transport({"SAC1": [NUMBER, "成达"], "SAC2": ["KY9277547890", None]})
        transport.sse = True
        value = await query(service(transport), ["SAC1", "SAC2"])
        self.assertEqual(transport.methods.count("initialize"), 1)
        self.assertEqual(transport.methods.count("tools/list"), 1)
        self.assertEqual(transport.methods.count("tools/call"), 2)
        self.assertEqual(transport.closed, 2)
        self.assertEqual(value["counts"]["tracking_requests"], 2)
        self.assertEqual(value["rows"][0]["carrier"], "德邦")
        self.assertEqual(value["rows"][0]["tracking_carrier_name"], "德邦快递官方")
        self.assertEqual(value["rows"][0]["event_time"], "2026-10-09 15:00:00")
        self.assertEqual(value["rows"][0]["latest_event"], "合成最新事件")
        self.assertEqual(value["rows"][1]["mes_logistics_text"], "成达")
        self.assertEqual(value["rows"][2]["carrier"], "跨越")
        self.assertEqual(value["rows"][3]["query_status"], "MES_EMPTY")
        self.assertNotIn(KEY, json.dumps(value))

    async def test_strict_json_text_supported_markdown_and_duplicate_rejected(self):
        for content, expected in [(json.dumps(trace(NUMBER)), "DELIVERED"),
                                  ("已签收：合成位置", "PROVIDER_INVALID_RESPONSE"),
                                  ('{"kuaidiNum":"A","kuaidiNum":"B"}', "PROVIDER_INVALID_RESPONSE")]:
            with self.subTest(expected=expected, content=content[:20]):
                transport = Transport()
                transport.alter_result = lambda method, value: {"content": [{"type": "text", "text": content}]} if method == "tools/call" else value
                result = await query(service(transport))
                self.assertEqual(result["rows"][0]["tracking_status"], expected)
                self.assertEqual(result["rows"][0]["mes_logistics_text"], NUMBER)

    async def test_structured_content_takes_priority_and_exact_waybill(self):
        for changed in [None, {**trace(NUMBER), "kuaidiNum": NUMBER.lower()}, {**trace(NUMBER), "data": "invalid"}]:
            transport = Transport()
            transport.alter_result = lambda method, value: {"structuredContent": changed,
                "content": [{"type": "text", "text": json.dumps(trace(NUMBER))}]} if method == "tools/call" else value
            result = await query(service(transport))
            self.assertEqual(result["rows"][0]["tracking_status"], "PROVIDER_INVALID_RESPONSE")

    async def test_message_only_and_iserror_false_not_success_or_raw_error_output(self):
        transport = Transport()
        transport.alter_result = lambda method, value: {"isError": False, "content": [],
            "structuredContent": {"message": "SYNTHETIC_PRIVATE_PROVIDER_ERROR"}} if method == "tools/call" else value
        result = await query(service(transport))
        self.assertEqual(result["rows"][0]["tracking_status"], "PROVIDER_UNAVAILABLE")
        self.assertNotIn("SYNTHETIC_PRIVATE_PROVIDER_ERROR", json.dumps(result))

    async def test_phone_missing_does_not_make_provider_or_metadata_calls(self):
        transport = Transport({"SAC1": ["SF9277547890", "ZY9277547890"]})
        result = await query(service(transport))
        self.assertEqual([r["tracking_status"] for r in result["rows"]], ["PHONE_REQUIRED", "PHONE_REQUIRED"])
        self.assertEqual(transport.methods, [])
        self.assertEqual(result["counts"]["tracking_requests"], 0)

    async def test_phone_only_from_trusted_resolver_and_no_phone_in_result(self):
        transport = Transport({"SAC1": ["SF9277547890"]})
        identities = []
        def phone(user, order, carrier, number):
            identities.append(user)
            return "13900008000"
        result = await query(service(transport, phone_resolver=phone, mes_user_code="M00001402"))
        self.assertEqual(transport.mes_users, ["M00001402"])
        self.assertEqual(identities, [ACTOR])
        self.assertEqual(transport.trace_arguments[0]["phone"], "13900008000")
        self.assertNotIn("13900008000", json.dumps(result))
        self.assertNotIn("M00001402", json.dumps(result))

    async def test_mcp_is_lazy_for_no_waybill_mes_reject_and_tracking_optout(self):
        cases = [(["成达", "客户自提", None], True),
                 ({"code": 400, "message": "private denial", "ObjectData": None}, True),
                 ([NUMBER], False)]
        for rows, enabled in cases:
            transport = Transport({"SAC1": rows})
            result = await query(service(transport), include_tracking=enabled)
            self.assertEqual(transport.methods, [])
            self.assertEqual(result["counts"]["tracking_requests"], 0)

    async def test_fixed_shared_mes_identity_not_a_model_argument(self):
        transport = Transport()
        instance = service(transport, mes_user_code="M00001402")
        for arguments in ({"user_code": "OTHER"}, {"mes_user_code": "OTHER"}, {"phone": "1234"}):
            with self.assertRaisesRegex(q.LogisticsQueryError, "INVALID_ARGUMENTS"):
                await query(instance, **arguments)
        with self.assertRaisesRegex(q.LogisticsQueryError, "INVALID_SERVER_CONFIGURATION"):
            service(transport, mes_user_code="OTHER")
        with self.assertRaisesRegex(q.LogisticsQueryError, "AUTHORIZATION_LOST"):
            await query(instance, authorize=lambda: False)
        self.assertEqual(transport.mes_users, [])

    async def test_twenty_call_budget_shared_across_orders(self):
        rows = {f"SAC{i}": [f"DPK{i:04d}{j:06d}" for j in range(2)] for i in range(20)}
        transport = Transport(rows)
        result = await query(service(transport), list(rows))
        self.assertEqual(len(transport.trace_arguments), 20)
        self.assertEqual(result["counts"]["tracking_requests"], 20)
        self.assertEqual(result["counts"]["tracking_budget_exceeded_rows"], 20)
        self.assertEqual(transport.methods.count("initialize"), 1)

    async def test_same_waybill_guard_shared_across_queries_and_carrier_labels(self):
        transport = Transport({"SAC1": [NUMBER, NUMBER]})
        instance = service(transport)
        first = await query(instance)
        second = await query(instance)
        self.assertEqual(len(transport.trace_arguments), 1)
        self.assertEqual(first["rows"][1]["tracking_status"], "RATE_LIMITED")
        self.assertTrue(all(row["tracking_status"] == "RATE_LIMITED" for row in second["rows"]))

    async def test_discovery_error_attempted_once_preserves_all_mes(self):
        transport = Transport({"SAC1": [NUMBER, "KY9277547890"], "SAC2": ["JD1234567890"]})
        async def rejected(body):
            return httpx.Response(401, json={"message": "not exposed"}) if body["method"] == "initialize" else None
        transport.on_rpc = rejected
        result = await query(service(transport), ["SAC1", "SAC2"])
        self.assertEqual(transport.methods, ["initialize"])
        self.assertEqual(len(result["rows"]), 3)
        self.assertTrue(all(row["tracking_status"] == "PROVIDER_AUTH_ERROR" for row in result["rows"]))
        self.assertEqual(result["counts"]["tracking_requests"], 0)

    async def test_total_timeout_retains_all_received_mes_and_closes_clients(self):
        transport = Transport({"SAC1": [NUMBER, "KY9277547890"]})
        async def delayed(body):
            if body["method"] == "tools/call":
                await asyncio.sleep(1)
        transport.on_rpc = delayed
        result = await query(service(transport, total_timeout_seconds=0.02))
        self.assertEqual([row["tracking_status"] for row in result["rows"]], ["TRACKING_TIMEOUT", "TRACKING_TIMEOUT"])
        self.assertEqual(transport.closed, 2)

    async def test_revocation_during_discovery_prevents_paid_call_and_result(self):
        transport = Transport()
        allowed = True
        async def revoke(body):
            nonlocal allowed
            if body["method"] == "tools/list":
                allowed = False
        transport.on_rpc = revoke
        with self.assertRaisesRegex(q.LogisticsQueryError, "AUTHORIZATION_LOST"):
            await query(service(transport), authorize=lambda: allowed)
        self.assertNotIn("tools/call", transport.methods)
        self.assertEqual(transport.closed, 2)

    async def test_revocation_after_trace_drops_all_results(self):
        transport = Transport({"SAC1": [NUMBER], "SAC2": ["KY9277547890"]})
        allowed = True
        async def revoke(body):
            nonlocal allowed
            if body["method"] == "tools/call":
                allowed = False
        transport.on_rpc = revoke
        with self.assertRaisesRegex(q.LogisticsQueryError, "AUTHORIZATION_LOST"):
            await query(service(transport), ["SAC1", "SAC2"], authorize=lambda: allowed)
        self.assertEqual(transport.methods.count("tools/call"), 1)
        self.assertEqual(transport.closed, 2)

    async def test_task_cancellation_closes_lazy_mcp_client(self):
        transport = Transport()
        started = asyncio.Event()
        async def delayed(body):
            if body["method"] == "tools/call":
                started.set()
                await asyncio.sleep(10)
        transport.on_rpc = delayed
        task = asyncio.create_task(query(service(transport)))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(transport.closed, 2)

    async def test_unparseable_event_never_claimed_latest(self):
        transport = Transport()
        def invalid_date(method, result):
            if method == "tools/call":
                result["structuredContent"]["data"][0]["time"] = "yesterday"
            return result
        transport.alter_result = invalid_date
        result = await query(service(transport))
        row = result["rows"][0]
        self.assertTrue(row["tracking_time_unverified"])
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(row["latest_event"])
        self.assertIsNone(row["event_time"])
        self.assertEqual(row["tracking_state_text"], "已签收")

    async def test_offsets_are_compared_by_instant(self):
        transport = Transport()
        def change(method, result):
            if method == "tools/call":
                result["structuredContent"]["data"] = [
                    {"time": "2026-10-09T08:00:00Z", "status": "签收", "context": "later"},
                    {"time": "2026-10-09T15:59:59+08:00", "status": "签收", "context": "earlier"}]
            return result
        transport.alter_result = change
        result = await query(service(transport))
        self.assertEqual(result["rows"][0]["latest_event"], "later")

    async def test_two_concurrent_queries_share_same_waybill_guard(self):
        transport = Transport()
        async def interleave(body):
            await asyncio.sleep(0)
        transport.on_rpc = interleave
        instance = service(transport)
        results = await asyncio.gather(query(instance), query(instance))
        self.assertEqual(transport.methods.count("tools/call"), 1)
        self.assertEqual(sum(result["counts"]["tracking_requests"] for result in results), 1)

    async def test_bad_remote_tool_contract_has_no_paid_call(self):
        transport = Transport()
        transport.alter_result = lambda method, value: {"tools": []} if method == "tools/list" else value
        result = await query(service(transport))
        self.assertEqual(result["rows"][0]["tracking_status"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(result["counts"]["tracking_requests"], 0)
        self.assertNotIn("tools/call", transport.methods)

    async def test_concurrency_and_deadline_upper_bounds(self):
        for options in ({"concurrency": 5}, {"total_timeout_seconds": 46}):
            with self.assertRaisesRegex(q.LogisticsQueryError, "INVALID_SERVER_CONFIGURATION"):
                service(Transport(), **options)


if __name__ == "__main__":
    unittest.main()
