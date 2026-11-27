"""Human final approval of the assigned Oracle order, followed by ERP booking."""
import json
import re
from datetime import datetime, timedelta, timezone

from .integrations import archive_state
from .store import digest, now


def order_snapshot(record):
    receipt = record["oracle_result"]
    return digest([record["id"], str(receipt["order_number"]), str(receipt.get("batch_number", "")),
                   receipt.get("booking_source"), record["order"], record["sha256"]])


def local_decision(record):
    decision = record.get("approval_decision") or {}
    batch = str((record.get("oracle_result") or {}).get("batch_number") or "")
    if (decision.get("oracle_status") != "S" or not batch or decision.get("batch_number") != batch
            or record.get("status") != "submitted" or decision.get("snapshot") != order_snapshot(record)):
        return {"approved": False, "source": "maimi"}
    return {"approved": True, "source": "maimi", "oracle_status": "S", "batch_number":batch, "checked_at": decision["approved_at"],
            "approved_by": decision["actor"], "order_number": str(record["oracle_result"]["order_number"])}


def booking_state(record):
    attempt = record.get("booking_attempt") or {}
    if attempt and attempt.get("state") != "succeeded":
        return attempt.get("state", "unknown")
    if record.get("approval_decision") and not local_decision(record)["approved"]:
        return "legacy_unverified"
    return "pending"


def review_message(state):
    return {"in_progress": "已受理 ERP 审核。若长时间未返回，请按原批次核查。",
            "unknown": "ERP 审核结果待核查，请按建单批次核实后处理。",
            "rejected": "ERP 返回审核失败，请联系管理员按建单批次核查。",
            "batch_requires_check": "建单批次与待审核订单未能唯一匹配，或订单已在 ERP 审核，请管理员核查。",
            "legacy_unverified": "历史审核仅有麦米回执，请管理员核查 Oracle 审核状态。"}.get(state, "")


