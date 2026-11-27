from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from order_mcp.config import Settings
from order_mcp.order_suggestions import set_currency_hints, ENTITIES_FOR_REVIEW
from order_mcp.service import apply_business_defaults, OrderService
from order_mcp.store import Store
from test_orders import FakeOracle, order

class Reference:
    def session(self): return nullcontext(self)
    def used_po_sequences(self, account, date): return {2}
    def customers(self, name, *, account_number=None):
        return [{"name":"完整客户"+v,"account_number":v} for v in ([account_number] if account_number else ["A","B"])]
    def items(self, account, item, entity): return [{"item_code":"R"+account}]
    def price(self, account, name, item, entity, currency, basis, *, unit, exclude_po=None):
        return {"price":"42" if account=="A" else "13","price_basis":"unknown"}

def make(tmp):
    svc=OrderService(Settings(data_dir=tmp),oracle=FakeOracle())
    svc.reference=Reference()
    data=order().model_dump(mode="json")
    data.update(customer_name="简写",account_number=None,customer_po=None)
    data["lines"][0].update(customer_item="CK",item_code=None,unit_price=None,price_basis="unknown")
    item=svc.store.create({"kind":"order","status":"review","order":data,"provenance":{},"warnings":[]})
    return svc,item["id"]

def test_first_view_and_change(tmp_path):
    svc,rid=make(tmp_path)
    first=svc.review(rid)
    assert first["customer_candidates"][0]["account_number"]=="A"
    assert len(first["business_entities"])==12
    assert first["order"]["customer_po"].endswith("03")
    assert first["order"]["lines"][0]["item_code"]=="RA" and first["order"]["lines"][0]["unit_price"]=="42"
    assert svc.review(rid)["revision"]==first["revision"]
    changed=svc.revise(rid,first["revision"],{"customer_name":"完整客户B","account_number":"B"},"改用另一候选",human=True)
    assert changed["order"]["lines"][0]["item_code"]=="RB"
    assert changed["order"]["lines"][0]["unit_price"]=="13"
    assert changed["order"]["customer_po"].endswith("03")
    manual=svc.revise(rid,changed["revision"],{"lines.0.unit_price":"123.45"},"按原件",human=True)
    assert manual["order"]["lines"][0]["unit_price"]=="123.45"

async def test_lookup_only_for_unpriced_lines_from_recognition_to_prepare(tmp_path):
    from unittest.mock import AsyncMock, Mock

    svc, _ = make(tmp_path)
    header = {"业务实体": "示例制造甲有限公司", "客户名称": "完整客户A",
              "客户代号": "A", "客户PO号": "QA-MIXED", "币种": "CNY", "订单数量": 1}
    svc.provider = AsyncMock()
    svc.provider.extract.return_value = {"rows": [
        {**header, "客户料号": "001", "含税单价": "19.23"},
        {**header, "客户料号": "002"},
    ]}
    svc.reference.items = lambda account, item, entity: [{"item_code": "R" + item}]
    svc.reference.price = Mock(return_value={"price": "42", "price_basis": "inclusive"})
    source = tmp_path / "mixed.png"
    source.write_bytes(b"fixture")
    draft, = (await svc.recognize(source, source.name, "M90000005", "示例员甲"))["orders"]
    assert [line["unit_price"] for line in draft["order"]["lines"]] == ["19.23", "42"]
    assert svc.reference.price.call_count == 1
    assert svc.reference.price.call_args.args[2] == "R002"

    existing = await svc.lookup_price(draft["id"], 0)
    assert existing["price_present"] and existing["current_price"] == "19.23" and existing["candidate"] is None
    revised = svc.revise(draft["id"], draft["revision"], {"lines.0.item_code": "R003"}, "按复核修改料号", human=True)
    prepared = svc.prepare_submission(draft["id"], revised["revision"])
    assert prepared["normalized"]["rows"][0]["unit_selling_price"] == "19.23"
    assert svc.reference.price.call_count == 1


def test_concurrent_per_customer_reservations(tmp_path):
    store=Store(tmp_path)
    with ThreadPoolExecutor(5) as pool:
        values=list(pool.map(lambda i:store.reserve_po(str(i),"A","20261125",{4}),range(12)))
    assert len(set(values))==12 and min(values)=="2026112505" and max(values)=="2026112516"
    assert store.reserve_po("0","A","20261125",set())==values[0]
    assert store.reserve_po("0","B","20261125",set())=="2026112501"
    assert store.reserve_po("0","A","20261126",set())=="2026112601"
    assert store.reserve_po("many","A","20261125",{99})=="20261125100"


