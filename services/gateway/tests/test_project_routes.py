import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import access_control, auth_middleware, main
from app.dsh_adapter import DshConversationResult


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "legacy")
    monkeypatch.setenv("AUTH_REQUIRED", "true")

    async def user(_cookies):
        return {"user_no": "acl-test-user", "organization_code": "acl-test-org"}

    monkeypatch.setattr(auth_middleware, "query_user", user)
    monkeypatch.setattr(main, "is_allowed", lambda *args: True)
    monkeypatch.setattr(main, "assert_personal_conversation", lambda **kwargs: None)
    with TestClient(main.app) as instance:
        yield instance


@pytest.mark.parametrize("endpoint", ["route", "run"])
def test_nonmember_is_denied_even_in_dsh_mode(client, monkeypatch, endpoint):
    def deny(**kwargs):
        raise access_control.AccessDenied("project access denied")

    monkeypatch.setattr(main, "authorize_enterprise_context", deny)
    response = client.post(f"/v1/chat/{endpoint}", json={
        "conversation_id": "acl-test", "input": "hello",
        "route_mode": "dsh", "project_key": "restricted-project",
    })
    assert response.status_code == 403


def test_member_route_is_accepted_but_runner_is_not_claimed(client, monkeypatch):
    monkeypatch.setattr(main, "authorize_enterprise_context", lambda **kwargs: {
        "project_key": "allowed-project", "role_key": "member",
    })
    body = {"conversation_id": "acl-test", "input": "hello", "project_key": "allowed-project"}
    assert client.post("/v1/chat/route", json=body).status_code == 200
    assert client.post("/v1/chat/run", json=body).status_code == 501


def test_ordinary_chat_skips_project_authorization(client, monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("ordinary chat must not query project permissions")

    async def reply(request):
        return DshConversationResult("completed", request.conversation_id, "CHAT_OK", [])

    monkeypatch.setattr(main, "authorize_enterprise_context", unexpected)
    monkeypatch.setattr(main.dsh_adapter, "run", reply)
    monkeypatch.setattr(main, "record_chat_turn", lambda **kwargs: "test")
    body = {"conversation_id": "ordinary-test", "input": "hello"}
    assert client.post("/v1/chat/route", json=body).status_code == 200
    response = client.post("/v1/chat/run", json=body)
    assert response.status_code == 200
    assert response.json()["text"] == "CHAT_OK"


def test_ordinary_chat_passes_stable_user_workspace_key(client, monkeypatch):
    """Conversations from one SSO identity share one opaque workspace root."""
    seen: list[str | None] = []

    async def reply(request):
        seen.append(request.workspace_key)
        return DshConversationResult("completed", request.conversation_id, "CHAT_OK", [])

    monkeypatch.setattr(main.dsh_adapter, "run", reply)
    monkeypatch.setattr(main, "record_chat_turn", lambda **kwargs: "test")

    for conversation_id in ("workspace-one", "workspace-two"):
        response = client.post(
            "/v1/chat/run",
            json={"conversation_id": conversation_id, "input": "hello"},
        )
        assert response.status_code == 200

    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0] is not None
    assert seen[0].startswith("user-")
    assert len(seen[0]) == len("user-") + 32


def test_user_workspace_key_changes_with_identity():
    first = SimpleNamespace(
        state=SimpleNamespace(user={"user_no": "u-1", "organization_code": "org-a"})
    )
    same = SimpleNamespace(
        state=SimpleNamespace(user={"userNo": "u-1", "organizationCode": "org-a"})
    )
    other = SimpleNamespace(
        state=SimpleNamespace(user={"user_no": "u-2", "organization_code": "org-a"})
    )
    missing = SimpleNamespace(state=SimpleNamespace(user=None))

    assert main._user_workspace_key(first) == main._user_workspace_key(same)
    assert main._user_workspace_key(first) != main._user_workspace_key(other)
    assert main._user_workspace_key(missing) is None


def test_authorization_storage_failure_is_closed(client, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "authorize_enterprise_context", broken)
    response = client.post("/v1/chat/run", json={
        "conversation_id": "acl-test", "input": "hello", "project_key": "p",
    })
    assert response.status_code == 503
    assert "database unavailable" not in response.text


def test_project_list_requires_sso(client, monkeypatch):
    async def logged_out(_cookies):
        return None

    monkeypatch.setattr(auth_middleware, "query_user", logged_out)
    assert client.get("/v1/projects").status_code == 403


def test_project_list_uses_sso_identity(client, monkeypatch):
    def listing(**kwargs):
        assert kwargs == {"user_no": "acl-test-user", "organization_code": "acl-test-org"}
        return [{"project_key": "p", "role_key": "member"}]

    monkeypatch.setattr(main, "list_projects", listing)
    response = client.get("/v1/projects?user_no=someone-else&organization_code=other")
    assert response.status_code == 200
    assert response.json() == [{"project_key": "p", "role_key": "member"}]
