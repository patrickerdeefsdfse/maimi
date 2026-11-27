from hashlib import sha256
from types import SimpleNamespace

import pytest

from order_mcp.cloud_store import CloudStore
from order_mcp.integrations import ArchiveService
from order_mcp.order_approvals import OrderApprovals


class Assigned:
    configured = True

    def __init__(self):
        self.by = {("M90000002","1001","测试主体"): "step-1"}
        self.calls = []
        self.gateway = SimpleNamespace(book_order=self.book_order)

    def book_order(self, batch_number, order_number, source=None):
        assert order_number == '1001'
        self.calls.append(batch_number)
        return {"status":"S", "batch_number":batch_number}

    def get(self, employee, number, entity):
        return self.by.get((employee, number, entity))

    def for_employee(self, employee, pairs):
        return {pair: self.get(employee, *pair) for pair in pairs}


def setup_order(tmp_path):
    owner = CloudStore(tmp_path,"M90000001","first-session")
    original = tmp_path/"source.pdf"
    original.write_bytes(b"synthetic original")
    record = owner.create({"kind":"order","status":"submitted","archive_status":"awaiting_approval",
        "order":{"business_entity":"测试主体","customer_po":"QA","customer_name":"测试客户","account_number":"1",
                 "lines":[{"item_code":"A","quantity":1}]},
        "oracle_result":{"order_number":"1001","batch_number":"9"},"business_key":"qa-key",
        "file_name":original.name,"original_path":str(original),"sha256":sha256(original.read_bytes()).hexdigest()})
    other = CloudStore(tmp_path,"M90000002","reviewer-session")
    return owner, record, other


def test_approval_assignment_rechecked_on_click_and_scoped(tmp_path):
    owner,r,assignee=setup_order(tmp_path)
    auth=Assigned()
    svc=OrderApprovals(assignee,auth)
    assert [x["order_id"] for x in svc.pending()["items"]] == [r["id"]]
    assert not OrderApprovals(owner,auth).pending()["items"]
    with pytest.raises(ValueError):
        OrderApprovals(owner,auth).detail(r["id"])
    preview=svc.detail(r["id"])
    with pytest.raises(ValueError):
        OrderApprovals(owner,auth).approve(preview["review_id"])
    auth.by.clear()
    with pytest.raises(ValueError):
        svc.approve(preview["review_id"])
    assert owner.get(r["id"])["archive_status"] == "awaiting_approval"


async def test_local_decision_wakes_archiver_and_repeated_delivery_uploads_once(tmp_path):
    owner,r,assignee=setup_order(tmp_path)
    auth = Assigned()
    svc=OrderApprovals(assignee,auth)
    calls=[]
    class ECM:
        def endpoint(self, key):
            assert key=="archive_upload"
            return {"idempotency_header":"Idempotency-Key"},{}
        async def request(self, key, values, source, archive_key):
            calls.append((source.read_bytes(),archive_key))
            return {"document_id":"QA-DOC-1", "collection_status":"archived"}
    archive=ArchiveService(SimpleNamespace(store=owner),ECM())
    assert not owner.pending()
    assert (await archive.sync(r["id"]))["archive_status"]=="awaiting_approval"
    assert not calls
    preview=svc.detail(r["id"])
    decision=svc.approve(preview["review_id"])
    assert decision["archive_status"]=="awaiting_archive"
    assert svc.approve(preview["review_id"])==decision
    assert auth.calls==["9"] and decision["oracle_status"]=="S"
    assert not svc.pending()["items"]
    assert [row["order_id"] for row in owner.pending()] == [r["id"]]
    assert (await archive.sync(r["id"]))["document_id"]=="QA-DOC-1"
    assert (await archive.sync(r["id"]))["document_id"]=="QA-DOC-1"
    assert len(calls)==1 and calls[0][0]==b"synthetic original"
    assert svc.approve(preview["review_id"])["archive_status"]=="archived"


def test_source_revision_and_decision_are_bound_to_reviewed_snapshot(tmp_path):
    owner,r,assignee=setup_order(tmp_path)
    auth=Assigned()
    svc=OrderApprovals(assignee,auth)
    preview=svc.detail(r["id"])
    auth.by[("M90000002","1001","测试主体")]="reassigned"
    with pytest.raises(ValueError, match="已变化"):
        svc.approve(preview["review_id"])
    preview=svc.detail(r["id"])
    r["order"]["lines"][0]["quantity"]=2
    owner.save(r,"fixture_changed")
    with pytest.raises(ValueError, match="已变化"):
        svc.approve(preview["review_id"])

