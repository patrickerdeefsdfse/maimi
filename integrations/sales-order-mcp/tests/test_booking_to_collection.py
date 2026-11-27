"""Exercise the durable human decision through the V6 transport using synthetic backends."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from order_mcp.hongyi import HongyiClient
from order_mcp.integrations import ArchiveService, archive_state
from order_mcp.order_approvals import OrderApprovals
from test_hongyi import v6_config, v6_transport
from test_maimi_business_approval import Assigned, setup_order


@pytest.mark.parametrize(("reply", "state"), [
    ("received", "collection_accepted"), ("duplicate", "collection_duplicate"), ("filed", "archived"),
])
async def test_archive_job_uses_original_file_id_and_stable_receipt(tmp_path, monkeypatch, reply, state):
    monkeypatch.setenv("TEST_ECM_TOKEN","qa-ecm")
    monkeypatch.setenv("TEST_EDRMS_TOKEN","qa-archive")
    owner, record, reviewer = setup_order(tmp_path)
    source = b"test-pdf-bytes"
    Path(record["original_path"]).write_bytes(source)
    record["sha256"] = sha256(source).hexdigest()
    owner.save(record,"fixture_source")
    cfg = v6_config()
    cfg["destination"]["folders"][0]["business_entities"] = [record["order"]["business_entity"]]
    sent = []
    transport = v6_transport(sent, httpx.Response(200,json={"data":{"state":reply,"id":"receipt-qa"}}))
    archiver = ArchiveService(SimpleNamespace(store=owner), SimpleNamespace(hongyi=HongyiClient(cfg,transport)))
    assignments = Assigned()
    approval = OrderApprovals(reviewer,assignments)
    assert not owner.pending()
    assert (await archiver.sync(record["id"]))["archive_status"] == "awaiting_approval" and not sent
    detail = approval.detail(record["id"])
    assert approval.approve(detail["review_id"])["oracle_status"] == "S"
    assert len(owner.pending()) == 1
    receipt = await archiver.sync(record["id"])
    assert receipt["archive_status"] == state
    assert approval.approve(detail["review_id"])["archive_status"] == state
    assert await archiver.sync(record["id"]) == receipt and not owner.pending()
    assert len(assignments.calls) == 1
    collected, = [body for action,body in sent if action == "collection"]
    assert collected["fileEntityBlock"][0]["documents"][0]["downloadContent"] == receipt["document_id"]


def test_old_file_id_without_collection_receipt_is_only_an_upload():
    assert archive_state({"archive_status":"archived","archive_result":{"document_id":"123"}}) == "ecm_uploaded"
