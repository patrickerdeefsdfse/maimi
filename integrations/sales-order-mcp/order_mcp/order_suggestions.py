"""Editable sales-order defaults and account-scoped ERP suggestions."""
import re
import unicodedata
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

ENTITIES_FOR_REVIEW = (
    "Example Trading G Ltd.", "Example Trading H Ltd.",
    "Example Trading I Ltd.", "Example Trading J Ltd.",
    "示例制造子有限公司", "示例制造甲有限公司", "示例制造丑有限公司",
    "示例制造乙有限公司", "示例制造丁有限公司", "示例制造戊有限公司",
    "示例制造己有限公司", "示例制造丙有限公司",
)
def _configured_review_entities(defaults):
    import json
    import os
    from pathlib import Path
    profile = os.getenv("ORDER_REVIEW_ENTITIES_FILE")
    if not profile:
        return defaults
    names = json.loads(Path(profile).read_text("utf-8"))
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("Invalid sales review entities")
    return tuple(names)


ENTITIES_FOR_REVIEW = _configured_review_entities(ENTITIES_FOR_REVIEW)


CURRENCIES = {
    "CNY": ("人民币","RMB","CN¥"), "USD": ("美元","美金","US$"), "HKD": ("港币","港元","HK$"),
    "EUR": ("欧元","€"), "GBP": ("英镑","£"), "JPY": ("日元","日圆"), "KRW": ("韩元","韩币","₩"),
    "INR": ("印度卢比","₹"), "THB": ("泰铢","฿"), "TWD": ("新台币","NT$"),
    "SGD": ("新加坡元","新币"), "CAD": ("加元","加拿大元"), "AUD": ("澳元","澳币"),
}

def currency_codes(text):
    text = unicodedata.normalize("NFKC", str(text or "")).upper()
    found = set()
    for code, alternatives in CURRENCIES.items():
        for word in (code, *alternatives):
            pattern = re.escape(word)
            if word.isascii() and word.isalpha():
                pattern = "(?<![A-Z])" + pattern + "(?![A-Z])"
            if re.search(pattern, text):
                found.add(code)
                break
    return found

def original_excerpt(path):
    """Best-effort original text, bounded separately from the main recognition service."""
    try:
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            return " ".join((page.extract_text() or "")[:4000] for page in reader.pages[:2])
        if path.suffix.lower() not in {".xlsx",".xlsm",".docx"}:
            return ""
        with zipfile.ZipFile(path) as archive:
            names = ("word/document.xml",) if path.suffix.lower()==".docx" else ("xl/sharedStrings.xml","xl/worksheets/sheet1.xml")
            parts = []
            for name in names:
                if name in archive.namelist() and archive.getinfo(name).file_size <= 2_000_000:
                    tree = ElementTree.fromstring(archive.read(name))
                    parts.extend((n.text or "")[:256] for n in tree.iter() if n.tag.endswith("}t"))
            return " ".join(parts)[:10000]
    except Exception:
        return ""

_PARTY_LABEL = r"(?:采购方|需方|买方|购方|客[户戶](?:名称|简称|名)?|采购单位|发件人|发送人|群名称)"
_ROLE = r"(?:商务(?:部|群)?|采购(?:部|群)?|采购员|业务(?:部|群)?|订单群|对接群)"
_NO_PARTY = {"甲方","乙方","客户","未知","不详","未识别","未提供","未填写","未指定","无","暂无",
             "采购","供应","订单","业务","商务","有限公司","销售","对接","合同","采购订单","N/A","UNKNOWN"}

def customer_keywords(names, text):
    """Extract bounded name evidence, keeping sender labels and mentions distinct."""
    phrases = list(names)
    text = unicodedata.normalize("NFKC", text[:12000])
    phrases.extend(re.findall(_PARTY_LABEL + r"\s*[:：=]\s*([^\s,，;；:：@<>]{2,48})", text))
    phrases.extend(re.findall(r"(?<![\w@])([\u3400-\u9fffA-Za-z0-9]{2,24}" + _ROLE + r")(?=\s|[:：,，;；@]|$)", text))
    phrases.extend(re.findall(r"(?m)^\s*([\u3400-\u9fffA-Za-z0-9]{2,24})\s*(?=@\S)",text))
    phrases.extend(re.findall(r"(?:客户(?:名称)?(?:是|叫|为)|(?:帮|给|为)(?=[\u3400-\u9fff]{2,20}(?:下单|订购|建单)))\s*([\u3400-\u9fffA-Za-z0-9]{2,32}?)(?=\s|[，,。]|下单|订购|建单|$)",text))
    phrases.extend(re.findall(r"(?:^|[\s，,:：;；])([\u3400-\u9fffA-Za-z0-9]{2,12})(?=(?:这个客户|这家客户|客户下单))",text))
    result=[]
    for value in phrases:
        if not isinstance(value,str):
            continue
        term=unicodedata.normalize("NFKC",value).strip(" \t\r\n:：,，;；")
        term=re.sub(_ROLE+r"$","",term).strip()
        if (2<=len(term)<=64 and term.upper() not in _NO_PARTY and not term.startswith(("@","请","帮我","帮忙"))
                and not term.isdecimal() and term not in ENTITIES_FOR_REVIEW and term not in result):
            result.append(term)
    return result[:6]