@pytest.mark.parametrize("behavior,expected", [("error","rejected"),("timeout","unknown"),("wrong_batch","unknown")])
def test_oracle_book_failure_blocks_archive_and_replay_after_restart(tmp_path, behavior, expected):
    owner, record, reviewer = setup_order(tmp_path)
    assignment = Assigned()
    attempted = []

    def book(batch, number, source=None):
        assert number == "1001"
        attempted.append(batch)
        if behavior == "timeout":
            raise TimeoutError()
        return {"status":"E" if behavior == "error" else "S", "batch_number":batch if behavior == "error" else "999"}

    assignment.gateway.book_order = book
    reviews = OrderApprovals(reviewer, assignment)
    review_id = reviews.detail(record["id"])["review_id"]
    result = reviews.approve(review_id)
    assert not result["approved"] and result["booking_state"] == expected
    assert not owner.pending() and not owner.business_approval(owner.get(record["id"]))["approved"]
    recovered = OrderApprovals(CloudStore(tmp_path,*reviewer.scope), assignment)
    assert recovered.approve(review_id) == result
    assert recovered.pending()["items"][0]["booking_state"] == expected
    new_detail = recovered.detail(record["id"])
    assert not new_detail["can_approve"] and recovered.approve(new_detail["review_id"]) == result
    assert attempted == ["9"]


def test_legacy_local_decision_is_visible_for_reconciliation_and_never_booked(tmp_path):
    owner, record, reviewer = setup_order(tmp_path)
    record["approval_decision"] = {"source":"maimi", "actor":reviewer.scope[0], "review_id":"old", "approved_at":"2026-01-01"}
    record["archive_status"] = "awaiting_archive"
    owner.save(record, "legacy")
    assignment = Assigned()
    reviews = OrderApprovals(reviewer, assignment)
    assert not owner.business_approval(owner.get(record["id"]))["approved"]
    assert not owner.pending()
    assert reviews.pending()["items"][0]["booking_state"] == "legacy_unverified"
    detail = reviews.detail(record["id"])
    assert not detail["can_approve"]
    assert reviews.approve(detail["review_id"])["booking_state"] == "legacy_unverified"
    assert not assignment.calls


def test_approval_reservation_is_committed_before_erp_and_concurrent_review(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    owner, record, reviewer = setup_order(tmp_path)
    assignment = Assigned()
    started, release = Event(), Event()

    def delayed_book(batch, number, source=None):
        assert number == "1001"
        assignment.calls.append(batch)
        started.set()
        assert release.wait(5)
        return {"status":"S", "batch_number":batch}

    assignment.gateway.book_order = delayed_book
    reviews = OrderApprovals(reviewer, assignment)
    first, second = (reviews.detail(record["id"])["review_id"] for _ in range(2))
    with ThreadPoolExecutor() as workers:
        winner = workers.submit(reviews.approve, first)
        try:
            assert started.wait(5)
            assert owner.get(record["id"])["booking_attempt"]["state"] == "in_progress"
            assert reviews.approve(second)["booking_state"] == "in_progress"
            assert not owner.pending()
        finally:
            release.set()
        assert winner.result()["approved"]
    assert assignment.calls == ["9"]

@pytest.mark.parametrize("oracle_status", ["S", "E", None])
def test_documented_book_order_uses_batch_receipt_and_commits_success_only(oracle_status):
    from unittest.mock import MagicMock
    from order_mcp.oracle import OracleGateway

    service = OracleGateway(SimpleNamespace(oracle_environment="test"))
    service.target_status = lambda: {"write_ready": True, "target_matches":False, "environment":"test", "expected_target":"fixture"}
    connection, cursor = MagicMock(), MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    status_binding, message_binding = MagicMock(), MagicMock()
    status_binding.getvalue.return_value = oracle_status
    cursor.var.side_effect = [status_binding, message_binding]
    service.connect = lambda: connection
    cursor.fetchone.return_value = (2, 2, 1)
    assert service.book_order("12345","1001",service.booking_source(service.target_status())) == {"status":oracle_status,"batch_number":"12345"}
    cursor.callproc.assert_called_once_with("apps.cux_blue_ai_oe_order_pkg.book_order", keyword_parameters={
        "p_file_id":12345, "x_return_status":status_binding, "x_return_message":message_binding})
    if oracle_status == "S":
        connection.commit.assert_called_once()
    else:
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()


@pytest.mark.parametrize("counts", [(0,0,0),(2,1,1),(2,2,2),(2,0,1)])
def test_ambiguous_or_already_booked_batch_never_calls_procedure(counts):
    from unittest.mock import MagicMock
    from order_mcp.oracle import OracleGateway

    gateway = OracleGateway(SimpleNamespace(oracle_environment="test"))
    gateway.target_status = lambda: {"write_ready":True, "target_matches":False, "environment":"test", "expected_target":"fixture"}
    conn = MagicMock()
    cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = counts
    gateway.connect = lambda: conn
    assert gateway.book_order("9","1001",gateway.booking_source(gateway.target_status()))["status"] == "BATCH_REQUIRES_CHECK"
    cur.callproc.assert_not_called()


def test_booking_refuses_unstamped_or_foreign_receipt_on_other_environment():
    from unittest.mock import MagicMock
    from order_mcp.oracle import OracleGateway
    gateway = OracleGateway(SimpleNamespace(oracle_environment="production"))
    gateway.target_status = lambda: {"write_ready":True, "target_matches":True, "environment":"production",
                                    "expected_target":{"host":"fixture","port":1521,"service_name":"fixture"}}
    gateway.connect = MagicMock()
    for source in (None,"other-test-service"):
        assert gateway.book_order("2","3",source=source)["status"] == "BATCH_REQUIRES_CHECK"
    gateway.connect.assert_not_called()
