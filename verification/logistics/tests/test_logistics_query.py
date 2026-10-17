"""Local contract/security tests; every network call uses httpx.MockTransport."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

_FILE = Path(__file__).resolve().parents[1] / "backend" / "app" / "logistics_query.py"
_PACKAGE = types.ModuleType("logistics_query_test_subject")
_PACKAGE.__path__ = [str(_FILE.parent)]
sys.modules[_PACKAGE.__name__] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(_PACKAGE.__name__ + ".logistics_query", _FILE)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


def mes_response(text="SF927365110001"):
    return httpx.Response(200, json={"code": 0, "ObjectData": [{"物流单号": text}]})


def service(handler, **config):
    return mod.LogisticsQueryService(mod.LogisticsConfig(enabled=True, **config), transport=httpx.MockTransport(handler))


def provider(**options):
    return mod.Kuaidi100Config(customer="test-customer", key="test-signing-key",
        carrier_codes=options.pop("carrier_codes", {"顺丰快递": "shunfeng", "德邦": "debangkuaidi"}),
        single_process_rate_guard=True, **options)


class ConfigurationAndParserTests(unittest.TestCase):
    def test_explicit_enable_and_no_credentials_required_for_mes(self):
        config = mod.LogisticsConfig.from_env({"MAIMI_LOGISTICS_ENABLED": "true"})
        config.require_ready()
        self.assertEqual(config.availability(), {"enabled": True, "mes_ready": True, "tracking_ready": False, "provider": None})
        self.assertEqual(dict(config.mes_headers), {})
        with self.assertRaisesRegex(mod.LogisticsQueryError, "NOT_CONFIGURED"):
            mod.LogisticsConfig.from_env({}).require_ready()
        with self.assertRaisesRegex(mod.LogisticsQueryError, "INVALID_SERVER_CONFIGURATION"):
            mod.LogisticsConfig.from_env({"MAIMI_LOGISTICS_ENABLED": "yes"})

    def test_provider_guard_and_secret_repr(self):
        with self.assertRaisesRegex(mod.LogisticsQueryError, "INVALID_SERVER_CONFIGURATION"):
            mod.LogisticsQueryService(mod.LogisticsConfig(enabled=True, kuaidi100=mod.Kuaidi100Config("secret-customer", "secret-key")))
        with self.assertRaisesRegex(mod.LogisticsQueryError, "INVALID_SERVER_CONFIGURATION"):
            mod.LogisticsConfig.from_env({"MAIMI_LOGISTICS_ENABLED": "true", "MAIMI_LOGISTICS_KUAIDI100_ENABLED": "true", "MAIMI_LOGISTICS_KUAIDI100_SINGLE_PROCESS": "true", "WEB_CONCURRENCY": "2"})
        self.assertNotIn("test-signing-key", repr(provider()))
        self.assertNotIn("test-customer", repr(provider()))

    def test_invalid_and_extra_arguments(self):
        invalid = [{}, {"order_codes": []}, {"order_codes": ["S"] * 21}, {"order_codes": "S"},
            {"order_codes": ["SAC.1"]}, {"order_codes": ["SAC 1"]}, {"order_codes": ["SＡC1"]},
            {"order_codes": ["SAC\n"]}, {"order_codes": [None]}, {"order_codes": ["SAC1"], "include_tracking": 1},
            {"order_codes": ["SAC1"], "user_no": "other"}, {"order_codes": ["SAC1"], "endpoint": "https://evil.invalid"}]
        for args in invalid:
            with self.subTest(args=args), self.assertRaisesRegex(mod.LogisticsQueryError, "INVALID_ARGUMENTS"):
                mod.validate_arguments(args)
        self.assertEqual(mod.validate_arguments({"order_codes": ["sac_1", "SAC_1", "sdxm-2"]}), (["SAC_1", "SDXM-2"], True))

    def test_snapshot_priority_not_universal_prefixes(self):
        cases = {"ZY974090001": ("ZY974090001", "中通"), "z000000001": (None, "自提"),
            "910012345678": ("910012345678", "思邦"), "810012345678": ("810012345678", "思邦"),
            "850012345678": ("850012345678", "思邦"), "820012345678": ("820012345678", "优速快递"),
            "fedex0000001": ("fedex0000001", "飞帆(国外)"), "XYZ000000001": (None, None),
            "SAC974090001": (None, None), "德邦专车": (None, "德邦"), "成达": (None, "成达")}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.classify_references(raw), [expected])

    def test_grouped_numbers_and_mixed_notes(self):
        self.assertEqual(mod.classify_references("DPK 9921 0598 9650"), [("DPK992105989650", "德邦")])
        self.assertEqual(mod.classify_references("ZY9055 3522 2493"), [("ZY905535222493", "中通")])
        self.assertEqual(mod.classify_references("SF927365110001，成达；DPK920749420002"),
            [("SF927365110001", "顺丰快递"), (None, "成达"), ("DPK920749420002", "德邦")])
        # r46: the user requested extraction of strong waybill prefixes inside
        # Chinese remarks, while retaining the original MES text in results.
        self.assertEqual(mod.classify_references("备注SF927365110001已交司机"), [("SF927365110001", "顺丰快递")])
        self.assertEqual(mod.classify_references("SF927365110001 123456789012"), [(None, "顺丰快递")])
        self.assertEqual(mod.classify_references("顺丰：DPK927365110001"), [(None, None)])


class LookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_payload_dedup_and_default_original(self):
        requests = []
        def handler(request):
            requests.append(request)
            return mes_response("  DPK 9921 0598 9650  ")
        result = await service(handler).query({"order_codes": ["sac_1", "SAC_1"]}, trusted_user_no="MTEST", authorize=lambda: None)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), mod.MES_ENDPOINT)
        self.assertEqual(json.loads(requests[0].content), {"module": "query_sa_record", "user_code": "MTEST", "filters": [{"field": "sa_code", "operator": "eq", "value": "SAC_1"}]})
        row = result["rows"][0]
        self.assertEqual(row["mes_logistics_text"], "  DPK 9921 0598 9650  ")
        self.assertEqual(row["waybill"], "DPK992105989650")
        self.assertEqual(row["tracking_status"], "NOT_CONFIGURED")
        self.assertEqual(row["rule_source"], "MES_WORKBOOK_SNAPSHOT")
        self.assertEqual(result["schema"], "logistics-query-result-v1")

    async def test_empty_no_row_and_failure_are_distinct(self):
        def handler(request):
            order = json.loads(request.content)["filters"][0]["value"]
            payload = {"EMPTY": {"code": 0, "ObjectData": [{"物流单号": ""}]},
                "NONE": {"code": 0, "ObjectData": []}, "BAD": {"code": 2, "message": "SENSITIVE"},
                "SHAPE": {"code": 0, "ObjectData": {"物流单号": "SECRET"}}}[order]
            return httpx.Response(200, json=payload)
        result = await service(handler).query({"order_codes": ["EMPTY", "NONE", "BAD", "SHAPE"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual([r["query_status"] for r in result["rows"]], ["MES_EMPTY", "MES_NO_RECORD", "MES_BUSINESS_REJECTED", "MES_INVALID_RESPONSE"])
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("SENSITIVE", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_names_remain_exact_and_tracking_can_be_disabled(self):
        texts = ["成达", "飞帆", "德邦专车", "客户自提", "公司车", "普通", "未知承运商备注"]
        def handler(request):
            return httpx.Response(200, json={"code": 0, "ObjectData": [{"物流单号": t} for t in texts]})
        result = await service(handler).query({"order_codes": ["SAC1"], "include_tracking": False}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual([r["mes_logistics_text"] for r in result["rows"]], texts)
        self.assertTrue(all(r["waybill"] is None and r["tracking_status"] == "NOT_REQUESTED" for r in result["rows"]))

    async def test_unauthorized_before_request_and_before_exposure(self):
        requests = []
        def handler(request):
            requests.append(request)
            return mes_response()
        lookup = service(handler)
        with self.assertRaisesRegex(mod.LogisticsQueryError, "AUTHORIZATION_LOST"):
            await lookup.query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: False)
        self.assertEqual(len(requests), 0)
        calls = 0
        async def revoke():
            nonlocal calls
            calls += 1
            return calls < 3
        with self.assertRaisesRegex(mod.LogisticsQueryError, "AUTHORIZATION_LOST"):
            await lookup.query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=revoke)
        self.assertEqual(len(requests), 1)

    async def test_response_bound_and_redirects(self):
        for response in [httpx.Response(200, content=b"x" * (mod.MAX_RESPONSE_BYTES + 1)),
                         httpx.Response(302, headers={"location": "https://evil.invalid/secret"})]:
            calls = []
            def handler(request):
                calls.append(request)
                return response
            result = await service(handler).query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
            self.assertEqual(len(calls), 1)
            self.assertIn(result["rows"][0]["query_status"], {"MES_INVALID_RESPONSE", "MES_UNAVAILABLE"})

    async def test_ignores_environment_proxy(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
            result = await service(lambda request: mes_response()).query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
            self.assertEqual(result["rows"][0]["query_status"], "MES_FOUND")

    async def test_total_timeout_cancels_pending_and_semaphore_bounds(self):
        active, peak, finished = 0, 0, 0
        async def handler(request):
            nonlocal active, peak, finished
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.1)
                return mes_response()
            finally:
                active -= 1
                finished += 1
        result = await service(handler, concurrency=2, total_timeout_seconds=0.01).query(
            {"order_codes": ["SAC1", "SAC2", "SAC3"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertEqual(finished, 2)
        self.assertTrue(all(r["query_status"] == "MES_TIMEOUT" for r in result["rows"]))

    async def test_output_and_source_truncation_are_explicit(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "ObjectData": [{"物流单号": "备" * 2100} for _ in range(40)]})
        result = await service(handler).query({"order_codes": ["SAC1", "SAC2"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), mod.MAX_RESULT_BYTES)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode()), 85_000)
        self.assertEqual(result["counts"]["source_records"], 80)
        self.assertEqual(result["truncation"]["omitted_source_records"], 16)
        self.assertGreater(result["truncation"]["omitted_rows"], 0)
        self.assertTrue(result["truncation"]["truncated"])
        self.assertTrue(all(r["waybill"] is None for r in result["rows"]))

    async def test_total_timeout_during_tracking_preserves_all_received_mes_values(self):
        texts = ["DPK927365110001", "成达", "DPK920749420002"]
        async def handler(request):
            if str(request.url) == mod.MES_ENDPOINT:
                return httpx.Response(200, json={"code": 0, "ObjectData": [{"物流单号": t} for t in texts]})
            await asyncio.sleep(0.2)
            raise AssertionError("tracking task should have been cancelled")
        result = await service(handler, kuaidi100=provider(), total_timeout_seconds=0.01).query(
            {"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual([r["mes_logistics_text"] for r in result["rows"]], texts)
        self.assertTrue(all(r["query_status"] == "MES_FOUND" for r in result["rows"]))
        self.assertEqual([r["tracking_status"] for r in result["rows"]], ["TRACKING_TIMEOUT", "NOT_APPLICABLE", "TRACKING_TIMEOUT"])
        self.assertEqual(result["counts"]["source_records"], 3)
        self.assertEqual(result["status"], "partial")

    async def test_http_tracking_timeout_retains_mes_and_marks_tracking_timeout(self):
        def handler(request):
            if str(request.url) == mod.MES_ENDPOINT:
                return mes_response("DPK927365110001")
            raise httpx.ReadTimeout("secret upstream detail", request=request)
        result = await service(handler, kuaidi100=provider()).query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual(result["rows"][0]["mes_logistics_text"], "DPK927365110001")
        self.assertEqual(result["rows"][0]["query_status"], "MES_FOUND")
        self.assertEqual(result["rows"][0]["tracking_status"], "TRACKING_TIMEOUT")
        self.assertNotIn("secret upstream detail", json.dumps(result))

    async def test_phone_required_prevents_any_carrier_request(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return mes_response()
        result = await service(handler, kuaidi100=provider()).query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual(calls, [mod.MES_ENDPOINT])
        self.assertEqual(result["rows"][0]["tracking_status"], "PHONE_REQUIRED")

    async def test_signed_tracking_and_half_hour_cross_user_guard(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if str(request.url) == mod.MES_ENDPOINT:
                return mes_response()
            self.assertEqual(str(request.url), mod.KUAIDI100_ENDPOINT)
            data = parse_qs(request.content.decode())
            params = json.loads(data["param"][0])
            self.assertEqual(params["phone"], "1234")
            self.assertEqual(data["sign"][0], hashlib.md5((data["param"][0] + "test-signing-key" + "test-customer").encode()).hexdigest().upper())
            return httpx.Response(200, json={"status": "200", "state": "3", "com": "shunfeng", "nu": params["num"], "data": [{"context": "测试已签收", "ftime": "2026-01-01 12:00:00"}]})
        lookup = service(handler, kuaidi100=provider(phone_resolver=lambda *args: "1234"))
        result = await lookup.query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual(result["rows"][0]["tracking_status"], "DELIVERED")
        self.assertEqual(result["rows"][0]["latest_event"], "测试已签收")
        second = await lookup.query({"order_codes": ["SAC1"]}, trusted_user_no="MOTHER", authorize=lambda: True)
        self.assertEqual(second["rows"][0]["tracking_status"], "RATE_LIMITED")
        self.assertIsNone(second["rows"][0]["latest_event"])
        self.assertEqual(calls.count(mod.KUAIDI100_ENDPOINT), 1)
        self.assertNotIn("1234", json.dumps(result))

    async def test_carrier_response_identity_mismatch_never_exposes_events(self):
        def handler(request):
            if str(request.url) == mod.MES_ENDPOINT:
                return mes_response("DPK927365110001")
            return httpx.Response(200, json={"status": "200", "state": "3", "com": "debangkuaidi", "nu": "OTHER0000000001", "data": [{"context": "OTHER_USER_SECRET", "ftime": "now"}]})
        result = await service(handler, kuaidi100=provider()).query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual(result["rows"][0]["tracking_status"], "PROVIDER_INVALID_RESPONSE")
        self.assertNotIn("OTHER_USER_SECRET", json.dumps(result))

    async def test_provider_auth_error_body_never_exposes_credentials(self):
        def handler(request):
            if str(request.url) == mod.MES_ENDPOINT:
                return mes_response("DPK927365110001")
            return httpx.Response(200, json={"result": False, "returnCode": "503", "message": "SECRET_CREDENTIALS"})
        result = await service(handler, kuaidi100=provider()).query({"order_codes": ["SAC1"]}, trusted_user_no="MTEST", authorize=lambda: True)
        self.assertEqual(result["rows"][0]["tracking_status"], "PROVIDER_AUTH_ERROR")
        self.assertNotIn("SECRET_CREDENTIALS", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
