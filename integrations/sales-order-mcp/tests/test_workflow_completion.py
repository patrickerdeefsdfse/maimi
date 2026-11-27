from dataclasses import replace
import json

import pytest

from order_mcp.config import Settings
from order_mcp.oracle import OracleGateway
from test_orders import record, service


async def test_completed_empty_recognition_keeps_a_blank_editable_draft(tmp_path):
    from unittest.mock import AsyncMock
    workflow = service(tmp_path)
    workflow.provider = AsyncMock()
    workflow.provider.extract.return_value = {"rows":[]}
    source = tmp_path / "original.png"
    source.write_bytes(b"qa")
    result = await workflow.recognize(source,source.name,"M90000001","示例员甲")
    draft, = result["orders"]
    assert not draft["can_prepare"] and "workbook" not in result
    assert len(draft["order"]["lines"]) == 1 and draft["order"]["lines"][0]["item_code"] is None
    assert any("空白草稿" in msg for msg in draft["warnings"])
    with pytest.raises(ValueError):
        workflow.revise(draft["id"],draft["revision"],{"line_layout":[0,0]},"测试",human=True)
    stored = workflow.store.get(draft["id"])
    assert stored["revision"]==draft["revision"] and stored["original_path"]==str(source)


def test_line_layout_keeps_evidence_and_default_on_matching_original_rows():
    from order_mcp.service import revise_line_layout
    record = {"order":{"lines":[{"item_code":"original0"},{"item_code":"original1"}],
                       "evidence":{"lines.0.quantity":{"confidence":0.2},"lines.1.quantity":{"confidence":0.7}}},
              "sources":[5,8],"provenance":{"lines.0.item_code":"drop","lines.1.item_code":"keep","currency":"header"},
              "business_defaults":{"lines.1.request_date":{"value":"2026-11-18"}}}
    revise_line_layout(record,[None, None, 1])
    assert record["order"]["evidence"]=={"lines.2.quantity":{"confidence":0.7}}
    assert record["provenance"]=={"lines.2.item_code":"keep","currency":"header"}
    assert set(record["business_defaults"])=={"lines.2.request_date"}
    assert record["order"]["lines"][2]["item_code"]=="original1" and record["sources"]==[None,None,8]


async def test_lookup_returns_candidates_without_changing_order(tmp_path):
    workflow = service(tmp_path)
    original = record(workflow, with_price=False)
    workflow.oracle.customer = lambda name: [{"name": name, "account_number": "910001"}]
    workflow.oracle.price = lambda *args: {"price": "19.23", "price_basis": "unknown", "booked_date": "2026-10-01"}
    customer = await workflow.lookup_customer(original["id"])
    price = await workflow.lookup_price(original["id"], 0)
    assert customer["requires_confirmation"] and customer["candidates"][0]["account_number"] == "910001"
    assert price["candidate"]["price_basis"] == "unknown" and price["candidate"]["source"] == "oracle_history"
    assert workflow.store.get(original["id"]) == original
    assert workflow.oracle.calls == 0


async def test_missing_history_explains_unconfigured_price_list(tmp_path):
    workflow = service(tmp_path)
    workflow.settings = replace(workflow.settings, integration_config=tmp_path / "no-integration")
    original = record(workflow, with_price=False)
    workflow.oracle.price = lambda *args: None
    result = await workflow.lookup_price(original["id"], 0)
    assert result["candidate"] is None and "提交人" in result["message"]
    for line in (-1, True, 1, "0"):
        with pytest.raises(ValueError, match="订单行编号"):
            await workflow.lookup_price(original["id"], line)


async def test_customer_price_list_is_only_used_after_no_history(tmp_path, monkeypatch):
    from order_mcp.integrations import CompanyApis
    config = tmp_path / "integrations.json"
    config.write_text(json.dumps({"price_list": {"enabled": True}}))
    workflow = service(tmp_path)
    workflow.settings = replace(workflow.settings, integration_config=config)
    original = record(workflow, with_price=False)
    calls = []
    workflow.oracle.price = lambda *args: None

    async def price_list(self, name, values):
        calls.append((name, values))
        return {"price": "19.23", "price_basis": "inclusive", "price_list_name": "示例员甲价目表"}

    monkeypatch.setattr(CompanyApis, "request", price_list)
    result = await workflow.lookup_price(original["id"], 0)
    assert calls == [("price_list", {"account_number": "910001", "item_code": "R0001", "business_entity": "示例制造甲有限公司"})]
    assert result["candidate"]["source"] == "customer_price_list"
    assert workflow.store.get(original["id"]) == original


def test_internal_po_is_revision_bound_and_preserves_existing_po(tmp_path):
    workflow = service(tmp_path)
    original = record(workflow)
    with pytest.raises(ValueError, match="不可覆盖"):
        workflow.generate_po(original["id"], 1)
    workflow.revise(original["id"], 1, {"customer_po": None}, "原件无 PO")
    with pytest.raises(ValueError, match="版本已变化"):
        workflow.generate_po(original["id"], 1)
    updated = workflow.generate_po(original["id"], 2)
    assert updated["revision"] == 3 and updated["status"] == "review"
    assert "内部代号" in updated["provenance"]["customer_po"]


def test_expected_oracle_target_is_controlled_by_runtime_configuration():
    expected = "192.0.2.45:1541/EXAMPLE_TEST"
    descriptor = "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.45)(PORT=1541))(CONNECT_DATA=(SERVICE_NAME=EXAMPLE_TEST)))"
    settings = Settings(dsn=descriptor, oracle_expected_dsn=expected, oracle_environment="test",
                        db_user="synthetic", db_password="synthetic", enable_write=True)
    status = OracleGateway(settings).target_status()
    assert status["target_matches"] and status["write_ready"] and status["environment"] == "test"
    assert status["expected_target"] == {"host": "192.0.2.45", "port": 1541, "service_name": "EXAMPLE_TEST"}
    assert "synthetic" not in repr(status)
    gateway = OracleGateway(replace(settings, dsn="192.0.2.45:1541/other_service"))
    assert not gateway.target_status()["write_ready"]
    with pytest.raises(ValueError, match="环境或目标尚未核对"):
        gateway.connect()
    assert not OracleGateway(replace(settings, oracle_environment="unconfigured")).target_status()["write_ready"]


def test_thick_is_explicit_and_initializes_once_after_target_check(monkeypatch):
    import oracledb
    calls = []
    mode = {"thin": True}
    monkeypatch.setattr(oracledb, "is_thin_mode", lambda: mode["thin"])
    def initialize():
        calls.append("initialize")
        mode["thin"] = False
    monkeypatch.setattr(oracledb, "init_oracle_client", initialize)
    class Connection:
        call_timeout = 0
    monkeypatch.setattr(oracledb, "connect", lambda **kwargs: calls.append("connect") or Connection())
    settings = Settings(dsn="test-db.example:1541/EXAMPLE_TEST", oracle_expected_dsn="test-db.example:1541/EXAMPLE_TEST",
        oracle_environment="test", oracle_driver_mode="thick", db_user="synthetic", db_password="synthetic")
    gateway = OracleGateway(settings)
    gateway.connect()
    gateway.connect()
    assert calls == ["initialize", "connect", "connect"]
    with pytest.raises(ValueError, match="环境或目标"):
        OracleGateway(replace(settings, dsn="other-db.example:1541/EXAMPLE_TEST")).connect()
    assert calls == ["initialize", "connect", "connect"]
    with pytest.raises(ValueError, match="驱动模式"):
        OracleGateway(replace(settings, oracle_driver_mode="thin")).connect()
