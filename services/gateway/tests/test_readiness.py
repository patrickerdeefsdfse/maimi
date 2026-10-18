"""Exercise the exact ready() function without constructing company integrations.

No company request, credential, application startup or database writes occur.
The cursor preserves psycopg dict_row's column-name mapping (including duplicate
column collapse); the SQL remains the candidate's real SQL, not a test copy.
"""
import ast
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
BASE = HERE / "tests/fixtures/readiness_before.py"
TABLES = ("schema_migrations", "users", "conversations", "messages")


class HttpFailure(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def function_from(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
    selected = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ready")
    selected.decorator_list = []
    namespace = {"__package__": "readiness_fixture", "os": os, "HTTPException": HttpFailure}
    exec(compile(ast.Module(body=[selected], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["ready"]


class Cursor:
    def __init__(self, missing=(), migration=True, row_override=False):
        self.missing = set(missing)
        self.migration = migration
        self.row_override = row_override
        self.queries = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "to_regclass" in sql:
            fields = re.findall(r"to_regclass\('platform\.([a-z_]+)'\)(?:\s+AS\s+([a-z_]+))?", sql)
            if len(fields) != 4:
                raise AssertionError("all four dependency tables must be checked")
            self.result = {alias or "to_regclass": None if name in self.missing else "platform." + name
                           for name, alias in fields}
            if self.row_override is not False:
                self.result = self.row_override
        else:
            if "schema_migrations" in self.missing:
                raise RuntimeError("synthetic missing migration relation")
            if "SELECT 1 FROM platform.schema_migrations WHERE version = %s" not in sql:
                raise AssertionError("unexpected query")
            self.result = {"?column?": 1} if self.migration else None

    def fetchone(self):
        return self.result


class Connection:
    def __init__(self, cursor):
        self.value = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.value


@contextmanager
def dependencies(cursor, *, db_error=False, redis_error=False, env=None):
    package = types.ModuleType("readiness_fixture")
    package.__path__ = []
    history = types.ModuleType("readiness_fixture.history_store")
    def connect():
        if db_error:
            raise RuntimeError("synthetic secret must never be in the HTTP response")
        return Connection(cursor)
    history._connect = connect
    redis = types.ModuleType("redis")
    closed = []
    class RedisClient:
        def ping(self):
            if redis_error:
                raise RuntimeError("synthetic Redis failure")
            return True
        def close(self):
            closed.append(True)
    redis.from_url = lambda *a, **k: RedisClient()
    with patch.dict(sys.modules, {"readiness_fixture": package,
                                 "readiness_fixture.history_store": history, "redis": redis}), \
            patch.dict(os.environ, env or {}, clear=True):
        yield closed


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = staticmethod(function_from(HERE / "app/main.py"))

    def rejected(self, cursor, **kwargs):
        with dependencies(cursor, **kwargs):
            with self.assertRaises(HttpFailure) as raised:
                self.ready()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "dependencies not ready")

    def test_present_tables_migration_and_redis_ready(self):
        c = Cursor()
        with dependencies(c) as closed:
            self.assertEqual(self.ready(), {"status": "ready", "database": "ready", "redis": "ready"})
        self.assertEqual(closed, [True])
        self.assertEqual(len(c.queries), 2)

    def test_missing_schema_migrations_rejected_without_querying_missing_relation(self):
        c = Cursor(missing=["schema_migrations"])
        self.rejected(c)
        self.assertEqual(len(c.queries), 1)

    def test_missing_users_rejected(self):
        self.rejected(Cursor(missing=["users"]))

    def test_missing_conversations_rejected(self):
        self.rejected(Cursor(missing=["conversations"]))

    def test_missing_messages_rejected(self):
        self.rejected(Cursor(missing=["messages"]))

    def test_all_tables_missing_rejected(self):
        self.rejected(Cursor(missing=TABLES))

    def test_missing_required_migration_rejected(self):
        self.rejected(Cursor(migration=False))

    def test_empty_result_rejected(self):
        self.rejected(Cursor(row_override=None))

    def test_empty_dictionary_rejected(self):
        self.rejected(Cursor(row_override={}))

    def test_duplicate_unnamed_columns_rejected(self):
        self.rejected(Cursor(row_override={"to_regclass": "platform.messages"}))

    def test_missing_column_rejected(self):
        self.rejected(Cursor(row_override={k: "platform." + k for k in TABLES[1:]}))

    def test_tuple_result_rejected_in_dict_row_contract(self):
        self.rejected(Cursor(row_override=tuple("platform." + k for k in TABLES)))

    def test_database_failure_is_sanitized(self):
        self.rejected(Cursor(), db_error=True)

    def test_redis_failure_rejected(self):
        self.rejected(Cursor(), redis_error=True)

    def test_required_version_is_a_bound_parameter(self):
        c = Cursor()
        version = "synthetic.sql'; DROP TABLE ignored; --"
        with dependencies(c, env={"MIGRATIONS_REQUIRED_VERSION": version}):
            self.ready()
        self.assertEqual(c.queries[1][1], (version,))
        self.assertNotIn(version, c.queries[1][0])

    def test_baseline_false_ready_reproduced(self):
        old = function_from(BASE)
        for missing in ("users", "conversations", "messages"):
            with self.subTest(missing=missing), dependencies(Cursor(missing=[missing])):
                self.assertEqual(old()["status"], "ready")


if __name__ == "__main__":
    unittest.main()
