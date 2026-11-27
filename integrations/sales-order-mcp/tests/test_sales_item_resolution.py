from contextlib import nullcontext
from xml.etree import ElementTree

import pytest
from order_mcp.fastapi_rows import mapped_orders
from test_orders import service


class Catalog:
    def session(self):
        return nullcontext(self)

    def customers(self,name,**kwargs):
        return [{"name":"示例员甲买方","account_number":"42"}] if name else []

    def items(self,account,code,_entity):
        assert account=="42"
        return [{"item_code":"R-B"}] if code=="CP-B" else []

    def items_by_model(self,account,model):
        return {"DEMO40002-A":["R-A","R-B"],"DEMO40003-A":["R-C"]}.get(model,[])

    def inventory_code(self,code):
        return code if code=="R-B" else None

    def price(self,*args,**kwargs):
        return None


def setup(tmp_path, fields, *, customer=True):
    svc = service(tmp_path)
    svc.reference = Catalog()
    row={"业务实体":"示例制造乙有限公司","客户PO号":"QA-ITEM","币种":"CNY",
         "订单数量":1,"含税单价":3}
    if customer:
        row.update(客户名称="示例员甲买方",客户代号="42")
    order,source,_ = mapped_orders([row|fields])[0]
    rid = svc.store.create({"kind":"order","status":"review","model":"销售订单 FastAPI",
                            "order":order.model_dump(mode="json"),"provenance":source,"warnings":[]})["id"]
    return svc,rid


def test_verified_item_only_reaches_xml(tmp_path):
    svc,rid=setup(tmp_path,{"订购项目":"CP-B","规格型号":"DEMO40002-A"})
    view=svc.review(rid)
    line=view["order"]["lines"][0]
    assert [line[k] for k in ("customer_item","specification","item_code")]==["CP-B","DEMO40002-A","R-B"]
    assert ElementTree.fromstring(svc.export(rid)["xml"]).findtext(".//SEGMENT1")=="R-B"


def test_keyword_model_without_customer_can_offer_ambiguous_candidates(tmp_path):
    svc,rid=setup(tmp_path,{"原文":"@ExampleUser 早上好！帮忙订一台DEMO40002-A"},customer=False)
    view=svc.review(rid)
    line=view["order"]["lines"][0]
    assert line["item_code"] is None and line["customer_item"] is None and line["specification"]=="DEMO40002-A"
    assert view["item_candidates"]["lines.0.item_code"]==["R-A","R-B"] and not view["can_prepare"]
    assert any(i["code"]=="item_match" for i in view["issues"])


def test_exporter_model_in_ordered_item_survives_human_selection_and_other_edits(tmp_path):
    svc,rid=setup(tmp_path,{"订购项目":"DEMO40002-A"})
    first=svc.review(rid)
    assert first["order"]["lines"][0]["specification"]=="DEMO40002-A"
    assert first["order"]["lines"][0]["item_code"] is None
    choice=svc.revise(rid,first["revision"],{"lines.0.item_code":"R-B"},"核对后台规格",human=True)
    edited=svc.revise(rid,choice["revision"],{"lines.0.quantity":2},"核对数量",human=True)
    assert edited["item_candidates"]["lines.0.item_code"]==["R-A","R-B"]
    assert edited["can_prepare"] and svc.review(rid)["order"]["lines"][0]["item_code"]=="R-B"
    xml=ElementTree.fromstring(svc.export(rid)["xml"])
    assert xml.findtext(".//SEGMENT1")=="R-B" and "DEMO40002-A" not in svc.export(rid)["xml"]


def test_same_looking_internal_code_from_customer_column_needs_cross_reference(tmp_path):
    svc,rid=setup(tmp_path,{"订购项目":"R-B"})
    view=svc.review(rid)
    assert view["order"]["lines"][0]["customer_item"]=="R-B"
    assert view["order"]["lines"][0]["item_code"] is None
    assert not view["can_prepare"]


def test_conflicting_model_stays_editable_and_authorized_choice_resolves(tmp_path):
    svc,rid=setup(tmp_path,{"客户物料号":"CP-B","型号":"DEMO40003-A"})
    first=svc.review(rid)
    assert first["item_candidates"]["lines.0.item_code"]==["R-B","R-C"]
    assert not first["can_prepare"]
    fixed=svc.revise(rid,first["revision"],{"lines.0.item_code":"R-C"},"核对型号",human=True)
    assert fixed["can_prepare"] and not fixed["item_match_issues"]
    changed=svc.revise(rid,fixed["revision"],{"lines.0.specification":"DEMO40002-A"},"更正型号",human=True)
    assert changed["order"]["lines"][0]["item_code"]=="R-B"


@pytest.mark.parametrize("code,wanted",[("R-B","R-B"),("R-UNKNOWN",None)])
def test_explicit_internal_column_requires_directory_verification(tmp_path,code,wanted):
    svc,rid=setup(tmp_path,{"麦米编码":code},customer=False)
    line=svc.review(rid)["order"]["lines"][0]
    assert line["customer_item"] is None and line["item_code"]==wanted


async def test_existing_reviewed_draft_migrates_before_confirmation(tmp_path):
    svc,rid=setup(tmp_path,{"客户料号":"CP-B"})
    old=svc.store.get(rid)
    old["order"]["lines"][0].update(item_code="CP-B",customer_item=None)
    old["reference_reviewed"]=True
    old["status"]="prepared"
    old["provenance"]={"lines.0.item_code":"FastAPI 结果第 1 行「订购项目」"}
    old=svc.store.save(old,"legacy")
    updated=await svc.confirm_order(rid,old["revision"],{})
    assert updated["status"]=="needs_input" and updated["revision"]>old["revision"]
    line=updated["review"]["order"]["lines"][0]
    assert line["customer_item"]=="CP-B" and line["item_code"]=="R-B" and svc.oracle.calls==0
    assert svc.review(rid)["revision"]==updated["revision"]


def test_submitted_old_record_keeps_original_receipt_and_codes(tmp_path):
    svc,rid=setup(tmp_path,{"客户料号":"CP-B"})
    old=svc.store.get(rid)
    old["order"]["lines"][0].update(item_code="legacy",customer_item=None)
    old.update(reference_reviewed=True,status="submitted",oracle_result={"order_number":"QA-1","batch_number":"QA-2"},
               provenance={"lines.0.item_code":"FastAPI 结果第 1 行「订购项目」"})
    old=svc.store.save(old,"legacy")
    view=svc.review(rid)
    assert view["revision"]==old["revision"] and view["order"]["lines"][0]["item_code"]=="legacy"
    assert view["oracle_result"]==old["oracle_result"]
