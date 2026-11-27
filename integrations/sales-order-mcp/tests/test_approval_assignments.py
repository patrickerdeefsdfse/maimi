import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from order_mcp.approval_assignments import OracleAssignments


ACTOR = "M90000001"
OTHER = "M90000002"
CONFIG = {"enabled": True, "view": "MAIN.QA_VIEW", "open_value": "OPEN",
          "columns": {"final_stage": "KIND"}, "final_value": "SALES_FINAL"}


class Cursor:
    def __init__(self, db):
        self.cursor = db.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cursor.close()

    def execute(self, sql, binds=None, **kwargs):
        if sql != "SET TRANSACTION READ ONLY":
            self.cursor.execute(sql, kwargs if binds is None else binds)

    def fetchone(self):
        return self.cursor.fetchone()

    def __iter__(self):
        return iter(self.cursor)


class Connection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def cursor(self):
        return Cursor(self.db)


@pytest.fixture
def fixture(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE all_views(owner,view_name);
        INSERT INTO all_views VALUES('MAIN','QA_VIEW');
        CREATE TABLE QA_VIEW(EMPLOYEE_CODE,ORDER_NUMBER,BUSINESS_ENTITY,TASK_ID,TASK_STATUS,STEP_ID,ASSIGNMENT_VERSION,KIND);
    """)

    def adapter(config=CONFIG):
        lookup = OracleAssignments(SimpleNamespace(oracle_environment="test"), {"approval_assignments": config})
        monkeypatch.setattr(lookup.gateway, "connect", lambda: Connection(db))
        return lookup

    yield db, adapter
    db.close()


def test_bound_final_tasks_and_reassignment(fixture):
    db, adapter = fixture
    db.executemany("insert into QA_VIEW values(?,?,?,?,?,?,?,?)", [
        (ACTOR, "1", "甲", "t1", "OPEN", "review", 1, "SALES_FINAL"),
        (ACTOR, "2", "乙", "t2", "OPEN", "review", 1, "FYI"),
        (ACTOR, "3", "乙", "t3", "OPEN", "review", 1, "RETURN_FINAL"),
        (OTHER, "4", "甲", "t4", "OPEN", "review", 1, "SALES_FINAL"),
        (ACTOR, "5", "乙", "t5", "DONE", "review", 1, "SALES_FINAL"),
        (ACTOR, "6", "乙", "t6", "OPEN", "review", 1, "SALES_FINAL"),
        (ACTOR, "7", "甲", "t7", "OPEN", "review", 1, "SALES_FINAL"),
        (ACTOR, "7", "甲", "t8", "OPEN", "review", 1, "SALES_FINAL"),
        (ACTOR, "8", "甲", "t9", "OPEN", None, None, "SALES_FINAL"),
    ])
    lookup = adapter()
    keys = [("1","甲"),("2","乙"),("3","乙"),("4","甲"),("5","乙"),("6","甲"),("7","甲"),("8","甲")]
    keys += [(str(i),"乙") for i in range(20,100)]
    result = lookup.for_employee(ACTOR, keys)
    assert set(result) == {("1","甲")}
    first = result[("1","甲")]
    assert (first["task_id"], first["step"], first["version"]) == ("t1", "review", "1")
    db.execute("update QA_VIEW set ASSIGNMENT_VERSION=2 where TASK_ID='t1'")
    assert lookup.get(ACTOR, "1", "甲")["version"] == "2"
    db.execute("update QA_VIEW set EMPLOYEE_CODE=? where TASK_ID='t1'", (OTHER,))
    assert lookup.get(ACTOR, "1", "甲") is None
    assert lookup.get(OTHER, "1", "甲")["employee"] == OTHER


def test_dedicated_view_with_step_only_and_source_failure(fixture):
    db, adapter = fixture
    config = {"enabled": True, "view": "MAIN.QA_VIEW", "open_value": "OPEN",
              "dedicated_final_view": True, "columns": {"version": None}}
    lookup = adapter(config)
    db.execute("insert into QA_VIEW values(?,?,?,?,?,?,?,?)", (ACTOR,"1","甲","t1","OPEN","step-instance",None,None))
    assert lookup.get(ACTOR,"1","甲")["step"] == "step-instance"
    db.execute("delete from all_views")
    with pytest.raises(RuntimeError, match="无法从 Oracle 确认"):
        lookup.get(ACTOR,"1","甲")


@pytest.mark.parametrize("change", [
    {"view": "QA_VIEW"}, {"view": "MAIN.QA_VIEW@REMOTE"},
    {"columns": {"final_stage": "KIND;DROP X"}}, {"final_value": ""},
    {"columns": {}, "final_value": None},
    {"columns": {"step": None,"version": None,"final_stage":"KIND"}},
])
def test_unverified_finality_or_invalid_mapping_fails_closed(change):
    with pytest.raises(ValueError):
        OracleAssignments(None, {"approval_assignments": CONFIG | change})


def test_example_defaults_to_no_assignments():
    example = Path(__file__).resolve().parents[1] / "config" / "integrations.example.json"
    lookup = OracleAssignments(None, json.loads(example.read_text(encoding="utf-8")))
    assert not lookup.configured
    assert lookup.get(ACTOR, "1", "甲") is None