def test_screenshot_keyword_comes_from_sender_not_recipient_or_sales_entity():
    from order_mcp.order_suggestions import customer_keywords, customer_reference
    terms=customer_keywords(["未知"], "样例工控商务\n@ExampleUser 早上好！\n帮忙订一台DEMO40002-A")
    assert terms==["样例工控"]
    assert customer_keywords(["示例制造乙有限公司"],"") == []
    ref=Reference()
    items=[{"name":"示例样例工控有限公司","account_number":"C1"},{"name":"样例工控甲有限公司","account_number":"C2"},
           {"name":"样例买方乙有限公司","account_number":"C3"}]
    ref.customers=lambda query,**opts: items if query=="样例工控" else []
    record={"order":{"customer_name":None},"customer_keywords":terms,"provenance":{}}
    assert customer_reference(ref,record,set())==items[0]
    assert [r["account_number"] for r in record["customer_candidates"]]==["C1","C2"]
    assert customer_keywords([], "@ExampleUser 早上好！帮忙订一台DEMO40002-A") == []
    assert customer_keywords([], "客户是样例工控，下单") == ["样例工控"]
    assert customer_keywords([], "样例工控\n@ExampleUser 早上好！") == ["样例工控"]


def test_extract_customer_keywords_from_single_file_and_not_shared_across_groups(tmp_path):
    import asyncio
    svc,_=make(tmp_path)
    class Provider:
        rows=[]
        async def extract(self,*args):
            return {"rows":self.rows}
    provider=Provider()
    svc.provider=provider
    p=tmp_path/"chat.png"
    p.write_bytes(b"fixture")
    async def run():
        return (await svc.extract_drafts(p,p.name,"M90000005","示例",source_text="样例工控商务\n@ExampleUser 早上好！"))[0]
    assert asyncio.run(run())[0]["customer_keywords"]==["样例工控"]
    provider.rows=[{"客户名称":n,"客户PO号":n} for n in ("甲方甲","乙方乙")]
    assert all(not r["customer_keywords"] for r in asyncio.run(run()))


def test_price_not_found_and_query_failure_are_shown_on_the_price_field(tmp_path):
    from datetime import date
    for name in ("missing","outage","found"):
        svc,rid=make(tmp_path/name)
        def fetch_price(*args,**kwargs):
            if name=="outage":
                raise TimeoutError()
            if name=="found":
                assert args[-1]=="unknown" and kwargs["exclude_po"]
                return {"price":"42","price_date":date(2026,11,20).isoformat(),"price_basis":"unknown"}
            return None
        svc.reference.price=fetch_price
        first=svc.review(rid)
        if name=="found":
            assert "2026-11-20" in first["business_defaults"]["lines.0.unit_price"]["reason"]
            continue
        issue=next(x for x in first["issues"] if x["path"]=="lines.0.unit_price" and x["code"]=="required")
        assert ("暂未完成" if name=="outage" else "未查到") in issue["message"]

def test_currency_priority():
    def draft(chat,file,kind="",stored=None):
        r={"order":{"currency":stored,"order_type":kind,"lines":[]},"provenance":{}}
        set_currency_hints(r,chat,file)
        apply_business_defaults(r)
        return r
    assert draft("用港币", "unit price USD", "1011_内销", "CNY")["order"]["currency"]=="HKD"
    assert draft("", "EUR")["order"]["currency"]=="EUR"
    assert draft("", "purchase order","1011_内销")["order"]["currency"]=="CNY"
    assert draft("", "purchase order")["business_defaults"]["currency"]["value"]=="USD"
    assert draft("", "주문서")["order"]["currency"]=="KRW"
    assert draft("人民币或美元","purchase order")["order"]["currency"] is None
    assert draft("", "123ABC")["order"]["currency"] is None

def test_conflicting_customer_account_needs_selection(tmp_path):
    svc,rid=make(tmp_path)
    first=svc.review(rid)
    wrong=svc.revise(rid,first["revision"],{"customer_name":"完整客户A","account_number":"B"},"核对",human=True)
    assert any(issue["code"]=="customer_mismatch" for issue in wrong["issues"])
    assert {hit["account_number"] for hit in wrong["customer_candidates"]}=={"A","B"}
    fixed=svc.revise(rid,wrong["revision"],{"account_number":"A","customer_name":"完整客户A"},"按候选确认",human=True)
    assert not any(issue["code"]=="customer_mismatch" for issue in fixed["issues"])

def test_explicit_source_group_can_link_lines_without_po():
    from order_mcp.fastapi_rows import mapped_orders
    rows=[{"客户名称":"测试客户","订单分组":group,"计量单位":"PCS","客户物料":str(index)}
          for index,group in enumerate(["甲","甲","乙"])]
    orders=mapped_orders(rows)
    assert [len(o.lines) for o,_,_ in orders]==[2,1]
    assert orders[0][0].lines[0].unit=="PCS" and orders[0][0].lines[0].customer_item=="0"