def set_currency_hints(record, chat, text):
    order = record["order"]
    explicit = currency_codes(chat)
    recognized = currency_codes(order.get("currency"))
    if not explicit and not recognized and not order.get("currency"):
        recognized = currency_codes(text)
    if explicit or recognized:
        candidates = explicit or recognized
        if len(candidates) == 1:
            value = next(iter(candidates))
            record["provenance"]["currency"] = "本轮聊天明确指定币种" if explicit else "原件识别币种"
            order["currency"] = value
            record["explicit_currency"] = True
        else:
            order["currency"] = None
            record.setdefault("header_conflicts", {})["currency"] = sorted(candidates)
        return
    hints = []
    if re.search("[가-힣]{2,}|韩[文语]",text): hints.append("KRW")
    if re.search("[ぁ-ヿ]{2,}|日[文语]",text): hints.append("JPY")
    if re.search("[ก-๙]{2,}",text): hints.append("THB")
    if re.search("[ऀ-ॿ]{2,}",text): hints.append("INR")
    if not hints and re.search("采购|交货|订单|数量|单价|收货|中文|汉语", text): hints.append("CNY")
    if not hints and re.search(r"\b(purchase order|delivery date|unit price|invoice|English)\b|英语|英文",text,re.I):
        hints.append("USD")
    if len(hints) == 1:
        record["language_currency"] = hints[0]

def reset_reference_values(record, changed):
    order, defaults = record["order"], record.get("business_defaults", {})
    if "business_entity" in changed and "order_type" not in changed:
        from .rules import ENTITIES
        old_type=ENTITIES.get(order.get("business_entity"),(None,None))[0]
        if old_type and order.get("order_type")==old_type:
            order["order_type"]=None
            order.get("evidence",{}).pop("order_type",None)
            defaults.pop("order_type",None)
            record["provenance"].pop("order_type",None)
    who = bool({"customer_name","account_number"} & changed)
    price_context = who or bool({"business_entity","currency"} & changed)
    for path, info in list(defaults.items()):
        if not info.get("reference") or path in changed:
            continue
        parts=path.split(".")
        if len(parts)==3:
            base=".".join(parts[:2])+"."
            inputs_changed=bool({base+"customer_item",base+"specification"} & changed)
            line_changed=inputs_changed or bool({base+"item_code",base+"unit",base+"price_basis"} & changed)
            invalid = (parts[-1]=="item_code" and (who or inputs_changed)) or (parts[-1] in {"unit_price","price_basis"} and (price_context or line_changed))
            target=order["lines"][int(parts[1])]
        else:
            invalid=who and path in {"account_number","customer_name","customer_po"}
            target=order
        if invalid:
            target[parts[-1]]=info.get("previous")
            defaults.pop(path)
            record["provenance"].pop(path,None)
    if who:
        record.pop("customer_candidates",None)

def enrich(service, record, edits=()):
    """Suggest within one draft; a reference outage must still leave an editable review."""
    if service.reference is None:
        return
    try:
        with service.reference.session() as ref:
            _fill_reference(service,ref,record,set(edits))
    except Exception:
        notice="Oracle 参考资料暂未能完整取得，已保留原件字段，可在复核表手工补录。"
        if notice not in record.setdefault("warnings",[]):
            record["warnings"].append(notice)

