"""Real stateless HTTP MCP + gateway client; only business backends use synthetic doubles."""
import asyncio
import base64
import io
import socket
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from PIL import Image
from openpyxl import Workbook
from pypdf import PdfWriter

from order_mcp.cloud import create_app
from order_mcp.config import Settings
from order_mcp.schema import Extraction
from test_orders import order, FakeOracle, FakeApis

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/"server/gateway"))
from app import sales_order_transport as transport


def source(fmt):
    out = io.BytesIO()
    if fmt == "xlsx":
        wb = Workbook()
        wb.active.append(["SYNTHETIC", 1])
        wb.save(out)
    elif fmt == "pdf":
        pdf = PdfWriter()
        pdf.add_blank_page(width=72,height=72)
        pdf.write(out)
    else:
        Image.new("RGB",(80,40),"white").save(out,format="PNG")
    return {"name":"synthetic."+fmt,"data":base64.b64encode(out.getvalue()).decode()}


@pytest.fixture
async def backend(tmp_path,monkeypatch):
    api=FakeApis()
    db=FakeOracle()
    class SyntheticExtractor:
        async def extract(self,name,raw,employee,display):
            assert raw and employee.startswith("M") and display=="测试员工"
            orders=[{"业务实体":"示例制造甲有限公司","客户名称":"示例员甲测试购方","客户代号":"910001",
                     "客户PO号":"TEST-0001","币种":"CNY","订购项目":"TEST-001","订单数量":2,"含税单价":3,
                     "请求日期":"2026-12-30"}]
            return {"rows":orders,"workbook":b"synthetic-xlsx","workbook_kind":"xlsx"}
    master=tmp_path/"master.json"
    master.write_text(json.dumps({"items":[{"account_number":"910001","customer_item":"TEST-001","item_code":"R0001"}]}))
    settings=Settings(data_dir=tmp_path,master_data=master,enable_write=True)
    secret="synthetic-only-"*4
    app=create_app(settings,auth_key=secret,provider=SyntheticExtractor(),oracle=db,archive_apis=api)
    sock=socket.socket()
    sock.bind(("127.0.0.1",0))
    port=sock.getsockname()[1]
    url=f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setenv("MAIMI_ORDER_MCP_SECRET",secret)
    monkeypatch.setenv("MAIMI_ORDER_MCP_URL",url)
    server=uvicorn.Server(uvicorn.Config(app,log_level="error",access_log=False))
    runner=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if runner.done(): await runner
                await asyncio.sleep(.01)
        yield db,api,url
    finally:
        server.should_exit=True
        await runner
        sock.close()


