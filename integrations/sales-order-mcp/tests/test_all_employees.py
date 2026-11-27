"""New staff can confirm a test file while records remain employee/session bound."""
from uuid import uuid4

import pytest
from test_web_workflow import backend, source
from app import sales_order_transport as transport


async def test_new_staff_test_recognition_uses_own_identity_and_stays_private(backend, monkeypatch):
    from order_mcp import fastapi_extract

    seen = []

    class SyntheticTestExtractor:
        def __init__(self, base):
            assert base == fastapi_extract.TEST + "/noauth"

        async def extract(self, name, raw, employee, display_name):
            seen.append((employee, display_name))
            return {"rows": [{"业务实体": "示例制造甲有限公司", "客户名称": "示例员甲测试购方",
                              "客户代号": "910001", "客户PO号": "TEST-NEW-STAFF", "币种": "CNY",
                              "订购项目": "TEST-001", "订单数量": 2, "含税单价": 3, "请求日期": "2026-12-30"}],
                    "workbook": b"synthetic-xlsx", "workbook_kind": "xlsx"}

    monkeypatch.setattr(fastapi_extract, "FastAPIExtractor", SyntheticTestExtractor)
    db, _, _ = backend
    caller = {"source": "model", "display_name": "测试员工", "employee": "M90000005", "conversation": str(uuid4())}
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("recognize_test", source("xlsx"), **caller)
    assert seen == []
    result = await transport.rpc("recognize_test", source("xlsx"), **(caller | {"source": "human"}))
    assert seen == [(caller["employee"], caller["display_name"])]
    ref = {"order_id": result["orders"][0]["id"]}
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("review", ref, **(caller | {"employee": "M90000004"}))
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc("review", ref, **(caller | {"conversation": str(uuid4())}))
    assert (await transport.rpc("review", ref, **caller))["id"] == ref["order_id"]
    assert db.calls == 0
