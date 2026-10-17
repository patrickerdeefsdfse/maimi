"""Hybrid scope tests. SQL cases use a unique disposable synthetic PG schema."""
import asyncio
import base64
import hashlib
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import enterprise_tasks as cloud, history_store
from app.conversation_router import decide_route

A = SimpleNamespace(user_no="LOCAL-FIXTURE-A", organization_codes=("LOCAL-TEST",))
B = SimpleNamespace(user_no="LOCAL-FIXTURE-B", organization_codes=("LOCAL-TEST",))
C = SimpleNamespace(user_no="LOCAL-FIXTURE-C", organization_codes=("OTHER-DEPT",))
W = cloud.LOCAL_WORKSPACE_ID


@pytest.fixture
def database(monkeypatch):
    dsn = os.getenv("IAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("local synthetic PostgreSQL URL not configured")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    schema = "hybrid_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[3] / "db/migrations").glob("*.sql")):
            body = re.sub(r"\bplatform\b", schema, path.read_text())
            conn.execute(body)

    class Cursor:
        def __init__(self, raw):
            self.raw = raw
        def __enter__(self):
            self.raw.__enter__()
            return self
        def __exit__(self, *args):
            return self.raw.__exit__(*args)
        def execute(self, query, params=None):
            return self.raw.execute(query.replace("platform.", schema + "."), params)
        def fetchone(self):
            return self.raw.fetchone()
        def fetchall(self):
            return self.raw.fetchall()

    class Connection:
        def __init__(self):
            self.raw = psycopg.connect(dsn, row_factory=dict_row)
        def __enter__(self):
            self.raw.__enter__()
            return self
        def __exit__(self, *args):
            return self.raw.__exit__(*args)
        def cursor(self):
            return Cursor(self.raw.cursor())

    monkeypatch.setattr(cloud, "_connect", Connection)
    monkeypatch.setattr(history_store, "_connect", Connection)
    monkeypatch.setenv("HYBRID_TEST_FIXTURES", "true")
    cloud.seed_local()
    try:
        yield Connection
    finally:
        # Only the schema created above by this test is ever removed.
        assert re.fullmatch(r"hybrid_test_[0-9a-f]{32}", schema)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def grant(database, actor=B, *, plugin=True, share=False):
    with database() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO platform.hybrid_workspace_grants(workspace_id,user_no,can_share) VALUES(%s,%s,%s)",
                    (W, actor.user_no, share))
        if plugin:
            cur.execute("INSERT INTO platform.hybrid_plugin_grants(workspace_id,plugin_key,version,user_no) VALUES(%s,%s,%s,%s)",
                        (W, cloud.PLUGIN_KEY, cloud.PLUGIN_VERSION, actor.user_no))


def upload(actor=A, request_id=None, operation="plugin", text="First synthetic line\nSecond synthetic line", **overrides):
    data = text.encode()
    payload = {"name": "fixture.txt", "content_base64": base64.b64encode(data).decode(),
               "sha256": hashlib.sha256(data).hexdigest(), "request_id": request_id or uuid4(),
               "operation": operation, "explicit_upload": True}
    if operation == "plugin":
        payload.update(plugin_key=cloud.PLUGIN_KEY, plugin_version=cloud.PLUGIN_VERSION)
    payload.update(overrides)
    body = cloud.UploadRequest(**payload)
    return body, cloud.upload_attachment(actor, W, body)


def task_from_upload(body, receipt, **overrides):
    values = {"request_id": body.request_id, "plugin_key": cloud.PLUGIN_KEY,
              "plugin_version": cloud.PLUGIN_VERSION, "input": "请摘要本次上传内容",
              "attachment_ids": [receipt["attachment_id"]]}
    values.update(overrides)
    return cloud.TaskRequest(**values)


def run_summary(body, actor=A):
    prepared = cloud.prepare_run(actor, W, body, "plugin")
    if "existing" in prepared:
        return prepared["existing"]
    cloud.finish_run(actor, prepared["run_id"], cloud.synthetic_summary(prepared["files"]))
    return cloud.read_run(actor, prepared["run_id"])


