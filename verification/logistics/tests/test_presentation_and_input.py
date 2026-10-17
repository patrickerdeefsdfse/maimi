"""r48 user-input and fixed-table tests; all data and providers are synthetic."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("r48_display_test")
package.__path__ = [str(ROOT / "backend/app")]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".logistics_query", ROOT / "backend/app/logistics_query.py")
q = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = q
spec.loader.exec_module(q)


def result(rows, **truncation):
    return {"rows": rows, "truncation": {"omitted_rows": 0, "omitted_source_records": 0, **truncation}}


def row(order="SDXM9243300004", **extra):
    return {"order_code": order, "waybill": "SF927365110001", "carrier": "顺丰快递",
            "query_status": "MES_FOUND", "tracking_status": "DELIVERED", "mes_logistics_text": "顺丰SF927365110001",
            "latest_event": "【合成城市】快件已由同事签收", "event_time": "2026-10-09 19:20:25", **extra}


class InputTests(unittest.TestCase):
    def test_chinese_noisy_multiple_fullwidth_and_order_preservation(self):
        text = "麻烦查销售单：ｓｄｘｍ９２４３３００００４。 日期2026-10-12\n第二单：SAC96655820057；备注很急。重复sdxm9243300004"
        self.assertEqual(q.extract_order_codes(text), ["SDXM9243300004", "SAC96655820057"])

    def test_all_demonstrated_prefixes(self):
        codes = ["SACH9170720044", "SAC96655820057", "SAQHP952027008", "SDXM9243300004", "SDXT9312530007", "SAC97040150018"]
        self.assertEqual(q.extract_order_codes("查：" + "，".join(codes)), codes)

    def test_noise_phone_waybill_and_embedded_long_token_never_become_order(self):
        noise = ["800000000001", "SF927365110001", "DPK927365110001", "13900000000", "ABCSDXM3826100004",
                 "SDXM3826100004XYZ", "SDXM9243300004_foo", "x-SDXM9243300004", "SA915426789", "SAC123", "SAC99754018043444567"]
        for value in noise:
            with self.subTest(value=value), self.assertRaisesRegex(q.LogisticsQueryError, "NO_ORDER_CODES"):
                q.extract_order_codes(value)
        self.assertEqual(q.extract_order_codes("噪音：" + "；".join(noise) + "；单号SAC96655820057查下"), ["SAC96655820057"])

    def test_twenty_unique_limit_and_duplicates_before_limit(self):
        twenty = [f"SDXM382610{i:04d}" for i in range(20)]
        self.assertEqual(q.extract_order_codes(" ".join(twenty + twenty)), twenty)
        with self.assertRaisesRegex(q.LogisticsQueryError, "ORDER_LIMIT_EXCEEDED"):
            q.extract_order_codes(" ".join(twenty + ["SDXM9485910020"]))

    def test_input_modes_exclusive_and_input_length_bound(self):
        for value in ({"order_codes": ["SDXM9243300004"], "query_text": "SDXM9243300004"},
                      {"query_text": "x" * 16001}, {"query_text": "SDXM9243300004\x00"},
                      {"query_text": "SDXM9243300004", "include_tracking": "true"}):
            with self.subTest(value=str(value)[:50]), self.assertRaisesRegex(q.LogisticsQueryError, "INVALID_ARGUMENTS"):
                q.validate_arguments(value)
        self.assertEqual(q.validate_arguments({"query_text": "查SDXM9243300004", "order_codes": None}), (["SDXM9243300004"], True))

    def test_explicit_array_rejects_numbers_and_known_carrier_waybills(self):
        for code in ("13900000000", "800000000001", "SF927365110001", "DPK927365110001"):
            with self.subTest(code=code), self.assertRaisesRegex(q.LogisticsQueryError, "NO_ORDER_CODES"):
                q.validate_arguments({"order_codes": [code]})

    def test_mcp_signature_time_comes_from_verified_event_status(self):
        target = row()
        q._apply_mcp_trace(target, {"structuredContent": {"kuaidiNum": target["waybill"], "kuaidiCom": "shunfeng", "kuaidiName": "顺丰", "state": "已签收", "fromTo": "合成路线", "data": [
            {"time": "2026-10-09 19:20:00", "status": "签收", "context": "合成签收事件"},
            {"time": "2026-10-09 19:25:00", "status": "在途", "context": "合成补充服务说明"}]}})
        self.assertEqual(target["signed_event_time"], "2026-10-09 19:20:00")
        self.assertEqual(target["latest_event"], "合成补充服务说明")
        self.assertIn("已签收&#10;&#10;签收时间：2026-10-09 19:20&#10;&#10;合成补充服务说明", q.render_logistics_table(result([target]), [target["order_code"]]))


class PresentationTests(unittest.TestCase):
    def test_single_row_four_columns_fixed_status_timestamp_no_diagnostics(self):
        table = q.render_logistics_table(result([row()]), ["SDXM9243300004"])
        self.assertEqual(len(table.splitlines()), 3)
        self.assertIn("已签收&#10;&#10;签收时间：暂未提供&#10;&#10;【合成城市】快件已由同事签收", table)
        for forbidden in ("MES", "DELIVERED", "MES_FOUND", "说明", "查询时间", "queried_at", "<br>"):
            self.assertNotIn(forbidden, table)

    def test_provider_status_wins_over_signed_words_in_context(self):
        table = q.render_logistics_table(result([row(tracking_status="IN_TRANSIT")]), ["SDXM9243300004"])
        self.assertIn("运输中&#10;&#10;【合成城市】快件已由同事签收", table)
        self.assertNotIn("预计送达：", table)
        self.assertNotIn("已签收--", table)

    def test_signed_event_time_is_used_when_verified(self):
        table = q.render_logistics_table(result([row(signed_event_time="2026-10-09T10:00:00Z")]), ["SDXM9243300004"])
        self.assertIn("已签收&#10;&#10;签收时间：2026-10-09 18:00&#10;&#10;", table)

    def test_missing_or_invalid_eta_is_hidden_for_all_active_states_and_outcomes(self):
        for state, label in (("IN_TRANSIT", "运输中"), ("COLLECTED", "已揽收"), ("OUT_FOR_DELIVERY", "派送中")):
            for outcome in (None, "SOURCE_INCOMPLETE_OR_UNSUPPORTED", "PROVIDER_UNAVAILABLE", "RESPONSE_UNVERIFIED", "BUDGET_EXCEEDED"):
                for eta in (None, "", "not-a-time"):
                    with self.subTest(state=state, outcome=outcome, eta=eta):
                        value = row(tracking_status=state, eta_status=outcome, estimated_arrival_time=eta, latest_event="合成有效动态")
                        table = q.render_logistics_table(result([value]), [value["order_code"]])
                        cell = table.splitlines()[2].split(" | ")[-1].removesuffix(" |")
                        self.assertEqual(cell, label + "&#10;&#10;合成有效动态")
                        self.assertNotIn("预计送达", table)
                        self.assertNotIn("暂未提供", table)

    def test_verified_eta_keeps_three_parts_and_other_time_branches_unchanged(self):
        examples = [
            row(tracking_status="IN_TRANSIT", estimated_arrival_time="2026-10-10 17:30:00", latest_event="合成有效动态"),
            row(signed_event_time="2026-10-09 19:20:00", latest_event="合成有效动态"),
            row(tracking_status="AT_PICKUP_POINT", latest_event="合成有效动态"),
            row(tracking_status="RETURN_SIGNED", latest_event="合成有效动态"),
        ]
        times = ["预计送达：2026-10-10 17:30", "签收时间：2026-10-09 19:20", "动态时间：2026-10-09 19:20", "动态时间：2026-10-09 19:20"]
        for value, expected in zip(examples, times):
            with self.subTest(state=value["tracking_status"]):
                table = q.render_logistics_table(result([value]), [value["order_code"]])
                self.assertEqual(table.count("&#10;&#10;"), 2)
                self.assertIn("&#10;&#10;" + expected + "&#10;&#10;合成有效动态", table)

    def test_multi_parcels_stay_paired_in_one_order_row(self):
        table = q.render_logistics_table(result([row(), row(waybill="DPK920749420002", carrier="德邦", tracking_status="IN_TRANSIT")]), ["SDXM9243300004"])
        self.assertEqual(len(table.splitlines()), 3)
        self.assertIn("1. SF927365110001；2. DPK920749420002", table)
        self.assertIn("1. 顺丰快递；2. 德邦", table)
        self.assertIn("1. 已签收&#10;", table)
        self.assertIn("2. 运输中&#10;", table)

    def test_phone_missing_and_unknown_time_do_not_reuse_stale_dynamic(self):
        for value in (row(tracking_status="PHONE_REQUIRED"), row(tracking_time_unverified=True), row(event_time="not-a-time")):
            table = q.render_logistics_table(result([value]), ["SDXM9243300004"])
            self.assertNotIn("合成城市", table)
            self.assertNotIn("--", table.split("\n")[-1])
            self.assertNotIn("2026-10-09 19:20", table)
            self.assertNotIn("PHONE_REQUIRED", table)

    def test_original_text_is_retained_only_when_no_waybill(self):
        table = q.render_logistics_table(result([row(waybill=None, carrier="成达", mes_logistics_text="成达专车", tracking_status="NOT_APPLICABLE")]), ["SDXM9243300004"])
        self.assertIn("成达专车", table)
        self.assertIn("暂无可查询运单", table)

    def test_escaping_normalization_without_html_or_new_table_rows(self):
        dynamic = "到达\r\n合成网点<br />已  签收 | [恶意](https://example.invalid) <img src=x> &copy; *备注*"
        table = q.render_logistics_table(result([row(latest_event=dynamic)]), ["SDXM9243300004"])
        self.assertEqual(len(table.splitlines()), 3)
        self.assertIn("到达 合成网点 已 签收 \\| \\[恶意\\]", table)
        self.assertIn("&lt;img", table)
        self.assertNotIn("<br", table)
        self.assertIn("©", table)

    def test_every_requested_order_remains_visible_when_result_is_truncated(self):
        table = q.render_logistics_table(result([row()], omitted_rows=1), ["SDXM9243300004", "SAC96655820057"])
        self.assertEqual(len(table.splitlines()), 4)
        self.assertIn("SAC96655820057", table)
        self.assertIn("结果未完整返回&#10;&#10;请分批查询", table)
        self.assertIn("部分记录未返回", table)


class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_input_drives_each_mes_order_once_and_returns_complete_display(self):
        calls = []
        async def transport(request):
            body = json.loads(request.content)
            calls.append(body["filters"][0]["value"])
            return httpx.Response(200, json={"code": 0, "ObjectData": [{"物流单号": "汇森800000000001一箱良品"}]})
        service = q.LogisticsQueryService(q.LogisticsConfig(enabled=True), transport=httpx.MockTransport(transport))
        out = await service.query({"query_text": "查 SDXM9243300004, SAC96655820057\n重复sdxm9243300004；电话13900000000"}, trusted_user_no="SYNTHETIC", authorize=lambda: True)
        self.assertEqual(calls, ["SDXM9243300004", "SAC96655820057"])
        self.assertEqual(out["counts"]["queried_orders"], 2)
        self.assertEqual(out["display_schema"], "logistics-table-v1")
        self.assertEqual(len(out["display_markdown"].splitlines()), 4)
        self.assertNotIn("13900000000", json.dumps(out))

    async def test_excess_orders_make_no_business_calls(self):
        calls = []
        service = q.LogisticsQueryService(q.LogisticsConfig(enabled=True), transport=httpx.MockTransport(lambda r: calls.append(r)))
        with self.assertRaisesRegex(q.LogisticsQueryError, "ORDER_LIMIT_EXCEEDED"):
            await service.query({"query_text": " ".join(f"SDXM382610{i:04d}" for i in range(21))}, trusted_user_no="SYNTHETIC", authorize=lambda: True)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