class OrderApprovals:
    page_size = 20

    def __init__(self, store, assignments):
        self.store = store
        self.actor = store.scope[0]
        self.assignments = assignments

    def pending(self, after=""):
        if not self.assignments.configured:
            return {"configured": False, "items": []}
        if not isinstance(after, str) or (after and not re.fullmatch(r"[a-f0-9]{32}", after)):
            raise ValueError("审核分页参数无效。")
        # Select only routing keys until Oracle has confirmed the reviewer.
        items = []
        with self.store.connect() as db:
            while len(items) <= self.page_size:
                rows = db.execute("""SELECT id, json_extract(data,'$.oracle_result.order_number'),
                    json_extract(data,'$.order.business_entity'), json_extract(data,'$.booking_attempt.state'),
                    json_type(data,'$.approval_decision') FROM records WHERE id > ?
                    AND json_extract(data,'$.kind')='order' AND json_extract(data,'$.status')='submitted'
                    AND coalesce(json_extract(data,'$.approval_decision.oracle_status'),'')!='S'
                    AND coalesce(json_extract(data,'$.archive_status'),'awaiting_approval') IN ('awaiting_approval','awaiting_archive')
                    ORDER BY id LIMIT 64""", (after,)).fetchall()
                if not rows:
                    break
                keys = [(str(number), entity) for _, number, entity, _, _ in rows if number is not None and entity]
                assigned = self.assignments.for_employee(self.actor, keys) if keys else {}
                for rid, number, entity, attempt, previous in rows:
                    if assigned.get((str(number), entity)):
                        state = attempt or ("legacy_unverified" if previous else "pending")
                        items.append({"order_id": rid, "order_number": str(number), "business_entity": entity,
                                      "booking_state":state, "message":review_message(state)})
                after = rows[-1][0]
                if len(rows) < 64:
                    break
        page = items[:self.page_size]
        return {"configured": True, "items": page,
                "next": page[-1]["order_id"] if len(items) > len(page) else ""}

    def _assigned_order(self, rid):
        denied = "该订单当前未分派给你，或已经不在待审清单。"
        if not self.assignments.configured or not isinstance(rid, str) or not re.fullmatch(r"[a-f0-9]{32}", rid):
            raise ValueError(denied)
        with self.store.connect() as db:
            row = db.execute("""SELECT json_extract(data,'$.oracle_result.order_number'),json_extract(data,'$.order.business_entity')
                FROM records WHERE id=? AND json_extract(data,'$.kind')='order' AND json_extract(data,'$.status')='submitted'""",(rid,)).fetchone()
            if not row:
                raise ValueError(denied)
            assignment = self.assignments.get(self.actor, str(row[0]), row[1])
            if not assignment:
                raise ValueError(denied)
            record = json.loads(db.execute("SELECT data FROM records WHERE id=?", (rid,)).fetchone()[0])
        if record.get("archive_status") not in {"awaiting_approval","awaiting_archive"} or local_decision(record)["approved"]:
            raise ValueError(denied)
        return record, digest(assignment)

    def detail(self, rid):
        record, assignment = self._assigned_order(rid)
        preview = self.store.create({"kind": "sales_approval_preview", "order_id": rid, "actor": self.actor,
            "snapshot": order_snapshot(record), "assignment": assignment, "created_at": now()})
        state = booking_state(record)
        return {"review_id": preview["id"], "order_number": str(record["oracle_result"]["order_number"]),
                "order": record["order"], "source_name": record["file_name"], "booking_state":state,
                "can_approve": state == "pending", "message":review_message(state)}

    def approve(self, review_id):
        if not isinstance(review_id, str) or not re.fullmatch(r"[a-f0-9]{32}", review_id):
            raise ValueError("请重新打开审核详情。")
        preview = self.store.get(review_id)
        if preview.get("kind") != "sales_approval_preview" or preview.get("actor") != self.actor:
            raise ValueError("请重新打开审核详情。")
        # A late retry from the same reviewer may retrieve the original receipt.
        with self.store.connect() as db:
            receipt = db.execute("""SELECT data FROM records WHERE id=?
                AND json_extract(data,'$.booking_attempt.review_id')=?
                AND json_extract(data,'$.booking_attempt.actor')=?""",
                (preview["order_id"],review_id,self.actor)).fetchone()
        if receipt:
            return self._receipt(json.loads(receipt[0]))
        if datetime.now(timezone.utc) - datetime.fromisoformat(preview["created_at"]) > timedelta(hours=1):
            raise ValueError("审核预览已过期，请重新打开。")
        record, assignment = self._assigned_order(preview["order_id"])
        snapshot = order_snapshot(record)
        if assignment != preview["assignment"] or snapshot != preview["snapshot"]:
            raise ValueError("订单或审核分派已变化，请重新打开审核详情。")
        if booking_state(record) != "pending":
            return self._receipt(record)
        batch = str(record["oracle_result"].get("batch_number") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,37}", batch):
            raise ValueError("缺少原建单批次，请管理员核查后继续。")
        record["booking_attempt"] = {"state":"in_progress","actor":self.actor,"review_id":review_id,
                                     "started_at":now(),"batch_number":batch, "assignment":assignment, "snapshot":snapshot}
        # Reserve the batch durably before touching ERP, including across concurrent clients and restarts.
        environment = getattr(self.assignments, "environment", "")
        record = self.store.begin_unique("book:"+digest([environment,batch]), record, "oracle_booking_started")
        try:
            original = record["oracle_result"]
            reply = self.assignments.gateway.book_order(batch, str(original["order_number"]),
                                                         source=original.get("booking_source"))
            status = reply["status"] if reply.get("batch_number") == batch else None
        except Exception:
            # A disconnected session or failed commit cannot prove the procedure did not take effect.
            status = None
        if status != "S":
            record["booking_attempt"]["state"] = {"E":"rejected", "BATCH_REQUIRES_CHECK":"batch_requires_check"}.get(status, "unknown")
            saved = self.store.save(record, "oracle_booking_"+record["booking_attempt"]["state"],
                                    {"actor":self.actor,"review_id":review_id,"batch_number":batch})
            return self._receipt(saved)
        approved_at = now()
        record["booking_attempt"]["state"] = "succeeded"
        record["approval_decision"] = {"source":"maimi","actor":self.actor,"approved_at":approved_at,
                                       "snapshot":snapshot,"assignment":assignment,"review_id":review_id,
                                       "oracle_status":"S","batch_number":batch}
        record["archive_status"] = "awaiting_archive"
        saved = self.store.save(record, "business_approved", {"actor":self.actor,"snapshot":snapshot,
                                                              "review_id":review_id,"batch_number":batch,"oracle_status":"S"})
        return self._receipt(saved)

    @staticmethod
    def _receipt(record):
        result = {"approved":local_decision(record)["approved"], "order_number":str(record["oracle_result"]["order_number"]),
                  "archive_status":archive_state(record)}
        if result["approved"]:
            result.update(approved_at=record["approval_decision"]["approved_at"],oracle_status="S")
        else:
            state = booking_state(record)
            result.update(booking_state=state, message=review_message(state))
        return result