def test_personal_files_do_not_select_enterprise():
    result = decide_route(file_ids=["local-agent-receipt"])
    assert result.route == "dsh_chat"
    assert not result.enterprise_context_required
    with pytest.raises(ValueError):
        decide_route(route_mode="enterprise", file_ids=["local-agent-receipt"])
    assert decide_route(plugin_key="selected-plugin").enterprise_context_required


def test_workspace_requires_both_iam_department_and_explicit_grant(database):
    assert len(cloud.list_workspaces(A)["items"]) == 1
    assert cloud.list_workspaces(B)["items"] == []
    with pytest.raises(cloud.CloudError, match="workspace_access_denied"):
        cloud.list_plugins(B, W)
    grant(database, C)
    assert cloud.list_workspaces(C)["items"] == []
    with pytest.raises(cloud.CloudError, match="workspace_access_denied"):
        cloud.list_plugins(C, W)
    assert cloud.list_plugins(A, W)["items"][0]["simulation"] is True


def test_workspace_grant_does_not_grant_every_plugin(database):
    grant(database, B, plugin=False)
    assert len(cloud.list_workspaces(B)["items"]) == 1
    assert cloud.list_plugins(B, W) == {"items": []}
    with pytest.raises(cloud.CloudError, match="plugin_access_denied"):
        upload(actor=B)


def test_summary_executes_once_persists_across_connections_and_rejects_changed_retry(database):
    body, receipt = upload()
    task = task_from_upload(body, receipt)
    result = run_summary(task)
    assert result["status"] == "completed"
    assert result["simulation"] is True
    assert result["result"]["model_called"] is False
    assert result["result"]["files"][0]["lines"] == 2
    assert "First synthetic line" in result["result"]["text"]
    # Every store call opens a new PG connection; no process cache is involved.
    assert cloud.read_run(A, result["run_id"]) == result
    assert run_summary(task)["run_id"] == result["run_id"]
    with pytest.raises(cloud.CloudError, match="task_idempotency_conflict"):
        run_summary(task.model_copy(update={"input": "different task"}))
    with database() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM platform.hybrid_runs")
        assert cur.fetchone()["n"] == 1


def test_attachments_cannot_cross_employee_or_request_or_operation(database):
    grant(database)
    body, receipt = upload()
    with pytest.raises(cloud.CloudError, match="attachment_scope_denied"):
        run_summary(task_from_upload(body, receipt), actor=B)
    with pytest.raises(cloud.CloudError, match="attachment_scope_denied"):
        run_summary(task_from_upload(body, receipt, request_id=uuid4()))
    with pytest.raises(cloud.CloudError, match="attachment_scope_denied"):
        cloud.prepare_run(A, W, cloud.ChatRequest(request_id=body.request_id, input="hello",
                              attachment_ids=[receipt["attachment_id"]]), "chat")


def test_private_cloud_history_and_files_require_explicit_share_and_current_grant(database):
    grant(database)
    body, receipt = upload()
    result = run_summary(task_from_upload(body, receipt))
    conversation = result["conversation_id"]
    assert cloud.list_conversations(B, W) == {"items": []}
    with pytest.raises(cloud.CloudError, match="cloud_conversation_not_found"):
        cloud.read_conversation(B, conversation)
    with pytest.raises(cloud.CloudError, match="attachment_not_found"):
        cloud.read_attachment(B, receipt["attachment_id"])
    cloud.share_conversation(A, conversation, "workspace")
    assert cloud.read_conversation(B, conversation)["owned"] is False
    assert cloud.read_attachment(B, receipt["attachment_id"])["sha256"] == body.sha256
    with pytest.raises(cloud.CloudError, match="cloud_conversation_not_found"):
        cloud.share_conversation(B, conversation, "private")
    with database() as conn, conn.cursor() as cur:
        cur.execute("UPDATE platform.hybrid_plugin_grants SET enabled=FALSE WHERE user_no=%s", (B.user_no,))
    with pytest.raises(cloud.CloudError, match="plugin_access_denied"):
        cloud.read_conversation(B, conversation)
    cloud.share_conversation(A, conversation, "private")
    assert cloud.list_conversations(B, W) == {"items": []}


