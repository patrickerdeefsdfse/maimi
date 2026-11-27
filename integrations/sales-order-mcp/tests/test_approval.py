import json
import httpx
import pytest
from order_mcp.integrations import CompanyApis

@pytest.mark.parametrize("result,entity,expected", [(True,"湖南",True),(1,"湖南",False),(True,"其他实体",False)])
async def test_approval_exact_success_type_and_entity(tmp_path,monkeypatch,result,entity,expected):
    cfg={"approval":{"enabled":True,"url":"https://company.example.test/approval","method":"GET",
                     "request_fields":{"order_number":"orderNumber","business_entity":"businessEntity"},
                     "success_path":"ok","success_value":True,
                     "response_fields":{"order_number":"data.number","business_entity":"data.entity","status":"data.status"},
                     "approved_values":["APPROVED"]}}
    path=tmp_path/'integrations.json';path.write_text(json.dumps(cfg),'utf-8')
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"ok":result,"data":{"number":"1001","entity":entity,"status":"APPROVED"}}))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=transport,**kw))
    api=CompanyApis(path)
    if expected:assert (await api.approval('1001','湖南'))['approved'] is True
    else:
        with pytest.raises(ValueError):await api.approval('1001','湖南')
