import json
from hashlib import sha256
import httpx
import pytest
from order_mcp.hongyi import HongyiClient

def config():
    return {"base_url": "https://ecm.example.test", "folder_id": 25, "form_id": "F100",
            "token_env": "TEST_ECM_TOKEN", "metadata_fields": {"order_number": "eformOrder", "customer_po": "eformPo", "account_number": "eformAccount", "sha256": "eformSha"}}

def values():
    return {"order_number": "1001", "customer_po": "PO001", "account_number": "001",
            "sha256": sha256(b"test-pdf-bytes").hexdigest()}

def responder(calls, *, status="End", region="", metadata_code=0):
    def handler(request):
        calls.append((request.url.path, request.content))
        path = request.url.path
        if path.endswith("SearchFileList"):
            return httpx.Response(200, json={"result": 0, "data": {"DocListInfo": {"FilesInfo": [], "Settings": {"searchTotalCount": 0}}}})
        if path == "/WebCore":
            assert "multipart/form-data" in request.headers["content-type"]
            assert b'CheckAndCreateDocInfo' in request.content
            return httpx.Response(200, json={"result": 0, "data": {"FileId": 101, "FileVerId": 102, "RegionHash": "region-secret", "RegionId": 1, "RegionUrl": region}})
        if path == "/document/upload":
            assert b'name="file"' in request.content and b'test-pdf-bytes' in request.content
            return httpx.Response(200, json={"errorCode": 0, "status": status, "percent": 100})
        if path.endswith("UpdateEntityMetaMapEx"):
            body = json.loads(request.content)
            assert "EntityId=101|EntityType=2|metaStrategy=0|eformOrder=1001" in body["dataset"]
            assert body["fileId"] == 101 and body["fileVerId"] == 102
            return httpx.Response(200, json={"result": 0, "data": {"code": metadata_code, "result": 0, "entityId": 101}})
        if path.endswith("GetMetaDatasByFileId"):
            return httpx.Response(200, json={"result": 0, "data": []})
        raise AssertionError(path)
    return handler

async def test_native_ecm_full_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ECM_TOKEN", "synthetic-secret")
    path = tmp_path / "order.pdf";path.write_bytes(b"test-pdf-bytes")
    calls=[];checkpoints=[]
    client = HongyiClient(config(), httpx.MockTransport(responder(calls)))
    result = await client.upload_ecm_with_metadata(path, values(), "key", checkpoints.append)
    assert result["document_id"] == "101"
    assert [x["stage"] for x in checkpoints] == ["file_created", "file_uploaded"]
    assert "synthetic-secret" not in json.dumps(result) and "region-secret" not in json.dumps(checkpoints)
    await client.query(result)
    assert calls[-1][0].endswith("GetMetaDatasByFileId")

@pytest.mark.parametrize("status,region,metadata_code", [("Uploading", "", 0), ("End", "https://other.example.test", 0), ("End", "", 1)])
async def test_native_ecm_partial_failure_not_success(tmp_path, monkeypatch, status, region, metadata_code):
    monkeypatch.setenv("TEST_ECM_TOKEN", "synthetic-secret")
    path=tmp_path/'order.pdf';path.write_bytes(b"test-pdf-bytes")
    calls=[]
    client=HongyiClient(config(), httpx.MockTransport(responder(calls, status=status, region=region, metadata_code=metadata_code)))
    with pytest.raises(ValueError):await client.upload_ecm_with_metadata(path, values(), "key", lambda x:None)
    if region:assert all(p!="/document/upload" for p,_ in calls)
    if status!="End":assert all(not p.endswith("UpdateEntityMetaMapEx") for p,_ in calls)

async def test_dataset_unsafe_delimiters_fail_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ECM_TOKEN", "synthetic-secret")
    path=tmp_path/'order.pdf';path.write_bytes(b"test-pdf-bytes")
    calls=[];client=HongyiClient(config(), httpx.MockTransport(responder(calls)))
    with pytest.raises(ValueError):await client.upload_ecm_with_metadata(path, values() | {"customer_po": "PO|inject=x"}, "key", lambda x:None)
    assert not calls


def routed_config():
    return config() | {"folder_id": 0, "destination": {
        "parent_folder_id": 9000001,
        "folders": [
            {"name": "01 示例部门甲", "folder_id": 501, "parent_folder_id": 9000001,
             "business_entities": ["示例制造乙有限公司"]},
            {"name": "03 示例部门丙", "folder_id": 503, "parent_folder_id": 9000001,
             "business_entities": ["示例制造甲有限公司"]},
            {"name": "08 示例部门辛", "folder_id": 508, "parent_folder_id": 9000001,
             "business_entities": []},
        ]}}