def test_conversation_cannot_change_plugin_scope_or_owner(database):
    grant(database)
    body, receipt = upload()
    result = run_summary(task_from_upload(body, receipt))
    with pytest.raises(cloud.CloudError, match="conversation_scope_immutable"):
        cloud.prepare_run(A, W, cloud.ChatRequest(request_id=uuid4(), input="personal now",
                          conversation_id=result["conversation_id"]), "chat")
    body_b, receipt_b = upload(B)
    with pytest.raises(cloud.CloudError, match="cloud_conversation_not_found"):
        run_summary(task_from_upload(body_b, receipt_b, conversation_id=result["conversation_id"]), B)


@pytest.mark.parametrize("changes,code", [
    ({"name": "../private.txt"}, "text_filename_required"),
    ({"name": "payload.exe"}, "text_filename_required"),
    ({"sha256": "0" * 64}, "file_hash_mismatch"),
    ({"content_base64": "!!!!"}, "utf8_text_required"),
    ({"content_base64": base64.b64encode(b"\xff").decode()}, "utf8_text_required"),
])
def test_upload_rejects_unsafe_or_corrupted_text_before_storage(changes, code):
    payload = {"name": "fixture.txt", "content_base64": "eA==", "sha256": hashlib.sha256(b"x").hexdigest(),
               "request_id": uuid4(), "operation": "chat", "explicit_upload": True, **changes}
    with pytest.raises(cloud.CloudError, match=code):
        cloud._file_bytes(cloud.UploadRequest(**payload))


