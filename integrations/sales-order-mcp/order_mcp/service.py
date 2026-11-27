import asyncio
import hashlib
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import Settings
from .oracle import OracleGateway, OraclePrecreateRejected, OracleCreateUncertain
from .fastapi_extract import FastAPIExtractor
from .fastapi_rows import mapped_orders
from .rules import ENTITIES, MasterData, validate, export_xml
from .schema import Issue, Line, Order
from .order_suggestions import customer_keywords, history_basis
from pydantic import ValidationError
from .store import Store, digest, now
from .order_suggestions import ENTITIES_FOR_REVIEW, enrich, reset_reference_values, set_currency_hints, original_excerpt, refresh_item_roles


def apply_business_defaults(record):
    """Materialize editable defaults once, keeping their source for the review card."""
    order = record["order"]
    defaults = record.setdefault("business_defaults", {})
    provenance = record.setdefault("provenance", {})
    changed = False

    def fill(target, field, path, value, reason):
        nonlocal changed
        previous = target.get(field)
        defaults[path] = {"value": value, "previous": previous, "reason": reason,
                          "previous_source": provenance.get(path)}
        provenance[path] = reason
        target[field] = value
        changed = True

    for index, line in enumerate(order.get("lines", [])):
        basis_path = f"lines.{index}.price_basis"
        if line.get("price_basis") in (None, "", "unknown") and not str(provenance.get(basis_path, "")).startswith("人工复核"):
            fill(line, "price_basis", basis_path, "inclusive", "原单未注明单价口径，默认含税，可修改。")
        if not str(line.get("request_date") or "").strip():
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            fill(line, "request_date", f"lines.{index}.request_date", today,
                 "原单未给交期，按北京时间当天补入，可修改。")

    kind = (order.get("order_type") or "").strip()
    original_currency = order.get("currency")
    currency = (original_currency or "").strip().upper()
    yuan = {"CNY", "RMB", "人民币"}
    if not kind and (not currency or currency in yuan):
        kind = ENTITIES.get(order.get("business_entity"), ("", ""))[0]
    domestic = "内销" in kind and "外销" not in kind
    independent = record.get("explicit_currency") or str(provenance.get("currency","")).startswith("人工复核")
    if domestic and original_currency != "CNY" and not independent and "currency" not in record.get("header_conflicts",{}):
        reason = "内销交易使用人民币。"
        if currency and currency not in yuan:
            reason += f"原币种为 {original_currency}，请核对人民币单价和金额，系统未折算金额。"
        fill(order, "currency", "currency", "CNY", reason)
    elif not domestic and "currency" in defaults and defaults["currency"]["reason"].startswith("内销"):
        old = defaults.pop("currency")
        if original_currency == old["value"]:
            order["currency"] = old["previous"]
            if old.get("previous_source"):
                provenance["currency"] = old["previous_source"]
            else:
                provenance.pop("currency", None)
        changed = True
    if not order.get("currency") and record.get("language_currency") and "currency" not in record.get("header_conflicts",{}) and not independent:
        fill(order,"currency","currency",record["language_currency"],"仅按文件语言推测币种，请核实后确认。")
    return changed


def revise_line_layout(record, layout):
    data = record["order"]
    original = data["lines"]
    if not isinstance(layout, list) or len(layout) > 500:
        raise ValueError("明细结构无效，请重新打开复核表。")
    kept = {}
    for new_index, old_index in enumerate(layout):
        if old_index is None:
            continue
        if type(old_index) is not int or not 0 <= old_index < len(original) or old_index in kept:
            raise ValueError("明细结构无效，请重新打开复核表。")
        kept[old_index] = new_index
    def remap(fields):
        output = {}
        for path, value in fields.items():
            bits = path.split(".", 2)
            if len(bits) == 3 and bits[0] == "lines" and bits[1].isdigit():
                old = int(bits[1])
                if old in kept:
                    output[f"lines.{kept[old]}.{bits[2]}"] = value
            else:
                output[path] = value
        return output
    data["lines"] = [Line().model_dump(mode="json") if i is None else original[i] for i in layout]
    data["evidence"] = remap(data.get("evidence", {}))
    for key in ("provenance", "business_defaults","item_candidates","item_match_issues"):
        record[key] = remap(record.get(key, {}))
    sources = record.get("sources") or []
    record["sources"] = [sources[i] if i is not None and i < len(sources) else None for i in layout]