async def test_entity_folder_used_for_search_create_and_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ECM_TOKEN", "synthetic-secret")
    path = tmp_path / "order.pdf"; path.write_bytes(b"test-pdf-bytes")
    calls = []; checkpoints = []
    client = HongyiClient(routed_config(), httpx.MockTransport(responder(calls)))
    result = await client.upload_ecm_with_metadata(path, values() | {"business_entity": "示例制造甲有限公司"}, "key", checkpoints.append)
    assert json.loads(calls[0][1])["folderId"] == 503
    assert b'name="folderId"\r\n\r\n503\r\n' in calls[1][1]
    assert result["folder_id"] == 503 and result["folder_name"] == "03 示例部门丙"
    assert all(x["folder_id"] == 503 for x in checkpoints)


@pytest.mark.parametrize("case", ["unknown", "missing_id", "wrong_parent", "duplicate_route", "parent_as_child"])
async def test_invalid_entity_route_stops_before_network(tmp_path, case):
    cfg = routed_config()
    entity = "示例制造甲有限公司"
    selected = cfg["destination"]["folders"][1]
    if case == "unknown": entity = "未经确认的公司"
    if case == "missing_id": selected["folder_id"] = 0
    if case == "wrong_parent": selected["parent_folder_id"] = 999
    if case == "duplicate_route": cfg["destination"]["folders"][0]["business_entities"].append(entity)
    if case == "parent_as_child": selected["folder_id"] = 9000001
    path = tmp_path / "order.pdf"; path.write_bytes(b"test-pdf-bytes")
    calls = []
    client = HongyiClient(cfg, httpx.MockTransport(responder(calls)))
    with pytest.raises(ValueError):
        await client.upload_ecm_with_metadata(path, values() | {"business_entity": entity}, "key", lambda x: None)
    assert not calls


async def test_query_by_document_id_does_not_require_upload_mapping(monkeypatch):
    monkeypatch.setenv("TEST_ECM_TOKEN", "synthetic-secret")
    calls = []
    client = HongyiClient({"base_url": "https://ecm.example.test", "token_env": "TEST_ECM_TOKEN"},
                          httpx.MockTransport(responder(calls)))
    result = await client.query({"document_id": "101", "file_version_id": "102"})
    assert result["document_id"] == "101"
    assert len(calls) == 1 and calls[0][0].endswith("GetMetaDatasByFileId")


async def test_configured_metadata_without_artificial_required_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ECM_TOKEN", "synthetic-secret")
    path = tmp_path / "order.pdf"; path.write_bytes(b"test-pdf-bytes")
    calls = []
    client = HongyiClient(config() | {"metadata_fields": {"order_number": "eformOrder"}},
                          httpx.MockTransport(responder(calls)))
    assert (await client.upload_ecm_with_metadata(path, values(), "key", lambda x: None))["document_id"] == "101"


async def test_missing_metadata_source_never_silently_writes_empty(tmp_path):
    path = tmp_path / "order.pdf"; path.write_bytes(b"test-pdf-bytes")
    calls = []
    client = HongyiClient(config() | {"metadata_fields": {"unavailable_source": "eformOrder"}},
                          httpx.MockTransport(responder(calls)))
    with pytest.raises(ValueError, match="缺失的订单字段"):
        await client.upload_ecm_with_metadata(path, values(), "key", lambda x: None)
    assert not calls


def v6_config():
    cfg = routed_config()
    cfg["destination"]["parent_directory_reference"] = "9000001"
    cfg["collection"] = {
        "submit_url":"https://edrms.example.test/edrms/tool/collectArchives/submitMetadata/fixture",
        "rule_name":"客户销售订单","token_env":"TEST_EDRMS_TOKEN",
        "content_fields":{"company":"business_entity","orderNo":"order_number"},
        "accepted_response":{"field":"data.state","equals":"received"},
        "duplicate_response":{"field":"data.state","equals":"duplicate"},
        "archived_response":{"field":"data.state","equals":"filed"},
        "receipt_field":"data.id"
    }
    return cfg


def v6_transport(calls,reply):
    ecm = responder(calls)
    def dispatch(req):
        if req.url.host=="ecm.example.test":
            return ecm(req)
        assert req.url.host=="edrms.example.test" and req.method=="POST"
        assert req.url.path=="/edrms/tool/collectArchives/submitMetadata/fixture" and not req.url.query
        calls.append(("collection",json.loads(req.content)))
        return reply
    return httpx.MockTransport(dispatch)


