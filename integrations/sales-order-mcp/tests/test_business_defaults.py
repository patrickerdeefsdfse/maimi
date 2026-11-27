from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from order_mcp.config import Settings
from order_mcp.schema import Line, Order
from order_mcp.service import OrderService, apply_business_defaults


def draft(kind="8001_内销-出货订单", currency=None):
    order = Order(business_entity="示例制造乙有限公司",
                  order_type=kind, currency=currency,
                  lines=[Line(request_date=None), Line(request_date="2026-12-30")])
    return {"kind": "order", "status": "review", "warnings": [], "provenance": {},
            "order": order.model_dump(mode="json")}


def test_domestic_rules_and_shanghai_date(monkeypatch):
    class Instant(datetime):
        @classmethod
        def now(cls, tz):
            assert isinstance(tz, ZoneInfo)
            return datetime(2026, 11, 18, 16, 5, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("order_mcp.service.datetime", Instant)
    r = draft(currency="USD")
    assert apply_business_defaults(r)
    assert r["order"]["currency"] == "CNY"
    assert r["business_defaults"]["currency"]["previous"] == "USD"
    assert r["order"]["lines"][0]["request_date"] == "2026-11-19"
    assert r["order"]["lines"][1]["request_date"] == "2026-12-30"
    assert not apply_business_defaults(r)
    r["order"]["order_type"] = "8920_外销-出货订单"
    assert apply_business_defaults(r)
    assert r["order"]["currency"] == "USD"
    assert "currency" not in r["business_defaults"]


def test_unclassified_foreign_currency_and_unreadable_date_are_preserved():
    for kind in ("8920_外销-出货订单", None):
        r = draft(kind=kind, currency="USD")
        r["order"]["lines"][0]["request_date"] = "待核原文"
        assert apply_business_defaults(r)
        assert set(r["business_defaults"])=={"lines.0.price_basis","lines.1.price_basis"}
        assert not apply_business_defaults(r)
        assert r["order"]["currency"] == "USD"
        assert r["order"]["lines"][0]["request_date"] == "待核原文"


def test_legacy_preview_is_invalidated_once_and_manual_date_survives(tmp_path):
    svc = OrderService(Settings(data_dir=tmp_path, master_data=tmp_path/"master.json"), provider=object())
    r = svc.store.create(dict(draft(), status="prepared", confirmation={"hash":"old"}))
    first = svc.review(r["id"])
    assert first["revision"] == 2 and first["status"] == "review"
    assert "confirmation" not in svc.store.get(r["id"])
    assert svc.review(r["id"])["revision"] == 2
    edited = svc.revise(r["id"], 2, {"lines.0.request_date":"2026-12-25"}, "经办人按原件确认")
    assert edited["order"]["lines"][0]["request_date"] == "2026-12-25"
    assert "lines.0.request_date" not in edited["business_defaults"]
    assert "currency" in edited["business_defaults"]
