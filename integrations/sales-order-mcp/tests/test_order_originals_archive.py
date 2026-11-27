import hashlib
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from order_mcp.hongyi import HongyiClient
from order_mcp.integrations import ArchiveService
from order_mcp.order_approvals import OrderApprovals
from test_hongyi import v6_config
from test_maimi_business_approval import Assigned, setup_order


class ArchiveTransport:
    def __init__(self):
        self.searches = 0
        self.created = 0
        self.uploads = []
        self.collections = []
        self.fail = None

    def __call__(self, req):
        suffix = req.url.path.split("/")[-1]
        def wrapped(data):
            return httpx.Response(200,json={"result":0,"data":data})
        if suffix=="SearchFileList":
            self.searches+=1
            if self.fail=="search" or (self.fail=="second_search" and self.searches==2):
                return httpx.Response(503)
            return wrapped({"DocListInfo":{"FilesInfo":[],"Settings":{"searchTotalCount":0}}})
        if suffix=="WebCore":
            self.created+=1
            if self.fail=="create":
                return httpx.Response(503)
            return wrapped({"FileId":self.created,"FileVerId":101+self.created,"RegionHash":"test",
                            "RegionId":1,"RegionUrl":""})
        if suffix=="upload":
            self.uploads.append(req.content)
            if self.fail=="upload":
                return httpx.Response(503)
            return httpx.Response(200,json={"errorCode":0,"status":"End"})
        if req.url.host=="edrms.example.test":
            self.collections.append(json.loads(req.content))
            if self.fail=="collection":
                return httpx.Response(503)
            return httpx.Response(200,json={"data":{"state":"received","id":"qa-v6"}})
        if suffix=="GetMetaDatasByFileId":
            return wrapped([])
        raise AssertionError(req.url.path)

@pytest.fixture
def bundle(tmp_path,monkeypatch):
    monkeypatch.setenv("TEST_ECM_TOKEN","qa-ecm")
    monkeypatch.setenv("TEST_EDRMS_TOKEN","qa-edrms")
    store,record,reviewer=setup_order(tmp_path)
    extra=tmp_path/"delivery.xlsx"
    extra.write_bytes(b"synthetic additional attachment")
    fields=("file_name","original_path","sha256")
    record["originals"]=[{k:record[k] for k in fields},
                         {"file_name":extra.name,"original_path":str(extra),"sha256":hashlib.sha256(extra.read_bytes()).hexdigest()}]
    store.save(record,"fixture_sources")
    cfg=v6_config()
    cfg["destination"]["folders"][0]["business_entities"]=[record["order"]["business_entity"]]
    transport=ArchiveTransport()
    def new_archiver():
        return ArchiveService(SimpleNamespace(store=store),SimpleNamespace(hongyi=HongyiClient(cfg,httpx.MockTransport(transport))))
    def approve():
        flow=OrderApprovals(reviewer,Assigned())
        flow.approve(flow.detail(record["id"])["review_id"])
    return store,record["id"],new_archiver,approve,transport


async def test_two_originals_one_collection_only_after_approval(bundle):
    store,rid,archiver,approve,remote=bundle
    assert (await archiver().sync(rid))["archive_status"]=="awaiting_approval" and not remote.searches
    approve()
    first=await archiver().sync(rid)
    assert await archiver().sync(rid)==first
    assert len(remote.uploads)==2 and b"synthetic original" in remote.uploads[0] and b"synthetic additional" in remote.uploads[1]
    message,=remote.collections
    block,=message["fileEntityBlock"]
    assert [d["downloadContent"] for d in block["documents"]]==["1","2"]
    assert [d["sourceFileName"] for d in block["documents"]]==["source.pdf","delivery.xlsx"]
    assert len({d["uniqueId"] for d in block["documents"]})==2
    assert store.get(rid)["archive_status"]=="collection_accepted"
    assert len((await archiver().query(rid))["remote_result"]["documents"])==2


@pytest.fixture
def single_original(bundle):
    store, rid, new_archiver, approve, transport = bundle
    record = store.get(rid)
    del record["originals"]
    store.save(record, "fixture_single_original")
    approve()
    return store, rid, new_archiver, transport


async def test_single_original_retries_interrupted_lookup_after_restart(single_original):
    store, rid, new_archiver, transport = single_original
    transport.fail = "search"
    with pytest.raises(ValueError):
        await new_archiver().sync(rid)
    assert store.get(rid)["archive_status"] == "awaiting_archive"
    assert "archive_key" not in store.get(rid)
    assert transport.created == 0
    transport.fail = None
    assert (await new_archiver().sync(rid))["archive_status"] == "collection_accepted"
    assert transport.created == len(transport.uploads) == len(transport.collections) == 1