def customer_reference(ref, record, changes):
    from .rules import normalize_name
    order = record["order"]
    name, account = order.get("customer_name"), order.get("account_number")
    if {"customer_name", "account_number"} & record.get("header_conflicts", {}).keys():
        return None
    hints=record.get("customer_keywords") or []
    clean_name=customer_keywords([name], "")
    query=clean_name[0] if clean_name else ""
    hits=ref.customers(query, account_number=account) if account or query else []
    no_named_match=bool(hints and query and not any(normalize_name(query).casefold() in normalize_name(h["name"]).casefold() for h in hits))
    if not account and (not hits or not query or query != name or no_named_match):
        # A short sender alias is useful only when explicitly found in the ERP name.
        # Broad fuzzy matches on administrative words can point to unrelated parties.
        candidates={}
        for term in dict.fromkeys(clean_name+hints):
            choices=hits if term==query and hits else ref.customers(term)
            key=normalize_name(term).casefold()
            for choice in choices:
                if key in normalize_name(choice["name"]).casefold():
                    candidates.setdefault(choice["account_number"],choice)
            if len(candidates)>=12:
                break
        hits=list(candidates.values()) if candidates or not query or query!=name else hits
    first = hits[0] if hits else None
    matches = first and (not name or normalize_name(name).casefold() == normalize_name(first["name"]).casefold())
    manual_name = "customer_name" in changes or str(record["provenance"].get("customer_name", "")).startswith("人工复核")
    mismatch = bool(account and not first or name and manual_name and not matches)
    if account and name and not matches:
        alternatives = ref.customers(name)
        # Keep both sides visible if the account and the name point at different customers.
        same_top = bool(first and alternatives and alternatives[0]["account_number"] == account)
        mismatch = mismatch or not same_top
        keys = {hit["account_number"] for hit in alternatives}
        hits = alternatives + [hit for hit in hits if hit["account_number"] not in keys]
    current = [{"party_name": hit["name"], "account_number": hit["account_number"]} for hit in hits[:12]]
    previous = record.get("customer_candidates", [])
    if not (matches and len(current) == 1 and current[0] in previous and len(previous) > 1):
        record["customer_candidates"] = current
    if mismatch:
        record["customer_reference_issue"] = "请从候选列表选择匹配的 Oracle 客户名称和账号，或更正这两个字段。"
        return None
    record.pop("customer_reference_issue", None)
    return first

def history_basis(record, index, value):
    # A display default for an absent source tax basis cannot certify the basis of a historical price.
    default=record.get("business_defaults",{}).get(f"lines.{index}.price_basis",{})
    return "unknown" if default and not default.get("reference") else value or "unknown"

def refresh_item_roles(record):
    """Correct source-code provenance in still-editable drafts created on the previous service."""
    revised = False
    provenance = record.get("provenance", {})
    defaults = record.get("business_defaults", {})
    for index,line in enumerate(record["order"]["lines"]):
        base = f"lines.{index}."
        path = base+"item_code"
        source = provenance.get(path,"")
        if source.startswith("FastAPI 结果") and "「订购项目」" in source and line.get("item_code"):
            if not line.get("customer_item"):
                line["customer_item"]=line["item_code"]
                provenance[base+"customer_item"]=source
            line["item_code"]=None
            provenance.pop(path,None)
            revised=True
        # Previous releases stored the raw customer part as the pre-lookup internal item.
        if defaults.get(path,{}).get("reference"):
            line["item_code"]=None
            defaults.pop(path)
            provenance.pop(path,None)
            revised=True
        old_price=defaults.get(base+"unit_price",{})
        if old_price.get("reference"):
            line["unit_price"]=old_price.get("previous")
            defaults.pop(base+"unit_price",None)
            provenance.pop(base+"unit_price",None)
            revised=True
    return revised

