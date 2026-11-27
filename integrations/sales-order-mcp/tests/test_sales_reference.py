from datetime import date
from types import SimpleNamespace

import pytest

from order_mcp.sales_reference import SalesReference, rerank_customers


class Fixture(SalesReference):
    def __init__(self,rows=(),basis="unknown",ranker=None):
        super().__init__(SimpleNamespace(settings=SimpleNamespace(history_price_basis=basis)),ranker=ranker)
        self.rows=rows
        self.calls=[]

    def _read(self,sql,binds):
        self.calls.append((sql,binds))
        return self.rows


def test_exact_customer_name_first_including_duplicate_accounts():
    ref=Fixture([("3","样例买方丁有限公司"),("2","样例买方丙有限公司"),("1","样例买方丙有限公司")])
    assert [r["account_number"] for r in ref.customers("样例买方丙有限公司")]==["1","2","3"]
    assert ":full_name" in ref.calls[0][0] and "甲乙" not in ref.calls[0][0]


def test_shortlist_protects_exact_match_and_only_accepts_existing_indices():
    rows=[dict(name="甲乙电子",account_number="1"),dict(name="甲乙电器",account_number="2")]
    def rank(query, choices):
        assert all(set(c)=={"index","name"} for c in choices)
        return [1,0]
    assert rerank_customers("甲乙电子",rows,rank)==rows
    assert rerank_customers("甲乙电",rows,rank)==list(reversed(rows))
    for value in ([1,1],[-1,0],["0",1],[True,0],{"new_account":"3"}):
        assert rerank_customers("甲乙电",rows,lambda *_:value)==rows


def test_sparse_or_invalid_search_does_not_read_full_directory():
    ref=Fixture()
    for query in ("","!","甲",None):
        assert ref.customers(query)==[]
    assert ref.calls==[]
    ref.customers("测试企业",account_number="A02")
    assert "s.account_number=:account" in ref.calls[0][0]
    assert ref.calls[0][1]["account"]=="A02"
    ref.customers("",account_number="A02")
    assert ref.calls[-1][1]["account"]=="A02"


def test_mapping_scoped_to_exact_customer_and_item_and_master_org():
    ref=Fixture([("X123","A00019","PCS")])
    assert ref.items("","A00019","test")==[]
    assert ref.items("A","A00019","test")==[{"item_code":"X123","customer_item":"A00019","unit":"PCS"}]
    sql,b=ref.calls[0]
    assert "msi.organization_id=82" in sql and "mci.customer_item_number=:customer_item" in sql
    assert b["account"]=="A" and b["customer_item"]=="A00019"


def test_history_uses_pickup_date_and_matching_basis_currency_entity_item_account():
    ref=Fixture([("3.25",date(2025,1,1),date(2025,2,1),"PCS",date(2025,2,1))],"exclusive")
    scopes=("A","示例员甲客户","测试物料","示例员甲主体","RMB")
    assert ref.price(*scopes,"inclusive") is None
    assert ref.calls==[]
    result=ref.price(*scopes,"exclusive",unit="pcs")
    assert (result["currency"],result["price_basis"],result["latest_pickup_date"])==("CNY","exclusive","2025-02-01")
    sql,b=ref.calls[0]
    assert b==dict(account="A",customer_name="示例员甲客户",item="测试物料",entity="示例员甲主体",currency="CNY",unit="PCS",exclude_po=None)
    assert "coalesce(d.latest_pickup_date,h.booked_date,h.ordered_date,h.creation_date)" in sql and "order by price_date desc nulls last" in sql
    assert "msi.segment1=:item" in sql and "msi.inventory_item_id=l.inventory_item_id" in sql and "w.released_status not in ('R','D')" in sql


def test_unknown_price_basis_is_never_used_as_known():
    ref=Fixture([("3.25",None,date(2025,2,1),"PCS",date(2025,2,1))])
    assert ref.price("1","甲","P","主体","CNY","exclusive") is None
    assert ref.price("1","甲","P","主体","CNY")["price_basis"]=="unknown"
    assert ref.price("1","甲","P","","CNY") is None


