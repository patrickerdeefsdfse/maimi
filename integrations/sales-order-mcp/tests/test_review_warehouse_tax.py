from decimal import Decimal
from xml.etree import ElementTree

import pytest

from order_mcp.fastapi_rows import mapped_orders
from order_mcp.rules import export_xml, validate
from order_mcp.schema import Line, Order
from order_mcp.service import apply_business_defaults
from order_mcp.service import OrderService
from order_mcp.config import Settings


@pytest.mark.parametrize(("entity","warehouse"), [
    ("示例制造乙有限公司","101"),
    ("示例制造丙有限公司","141"),
    ("示例制造甲有限公司","381"),
    ("示例制造丁有限公司","961"),
])
def test_warehouse_in_all_order_lines(entity, warehouse):
    order = Order(business_entity=entity, lines=[Line(), Line()])
    xml = ElementTree.fromstring(export_xml(order, validate(order)[0]))
    for tag in ("SHIP_ORG_CODE", "RELEASE_ORG_CODE"):
        assert [node.text for node in xml.findall("G_HEADER/"+tag)] == [warehouse, warehouse]


@pytest.mark.parametrize(("columns","basis","defaulted"), [
    ({"单价": "10"}, "inclusive", True),
    ({}, "inclusive", True),
    ({"不含税单价": "10"}, "exclusive", False),
    ({"单价": "10", "单价口径": "不含税"}, "exclusive", False),
    ({"含税单价": "11.30", "未税单价": "10"}, "inclusive", False),
])
def test_review_basis_provenance(columns, basis, defaulted):
    order, provenance, _ = mapped_orders([columns])[0]
    record = {"order": order.model_dump(mode="json"), "provenance": provenance}
    apply_business_defaults(record)
    assert record["order"]["lines"][0]["price_basis"] == basis
    assert ("lines.0.price_basis" in record["business_defaults"]) == defaulted
    assert not apply_business_defaults(record)

@pytest.mark.parametrize(("columns", "price", "source", "basis"), [
    ({"含税单价": "/", "未税单价": "1,230.50"}, "1230.50", "未税单价", "exclusive"),
    ({"未税单价": "—", "不含税单价": 0, "单价": "100"}, "0", "不含税单价", "exclusive"),
    ({"含税单价": 0, "未税单价": "100"}, "0", "含税单价", "inclusive"),
    ({"含税单价": "12,34", "未税单价": "NaN", "单价": "12.34"}, "12.34", "单价", "unknown"),
    ({"单价": "1,234,567.89"}, "1234567.89", "单价", "unknown"),
])
def test_source_price_uses_first_parseable_column(columns, price, source, basis):
    order, provenance, _ = mapped_orders([columns])[0]
    line = order.lines[0]
    assert line.unit_price == Decimal(price)
    assert line.price_basis == basis
    assert source in provenance["lines.0.unit_price"]
    assert "待核" not in provenance["lines.0.unit_price"]

@pytest.mark.parametrize("raw", ["12,34", "1,2,3", "1.234,56", "Infinity", "约 1,700 至 1,800"])
def test_unresolved_source_price_stays_missing(raw):
    order, provenance, _ = mapped_orders([{"单价": raw}])[0]
    assert order.lines[0].unit_price is None
    assert raw in provenance["lines.0.unit_price"]

def test_manual_pending_tax_decision_is_preserved():
    order = Order(lines=[Line(price_basis="unknown")])
    record = {"order": order.model_dump(mode="json"), "provenance": {"lines.0.price_basis":"人工复核：待核"}}
    apply_business_defaults(record)
    assert record["order"]["lines"][0]["price_basis"] == "unknown"

def test_review_identifies_missing_configured_warehouse_and_recovers(tmp_path):
    service=OrderService(Settings(data_dir=tmp_path,master_data=tmp_path/"master.json"),provider=object())
    class Registry:
        available=False
        def warehouse_exists(self,code):
            assert code=="141"
            return self.available
    service.reference=Registry()
    order=Order(business_entity="示例制造丙有限公司",lines=[Line()])
    draft=service.store.create({"kind":"order","status":"review","order":order.model_dump(mode="json"),
                                "reference_reviewed":True,"reference_rules":2,"warnings":[],"provenance":{}})
    missing=service.review(draft["id"])
    assert any(i["code"]=="warehouse_missing" and "141" in i["message"] for i in missing["issues"])
    service.reference.available=True
    assert all(i["code"]!="warehouse_missing" for i in service.review(draft["id"])["issues"])

@pytest.mark.asyncio
async def test_cancelled_receipt_stays_read_only(tmp_path):
    service=OrderService(Settings(data_dir=tmp_path,master_data=tmp_path/"master.json"),provider=object())
    record=service.store.create({"kind":"order","status":"cancelled","oracle_result":{"order_number":"QA012","batch_number":"18"}})
    receipt=await service.confirm_order(record["id"],record["revision"],{})
    assert receipt["status"]=="cancelled" and receipt["order_number"]=="QA012"
    assert (await service.query_submission(record["id"]))==receipt