def resolve_line_item(ref,record,index,account,changes,suggest):
    from .fastapi_rows import INTERNAL_EVIDENCE, original_model
    order=record["order"]
    line=order["lines"][index]
    base=f"lines.{index}."
    path=base+"item_code"
    options=record.setdefault("item_candidates",{})
    reasons=record.setdefault("item_match_issues",{})
    reasons.pop(path,None)
    if path in changes or str(record["provenance"].get(path,"")).startswith("人工复核"):
        return
    options.pop(path,None)
    # A verified local mapping counts as evidence, just like the live ERP cross reference.
    previous_source=record["provenance"].get(path,"")
    if line.get("item_code") and not previous_source and not (line.get("customer_item") or line.get("specification")):
        return
    hints=[]
    explicit=order.get("evidence",{}).get(path,{})
    internal=explicit.get("quote") if explicit.get("source")==INTERNAL_EVIDENCE else None
    if internal:
        match=ref.inventory_code(internal)
        if match:
            hints.append(("原件明示的麦米料号经 Oracle 组织 82 核实",[match]))
        else:
            reasons[path]="原件标注的麦米料号未在 Oracle 主组织查到，请核对。"
    if line.get("customer_item") and account:
        matches=ref.items(account,line["customer_item"],order.get("business_entity") or "")
        if matches:
            hints.append(("Oracle 当前客户的客户料号对应",[m["item_code"] for m in matches]))
    if line.get("item_code") and previous_source=="客户账号 + 客户物料号唯一精确匹配":
        hints.append(("客户对照表已核实的内部料号",[line["item_code"]]))
    if line.get("specification"):
        codes=ref.items_by_model(account,line["specification"])
        token=original_model(line["specification"])
        if not codes and token and token!=line["specification"]:
            codes=ref.items_by_model(account,token)
        if codes:
            hints.append(("Oracle 型号匹配",codes))
    if not hints:
        return
    candidates=sorted({v for _,codes in hints for v in codes})
    same=set.intersection(*(set(codes) for _,codes in hints))
    # If both sources were truncated, do not assume a one-code overlap proves uniqueness.
    unique = len(same)==1 and (any(len(codes)==1 for _,codes in hints) or all(len(codes)<8 for _,codes in hints))
    if unique and path not in reasons:
        code=next(iter(same))
        suggest(line,"item_code",path,code,"、".join(src for src,_ in hints)+"，请复核。")
        return
    options[path]=candidates
    if path not in reasons:
        reasons[path]=("客户料号、型号或原件标注的内部料号指向不同物料，请选择。" if not same
                       else "匹配到多个物料编码，请按实际规格选择后确认。")
    line["item_code"]=None
    record["provenance"].pop(path,None)
    record.get("business_defaults",{}).pop(path,None)

def _fill_reference(service,ref,record,changes):
    order=record["order"]
    notes=record["price_lookup_notes"]={}
    def suggest(target,field,path,value,reason):
        if value is None or path in changes or path in record.get("header_conflicts",{}):
            return
        if str(record["provenance"].get(path,"")).startswith("人工复核") or target.get(field)==value:
            return
        previous=target.get(field)
        record.setdefault("business_defaults",{})[path]={"value":value,"previous":previous,"reason":reason,"reference":True}
        target[field]=value
        record["provenance"][path]=reason
    if ref is not None:
        from .rules import ENTITIES
        mapping=ENTITIES.get(order.get("business_entity"))
        if mapping and not order.get("order_type") and (order.get("currency") or "CNY").upper() in {"CNY","RMB","人民币"}:
            suggest(order,"order_type","order_type",mapping[0],"按已核对的销售公司内销出货订单类型填写。")
        hit=customer_reference(ref,record,changes)
        if hit is None:
            for index in range(len(order["lines"])):
                resolve_line_item(ref,record,index,None,changes,suggest)
            return
        suggest(order,"customer_name","customer_name",hit["name"],"Oracle 客户名称候选，请核对名称和账号。")
        suggest(order,"account_number","account_number",hit["account_number"],"Oracle 客户名称对应的账号，请核对。")
        account=order.get("account_number")
        if not account:
            return
        if not order.get("customer_po") and "customer_po" not in changes and "customer_po" not in record.get("header_conflicts",{}):
            day=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
            occupied=ref.used_po_sequences(account,day)
            number=service.store.reserve_po(record["id"],account,day,occupied)
            suggest(order,"customer_po","customer_po",number,"原件无采购单号，按客户账号和北京时间当天流水预留。")
        for index,line in enumerate(order["lines"]):
            base=f"lines.{index}."
            resolve_line_item(ref,record,index,account,changes,suggest)
            if line.get("unit_price") is not None or not all((line.get("item_code"),order.get("business_entity"),order.get("customer_name"),order.get("currency"))):
                continue
            basis=history_basis(record,index,line.get("price_basis"))
            try:
                hit=ref.price(account,order["customer_name"],line["item_code"],order["business_entity"],order["currency"],
                              basis,unit=line.get("unit"),exclude_po=order.get("customer_po"))
            except Exception:
                notes[base+"unit_price"]="历史单价查询暂未完成，请重试查询或填写单价。"
                continue
            if hit:
                date=str(hit.get("price_date") or "")[:10]
                method="发货" if hit.get("latest_pickup_date") else "订单"
                reason=f"同客户、主体、料号和币种最近的历史单价（{method}{date}），请复核税价口径。"
                suggest(line,"unit_price",base+"unit_price",str(hit["price"]),reason)
                if hit.get("price_basis") in {"inclusive","exclusive"}:
                    suggest(line,"price_basis",base+"price_basis",hit["price_basis"],reason)
            else:
                notes[base+"unit_price"]="未查到同客户、主体、料号和币种的适用历史单价，请填写。"
