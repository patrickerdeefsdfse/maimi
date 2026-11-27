"""Map the official export columns into reviewable orders."""
from decimal import Decimal, InvalidOperation
import re
from .schema import Evidence, Line, Order

HEADER={"business_entity":"业务实体","customer_name":"客户名称","account_number":"客户代号",
        "customer_po":"客户PO号","order_type":"订单类型","head_remark":"头备注","salesrep_name":"业务员",
        "currency":"币种","price_list_name":"价目表"}
LINE={"quantity":"订单数量","unit_price":"含税单价",
      "request_date":"请求日期","remark":"行备注"}

CUSTOMER_NUMBER_FIELDS = ("客户物料号","客户料号","客户物料编码","客户物料","客户产品编码",
                          "新物料编码","订购项目","产品料号","物料编码","产品编码","料号")
MODEL_FIELDS = ("规格型号","型号规格","型号","产品型号","客户型号","规格")
INTERNAL_NUMBER_FIELDS = ("麦米编码","麦米料号","麦米物料编码","麦格米特料号","我司料号","内部料号")
INTERNAL_EVIDENCE = "原件明示麦米内部料号"
# Restrict OCR fallback to a distinctive model-shaped token; preserve punctuation and all digits.
MODEL_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_.-]*[0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9.]+)+(?![A-Za-z0-9_-])")
GROUPED_PRICE = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?")

def number(text):
    if text is None:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None

def original_model(text):
    matches = set(MODEL_TOKEN.findall(str(text or "")))
    return matches.pop() if len(matches)==1 else None

def mapped_orders(rows):
    groups={}
    for index,row in enumerate(rows,1):
        def field(column):
            value=row.get(column)
            return str(value).strip() if isinstance(value,(str,int,float)) and not isinstance(value,bool) and str(value).strip() else None
        header={k:field(col) for k,col in HEADER.items()}
        key=tuple(header.values())
        if not header["customer_po"] or not header["customer_name"]:
            source_group = field("原单序号") or field("订单分组")
            key+=("source",source_group) if source_group else ("unresolved",index)
        if key not in groups:
            provenance={k:f"FastAPI 结果第 {index} 行「{HEADER[k]}」" for k,v in header.items() if v is not None}
            groups[key]=(Order(**header),provenance,[])
        order,provenance,positions=groups[key]
        position=len(order.lines)
        values={k:field(col) for k,col in LINE.items()}
        labels = {}
        for key, columns in (("customer_item",CUSTOMER_NUMBER_FIELDS),("specification",MODEL_FIELDS)):
            column = next((c for c in columns if field(c) and field(c).upper() not in {"/","-","—","无","未知","N/A"}),None)
            if column:
                values[key], labels[key] = field(column), column
        if not values.get("specification"):
            text = " ".join(field(c) or "" for c in ("型号规格描述","物料描述","品名","原文","行备注"))
            model = original_model(text)
            if model:
                values["specification"], labels["specification"] = model, "原文型号"
            elif labels.get("customer_item")=="订购项目" and MODEL_TOKEN.fullmatch(values["customer_item"]):
                values["specification"], labels["specification"] = values["customer_item"], labels["customer_item"]
        internal = next((field(c) for c in INTERNAL_NUMBER_FIELDS if field(c)), None)
        if internal:
            order.evidence[f"lines.{position}.item_code"]=Evidence(source=INTERNAL_EVIDENCE,quote=internal,confidence=1)
        values["unit"]=field("单位") or field("计量单位")
        basis_text = field("单价口径") or field("价格口径") or field("是否含税")
        explicit_basis = {"未税": "exclusive", "不含税": "exclusive", "否": "exclusive", "exclusive": "exclusive",
                          "含税": "inclusive", "是": "inclusive", "inclusive": "inclusive"}.get(basis_text)
        price_basis = explicit_basis or "unknown"
        values["unit_price"] = None
        for label, basis in (("含税单价", "inclusive"), ("未税单价", "exclusive"),
                             ("不含税单价", "exclusive"), ("单价", price_basis)):
            raw = field(label)
            if raw is None:
                continue
            normalized = raw.replace(",", "") if GROUPED_PRICE.fullmatch(raw) else raw
            price = number(normalized)
            if price is not None:
                values["unit_price"], labels["unit_price"], price_basis = price, label, basis
                break
            provenance.setdefault(f"lines.{position}.unit_price", "原识别数值待核：" + raw[:200])
        quantity = values["quantity"]
        values["quantity"] = number(quantity)
        if quantity is not None and values["quantity"] is None:
            provenance[f"lines.{position}.quantity"] = "原识别数值待核：" + quantity[:200]
        for name,val in values.items():
            if val is not None:
                label=labels.get(name) or LINE.get(name) or "单位"
                provenance[f"lines.{position}.{name}"]=f"FastAPI 结果第 {index} 行「{label}」"
        order.lines.append(Line(**values,price_basis=price_basis,
                                source_line=str(index)))
        positions.append(index)
    return list(groups.values())