class OrderService:
    def __init__(self, settings: Settings, provider=None, oracle=None, store=None):
        self.settings = settings
        self.store = store or Store(settings.data_dir)
        self.provider = provider or FastAPIExtractor.configured()
        self.oracle = oracle or OracleGateway(settings)
        self.master = MasterData(settings.master_data)
        from .sales_reference import SalesReference
        self.reference = SalesReference(self.oracle) if isinstance(self.oracle, OracleGateway) and settings.dsn else None

    async def extract_drafts(self, source, file_name, employee, display_name, *, allow_blank=False, chat_text="", source_text="", single_attachment=True):
        raw=source.read_bytes()
        digest_value=hashlib.sha256(raw).hexdigest()
        warnings = []
        try:
            extracted=await self.provider.extract(file_name,raw,employee,display_name)
        except (ValueError, TimeoutError):
            if not allow_blank:
                raise
            extracted={}
            warnings.append("本次自动识别失败，请按原件补录。")
        if not extracted.get("workbook"):
            warnings.append("识别结果工作簿暂不可用，请对照上传的原件复核。")
        rows=extracted.get("rows") or []
        if source_text and len(rows)<=1:
            row=dict(rows[0]) if rows else {}
            if not rows:
                warnings.append("识别服务未返回明细，已按原件可读文字提示候选，请核对。")
            row["原文"]=(str(row.get("原文") or "")[:1000]+"\n"+source_text[:6000]).strip()
            rows=[row]
        groups=mapped_orders(rows)
        if not groups:
            groups = [(Order(lines=[Line()]), {}, [])]
            warnings.append("识别服务未返回订单明细，已保留原件并提供空白草稿，请按原件录入。")
        drafts=[]
        excerpt = await asyncio.to_thread(original_excerpt,source) if len(groups)==1 and (
            not groups[0][0].currency or not groups[0][0].customer_name) else ""
        for order,provenance,positions in groups:
            resolved,master_provenance=self.master.resolve(order)
            draft={"kind":"order","status":"review","original_path":str(source),"file_name":file_name,
                   "sha256":digest_value,"raw":order.model_dump(mode="json"),"order":resolved.model_dump(mode="json"),
                   "provenance":provenance | master_provenance, "sources":positions, "model":"销售订单 FastAPI",
                   "warnings":warnings,"created_at":now()}
            fragments = rows
            visible = " ".join(str(fragments[n-1].get(k) or "")[:300] for n in positions for k in
                               ("原文语言","原文","行备注","头备注","物料描述","品名") if 0<n<=len(fragments))
            set_currency_hints(draft,chat_text,visible+" "+excerpt)
            names=[fragments[n-1].get(k) for n in positions if 0<n<=len(fragments)
                   for k in ("需方","买方","采购方","客户简称","发送人","发件人","群名称")]
            context="\n".join([visible,excerpt,source_text,chat_text if single_attachment else ""]) if len(groups)==1 else visible
            draft["customer_keywords"]=customer_keywords(names,context)
            drafts.append(draft)
        return drafts,extracted

    async def recognize(self, source, file_name, employee, display_name, *, chat_text="", source_text=""):
        drafts,extracted=await self.extract_drafts(source,file_name,employee,display_name,chat_text=chat_text,source_text=source_text)
        views=[]
        for draft in drafts:
            apply_business_defaults(draft)
            item=self.store.create(draft)
            views.append(self.review(item["id"]))
        import base64
        result = {"orders":views,"order_ids":[v["id"] for v in views]}
        if extracted.get("workbook"):
            result.update(workbook=base64.b64encode(extracted["workbook"]).decode("ascii"),
                          workbook_kind=extracted["workbook_kind"])
        return result

    def _current_record(self, rid, record=None):
        if record is None:
            record = self.store.get(rid)
        editable = record.get("status") in {"review","prepared"}
        changed = editable and apply_business_defaults(record)
        legacy = editable and record.get("reference_reviewed") and record.get("reference_rules")!=2
        if editable and record.get("reference_rules")!=2:
            if refresh_item_roles(record):
                changed=True
        if editable and self.reference and (legacy or not record.get("reference_reviewed")):
            enrich(self,record)
            record["reference_reviewed"]=True
            record["reference_rules"]=2
            changed=True
        if changed:
            record.pop("confirmation", None)
            record["status"] = "review"
            record = self.store.save(record, "business_defaults_applied")
        return record

    def review(self, rid):
        record = self._current_record(rid)
        if record.get("kind") != "order":
            raise ValueError("不是订单记录。")
        order = Order.model_validate(record["order"])
        normalized, issues = validate(order, record.get("provenance"))
        check_warehouse = getattr(self.reference, "warehouse_exists", None)
        warehouse = normalized.get("ship_org_code")
        if warehouse and callable(check_warehouse) and record.get("status") in {"review","prepared"}:
            try:
                exists = check_warehouse(warehouse)
            except Exception:
                exists = None
            if exists is False:
                issues.append(Issue(path="business_entity", code="warehouse_missing",
                    message=f"该销售公司指定的仓库 {warehouse} 在当前 Oracle 环境未建档，请 ERP 管理员核对发运组织。"))
        from .integrations import archive_state
        reviewed = {k: v for k, v in record.items() if k not in {"original_path","originals","raw","confirmation"}}
        if "originals" in record:
            reviewed["source_names"] = [doc["file_name"] for doc in record["originals"]]
        for field,values in record.get("header_conflicts",{}).items():
            issues.append(Issue(path=field,code="source_conflict",message="原件字段不一致，请核对后填写："+" / ".join(values)))
        if record.get("customer_reference_issue"):
            issues.append(Issue(path="customer_name",code="customer_mismatch",message=record["customer_reference_issue"]))
        for path,text in record.get("item_match_issues",{}).items():
            issues.append(Issue(path=path,code="item_match",message=text))
        price_notes=record.get("price_lookup_notes",{})
        for issue in issues:
            if issue.path in price_notes and issue.code=="required":
                issue.message=price_notes[issue.path]
        if record.get("segmented_po") and record["segmented_po"]==order.customer_po:
            issues.append(Issue(path="lines",code="line_limit",message="同一采购订单超过当前每单 500 行的上限，已分段展示。原 PO 整单尚不能提交，请核对原单并联系订单管理员。"))
        if "archive_status" in reviewed:
            reviewed["archive_status"] = archive_state(record)
        return reviewed | {
            "normalized": normalized, "issues": [i.model_dump() for i in issues],
            "business_entities": list(ENTITIES_FOR_REVIEW),
            "can_prepare": not any(i.severity == "error" for i in issues),
            "multi_line_confirmed": self.settings.multi_line_confirmed,
            "confirmation_required": True}

    def revise(self, rid, revision, changes, reason, *, human=False):
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
            raise ValueError("须说明修改依据。")
        if not isinstance(changes,dict) or not changes or type(revision) is not int:
            raise ValueError("更正内容无效，请检查字段和版本。")
        record = self.store.get(rid)
        if record.get("reference_rules")!=2 and (record.get("reference_reviewed") or record.get("model")=="销售订单 FastAPI"):
            record = self._current_record(rid,record)
        if record["revision"] != revision or record["status"] not in {"review", "prepared"}:
            raise ValueError("版本已变化或订单已提交，无法修改。")
        data = record["order"]
        edits = dict(changes)
        if "line_layout" in edits:
            if not human:
                raise ValueError("更改明细行须由员工在复核表中操作。")
            revise_line_layout(record, edits.pop("line_layout"))
        reset_reference_values(record, set(edits))
        for path, value in edits.items():
            pieces = path.split(".")
            if (isinstance(value,(dict,list,bool)) or isinstance(value,str) and len(value)>500):
                raise ValueError("更正内容无效，请检查字段和版本。")
            if path in Order.model_fields and path not in {"lines", "evidence"}:
                data[path] = value
            elif len(pieces) == 3 and pieces[0] == "lines" and pieces[1].isdigit():
                index = int(pieces[1])
                if index >= len(data["lines"]) or pieces[2] not in Line.model_fields or pieces[2] == "source_line":
                    raise ValueError("修改字段不存在。")
                data["lines"][index][pieces[2]] = value
            else:
                raise ValueError("更正内容无效，请检查字段和版本。")
            data.get("evidence", {}).pop(path, None)
            record.get("business_defaults", {}).pop(path, None)
            record["provenance"][path] = "人工复核：" + reason
            if human:
                record.get("header_conflicts",{}).pop(path,None)
        # Changing customer/PO/item must invalidate derived master-data results.
        if "customer_name" in changes:
            if "account_number" not in changes:
                data["account_number"] = None
                record["provenance"].pop("account_number", None)
        if {"customer_name", "account_number"} & changes.keys():
            for i, line in enumerate(data["lines"]):
                if f"lines.{i}.item_code" not in changes:
                    line["item_code"] = None
                    record["provenance"].pop(f"lines.{i}.item_code", None)
        for path in changes:
            if path.endswith((".customer_item",".specification")):
                base = path.rsplit(".", 1)[0]
                if base + ".item_code" not in changes:
                    data["lines"][int(path.split(".")[1])]["item_code"] = None
                    record["provenance"].pop(base + ".item_code", None)
        try:
            validated = Order.model_validate(data)
        except ValidationError:
            raise ValueError("更正内容无效，请检查字段和版本。") from None
        resolved, provenance = self.master.resolve(validated)
        record["order"] = resolved.model_dump(mode="json")
        record["provenance"].update(provenance)
        apply_business_defaults(record)
        if self.reference:
            enrich(self,record,edits)
            record["reference_reviewed"]=True
            record["reference_rules"]=2
        record.pop("confirmation", None)
        record.pop("submission_error", None)
        record["status"] = "review"
        self.store.save(record, "revised", {"changes": changes, "reason": reason})
        return self.review(rid)

    def generate_po(self, rid, revision):
        record = self.store.get(rid)
        if record["order"].get("customer_po"):
            raise ValueError("原订单已有 PO，不可覆盖。")
        if record["revision"] != revision:
            raise ValueError("版本已变化，请重新打开复核表。")
        account=record["order"].get("account_number")
        if not account:
            raise ValueError("请先选择客户并确认客户账号。")
        day=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        occupied=self.reference.used_po_sequences(account,day) if self.reference else set()
        value=self.store.reserve_po(rid,account,day,occupied)
        return self.revise(rid, revision, {"customer_po": value}, "原件无客户 PO，按业务截图规则生成内部代号；需提交人确认")

    async def lookup_customer(self, rid):
        record = self.store.get(rid)
        name = record["order"].get("customer_name")
        account = record["order"].get("account_number")
        if self.reference and (name or account):
            candidates = await asyncio.to_thread(self.reference.customers, name or "", account_number=account)
        elif self.reference and record.get("customer_keywords"):
            from .order_suggestions import customer_reference
            await asyncio.to_thread(customer_reference,self.reference,record,set())
            candidates=[{"name":v["party_name"],"account_number":v["account_number"]} for v in record.get("customer_candidates",[])]
        elif not name or not name.strip():
            raise ValueError("请先核对订单中的客户名称，再查询客户账号。")
        else:
            candidates = await asyncio.to_thread(self.oracle.customer, name)
        return {"order_id": rid, "revision": record["revision"], "customer_name": name,
                "candidates": candidates, "requires_confirmation": True,
                "message": "请核对候选客户名称和账号，再用更正操作录入当前订单。"}

    async def lookup_price(self, rid, line_index):
        record = self.store.get(rid)
        order = Order.model_validate(record["order"])
        if type(line_index) is not int or not 0 <= line_index < len(order.lines):
            raise ValueError("订单行编号无效，请从当前订单选择一行。")
        line = order.lines[line_index]
        if line.unit_price is not None:
            return {"order_id": rid, "revision": record["revision"], "line_index": line_index,
                    "price_present": True, "current_price": str(line.unit_price), "price_basis": line.price_basis,
                    "candidate": None, "requires_confirmation": True, "message": "本行已有单价，请在复核表中确认。"}
        item_code = line.item_code
        if not order.account_number or not order.customer_name or not item_code:
            raise ValueError("请先核对客户账号、客户名称和本行订购项目，再查询价格。")
        if self.reference:
            if not order.business_entity or not order.currency:
                raise ValueError("请先核对订单的销售公司和币种，再查询价格。")
            candidate = await asyncio.to_thread(self.reference.price, order.account_number, order.customer_name,
                item_code, order.business_entity, order.currency,history_basis(record,line_index,line.price_basis),
                unit=line.unit,exclude_po=order.customer_po)
        else:
            candidate = await asyncio.to_thread(self.oracle.price, order.account_number, order.customer_name, item_code)
        source = "oracle_history"
        if candidate is None:
            from .integrations import CompanyApis
            apis = CompanyApis(self.settings.integration_config)
            if not apis.config.get("price_list", {}).get("enabled"):
                return {"order_id": rid, "revision": record["revision"], "line_index": line_index,
                        "candidate": None, "requires_confirmation": True,
                        "message": "未找到匹配的历史订单价格，客户价目表接口尚未配置。请向提交人核对价格。"}
            candidate = await apis.request("price_list", {"account_number": order.account_number,
                "item_code": item_code, "business_entity": order.business_entity})
            source = "customer_price_list"
        try:
            price = Decimal(str(candidate["price"]))
            if not price.is_finite() or price <= 0:
                raise ValueError()
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise ValueError("查询未返回有效价格，请向提交人核对价格。") from None
        basis = candidate.get("price_basis", "unknown")
        candidate = {key: candidate[key] for key in ("price_date","booked_date", "latest_pickup_date", "price_list_name", "currency", "unit") if key in candidate}
        candidate.update(price=str(price), price_basis=basis if isinstance(basis, str) and basis in {"inclusive", "exclusive"} else "unknown",
                         source=source, requires_confirmation=True)
        return {"order_id": rid, "revision": record["revision"], "line_index": line_index,
                "candidate": candidate, "requires_confirmation": True,
                "message": "查询价格尚未填入订单。请核对价格及含税口径，再用更正操作录入并重新预览。"}

    def prepare_submission(self, rid, revision):
        record = self._current_record(rid)
        if record["revision"] != revision or record["status"] not in {"review", "prepared"}:
            raise ValueError("版本已变化或不可提交。")
        result = self.review(rid)
        if not result["can_prepare"]:
            raise ValueError("必填字段或金额校验尚未通过，请先处理 issues。")
        token = secrets.token_urlsafe(24)
        record["status"] = "prepared"
        record["confirmation"] = {"hash": hashlib.sha256(token.encode()).hexdigest(), "order_digest": digest(record["order"])}
        saved = self.store.save(record, "prepared")
        return {"order_id": rid, "revision": saved["revision"], "confirmation_token": token,
                "business_defaults": record.get("business_defaults", {}), "provenance": record.get("provenance", {}),
                "order": record["order"], "normalized": result["normalized"], "issues": result["issues"],
                "warnings": record["warnings"], "instruction": "向用户完整展示内容和警告，得到明确确认后才可提交。"}

    def export(self, rid, *, expected_revision=None):
        record = self._current_record(rid)
        if expected_revision is not None and record["revision"] != expected_revision:
            raise ValueError("版本已变化或不可提交。")
        order = Order.model_validate(record["order"])
        view = self.review(rid)
        normalized = view["normalized"]
        if not view["can_prepare"]:
            raise ValueError("字段或金额校验失败，不能生成可导入 XML。")
        xml = export_xml(order, normalized)
        target = self.settings.data_dir / "exports"
        target.mkdir(exist_ok=True)
        path = target / f"{rid}-v{record['revision']}.xml"
        path.write_text(xml, "utf-8")
        return {"path": str(path.resolve()), "xml": xml, "draft": record["status"] != "submitted"}

    async def submit(self, rid, revision, token, confirmed):
        if not confirmed:
            raise ValueError("必须先取得用户明确确认。")
        if not self.settings.enable_write or not self.settings.user_code:
            raise ValueError("Oracle 写入未配置启用，当前可以复核和导出。")
        record = self._current_record(rid)
        if record["status"] == "submitted":
            return record["oracle_result"]
        confirmation = record.get("confirmation", {})
        if record["revision"] != revision or record["status"] != "prepared" or not secrets.compare_digest(
                confirmation.get("hash", ""), hashlib.sha256(token.encode()).hexdigest()) or confirmation.get("order_digest") != digest(record["order"]):
            raise ValueError("确认凭据失效或版本变化，请重新预览确认。")
        order = Order.model_validate(record["order"])
        if len(order.lines) > 1 and not self.settings.multi_line_confirmed:
            raise ValueError("Oracle 多行 XML 合同尚未确认，管理员确认后启用 ORACLE_MULTI_LINE_CONFIRMED。")
        exported = self.export(rid, expected_revision=revision)
        key = digest([order.business_entity, order.account_number, order.customer_po])
        record.update(status="submitting", business_key=key)
        if isinstance(self.oracle, OracleGateway):
            record["submission_target"] = self.oracle.booking_source(self.oracle.target_status())
        record.pop("submission_receipt", None)
        record.pop("submission_error", None)
        record = self.store.begin_unique(key,record,"submission_started")
        try:
            result = await asyncio.to_thread(self.oracle.submit, exported["xml"], lambda d: self.store.checkpoint_order(key,rid,d))
            if not result.get("order_number") or not result.get("batch_number"):
                raise ValueError("缺少订单号或批次号。")
        except OraclePrecreateRejected as exc:
            record.update(status="review", submission_error=str(exc))
            record.pop("confirmation", None)
            self.store.settle_order(record, "rejected", "oracle_precreate_rejected")
            raise
        except BaseException as exc:
            # Including cancellation: backend may have committed. Never release the business key.
            reason = str(exc) if isinstance(exc,OracleCreateUncertain) else ""
            record.update(status="submission_unknown", error=reason+"建单结果待核查，请按批次/PO 检查 Oracle，禁止自动重试。")
            self.store.settle_order(record, "unknown", "submission_unknown")
            raise ValueError("Oracle 结果待核查，已锁定该客户 PO 防止重复创建。")
        record.update(status="submitted", oracle_result=result, archive_status="awaiting_approval")
        self.store.settle_order(record, "submitted", "submitted")
        return result | {"archive_status": "awaiting_approval", "message": "建单成功。请由 Oracle 当前分派的审核人核对订单并确认审核。"}

    def confirmation_result(self, rid, message=None):
        record = self.store.get(rid)
        result = {"order_id":rid,"revision":record["revision"],"status":record["status"]}
        if record["status"] == "cancelled":
            receipt=record.get("oracle_result") or {}
            return {**result, "order_number":receipt.get("order_number"), "batch_number":receipt.get("batch_number"),
                    "message":"这张 Oracle 销售订单已撤销。重新录入请上传原件并确认新草稿。"}
        if record["status"] == "submitted":
            receipt = record.get("oracle_result") or {}
            if not receipt.get("order_number"):
                return {**result,"status":"submission_unknown","message":"历史提交缺少订单号，请按采购单号核对 Oracle 回执。"}
            result.update(order_number=receipt["order_number"],archive_status=record.get("archive_status"),
                          message="Oracle 已建立销售订单。")
            if receipt.get("batch_number"):
                result["batch_number"] = receipt["batch_number"]
            return result
        if record["status"] in {"submitting","submission_unknown"}:
            receipt = record.get("submission_receipt") or self.store.submission_checkpoint(record)
            if str(receipt.get("batch_number","")).isdigit():
                result["batch_number"] = str(receipt["batch_number"])
            result["message"] = ("正在向 Oracle 提交，请查询本次回执。" if record["status"] == "submitting"
                                 else record.get("error") or "Oracle 建单结果待核查，已锁定该采购单，请按批次或采购单号核对回执。")
            if "batch_number" in result:
                result["message"] += f"Oracle 批次号 {result['batch_number']}。"
            return result
        if record["status"] not in {"review","prepared"}:
            raise ValueError("当前订单状态不支持确认，请联系订单管理员核对。")
        view = self.review(rid)
        if view["status"] not in {"review","prepared"}:
            return self.confirmation_result(rid)
        result.update(status="needs_input",revision=view["revision"],review=view,
                      message=message or view.get("submission_error") or "请核对当前复核表。")
        batch = (view.get("submission_receipt") or {}).get("batch_number")
        if batch:
            result["batch_number"] = batch
        return result

    async def confirm_order(self, rid, revision, changes):
        if type(revision) is not int or revision < 1 or not isinstance(changes,dict):
            raise ValueError("订单确认参数无效，请重新打开复核表。")
        record = self.store.get(rid)
        if record.get("kind") != "order":
            raise ValueError("不是订单记录。")
        if record["status"] not in {"review","prepared"}:
            return self.confirmation_result(rid)
        try:
            if record["revision"] != revision:
                raise ValueError("订单已更新，请核对最新复核表后再次确认。")
            if changes:
                edited=self.revise(rid, revision, changes, "提交人在复核表中修改并确认", human=True)
                revision += 1
                def displayed(order):
                    fields={k:v for k,v in order.items() if k not in {"lines","evidence"}}
                    for i,line in enumerate(order.get("lines",[])):
                        fields.update({f"lines.{i}.{k}":v for k,v in line.items()})
                    return fields
                old=displayed(record["order"])
                revised=displayed(edited["order"])
                if any(meta.get("reference") and path not in changes and revised.get(path)!=old.get(path)
                       for path,meta in edited.get("business_defaults",{}).items()):
                    return self.confirmation_result(rid,"已按更改重新匹配订单字段，请核对更新后的值，再确认建单。")
            view = self.review(rid)
            if view["revision"] != revision:
                raise ValueError("订单已更新，请核对最新复核表后再次确认。")
            if not view["can_prepare"]:
                reasons = [i["message"] for i in view["issues"] if i["severity"] == "error"]
                return self.confirmation_result(rid,"请更正复核表。" + " ".join(dict.fromkeys(reasons))[:360])
            if not self.settings.enable_write or not self.settings.user_code:
                raise ValueError("Oracle 写入尚未启用，请联系订单管理员。")
            if isinstance(self.oracle, OracleGateway) and not self.oracle.target_status()["write_ready"]:
                raise ValueError("Oracle 写入目标尚未配置就绪，请联系订单管理员。")
            prepared = self.prepare_submission(rid, revision)
            await self.submit(rid, prepared["revision"], prepared["confirmation_token"], True)
        except ValueError as exc:
            return self.confirmation_result(rid,str(exc))
        return self.confirmation_result(rid)

    async def query_submission(self, rid):
        record = self.store.get(rid)
        summary = self.confirmation_result(rid)
        if summary["status"] not in {"submitting","submission_unknown"}:
            return summary
        lookup = getattr(self.oracle,"created_receipt",None)
        if not callable(lookup) or not summary.get("batch_number") or record["revision"] != summary["revision"]:
            return summary
        try:
            receipt = await asyncio.to_thread(lookup,summary["batch_number"],record["order"],record.get("submission_target"))
        except Exception:
            receipt = None
        if receipt:
            previous = "unknown" if record["status"] == "submission_unknown" else "submitting"
            record.update(status="submitted",oracle_result=receipt,
                          archive_status="awaiting_approval")
            record.pop("error",None)
            try:
                self.store.settle_order(record,"submitted","creation_receipt_reconciled",previous_state=previous)
            except ValueError:
                # Another reader or the original submitter may already have recorded it.
                pass
        return self.confirmation_result(rid)
