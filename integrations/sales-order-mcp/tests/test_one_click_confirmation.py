"""Use the normal MCP route and a synthetic Oracle; no business systems are contacted."""
import asyncio
import threading
from uuid import uuid4

import pytest

from order_mcp.oracle import OraclePrecreateRejected, OracleCreateUncertain
from test_orders import service, record
from test_web_workflow import backend, source, transport


async def test_human_only_http_confirmation_and_scoped_receipt(backend):
    oracle, _, _ = backend
    caller = dict(source="model", display_name="测试员工", employee="M90000001", conversation=str(uuid4()))
    view = (await transport.rpc("recognize",source("xlsx"),**caller))["orders"][0]
    args = dict(order_id=view["id"],revision=view["revision"])
    for scope in (caller,caller|{"source":"human","conversation":str(uuid4())},caller|{"source":"human","employee":"M90000002"}):
        with pytest.raises(transport.OrderCallFailed):
            await transport.rpc("confirm_order", args, **scope)
    assert oracle.calls == 0
    human = caller | {"source":"human"}
    edited = args | {"changes":{"customer_po":"QA-CLICK"}}
    created = await transport.rpc("confirm_order",edited,**human)
    assert created["status"] == "submitted" and created["order_number"] == "1001" and created["batch_number"] == "10"
    assert await transport.rpc("confirm_order",edited,**human) == created
    receipt = await transport.rpc("query",{"order_id":view["id"]},**caller)
    assert receipt["order_number"] == created["order_number"] and receipt["batch_number"] == created["batch_number"]
    assert oracle.calls == 1


async def test_uncertain_query_through_http_is_private_and_never_resubmits(backend):
    oracle, _, _ = backend
    oracle.fail = True
    caller = dict(source="model",display_name="测试员工",employee="M90000003",conversation=str(uuid4()))
    view = (await transport.rpc("recognize",source("png"),**caller))["orders"][0]
    ref = {"order_id":view["id"]}
    args = dict(ref,revision=view["revision"])
    reply = await transport.rpc("confirm_order",args,**(caller|{"source":"human"}))
    assert reply["status"] == "submission_unknown" and "review" not in reply
    assert reply["batch_number"] == "10" and "unknown result" not in repr(reply)
    assert (await transport.rpc("query",ref,**caller)) == reply
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("query",ref,**(caller|{"conversation":str(uuid4())}))
    assert (await transport.rpc("confirm_order",args,**(caller|{"source":"human"}))) == reply and oracle.calls == 1
    found = {"order_number":"81001","batch_number":"10","creation_status":"S","validation_status":"S"}
    oracle.created_receipt = lambda batch, order, origin: found if batch=="10" and order["customer_po"]=="TEST-0001" else None
    results = await asyncio.gather(*(transport.rpc("query",ref,**caller) for _ in range(2)))
    assert all(r["order_number"]=="81001" and r["status"]=="submitted" for r in results)
    assert (await transport.rpc("confirm_order",args,**(caller|{"source":"human"})))["order_number"] == "81001"
    assert oracle.calls == 1


@pytest.mark.parametrize("row,wanted",[
    ((0,0,0,None),False), ((2,2,1,81001),False), ((1,0,1,81001),False),
    ((1,1,2,81001),False), ((1,1,1,None),False), ((1,1,1,81001),True),
])
def test_oracle_query_requires_complete_owned_batch_and_one_real_header(tmp_path, row, wanted):
    from unittest.mock import MagicMock
    from order_mcp.oracle import OracleGateway
    s = service(tmp_path)
    item = record(s)["order"]
    oracle = OracleGateway(s.settings)
    target = {"environment":"test","target_matches":True,"expected_target":{"host":"synthetic"}}
    oracle.target_status = lambda: target
    connection = MagicMock()
    oracle.connect = lambda:connection
    cur = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = row
    assert oracle.created_receipt("123",item,"wrong-target") is None and not cur.execute.called
    actual = oracle.created_receipt("123",item,oracle.booking_source(target))
    assert bool(actual) is wanted and cur.execute.call_count==1 and not cur.callproc.called
    sql=cur.execute.call_args.args[0]
    assert all(fragment in sql for fragment in ("LEFT JOIN oe_order_headers_all","t.created_by=","t.ou_name=","t.cust_po_number=","t.account_number="))