@pytest.mark.parametrize("stage", ["create", "upload", "collection"])
async def test_single_original_keeps_uncertain_writes_reserved(single_original, stage):
    store, rid, new_archiver, transport = single_original
    transport.fail = stage
    with pytest.raises(ValueError, match="待核查"):
        await new_archiver().sync(rid)
    record = store.get(rid)
    assert record["archive_status"] == "archive_unknown"
    assert record["archive_receipt"]["filename"]
    counts = (transport.created, len(transport.uploads), len(transport.collections))
    transport.fail = None
    with pytest.raises(ValueError, match="禁止重复"):
        await new_archiver().sync(rid)
    assert counts == (transport.created, len(transport.uploads), len(transport.collections))


async def test_single_original_uses_reviewed_snapshot(single_original, monkeypatch):
    store, rid, new_archiver, transport = single_original
    source = Path(store.get(rid)["original_path"])
    original = source.read_bytes()
    search = HongyiClient._search
    async def refresh_source(client, *args):
        source.write_bytes(b"synthetic new version")
        return await search(client, *args)
    monkeypatch.setattr(HongyiClient, "_search", refresh_source)
    assert (await new_archiver().sync(rid))["archive_status"] == "collection_accepted"
    assert original in transport.uploads[0] and source.read_bytes() not in transport.uploads[0]


async def test_all_source_bytes_checked_before_any_remote_write(bundle):
    store,rid,archiver,approve,remote=bundle
    approve()
    Path(store.get(rid)["originals"][1]["original_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError,match="已变更"):
        await archiver().sync(rid)
    assert remote.searches==remote.created==0 and not remote.collections


async def test_new_service_resumes_confirmed_upload_after_read_only_failure(bundle):
    store,rid,archiver,approve,remote=bundle
    approve()
    remote.fail="second_search"
    with pytest.raises(ValueError):
        await archiver().sync(rid)
    row=store.get(rid)
    assert row["archive_status"]=="awaiting_archive" and len(row["archive_receipt"]["documents"])==1
    remote.fail=None
    result=await archiver().sync(rid)
    assert result["archive_status"]=="collection_accepted"
    assert len(remote.uploads)==remote.created==2 and len(remote.collections)==1


@pytest.mark.parametrize("stage",["upload","collection"])
async def test_ambiguous_outbound_write_is_never_repeated(bundle,stage):
    store,rid,archiver,approve,remote=bundle
    approve()
    remote.fail=stage
    with pytest.raises(ValueError):
        await archiver().sync(rid)
    counts=(len(remote.uploads),len(remote.collections),remote.created)
    assert store.get(rid)["archive_status"]=="archive_unknown"
    with pytest.raises(ValueError,match="禁止重复"):
        await archiver().sync(rid)
    assert counts==(len(remote.uploads),len(remote.collections),remote.created)


@pytest.mark.parametrize("source_count", [1, 2])
async def test_two_service_instances_race_at_first_document(bundle,monkeypatch,source_count):
    store,rid,archiver,approve,remote=bundle
    if source_count == 1:
        record = store.get(rid)
        del record["originals"]
        store.save(record, "fixture_single_original")
    approve()
    entered=0
    both=asyncio.Event()
    search=HongyiClient._search
    async def race(self,*args):
        nonlocal entered
        entered+=1
        if entered==2:
            both.set()
        if entered<=2:
            await asyncio.wait_for(both.wait(),2)
        return await search(self,*args)
    monkeypatch.setattr(HongyiClient,"_search",race)
    results=await asyncio.gather(archiver().sync(rid),archiver().sync(rid),return_exceptions=True)
    assert sum(isinstance(result,dict) for result in results)==1
    assert sum(isinstance(result,ValueError) for result in results)==1
    assert remote.created==len(remote.uploads)==source_count and len(remote.collections)==1
    assert store.get(rid)["archive_status"]=="collection_accepted"


async def test_path_replaced_during_upload_only_verified_bytes_sent(bundle,monkeypatch):
    store,rid,archiver,approve,remote=bundle
    approve()
    search=HongyiClient._search
    original=Path(store.get(rid)["originals"][0]["original_path"])
    async def replace_file(self,*args):
        original.write_bytes(b"replacement bytes during search")
        return await search(self,*args)
    monkeypatch.setattr(HongyiClient,"_search",replace_file)
    assert (await archiver().sync(rid))["archive_status"]=="collection_accepted"
    assert b"synthetic original" in remote.uploads[0]
    assert all(b"replacement bytes during search" not in upload for upload in remote.uploads)
