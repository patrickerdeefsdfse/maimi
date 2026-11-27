import asyncio
import base64
import json
from decimal import Decimal
from pathlib import Path
import pytest
from order_mcp.config import Settings, ROOT
from order_mcp.ingest import allowed_file, prepare, upload
from order_mcp.rules import validate, export_xml, MasterData
from order_mcp.schema import Order, Line, Extraction
from order_mcp.service import OrderService
from order_mcp.integrations import ArchiveService

def order(**kwargs):
    return Order(business_entity="示例制造甲有限公司", customer_name="测试客户", account_number="910001",
                 customer_po="PO00001", currency="CNY", total_amount="3846", total_basis="inclusive",
                 lines=[Line(customer_item="89000000DEMO400", item_code="R0001", quantity="200", unit_price="19.23",
                             price_basis="inclusive", tax_rate="0.13", amount="3846", amount_basis="inclusive", request_date="2026/10/05")], **kwargs)

def test_inclusive_price_never_taxed_twice():
    o = order()
    norm, issues = validate(o)
    assert norm["rows"][0]["unit_selling_price"] == "19.23"
    assert norm["rows"][0]["request_date"] == "2026-10-05"
    assert not [i for i in issues if i.severity == "error"]

def test_exclusive_and_amount_mismatch():
    o = order(); o.total_amount = None
    o.lines[0].unit_price = Decimal("100"); o.lines[0].price_basis = "exclusive"
    o.lines[0].amount = Decimal("22600")
    norm, issues = validate(o)
    assert norm["rows"][0]["unit_selling_price"] == "113.000000"
    assert not any(i.code == "amount_mismatch" for i in issues)
    o.lines[0].amount = Decimal("20000")
    assert any(i.code == "amount_mismatch" for i in validate(o)[1])

def test_unknown_basis_missing_date_and_zero_quantity_block():
    o = order(); o.lines[0].price_basis = "unknown"; o.lines[0].request_date = "10/10"; o.lines[0].quantity = Decimal(0)
    codes = {i.code for i in validate(o)[1]}
    assert {"tax_basis", "date", "quantity"} <= codes

def test_xml_correct_fields_escaping_and_long_codes():
    o = order(); o.customer_po = "PO<&"; o.lines[0].item_code = "89000000DEMO400"
    xml = export_xml(o, validate(o)[0])
    assert "89000000DEMO400" in xml and "PO&lt;&amp;" in xml
    assert "LINE_REMATK" in xml and "SALESREP_NAME" not in xml and "customer_name" not in xml

def test_no_auto_match_multiple_accounts_or_items(tmp_path):
    p = tmp_path / "master.json"
    p.write_text(json.dumps({"customers": [{"name": "测试（公司）", "account_number": "1"}, {"name": "测试(公司)", "account_number": "2"}],
                           "items": [{"account_number": "1", "customer_item": "0001", "item_code": "A"}, {"account_number": "1", "customer_item": "0001", "item_code": "B"}]}), "utf-8")
    o = order(); o.customer_name = "测试(公司)"; o.account_number = None; o.lines[0].item_code = None
    master = MasterData(p)
    assert master.resolve(o)[0].account_number is None
    o.account_number = "1"; o.lines[0].customer_item = "0001"
    assert master.resolve(o)[0].lines[0].item_code is None

def test_excel_date_and_sheet_isolation(tmp_path):
    from datetime import datetime
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.title = "新订单"
    workbook.active.append(["交期","数量"])
    workbook.active.append([datetime(2026,10,8),70])
    workbook.create_sheet("历史").append(["其他示例员甲订单"])
    p = tmp_path / "synthetic.xlsx"
    workbook.save(p)
    result = prepare(p, Settings(), ["新订单"])
    assert "2026-10-08" in result.text and "其他示例员甲订单" not in result.text

def test_pdf_all_pages_and_scan(tmp_path):
    from pypdf import PdfWriter
    p = tmp_path / "synthetic.pdf"
    writer = PdfWriter()
    for i in range(2): writer.add_blank_page(width=72,height=72)
    writer.write(p)
    result = prepare(p, Settings())
    assert len(result.images) == 2 and result.sources == ["page:1", "page:2"]

def test_reject_encrypted_and_paths(tmp_path):
    p = tmp_path / "encrypted.pdf"; p.write_bytes(b"%TSD-xxx")
    with pytest.raises(ValueError, match="加密"):prepare(p, Settings())
    with pytest.raises(ValueError, match="允许读取"):allowed_file(str(p), Settings(input_roots=(tmp_path / "only-orders",), data_dir=tmp_path / "store"))
    s = Settings(data_dir=tmp_path, max_bytes=5)
    with pytest.raises(ValueError):upload("x.png", base64.b64encode(b"1234567890").decode(), s)

class FakeOracle:
    calls = 0
    fail = False
    def submit(self, xml, checkpoint):
        self.calls += 1
        checkpoint({"batch_number": "10"})
        if self.fail:raise TimeoutError("unknown result")
        return {"order_number": "1001", "batch_number": "10"}

def service(tmp_path):
    return OrderService(Settings(data_dir=tmp_path, enable_write=True, user_code="TEST", master_data=tmp_path / "none"), oracle=FakeOracle())

