"""Staff defaults, explicit revocation and mode confirmation in the gateway runtime."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import patch
from uuid import uuid4
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
from app import plugin_admin
from maimi_projects.service import Context, ProjectFailure, ProjectSkillService


class SalesAccessTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.assignments = assignments = {}

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, args=None):
                self.sql, self.args = sql, args

            def fetchone(self):
                value = assignments.get(self.args)
                return None if value is None else {"enabled": value}

            def fetchall(self):
                if 'FROM platform.enterprise_plugin_catalog p' in self.sql:
                    employee, history_employee = self.args
                    assert employee == history_employee
                    return [{"plugin_key": key, "version": plugin_admin.VERSION, "name": name,
                             "enabled": assignments.get((employee, key, plugin_admin.VERSION)),
                             "catalog_enabled": True, "kind": "skill", "state": "published"}
                            for key, name in plugin_admin.PROJECTS.items()]
                if any(table in self.sql for table in (
                        "platform.plugin_receipts", "platform.plugin_device_receipts", "platform.plugin_releases")):
                    return []
                raise AssertionError("Unexpected catalog query: "+self.sql)


        patcher = patch.object(plugin_admin, "_connect", lambda: nullcontext(SimpleNamespace(cursor=Cursor)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_staff_default_and_explicit_revocation_agree_with_inventory(self):
        for account in ("M92000003", "M92000004", "M99999999"):
            assert plugin_admin.project_granted(account, "sales-orders")
            assert next(r for r in plugin_admin.inventory(account) if r["plugin_key"] == "sales-orders")["effective"]
        key = ("M92000004", "sales-orders", plugin_admin.VERSION)
        self.assignments[key] = False
        assert not plugin_admin.project_granted("M92000004", "sales-orders")
        assert not next(r for r in plugin_admin.inventory("M92000004") if r["plugin_key"] == "sales-orders")["effective"]
        self.assignments[key] = True
        assert plugin_admin.project_granted("M92000004", "sales-orders")
        assert not plugin_admin.project_granted("", "sales-orders")
        assert not plugin_admin.project_granted("not-an-employee", "sales-orders")
        assert not plugin_admin.project_granted("M99999999", "employee-cards")

    async def test_catalog_recognition_is_direct_and_legacy_buttons_stay_human_only(self):
        calls, reads, saved = [], [], []

        async def authorize(ctx):
            assert ctx.user_no == "M99999999"

        async def call(action, arguments):
            calls.append((action, arguments))
            return {"orders": [], "workbook": "c3ludGhldGlj", "workbook_kind": "xlsx"}

        class State:
            async def get(self, ctx, key):
                return saved

            async def put(self, ctx, key, value):
                saved[:] = value

            def lock(self, ctx):
                class Lock:
                    async def __aenter__(self):
                        pass

                    async def __aexit__(self, *args):
                        pass
                return Lock()

        class Files:
            async def record(self, attachment_id):
                return {}, "native_message"

            async def read(self, ctx, attachment_id, maximum):
                reads.append((ctx.user_no, ctx.native_session_id, attachment_id))
                return {"name": "synthetic.png", "data": b"synthetic"}

            async def write(self, ctx, name, data, media_type):
                return {"attachment_id": "synthetic-result", "name": name}

        ctx = Context("M99999999", "session-" + str(uuid4()), "cloud", ("sales-orders",), "qa")
        svc = ProjectSkillService(authorize=authorize, state=State(), files=Files(), sales_order_call=call)
        catalog=await svc.catalog(ctx)
        assert "sales-orders" in {r["plugin_key"] for r in catalog}
        names={t['name'] for r in catalog if r['plugin_key']=='sales-orders' for t in r['tools']}
        assert 'recognize' in names and not {'recognize_test','recognize_production','submit'} & names
        recognized = await svc.invoke(ctx, "sales-orders", "recognize", {"attachment_id": "qa-input"})
        assert recognized['status']=='completed'
        assert recognized['project_result']['actions']==[]
        assert calls[0][0]=='recognize' and len(reads)==1
        with self.assertRaises(ProjectFailure):
            await svc.invoke(ctx, "sales-orders", "recognize_test", {"attachment_id": "qa-input"})
        await svc.invoke(replace(ctx, source="human"), "sales-orders", "recognize_test", {"attachment_id": "qa-input"})
        assert calls[1][0] == "recognize_test"
        await svc.invoke(replace(ctx, source="human"), "sales-orders", "recognize_production", {"attachment_id": "qa-input"})
        assert calls[2][0] == "recognize_production"
        assert reads == [(ctx.user_no, ctx.native_session_id, "qa-input")]*3
        self.assignments[(ctx.user_no, "sales-orders", plugin_admin.VERSION)] = False
        assert "sales-orders" not in {r["plugin_key"] for r in await svc.catalog(ctx)}
        with self.assertRaisesRegex(ProjectFailure, "project_not_authorized"):
            await svc.invoke(replace(ctx, source="human"), "sales-orders", "recognize_test", {"attachment_id": "qa-input"})
        assert len(calls) == 3


if __name__ == "__main__":
    main()