def test_po_date_all_entities_and_suffix_over_99():
    ref=Fixture([(v,0) for v in ("2026112501","2026112503","20261125100","20261125old","2026112500","20261125-01")]+[(None,7)])
    assert ref.used_po_sequences("A","20261125")=={1,3,7,100}
    assert ref.calls[0][1]==dict(account="A",prefix="20261125%",day="20261125")
    for bad in ["20261199","2026_125","2026%125",""]:
        with pytest.raises(ValueError):
            ref.used_po_sequences("A",bad)


def test_domestic_defaults_requires_a_unique_type_and_configured_warehouse():
    assert Fixture([(8,"8_内销-出货订单","88")]).domestic_defaults("主体")==dict(order_type="8_内销-出货订单",ship_org_code="88")
    for rows in ([],[(8,"8_内销-出货订单",None)],[(8,"A","X"),(9,"B","Y")]):
        assert Fixture(rows).domestic_defaults("主体") is None


def test_repeated_queries_reuse_one_order_snapshot_without_cross_order_cache():
    from unittest.mock import MagicMock
    gw=MagicMock()
    conn=gw.connect.return_value.__enter__.return_value
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[("X","P","PCS")]
    ref=SalesReference(gw)
    for i in range(2):
        with ref.session() as scope:
            assert scope.items("account","P")==scope.items("account","P")
    assert gw.connect.call_count==2 and cur.fetchall.call_count==2


def test_changing_seller_replaces_previous_domestic_type_and_xml_warehouse(tmp_path):
    from test_orders import service,record
    from order_mcp.schema import Order
    from order_mcp.rules import export_xml
    workflow=service(tmp_path)
    draft=record(workflow)
    first=workflow.revise(draft["id"],draft["revision"],{"order_type":"8000_内销-出货订单"},"示例员甲核对",human=True)
    new=workflow.revise(draft["id"],first["revision"],{"business_entity":"示例制造戊有限公司"},"更正主体",human=True)
    assert new["can_prepare"] and new["normalized"]["order_type"]=="8004_内销-出货订单"
    xml=export_xml(Order.model_validate(new["order"]),new["normalized"])
    assert "<SHIP_ORG_CODE>X05</SHIP_ORG_CODE>" in xml and "<RELEASE_ORG_CODE>X05</RELEASE_ORG_CODE>" in xml
    assert "3811_" not in xml
    unresolved=workflow.revise(draft["id"],new["revision"],{"business_entity":"示例制造丑有限公司"},"更正主体",human=True)
    assert not unresolved["can_prepare"] and unresolved["normalized"]["ship_org_code"] is None


def test_lookup_tools_use_scoped_reference_without_mutating_review(tmp_path):
    import asyncio
    from unittest.mock import Mock
    from test_orders import service,record
    workflow=service(tmp_path)
    draft=record(workflow, with_price=False)
    ref=Mock()
    ref.customers.return_value=[{"name":"示例员甲","account_number":"0001"}]
    ref.price.return_value={"price":"11","price_basis":"inclusive","latest_pickup_date":"2026-01-01","unit":"PCS","currency":"CNY"}
    workflow.reference=ref
    assert asyncio.run(workflow.lookup_customer(draft["id"]))["candidates"]==ref.customers.return_value
    quote=asyncio.run(workflow.lookup_price(draft["id"],0))
    ref.price.assert_called_once_with("910001","测试客户","R0001","示例制造甲有限公司","CNY","inclusive",unit=None,exclude_po="PO00001")
    assert quote["candidate"]["latest_pickup_date"]=="2026-01-01" and quote["requires_confirmation"]
    assert workflow.store.get(draft["id"])["revision"]==draft["revision"]


def test_recent_order_without_shipment_can_supply_price_but_skips_current_po():
    ref=Fixture([("12.345", None, None, "PCS", date(2026,11,20))])
    result=ref.price("C","客户","R1","主体","CNY",exclude_po="PO-NEW")
    assert result["price"]=="12.345" and result["price_date"]=="2026-11-20"
    assert result["latest_pickup_date"] is None and result["price_basis"]=="unknown"
    sql,binds=ref.calls[0]
    assert binds["exclude_po"]=="PO-NEW" and "h.cust_po_number<>:exclude_po" in sql
    assert "nvl(h.cancelled_flag,'N')='N'" in sql