@pytest.mark.parametrize("fmt",["png","pdf","xlsx"])
async def test_recognize_submit_approve_archive_once(backend,fmt,monkeypatch):
    db,api,url=backend
    caller={"source":"model","display_name":"测试员工","employee":"M90000001","conversation":str(uuid4())}
    data=await transport.rpc("recognize",source(fmt),**caller)
    assert len(data["orders"])==1
    r=data["orders"][0]
    assert r["file_name"]=="synthetic."+fmt and len(r["normalized"]["rows"])==1
    rid={"order_id":r["id"]}
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("prepare",dict(rid,revision=r["revision"]),**caller)
    result=await transport.rpc("prepare",dict(rid,revision=r["revision"]),**(caller|{"source":"human"}))
    submit=dict(rid,revision=result["revision"],confirmation_token=result["confirmation_token"])
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("submit",submit,**caller)
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("review",rid,**(caller|{"conversation":str(uuid4())}))
    await transport.rpc("submit",submit,**(caller|{"source":"human"}))
    assert db.calls==1
    assert (await transport.rpc("pending",{},source="scheduler"))["items"]==[]
    await transport.rpc("archive",rid,**(caller|{"source":"archive"}))
    assert api.uploads==0
    from order_mcp import cloud
    reviewer = caller | {"employee":"M90000003","conversation":str(uuid4())}
    class TestAssignments:
        configured = True
        class gateway:
            @staticmethod
            def book_order(batch_number, order_number, source=None):
                return {"status":"S", "batch_number":batch_number}
        def __init__(self, *_): pass
        def get(self, employee, number, entity):
            return ("final","version-1") if employee == reviewer["employee"] else None
        def for_employee(self, employee, pairs):
            return {pair:self.get(employee,*pair) for pair in pairs}
    monkeypatch.setattr(cloud, "OracleAssignments", TestAssignments)
    assert not (await transport.rpc("approval_pending",{},**caller))["items"]
    assert (await transport.rpc("approval_pending",{},**reviewer))["items"][0]["order_id"]==r["id"]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from maimi_projects.service import Context, ProjectFailure, ProjectSkillService
    monkeypatch.setitem(sys.modules,"app.plugin_admin",SimpleNamespace(project_granted=lambda *args:True))
    class ResultState:
        async def get(self, *_): return []
        async def put(self, *_): pass
        @asynccontextmanager
        async def lock(self, *_): yield
    async def authorize(ctx): assert ctx.user_no == reviewer["employee"]
    async def call_as(action, arguments, role):
        return await transport.rpc(action,arguments,**(reviewer|{"source":role}))
    def review_service(role):
        ctx = Context(reviewer["employee"],reviewer["conversation"],"cloud",("sales-order-approval",),"qa",source=role)
        svc = ProjectSkillService(authorize=authorize,state=ResultState(),files=None,
                                  sales_order_call=lambda action,arguments:call_as(action,arguments,role))
        return ctx,svc
    ctx,svc = review_service("model")
    pending = (await svc.invoke(ctx,"sales-order-approval","pending",{}))["project_result"]
    detail_action, = pending["actions"]
    detail = (await svc.invoke(ctx,"sales-order-approval","detail",detail_action["arguments"]))["project_result"]
    confirm, = detail["actions"]
    assert confirm["action"]=="approve" and set(confirm["arguments"])=={"review_id"}
    with pytest.raises(ProjectFailure):
        await svc.invoke(ctx,"sales-order-approval","approve",confirm["arguments"])
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("approval_decide",confirm["arguments"],**reviewer)
    ctx,svc = review_service("human")
    decision = (await svc.invoke(ctx,"sales-order-approval","approve",confirm["arguments"]))["project_result"]
    assert decision["status"]=="completed" and "已审核通过" in decision["summary"]
    assert (await transport.rpc("pending",{},source="scheduler"))["items"][0]["order_id"]==r["id"]
    await transport.rpc("archive",rid,**(caller|{"source":"archive"}))
    await transport.rpc("archive",rid,**(caller|{"source":"archive"}))
    assert api.uploads==1
    assert (await transport.rpc("pending",{},source="scheduler"))["items"]==[]
    assert (await transport.rpc("review",rid,**caller))["archive_status"]=="ecm_uploaded"
    # Same business order in another employee's independent conversation shares the ledger.
    other=caller|{"employee":"M90000002","conversation":str(uuid4())}
    second=(await transport.rpc("recognize",source(fmt),**other))["orders"][0]
    p=await transport.rpc("prepare",{"order_id":second["id"],"revision":second["revision"]},**(other|{"source":"human"}))
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("submit",{k:p[k] for k in submit},**(other|{"source":"human"}))
    assert db.calls==1


async def test_service_rejects_unsigned_and_browser_requests(backend):
    _,_,url=backend
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.post(url,json={})).status_code==403
        assert (await client.post(url,json={},headers={"Origin":"https://example.test","Authorization":"Bearer "+"synthetic-only-"*4})).status_code==403


async def test_web_lookup_po_and_target_status_are_conversation_bound(backend):
    db, _, _ = backend
    db.customer = lambda name: [{"name": name, "account_number": "910001"}]
    db.price = lambda *args: pytest.fail("recognized unit price must skip Oracle pricing")
    caller = {"source": "model", "display_name": "测试员工", "employee": "M90000001", "conversation": str(uuid4())}
    status = await transport.rpc("status", {}, **caller)
    assert not status["oracle"]["write_ready"] and status["oracle"]["environment"] == "unconfigured"
    view = (await transport.rpc("recognize", source("xlsx"), **caller))["orders"][0]
    ref = {"order_id": view["id"]}
    assert (await transport.rpc("lookup_customer", ref, **caller))["requires_confirmation"]
    receipt = await transport.rpc("lookup_price", ref | {"line_index": 0}, **caller)
    assert receipt["price_present"] and receipt["current_price"] == "3" and receipt["price_basis"] == "inclusive"
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("lookup_price", ref | {"line_index": 0}, **(caller | {"employee": "M90000002"}))
    changed = await transport.rpc("revise", ref | {"revision": view["revision"], "changes": {"customer_po": None}, "reason": "用户核对无 PO"}, **caller)
    result = await transport.rpc("generate_po", ref | {"revision": changed["revision"]}, **caller)
    assert result["order"]["customer_po"] and "内部代号" in result["provenance"]["customer_po"]
    assert (await transport.rpc("review", ref, **caller))["revision"] == result["revision"]
    assert db.calls == 0


async def test_revoked_grant_prevents_background_archive(monkeypatch):
    sent=[]
    async def fake_rpc(action,args,**scope):
        sent.append(action)
        return {"items":[{"employee":"M90000001","conversation":str(uuid4()),"order_id":"a"*32}]}
    async def denied(employee): return False
    monkeypatch.setattr(transport,"rpc",fake_rpc)
    monkeypatch.setattr(transport,"granted",denied)
    await transport.sweep()
    assert sent==["pending"]
