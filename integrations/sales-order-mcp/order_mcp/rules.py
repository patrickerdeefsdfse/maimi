import json
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree as ET
from .schema import Order, Issue

ENTITIES = {
    "示例制造甲有限公司": ("8000_内销-出货订单", "381"),
    "示例制造乙有限公司": ("8001_内销-出货订单", "101"),
    "示例制造丙有限公司": ("8002_内销-出货订单", "141"),
    # ERP order type and configured default warehouse verified together in EXAMPLE_TEST.
    "示例制造丁有限公司": ("8003_内销-出货订单", "961"),
    "示例制造戊有限公司": ("8004_内销-出货订单", "X05"),
    "示例制造己有限公司": ("8005_内销-出货订单", "X06"),
}

def _configured_entities(defaults):
    import os
    profile = os.getenv("ORDER_ENTITY_DEFAULTS_FILE")
    if not profile:
        return defaults
    rows = json.loads(Path(profile).read_text("utf-8"))
    if not isinstance(rows, dict) or not all(
        isinstance(name, str) and name.strip() and isinstance(pair, list)
        and len(pair) == 2 and all(isinstance(v, str) and v.strip() for v in pair)
        for name, pair in rows.items()
    ):
        raise ValueError("Invalid sales entity defaults")
    return {name: tuple(pair) for name, pair in rows.items()}


ENTITIES = _configured_entities(ENTITIES)


def _alternate_types(defaults):
    import os
    path = os.getenv("ORDER_ALTERNATE_TYPES_FILE")
    if not path:
        return defaults
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(name, str) and name in ENTITIES and isinstance(values, list)
        and all(isinstance(v, str) and v.strip() for v in values)
        for name, values in data.items()
    ):
        raise ValueError("Invalid alternate order types")
    return data


ALTERNATE_ORDER_TYPES = _alternate_types({'示例制造乙有限公司': ['8001_内销-出货订单', '8918_赠品-出货订单', '8919_内销-退货订单', '8920_外销-出货订单', '8921_外销-退货订单', '8922_内销-CKD订单', '8923_外销-CKD订单']})


def normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))

class MasterData:
    def __init__(self, path: Path):
        self.data = json.loads(path.read_text("utf-8")) if path.exists() else {"customers": [], "items": []}

    def resolve(self, order: Order) -> tuple[Order, dict]:
        out = order.model_copy(deep=True)
        provenance = {}
        for name in ENTITIES:
            if normalize_name(name) == normalize_name(out.business_entity):
                out.business_entity = name
        customers = [r for r in self.data.get("customers", []) if normalize_name(r["name"]) == normalize_name(out.customer_name)]
        accounts = sorted(set(r["account_number"] for r in customers))
        if not out.account_number and len(accounts) == 1:
            out.account_number = accounts[0]
            provenance["account_number"] = "客户对照表唯一精确匹配"
        for i, line in enumerate(out.lines):
            candidates = [r for r in self.data.get("items", []) if r.get("customer_item") == line.customer_item
                          and r.get("account_number") == out.account_number and out.account_number]
            codes = sorted(set(r["item_code"] for r in candidates))
            if not line.item_code and len(codes) == 1:
                line.item_code = codes[0]
                provenance[f"lines.{i}.item_code"] = "客户账号 + 客户物料号唯一精确匹配"
        return out, provenance