def record(s, *, with_price=True):
    p = s.settings.data_dir / "original.pdf"; p.write_bytes(b"fake-test-only")
    import hashlib
    source = order()
    if not with_price:
        source.lines[0].unit_price = None
    return s.store.create({"kind": "order", "status": "review", "order": source.model_dump(mode="json"),
                          "provenance": {}, "warnings": [], "file_name": "original.pdf", "original_path": str(p),
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})

async def test_confirm_and_duplicate_prevention(tmp_path):
    s = service(tmp_path); r = record(s); p = s.prepare_submission(r["id"], r["revision"])
    with pytest.raises(ValueError, match="明确确认"):await s.submit(r["id"], p["revision"], p["confirmation_token"], False)
    result = await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    assert result["order_number"] == "1001" and s.oracle.calls == 1
    other = record(s); q = s.prepare_submission(other["id"], other["revision"])
    with pytest.raises(ValueError, match="重复建单"):await s.submit(other["id"], q["revision"], q["confirmation_token"], True)
    assert s.oracle.calls == 1

async def test_revision_invalidates_confirm_and_mapping(tmp_path):
    s = service(tmp_path); r = record(s); p = s.prepare_submission(r["id"], 1)
    changed = s.revise(r["id"], p["revision"], {"customer_name": "另一个客户"}, "用户确认")
    assert changed["order"]["account_number"] is None and changed["order"]["lines"][0]["item_code"] is None
    with pytest.raises(ValueError, match="确认凭据"):await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    assert s.oracle.calls == 0

async def test_unknown_submission_cannot_retry(tmp_path):
    s = service(tmp_path); s.oracle.fail = True
    r = record(s); p = s.prepare_submission(r["id"], 1)
    with pytest.raises(ValueError, match="核查"):await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    assert s.store.get(r["id"])["status"] == "submission_unknown"
    with pytest.raises(ValueError):await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    assert s.oracle.calls == 1

class FakeApis:
    approved = False
    uploads = 0
    fail = False
    async def approval(self, number, entity):return {"order_number": number, "status": "APPROVED" if self.approved else "PENDING", "approved": self.approved}
    def endpoint(self, name):return {"idempotency_header": "Idempotency-Key"}, {}
    async def request(self, name, values, file_path=None, idempotency_key=None):
        self.uploads += 1
        if self.fail:raise TimeoutError()
        return {"document_id": "ECM1001"}

async def test_archive_only_after_approval_and_once(tmp_path):
    s = service(tmp_path); r = record(s); p = s.prepare_submission(r["id"], 1)
    await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    api = FakeApis(); a = ArchiveService(s, api)
    assert (await a.sync(r["id"]))["archive_status"] == "awaiting_approval" and api.uploads == 0
    api.approved = True
    assert (await a.sync(r["id"]))["document_id"] == "ECM1001"
    await a.sync(r["id"])
    assert api.uploads == 1

async def test_unknown_archive_does_not_repeat_upload(tmp_path):
    s = service(tmp_path); r = record(s); p = s.prepare_submission(r["id"], 1)
    await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    api = FakeApis();api.approved = True;api.fail = True;a = ArchiveService(s, api)
    with pytest.raises(ValueError, match="核查"):await a.sync(r["id"])
    with pytest.raises(ValueError, match="核查"):await a.sync(r["id"])
    assert api.uploads == 1


@pytest.mark.parametrize("has_rule,error", [(False,"专用收集配置"),(True,"父目录")])
async def test_missing_archive_configuration_does_not_mark_remote_outcome_unknown(tmp_path,monkeypatch,has_rule,error):
    from order_mcp.hongyi import HongyiClient
    s = service(tmp_path); r = record(s); p = s.prepare_submission(r["id"], 1)
    await s.submit(r["id"], p["revision"], p["confirmation_token"], True)
    api = FakeApis(); api.approved = True
    monkeypatch.setenv("QA_COLLECTION_TOKEN","synthetic")
    collection = {"submit_url":"https://qa.invalid/collectArchives/submitMetadata/qa",
                  "rule_name":"测试销售订单","token_env":"QA_COLLECTION_TOKEN",
                  "content_fields":{"title":"order_number"},"accepted_response":{"field":"received","equals":True}}
    api.hongyi = HongyiClient({"base_url":"https://ecm.example.test","collection":collection if has_rule else {},
        "destination":{"parent_directory_reference":"9000001","parent_folder_id":0,"folders":[]}})
    with pytest.raises(ValueError, match=error):
        await ArchiveService(s, api).sync(r["id"])
    current = s.store.get(r["id"])
    assert current["archive_status"] == "awaiting_approval"
    assert "archive_key" not in current and api.uploads == 0

@pytest.mark.parametrize("state",["PENDING","REJECTED","unconfigured","timeout"])
async def test_no_ecm_write_without_subsequent_approval(tmp_path,state):
    s=service(tmp_path)
    rec=record(s)
    p=s.prepare_submission(rec["id"],1)
    await s.submit(rec["id"],p["revision"],p["confirmation_token"],True)
    api=FakeApis()
    async def status(number,entity):
        if state=="timeout":
            raise TimeoutError()
        if state=="unconfigured":
            raise ValueError("approval not configured")
        return {"order_number":number,"business_entity":entity,"status":state,"approved":False}
    api.approval=status
    archive=ArchiveService(s,api)
    if state in {"unconfigured","timeout"}:
        with pytest.raises((ValueError,TimeoutError)):
            await archive.sync(rec["id"])
    else:
        assert (await archive.sync(rec["id"]))["archive_status"]=="awaiting_approval"
    assert api.uploads==0
