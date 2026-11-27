"""Employee-bound, read-only lookup of the current Oracle business approval task."""
import re
from datetime import date, datetime
from decimal import Decimal

from .oracle import OracleGateway


_IDENT = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_EMPLOYEE = re.compile(r"M[0-9]{8}\Z")
_DEFAULT_COLUMNS = {
    "employee": "EMPLOYEE_CODE", "order_number": "ORDER_NUMBER", "business_entity": "BUSINESS_ENTITY",
    "task_id": "TASK_ID", "status": "TASK_STATUS", "step": "STEP_ID", "version": "ASSIGNMENT_VERSION",
}


def _identifier(value):
    if not isinstance(value, str) or not _IDENT.fullmatch(value.upper()):
        raise ValueError("Oracle 审核分派视图或字段配置无效。")
    return value.upper()


def _scalar(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
        return str(value).rstrip(" ")
    return None


class OracleAssignments:
    """Read only a configured owner.view whose rows refer to individual assigned employees.

    approval_assignments configures enabled, view (OWNER.NAME), open_value, and optional
    columns. A source confirmed to contain only currently authorized final sales
    order approvers uses dedicated_final_view=true. A mixed view must publish
    columns.final_stage and a final_value defined by the ERP owner. task_id and
    at least one of step/version must be stable source identifiers. Set an
    unavailable step or version column to null. The verified view resolves
    role expansion/delegation, excluding watchers and unrelated approval kinds.
    """

    def __init__(self, settings, config):
        self.gateway = OracleGateway(settings)
        self.environment = getattr(settings, "oracle_environment", "")
        conf = config.get("approval_assignments", {}) if isinstance(config, dict) else {}
        self.configured = isinstance(conf, dict) and conf.get("enabled") is True
        if not self.configured:
            return
        view = conf.get("view")
        if not isinstance(view, str) or view.count(".") != 1:
            raise ValueError("请配置已核验的 Oracle 审核分派视图及其所有者。")
        self.owner, self.view = (_identifier(part) for part in view.split("."))
        overrides = conf.get("columns", {})
        if not isinstance(overrides, dict) or set(overrides) - (_DEFAULT_COLUMNS.keys() | {"final_stage"}):
            raise ValueError("Oracle 审核分派字段配置无效。")
        columns = _DEFAULT_COLUMNS | overrides
        self.columns = {key: _identifier(value) for key, value in columns.items()
                        if not (key in ("step", "version") and value is None)}
        if not {"step", "version"}.intersection(self.columns) or len(set(self.columns.values())) != len(self.columns):
            raise ValueError("Oracle 审核分派需要独立的任务和步骤或版本字段。")
        self.open_value = conf.get("open_value")
        if not isinstance(self.open_value, str) or not self.open_value.strip() or len(self.open_value) > 128:
            raise ValueError("请配置经核验的 Oracle 待审批任务状态值。")
        self.final_value = conf.get("final_value")
        if "final_stage" in self.columns:
            if not isinstance(self.final_value, str) or not self.final_value.strip() or len(self.final_value) > 128:
                raise ValueError("请配置该视图中已核验的销售订单终审任务类型或标记。")
        elif conf.get("dedicated_final_view") is not True or self.final_value is not None:
            raise ValueError("请指定经确认的销售订单专用终审视图，或配置明确的销售订单终审任务过滤条件。")

    def get(self, employee, order_number, business_entity):
        pair = (order_number, business_entity)
        if not all(isinstance(value, str) for value in pair):
            return None
        return self.for_employee(employee, [pair]).get(pair)

    def for_employee(self, employee, pairs):
        if not self.configured or not isinstance(employee, str) or not _EMPLOYEE.fullmatch(employee):
            return {}
        requested = list(dict.fromkeys((number, entity) for number, entity in pairs
                                       if isinstance(number, str) and 0 < len(number) <= 128
                                       and isinstance(entity, str) and 0 < len(entity) <= 250))
        if not requested:
            return {}
        cols = self.columns
        projection = [cols[key] for key in ("employee", "order_number", "business_entity", "task_id")]
        projection.extend(cols.get(key, "NULL") for key in ("step", "version"))
        source = self.owner+"."+self.view
        found = {}
        ambiguous = set()
        try:
            with self.gateway.connect() as conn, conn.cursor() as cur:
                conn.call_timeout = 10000
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SELECT count(*) FROM all_views WHERE owner=:owner AND view_name=:view_name",
                            owner=self.owner, view_name=self.view)
                if cur.fetchone()[0] != 1:
                    raise ValueError("审核分派视图尚未发布或当前不可读取。")
                for pos in range(0, len(requested), 64):
                    chunk = requested[pos:pos+64]
                    binds = {"employee": employee, "open_value": self.open_value}
                    final_filter = ""
                    if "final_stage" in cols:
                        final_filter = f" AND {cols['final_stage']}=:final_value"
                        binds["final_value"] = self.final_value
                    params = []
                    for i, (number, entity) in enumerate(chunk):
                        binds[f"n{i}"], binds[f"e{i}"] = number, entity
                        params.append(f"(:n{i},:e{i})")
                    cur.execute(
                        f"SELECT {','.join(projection)} FROM {source} WHERE {cols['employee']}=:employee"
                        f" AND {cols['status']}=:open_value{final_filter} AND ({cols['order_number']},{cols['business_entity']})"
                        f" IN ({','.join(params)})", binds)
                    for row in cur:
                        actor, number, entity, task, step, version = map(_scalar, row)
                        pair = (number, entity)
                        if actor != employee or pair not in chunk or pair in ambiguous:
                            continue
                        ref = dict(source=source, environment=self.environment, employee=actor, order_number=number, business_entity=entity,
                                   task_id=task, step=step, version=version)
                        if not task or not (step or version) or (pair in found and found[pair] != ref):
                            ambiguous.add(pair)
                            found.pop(pair, None)
                        else:
                            found[pair] = ref
        except Exception:
            raise RuntimeError("暂时无法从 Oracle 确认当前审核分派，请稍后重试。") from None
        return found
