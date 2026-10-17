"""Synthetic regression cases for per-record mobile suffixes and MES remarks.

All HTTP calls use the existing mock MES/MCP transport, never company data.
"""
import asyncio
import json
import unittest
from urllib.parse import parse_qs

import httpx

from test_logistics_mcp_integration import q, Transport, service, query


def records(*items):
    return {"code": 0, "ObjectData": list(items)}


def row(reference, phone=None, *, absent=False):
    value = {"物流单号": reference}
    if not absent:
        value[q.MES_PHONE_FIELD] = phone
    return value


class SuffixTests(unittest.TestCase):
    def test_normal_virtual_country_code_and_leading_zero(self):
        for raw, expected in [
            (None, None), ("", None), (" \t ", None), ("0012", "0012"),
            ("13900000012", "0012"), ("+8613900000012", "0012"),
            ("0086 139 0000 0012", "0012"), ("86-139-0000-0012", "0012"),
            ("139-0000-0012", "0012"), ("13900000012-0003", "0003"),
            ("+86 139 0000 0012 - 0004", "0004"),
        ]:
            with self.subTest(shape=type(raw).__name__, length=len(raw or "")):
                self.assertEqual(q.mes_phone_suffix(raw), expected)

    def test_invalid_does_not_salvage_digits(self):
        for raw in [13900000012, False, [], {}, "139****0012", "手机号13900000012",
                    "13900000012/13900000012", "13900000012 13900000012",
                    "13900000012-12", "+1 13900000012", "01234567890",
                    "１3900000012", "139\n00000012", "9" * 65,
                    "00012", "13900000012123", "++8613900000012"]:
            with self.subTest(shape=type(raw).__name__), self.assertRaisesRegex(q.LogisticsQueryError, "^PHONE_INVALID$"):
                q.mes_phone_suffix(raw)

    def test_secret_hint_repr_does_not_include_suffix(self):
        hint = q._record_phone(row("SF927365110001", "13900009187"))
        self.assertNotIn("9187", repr(hint))


class InlineReferenceTests(unittest.TestCase):
    def test_exact_huisen_label_with_remarks_and_numeric_prefix_override(self):
        for text in ["汇森800000091873", "汇森：800000091873", "汇森 800000091873",
                     "汇森速运800000091873", "备注汇森800000091873已发货", "汇森800000091873一箱良品"]:
            with self.subTest(text=text):
                self.assertEqual(q.classify_references(text), [("800000091873", "汇森速运")])
        self.assertEqual(q.classify_references("汇森830000091874一箱良品"), [("830000091874", "汇森速运")])
        self.assertEqual(q.classify_references("汇森运单号：830000091874一箱良品"), [("830000091874", "汇森速运")])
        self.assertEqual(q.classify_references("汇森物流单号830000091874一箱良品"), [("830000091874", "汇森速运")])
        self.assertEqual(q.classify_references("汇森单号 830000091874一箱良品"), [("830000091874", "汇森速运")])
        self.assertEqual(q.classify_references("830000091874"), [("830000091874", "优速快递")])

    def test_multiple_chinese_labels_and_strong_tokens_are_separate(self):
        self.assertEqual(q.classify_references("汇森800000091873一箱，备注SF942561121871已交司机；跨越KY941030171875两件"),
            [("800000091873", "汇森速运"), ("SF942561121871", "顺丰快递"), ("KY941030171875", "跨越")])
        self.assertEqual(q.classify_references("汇森800000091873一箱德邦DPK962001891876两箱"),
            [("800000091873", "汇森速运"), ("DPK962001891876", "德邦")])

    def test_explicit_conflicting_strong_prefix_does_not_retry_other_carrier(self):
        for text in ["汇森SF942561121871", "汇森：DPK962001891876已交司机",
                     "顺丰KY941030171875", "顺丰：DPK962001891876", "汇森SDXM962001891876"]:
            with self.subTest(text=text):
                self.assertEqual(q.classify_references(text), [(None, None)])

    def test_no_arbitrary_numeric_or_phone_or_quantity_extraction(self):
        for text in ["备注800000091873已发货", "汇森联系电话13900009187",
                     "汇森1箱良品", "汇森800 000 091873", "未知承运商800000091873",
                     "汇森8000000918739ABCDMORETHANTHIRTYTWODIGITS", "SF942561121871 123456789012"]:
            with self.subTest(text=text):
                self.assertTrue(all(number is None for number, _ in q.classify_references(text)))
        self.assertEqual(q.classify_references("SF942561121871一箱联系电话13900009187"), [("SF942561121871", "顺丰快递")])
        self.assertEqual(q.classify_references("备注DHL000000091877已交司机"), [("DHL000000091877", "DHL")])
        self.assertTrue(all(number is None for number, _ in q.classify_references("备注FEDEX" + "1" * 29 + "已交司机")))

    def test_whole_value_grouping_and_reference_limit_preserved(self):
        self.assertEqual(q.classify_references("DPK 9620 0189 1876"), [("DPK962001891876", "德邦")])
        self.assertEqual(q.classify_references("一箱".join("汇森" + str(800000091800 + n) for n in range(9))), [(None, None)])


class MESPhoneIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_sf_suffix_only_and_no_phone_to_unneeded_carrier(self):
        transport = Transport({"SAC1": records(
            row("顺丰SF942561121871", "+86 139 0000 9187"),
            row("汇森800000091873一箱良品", "13900008371"))})
        value = await query(service(transport))
        self.assertEqual([r["tracking_status"] for r in value["rows"]], ["DELIVERED", "DELIVERED"])
        self.assertEqual(transport.trace_arguments, [
            {"kuaidi_num": "SF942561121871", "phone": "9187"},
            {"kuaidi_num": "800000091873", "phone": ""},
        ])
        rendered = json.dumps(value, ensure_ascii=False)
        for secret in [q.MES_PHONE_FIELD, "13900009187", "139 0000 9187", "13900008371", '"phone"', '"9187"']:
            self.assertNotIn(secret, rendered)
        self.assertEqual(value["rows"][1]["mes_logistics_text"], "汇森800000091873一箱良品")
        self.assertEqual(value["rows"][1]["carrier"], "汇森速运")
        self.assertEqual(value["rows"][1]["rule_source"], "MES_EXPLICIT_CARRIER_LABEL")

    async def test_same_record_multi_reference_and_no_empty_or_invalid_fallback(self):
        calls = []
        def fallback(*args):
            calls.append(args)
            return "7777"
        transport = Transport({"SAC1": records(
            row("SF942561121871;SF900166871872", "13900000001"),
            row("SF946233151873", ""), row("SF960194751874", None),
            row("SF941030171875", "139****0001"), row("SF962001891876", absent=True))})
        value = await query(service(transport, phone_resolver=fallback))
        self.assertEqual([r["tracking_status"] for r in value["rows"]],
                         ["DELIVERED", "DELIVERED", "PHONE_REQUIRED", "PHONE_REQUIRED", "PHONE_INVALID", "DELIVERED"])
        self.assertEqual([item["phone"] for item in transport.trace_arguments], ["0001", "0001", "7777"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "SF962001891876")

    async def test_concurrent_orders_do_not_share_record_phone(self):
        transport = Transport({
            "SAC1": records(row("SF942561121871", "13900000001"), row("SF900166871872", "13900000002")),
            "SAC2": records(row("SF946233151873", "13900000003"), row("SF960194751874", "")),
        })
        value = await query(service(transport), ["SAC1", "SAC2"])
        actual = {a["kuaidi_num"]: a["phone"] for a in transport.trace_arguments}
        self.assertEqual(actual, {"SF942561121871": "0001", "SF900166871872": "0002", "SF946233151873": "0003"})
        self.assertEqual(value["rows"][-1]["tracking_status"], "PHONE_REQUIRED")

    async def test_duplicate_waybill_different_phone_not_overwritten_or_rate_bypassed(self):
        transport = Transport({"SAC1": records(row("SF942561121871", "13900000001"), row("SF942561121871", "13900000002"))})
        value = await query(service(transport))
        self.assertEqual(transport.trace_arguments, [{"kuaidi_num": "SF942561121871", "phone": "0001"}])
        self.assertEqual([r["tracking_status"] for r in value["rows"]], ["DELIVERED", "RATE_LIMITED"])

    async def test_invalid_phone_does_not_block_carrier_without_phone_requirement(self):
        transport = Transport({"SAC1": records(row("SF942561121871", 13900000001), row("汇森800000091873", 13900000001))})
        value = await query(service(transport))
        self.assertEqual([r["tracking_status"] for r in value["rows"]], ["PHONE_INVALID", "DELIVERED"])
        self.assertEqual(transport.trace_arguments, [{"kuaidi_num": "800000091873", "phone": ""}])

    async def test_provider_error_text_and_extra_fields_never_expose_mes_phone(self):
        transport = Transport({"SAC1": records(row("SF942561121871", "13900009187"))})
        def alter(method, result):
            if method == "tools/call":
                result["structuredContent"] = {"message": "Synthetic rejection 13900009187 / 9187"}
            return result
        transport.alter_result = alter
        value = await query(service(transport))
        self.assertEqual(value["rows"][0]["tracking_status"], "PROVIDER_UNAVAILABLE")
        self.assertNotIn("13900009187", json.dumps(value))
        self.assertNotIn('"9187"', json.dumps(value))

    async def test_timeout_progress_has_no_phone_data_and_preserves_all_mes_rows(self):
        transport = Transport({"SAC1": records(row("SF942561121871", "13900009187"), row("SF900166871872", "13900005678"))})
        async def slow(body):
            if body["method"] == "tools/call":
                transport.call_started.set()
                await asyncio.sleep(10)
        transport.on_rpc = slow
        value = await query(service(transport, total_timeout_seconds=0.06))
        self.assertTrue(transport.call_started.is_set())
        self.assertEqual(len(value["rows"]), 2)
        self.assertTrue(all(r["tracking_status"] == "TRACKING_TIMEOUT" for r in value["rows"]))
        for secret in [q.MES_PHONE_FIELD, "13900009187", "13900005678", '"9187"', '"5678"']:
            self.assertNotIn(secret, json.dumps(value))

    async def test_poll_adapter_uses_same_mes_suffix(self):
        posted = []
        def handler(request):
            if str(request.url) == q.MES_ENDPOINT:
                return httpx.Response(200, json=records(row("SF942561121871", "13900000001")))
            params = json.loads(parse_qs(request.content.decode())["param"][0])
            posted.append(params)
            return httpx.Response(200, json={"status": "200", "nu": params["num"], "com": "shunfeng", "state": "3", "data": []})
        config = q.LogisticsConfig(enabled=True, kuaidi100=q.Kuaidi100Config(
            "SYNTHETIC_CUSTOMER", "SYNTHETIC_KEY", {"顺丰快递": "shunfeng"}, single_process_rate_guard=True))
        value = await query(q.LogisticsQueryService(config, transport=httpx.MockTransport(handler)))
        self.assertEqual(posted[0]["phone"], "0001")
        self.assertEqual(value["rows"][0]["tracking_status"], "NO_TRACKING")


if __name__ == "__main__":
    unittest.main()