async def test_conflict_validation_and_definite_oracle_refusal_stay_editable(tmp_path):
    s = service(tmp_path)
    r = record(s)
    rid = r["id"]
    invalid = await s.confirm_order(rid,1,{"lines.0.quantity":0})
    assert invalid["status"] == "needs_input" and invalid["review"]["issues"]
    assert invalid["review"]["revision"] == invalid["revision"] and s.oracle.calls == 0
    stale = await s.confirm_order(rid,1,{"lines.0.quantity":100})
    assert stale["revision"] == invalid["revision"] and s.oracle.calls == 0
    original = s.oracle.submit
    def rejected(xml, checkpoint):
        checkpoint({"stage":"validate","status":"E","batch_number":"123"})
        raise OraclePrecreateRejected("validate","ORA-xxxxx; 密码 secret 客户账号 123 不存在 https://private.invalid")
    s.oracle.submit = rejected
    refused = await s.confirm_order(rid,stale["revision"],{"lines.0.quantity":r["order"]["lines"][0]["quantity"]})
    assert refused["status"] == "needs_input" and refused["batch_number"] == "123"
    assert "客户账号不存在" in refused["message"] and "secret" not in repr(refused)
    assert refused["review"]["status"] == "review"
    from maimi_projects import sales_orders
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    for action, reply in (("confirm_order", refused), ("review", s.review(rid))):
        gateway = SimpleNamespace(sales_orders=AsyncMock(return_value=reply), _authorize=AsyncMock(),
                                  _save_result=AsyncMock(side_effect=lambda ctx,card: {"project_result":card}))
        args = {"order_id":rid}
        if action == "confirm_order":
            args["revision"] = refused["revision"]
        result = await sales_orders.invoke(gateway, SimpleNamespace(source="human"), action, args)
        editor, = result["project_result"]["order_editors"]
        assert "客户账号不存在" in editor["submission_error"] and "接口批次号 123" in editor["submission_error"]
        assert "secret" not in editor["submission_error"]
    stale_editor = sales_orders.order_editor(refused["review"], "订单已更新")
    assert stale_editor["submission_error"] == "订单已更新"
    assert (await s.confirm_order(rid,stale["revision"],{}))["revision"] == refused["revision"]
    s.oracle.submit = original
    assert (await s.confirm_order(rid,refused["revision"],{}))["status"] == "submitted"


async def test_concurrent_clicks_and_ambiguous_result_are_read_only(tmp_path):
    s = service(tmp_path)
    rid = record(s)["id"]
    started, finish = threading.Event(), threading.Event()
    calls = []
    def uncertain(xml, checkpoint):
        calls.append(1)
        checkpoint({"stage":"create","batch_number":"456"})
        started.set()
        assert finish.wait(5)
        raise OracleCreateUncertain("订单号重复；凭据 internal-only")
    s.oracle.submit = uncertain
    first = asyncio.create_task(s.confirm_order(rid,1,{}))
    try:
        assert await asyncio.to_thread(started.wait,4)
        pending = await s.confirm_order(rid,1,{})
        assert pending["status"] == "submitting" and pending["batch_number"] == "456" and "review" not in pending
    finally:
        finish.set()
    result = await first
    assert result["status"] == "submission_unknown" and result["batch_number"] == "456" and "review" not in result
    assert "订单号重复" in result["message"] and "internal-only" not in result["message"]
    assert await s.confirm_order(rid,1,{"customer_po":"changed"}) == result
    assert calls == [1]
