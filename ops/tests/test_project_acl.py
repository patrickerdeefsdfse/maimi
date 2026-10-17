import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "services" / "gateway"))
from app import access_control  # noqa: E402


class _Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_obj


def test_project_membership_is_required(monkeypatch):
    monkeypatch.setattr(
        access_control,
        "_connect",
        lambda: _Connection([None]),
    )
    with pytest.raises(access_control.AccessDenied, match="project access denied"):
        access_control.authorize_enterprise_context(
            user_no="u1",
            organization_code="org1",
            project_key="p1",
            plugin_key=None,
            file_ids=[],
        )


def test_plugin_without_project_is_rejected():
    with pytest.raises(access_control.AccessDenied, match="project context"):
        access_control.authorize_enterprise_context(
            user_no="u1",
            organization_code="org1",
            project_key=None,
            plugin_key="plugin1",
            file_ids=[],
        )


def test_member_project_is_returned(monkeypatch):
    project = {
        "project_id": "00000000-0000-0000-0000-000000000001",
        "project_key": "p1",
        "project_name": "Project One",
        "role_key": "member",
    }
    monkeypatch.setattr(access_control, "_connect", lambda: _Connection([project]))
    assert access_control.authorize_enterprise_context(
        user_no="u1",
        organization_code="org1",
        project_key="p1",
        plugin_key=None,
        file_ids=[],
    ) == project