def normalized_date(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("日期须包含明确的四位年份、月份和日期")

def validate(order: Order, provenance: dict | None = None) -> tuple[dict, list[Issue]]:
    provenance = provenance or {}
    issues = []
    def add(path, code, message, severity="error"):
        issues.append(Issue(path=path, code=code, message=message, severity=severity))
    def evidence(path, value):
        if value is None or path in provenance:
            return
        ev = order.evidence.get(path)
        if not ev or not ev.quote.strip():
            add(path, "evidence_missing", "缺少原文依据，须核对原件。", "warning")
        elif ev.confidence < .90:
            add(path, "unclear", "原文不够清晰，须核对原件。", "warning")
    for field in ("business_entity", "customer_name", "account_number", "customer_po", "currency"):
        value = getattr(order, field)
        if value is None or not value.strip():
            add(field, "required", "必填字段缺失。")
        evidence(field, value)
    if order.business_entity not in ENTITIES:
        from .order_suggestions import ENTITIES_FOR_REVIEW
        message = ("已选销售公司，其 Oracle 订单类型及发运组织尚未配置。" if order.business_entity in ENTITIES_FOR_REVIEW
                   else "请从销售公司名单中选择正确的业务实体。")
        add("business_entity", "unknown_entity", message)
    if normalize_name(order.customer_name) in {normalize_name(n) for n in ENTITIES}:
        add("customer_name", "buyer_seller", "客户是麦格米特公司，请确认是否把买卖双方填反。")
    if order.currency and order.currency.upper() not in {"CNY", "RMB", "人民币"}:
        add("currency", "currency_unsupported", "现有 Oracle XML 没有币种字段，外币订单须补充接口规则。")
    if not order.lines:
        add("lines", "empty", "没有有效订单行。")
    rows = []
    inclusive_totals = []
    for i, line in enumerate(order.lines):
        prefix = f"lines.{i}."
        for field in ("item_code", "quantity", "unit_price", "request_date"):
            v = getattr(line, field)
            if v is None or v == "":
                add(prefix + field, "required", "必填字段缺失；不得猜测或使用零代替。")
            evidence(prefix + field, v)
        evidence(prefix + "customer_item", line.customer_item)
        q, p, rate = line.quantity, line.unit_price, line.tax_rate
        if q is not None and q <= 0:
            add(prefix + "quantity", "quantity", "订单数量必须大于零。")
        if p is not None and p <= 0:
            add(prefix + "unit_price", "price", "单价必须大于零，免费订单需单独业务规则。")
        if rate is not None and not Decimal(0) <= rate <= Decimal(1):
            add(prefix + "tax_rate", "tax", "税率必须在 0 到 1 之间。")
        date_value = None
        if line.request_date:
            try:
                date_value = normalized_date(line.request_date)
            except ValueError:
                add(prefix + "request_date", "date", "无法确定交货日期，不能使用下单日期替代。")
        gross = None
        if p is not None:
            if line.price_basis == "inclusive":
                gross = p
            elif line.price_basis == "exclusive":
                if rate is None:
                    rate = Decimal("0.13")
                    add(prefix + "tax_rate", "default_tax", "按业务规则暂用 13% 税率，请确认。", "warning")
                gross = (p * (1 + rate)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            else:
                add(prefix + "price_basis", "tax_basis", "必须确认单价是含税还是未税。")
        if line.amount is not None and q is not None and p is not None:
            expected = None
            if line.amount_basis == line.price_basis and line.price_basis != "unknown":
                expected = p * q
            elif line.amount_basis == "inclusive" and gross is not None:
                expected = gross * q
            elif line.amount_basis == "exclusive" and line.price_basis == "inclusive" and rate is not None and rate >= 0:
                expected = p * q / (1 + rate)
            if expected is None:
                add(prefix + "amount_basis", "amount_basis", "金额的含税口径不明确，无法核对。", "warning")
            elif abs(expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) - line.amount) > Decimal("0.02"):
                add(prefix + "amount", "amount_mismatch", "数量 × 单价与原件行金额不符，请核对数量、税率和价格。")
        if q is not None and gross is not None:
            inclusive_totals.append((q * gross).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        rows.append({"item_code": line.item_code, "quantity": str(q) if q is not None else None,
                     "unit_selling_price": str(gross) if gross is not None else None, "request_date": date_value})
    if order.total_amount is not None:
        if order.total_basis == "inclusive" and len(inclusive_totals) == len(order.lines):
            if abs(sum(inclusive_totals) - order.total_amount) > Decimal("0.02"):
                add("total_amount", "total_mismatch", "所有订单行含税合计与原件总金额不一致，可能漏行或价格错误。")
        else:
            add("total_amount", "total_unverified", "总金额未完成核对，请确认总额口径和完整行数。", "warning")
    entity = ENTITIES.get(order.business_entity, (None, None))
    order_type = order.order_type or entity[0]
    # Only types shown in the supplied screenshot are enabled beyond the default.
    if order.order_type and order.order_type != entity[0]:
        if order.order_type not in ALTERNATE_ORDER_TYPES.get(order.business_entity, ()):
            add("order_type", "order_type", "该业务实体未配置此订单类型。")
        else:
            add("order_type", "special_order_type", "退货、外销、赠品及 CKD 的数量、币种和价格规则需 Oracle 确认，当前只支持内销出货。")
    if order.price_list_name:
        add("price_list_name", "price_list", "已记录价目表；现有 XML 不含价目表字段，实际按行单价提交。", "warning")
    return {"order_type": order_type, "ship_org_code": entity[1], "rows": rows}, issues

def export_xml(order: Order, normalized: dict) -> str:
    root = ET.Element("ROOT")
    for line, row in zip(order.lines, normalized["rows"]):
        node = ET.SubElement(root, "G_HEADER")
        values = {"OU_NAME": order.business_entity, "ACCOUNT_NUMBER": order.account_number,
                  "CUST_PO_NUMBER": order.customer_po, "ORDER_TYPE": normalized["order_type"],
                  "SHIP_ORG_CODE": normalized["ship_org_code"], "SEGMENT1": row["item_code"],
                  "ORDER_QUANTITY": row["quantity"], "UNIT_SELLING_PRICE": row["unit_selling_price"],
                  "REQUEST_DATE": row["request_date"], "HEAD_REMARK": order.head_remark,
                  "LINE_REMATK": line.remark, "RELEASE_ORG_CODE": normalized["ship_org_code"]}
        if order.salesrep_name:
            values["SALESREP_NAME"] = order.salesrep_name
        for key, value in values.items():
            ET.SubElement(node, key).text = value or ""
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