def test_cloud_chat_uses_identity_bound_broker_and_keeps_its_own_context(database, monkeypatch):
    calls = []
    async def actor(_):
        return A
    async def broker(trusted_actor, messages, model, request_id):
        assert trusted_actor is A
        assert model == "deepseek-flash-v4.1"
        assert UUID(request_id)
        calls.append(messages)
        return {"text": "SYNTHETIC BROKER REPLY", "model": model, "usage": {"total_tokens": 1}}
    monkeypatch.setattr(cloud, "_actor", actor)
    monkeypatch.setattr(cloud, "model_runner", broker)
    app = FastAPI()
    app.include_router(cloud.router)
    with TestClient(app) as client:
        body, receipt = upload(operation="chat", text="CLOUD EXPLICIT ATTACHMENT")
        payload = {"request_id": str(body.request_id), "input": "first cloud turn", "attachment_ids": [receipt["attachment_id"]]}
        r = client.post(f"/v1/cloud/workspaces/{W}/chats", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["simulation"] is False
        assert client.post(f"/v1/cloud/workspaces/{W}/chats", json=payload).json()["run_id"] == r.json()["run_id"]
        assert len(calls) == 1
        second = client.post(f"/v1/cloud/workspaces/{W}/chats", json={
            "request_id": str(uuid4()), "conversation_id": r.json()["conversation_id"], "input": "next cloud turn"})
        assert second.json()["status"] == "completed"
        assert "CLOUD EXPLICIT ATTACHMENT" in calls[1][0]["content"]
        assert calls[1][-1]["content"] == "next cloud turn"
        assert client.post(f"/v1/cloud/workspaces/{W}/chats", json={
            **payload, "user_no": "OTHER EMPLOYEE"}).status_code == 422


def test_running_request_is_never_implicitly_replayed_after_process_interruption(database):
    body, receipt = upload()
    task = task_from_upload(body, receipt)
    first = cloud.prepare_run(A, W, task, "plugin")
    retry = cloud.prepare_run(A, W, task, "plugin")
    assert retry["existing"]["run_id"] == first["run_id"]
    assert retry["existing"]["status"] == "running"
    assert "files" not in retry


def test_legacy_enterprise_conversation_cannot_be_downgraded_by_ordinary_turn(database):
    row = history_store.record_chat_turn(user_no=A.user_no, organization_code="LOCAL-TEST",
        client_conversation_id="enterprise-fixture", input_text="synthetic enterprise input", assistant_text="old",
        provider_key="tokenkey", model="synthetic", route_mode="enterprise", plugin_key="synthetic-enterprise")
    with pytest.raises(ValueError, match="conversation scope is immutable"):
        history_store.assert_personal_conversation(user_no=A.user_no, client_conversation_id="enterprise-fixture")
    with pytest.raises(ValueError, match="conversation scope is immutable"):
        history_store.record_chat_turn(user_no=A.user_no, organization_code="OTHER-DEPT",
            client_conversation_id="enterprise-fixture", input_text="downgrade", assistant_text="new",
            provider_key="tokenkey", model="synthetic", route_mode="auto")
    detail = history_store.read_conversation(user_no=A.user_no, organization_code="LOCAL-TEST", conversation_id="enterprise-fixture")
    assert detail["route_mode"] == "enterprise"
    assert len(detail["messages"]) == 2


def test_legacy_personal_history_keeps_original_scope_after_department_move(database):
    for org in ("LOCAL-TEST", "OTHER-DEPT"):
        history_store.record_chat_turn(user_no=A.user_no, organization_code=org,
            client_conversation_id="personal-fixture", input_text="synthetic", assistant_text="reply",
            provider_key="tokenkey", model="synthetic", route_mode="auto")
    history_store.assert_personal_conversation(user_no=A.user_no, client_conversation_id="personal-fixture")
    detail = history_store.read_conversation(user_no=A.user_no, organization_code="OTHER-DEPT",
        conversation_id="personal-fixture", personal_across_organizations=True)
    assert len(detail["messages"]) == 4


def test_concurrent_identical_requests_admit_only_one_execution(database):
    body, receipt = upload()
    task = task_from_upload(body, receipt)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cloud.prepare_run(A, W, task, "plugin"), range(2)))
    fresh = [r for r in results if "run_id" in r]
    cached = [r for r in results if "existing" in r]
    assert len(fresh) == len(cached) == 1
    assert cached[0]["existing"]["run_id"] == fresh[0]["run_id"]


def test_attachments_cannot_cross_authorized_workspaces(database):
    other = str(uuid4())
    with database() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO platform.hybrid_workspaces(id,organization_code,name) VALUES(%s,'LOCAL-TEST','other')", (other,))
        cur.execute("INSERT INTO platform.hybrid_workspace_grants(workspace_id,user_no) VALUES(%s,%s)", (other,A.user_no))
        cur.execute("""INSERT INTO platform.hybrid_plugins(workspace_id,plugin_key,version,name,runner)
                    VALUES(%s,%s,%s,'same plugin','synthetic-summary-v1')""", (other,cloud.PLUGIN_KEY,cloud.PLUGIN_VERSION))
        cur.execute("INSERT INTO platform.hybrid_plugin_grants(workspace_id,plugin_key,version,user_no) VALUES(%s,%s,%s,%s)",
                    (other,cloud.PLUGIN_KEY,cloud.PLUGIN_VERSION,A.user_no))
    body, receipt = upload()
    with pytest.raises(cloud.CloudError, match="attachment_scope_denied"):
        cloud.prepare_run(A, other, task_from_upload(body, receipt), "plugin")


def test_cookie_cloud_mutation_rejects_cross_origin(monkeypatch):
    from starlette.requests import Request
    monkeypatch.setenv("IAM_PUBLIC_ORIGIN", "https://app.test")
    request = Request({"type": "http", "method": "POST", "path": "/v1/cloud/workspaces",
                       "headers": [(b"origin", b"https://foreign.test")]})
    with pytest.raises(Exception) as failure:
        asyncio.run(cloud._actor(request))
    assert failure.value.status_code == 403
    assert failure.value.detail == "cloud_origin_denied"
