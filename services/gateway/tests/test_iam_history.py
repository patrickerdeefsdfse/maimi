"""IAM history uses stable employee ownership, including department transfers.

Optional SQL tests use PostgreSQL pg_temp tables only. IAM_TEST_DATABASE_URL must
point at a local synthetic test database; no persistent business table is read
or modified. The adapter changes only the schema qualifier of production SQL.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import history_store, main


@pytest.mark.parametrize("provider,enabled", [("iam_direct", True), ("legacy", False)])
def test_history_routes_enable_stable_personal_scope_only_for_trusted_iam_actor(monkeypatch, provider, enabled):
    seen = []

    def listing(**kwargs):
        seen.append(kwargs)
        return []

    def detail(**kwargs):
        seen.append(kwargs)
        return {"messages": []}

    monkeypatch.setattr(main, "list_conversations", listing)
    monkeypatch.setattr(main, "read_conversation", detail)
    request = SimpleNamespace(state=SimpleNamespace(user={
        "user_no": "EMPLOYEE-A", "organization_code": "DEPT-B", "auth_provider": provider,
    }))
    asyncio.run(main.chat_history(request, limit=10))
    asyncio.run(main.chat_history_detail(request, "personal-a"))
    assert len(seen) == 2
    for call in seen:
        assert call["user_no"] == "EMPLOYEE-A"
        assert call["organization_code"] == "DEPT-B"
        assert call["personal_across_organizations"] is enabled


@pytest.fixture
def sql_store(monkeypatch):
    dsn = os.getenv("IAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("local synthetic PostgreSQL URL not configured")
    import psycopg
    from psycopg.rows import dict_row

    class Cursor:
        def __init__(self, cursor):
            self.raw = cursor

        def __enter__(self):
            self.raw.__enter__()
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def execute(self, query, params):
            return self.raw.execute(query.replace("platform.", "pg_temp."), params)

        def fetchone(self):
            return self.raw.fetchone()

        def fetchall(self):
            return self.raw.fetchall()

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            self.raw.__enter__()
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def cursor(self):
            return Cursor(self.raw.cursor())

    def connect():
        raw = psycopg.connect(dsn, row_factory=dict_row)
        with raw.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE users (id text, external_user_no text);
                CREATE TEMP TABLE organizations (id text, code text);
                CREATE TEMP TABLE projects (id text, project_key text, display_name text);
                CREATE TEMP TABLE conversations (
                    id uuid, client_conversation_id text, user_id text, organization_id text,
                    project_id text, plugin_key text, route_mode text, title text,
                    provider_key text, model text, last_activity_at timestamptz DEFAULT now(),
                    expires_at timestamptz, created_at timestamptz DEFAULT now(), archived_at timestamptz
                );
                CREATE TEMP TABLE messages (id uuid, conversation_id uuid, role text,
                    content jsonb, created_at timestamptz DEFAULT now());
                INSERT INTO users VALUES ('user-a', 'EMPLOYEE-A'), ('user-b', 'EMPLOYEE-B');
                INSERT INTO organizations VALUES ('org-a', 'DEPT-A'), ('org-b', 'DEPT-B');
                INSERT INTO projects VALUES ('project-a', 'enterprise-project-a', 'Synthetic project');
            """)
            rows = [
                ("personal-a", "user-a", "org-a", None, None, "auto"),
                ("personal-a-empty-plugin", "user-a", "org-a", None, "", "dsh"),
                ("enterprise-a-old", "user-a", "org-a", None, None, "enterprise"),
                ("enterprise-a-current", "user-a", "org-b", None, None, "enterprise"),
                ("plugin-a-old", "user-a", "org-a", None, "plugin-a", "auto"),
                ("project-a-old", "user-a", "org-a", "project-a", None, "dsh"),
                ("personal-b", "user-b", "org-a", None, None, "auto"),
            ]
            for name, owner, org, project, plugin, route in rows:
                conversation_id = uuid.uuid4()
                cur.execute("""INSERT INTO conversations (id, client_conversation_id, user_id,
                    organization_id, project_id, plugin_key, route_mode, title, provider_key, model)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'synthetic','synthetic')""",
                            (conversation_id, name, owner, org, project, plugin, route, name))
                cur.execute("""INSERT INTO messages (id,conversation_id,role,content)
                    VALUES (%s,%s,'user',jsonb_build_object('text',%s::text))""",
                            (uuid.uuid4(), conversation_id, "SYNTHETIC " + owner))
        return Connection(raw)

    monkeypatch.setattr(history_store, "_connect", connect)


def test_iam_personal_history_survives_department_transfer_without_project_expansion(sql_store):
    rows = history_store.list_conversations(user_no="EMPLOYEE-A", organization_code="DEPT-B",
                                             personal_across_organizations=True)
    assert {r["conversation_id"] for r in rows} == {
        "personal-a", "personal-a-empty-plugin", "enterprise-a-current",
    }
    detail = history_store.read_conversation(user_no="EMPLOYEE-A", organization_code="DEPT-B",
                                             conversation_id="personal-a", personal_across_organizations=True)
    assert detail["messages"][0]["content"] == {"text": "SYNTHETIC user-a"}
    for name in ("enterprise-a-old", "plugin-a-old", "project-a-old", "personal-b"):
        assert history_store.read_conversation(user_no="EMPLOYEE-A", organization_code="DEPT-B",
            conversation_id=name, personal_across_organizations=True) is None


def test_unknown_primary_org_allows_own_personal_history_but_no_enterprise_history(sql_store):
    rows = history_store.list_conversations(user_no="EMPLOYEE-A", organization_code="",
                                             personal_across_organizations=True)
    assert {r["conversation_id"] for r in rows} == {"personal-a", "personal-a-empty-plugin"}


def test_cross_employee_lookup_cannot_read_another_employees_personal_history(sql_store):
    rows = history_store.list_conversations(user_no="EMPLOYEE-B", organization_code="DEPT-B",
                                             personal_across_organizations=True)
    assert {r["conversation_id"] for r in rows} == {"personal-b"}
    assert history_store.read_conversation(user_no="EMPLOYEE-B", organization_code="DEPT-B",
        conversation_id="personal-a", personal_across_organizations=True) is None


def test_legacy_history_org_scope_remains_unchanged(sql_store):
    rows = history_store.list_conversations(user_no="EMPLOYEE-A", organization_code="DEPT-B")
    assert {r["conversation_id"] for r in rows} == {"enterprise-a-current"}