def test_only_one_verified_company_is_needed_for_availability(monkeypatch):
    from order_mcp.hongyi import archive_target_status
    monkeypatch.setenv("TEST_ECM_TOKEN","fixture-ecm");monkeypatch.setenv("TEST_EDRMS_TOKEN","fixture-collection")
    cfg=v6_config() | {"enabled":True}
    cfg["collection"]["rule_name"]="SO_TEST_014"
    cfg["destination"]["folders"]=cfg["destination"]["folders"][1:2]
    available=archive_target_status(cfg)
    assert available["ready"] and available["collection"]=={"mode":"edrms_v6_ecm","ready":True,"missing":[]}
    monkeypatch.delenv("TEST_EDRMS_TOKEN")
    unavailable=archive_target_status(cfg)
    assert not unavailable["ready"] and unavailable["collection"]["missing"]==["token_env"]


@pytest.mark.parametrize(("remote","local"),[("received","accepted"),("duplicate","duplicate"),("filed","archived")])
async def test_v6_receipt_and_original_file_reference(tmp_path,monkeypatch,remote,local):
    import uuid
    monkeypatch.setenv("TEST_ECM_TOKEN","fixture-ecm")
    monkeypatch.setenv("TEST_EDRMS_TOKEN","fixture-collection")
    path=tmp_path/"original.pdf";path.write_bytes(b"test-pdf-bytes")
    calls=[]; stages=[]
    entity="示例制造甲有限公司"
    v=values() | {"business_entity":entity,"file_name":"原始订单.pdf"}
    client=HongyiClient(v6_config(),v6_transport(calls,httpx.Response(200,json={"data":{"state":remote,"id":"C-7"}})))
    result=await client.archive(path,v,"key",stages.append)
    assert result["document_id"]=="101" and result["collection_status"]==local
    assert result["folder_id"]==503 and result["collection_receipt"]["receipt_id"]=="C-7"
    assert [x["stage"] for x in stages]==["file_created","file_uploaded","collection_submitting","collection_"+local]
    assert calls[-1][0]=="collection" and calls[-2][0]=="/document/upload"
    body=calls[-1][1];assert body["token"]=="fixture-collection" and body["ruleName"]=="客户销售订单"
    block,=body["fileEntityBlock"]
    assert block["contentDescnption"]=={"company":entity,"orderNo":"1001","uniqueId":"key"}
    doc,=block["documents"]
    assert doc=={"sourceFileName":"原始订单.pdf","fileType":"pdf","uniqueId":str(uuid.uuid5(uuid.NAMESPACE_URL,"maimi-sales-order:original:key")),
                 "downloadType":"ecm","downloadContent":"101"}
    assert block["businessEntity"]==[] and block["fileEntityRelationShip"]==[] and block["extend"]==""
    assert "fixture-collection" not in json.dumps([stages,result])


@pytest.mark.parametrize("damage",["missing_rule","other_rule","unknown_entity","bad_parent","missing_token","bad_outcome","missing_metadata_value"])
async def test_v6_preflight_no_remote_effects(tmp_path,monkeypatch,damage):
    monkeypatch.setenv("TEST_ECM_TOKEN","fixture-ecm");monkeypatch.setenv("TEST_EDRMS_TOKEN","fixture-collection")
    path=tmp_path/"order.pdf";path.write_bytes(b"test-pdf-bytes");calls=[]
    cfg=v6_config(); v=values()|{"business_entity":"示例制造甲有限公司"}
    if damage=="missing_rule": del cfg["collection"]
    elif damage=="other_rule":cfg["collection"]["rule_name"]="客户基础资料"
    elif damage=="unknown_entity":v["business_entity"]="unmapped"
    elif damage=="bad_parent":cfg["destination"]["parent_directory_reference"]="different"
    elif damage=="missing_token":monkeypatch.delenv("TEST_EDRMS_TOKEN")
    elif damage=="bad_outcome":cfg["collection"].pop("accepted_response")
    else:del v["order_number"]
    with pytest.raises(ValueError): await HongyiClient(cfg,v6_transport(calls,None)).archive(path,v,"key",lambda r:None)
    assert not calls


@pytest.mark.parametrize("response",[httpx.Response(200,json={"data":{"state":"undocumented"}}),
                                   httpx.Response(202,text="submitted"),httpx.Response(503)])
async def test_v6_unknown_submission_is_checkpointed_before_network(tmp_path,monkeypatch,response):
    monkeypatch.setenv("TEST_ECM_TOKEN","fixture-ecm");monkeypatch.setenv("TEST_EDRMS_TOKEN","fixture-collection")
    path=tmp_path/"order.pdf";path.write_bytes(b"test-pdf-bytes");calls=[];stages=[]
    client=HongyiClient(v6_config(),v6_transport(calls,response))
    with pytest.raises(ValueError,match="收集"):
        await client.archive(path,values()|{"business_entity":"示例制造甲有限公司"},"key",stages.append)
    assert stages[-1]["stage"]=="collection_submitting" and stages[-1]["business_unique_id"]=="key"
    assert sum(path=="collection" for path,body in calls)==1
