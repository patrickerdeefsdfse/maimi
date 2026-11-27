import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "orders.sqlite3"
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, record_id TEXT, at TEXT, event TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS submissions(business_key TEXT PRIMARY KEY, record_id TEXT, state TEXT, data TEXT);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, data):
        rid = uuid.uuid4().hex
        data = {**data, "id": rid, "revision": 1}
        with self.connect() as c:
            c.execute("INSERT INTO records VALUES(?,?,?)", (rid, 1, json.dumps(data, ensure_ascii=False)))
        return data

    def reserve_po(self, rid, account, day, occupied):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS sales_po_reservations(
                account TEXT NOT NULL, day TEXT NOT NULL, seq INTEGER NOT NULL, rid TEXT NOT NULL,
                PRIMARY KEY(account,day,seq), UNIQUE(rid,account,day))""")
            previous=db.execute("SELECT seq FROM sales_po_reservations WHERE rid=? AND account=? AND day=?",
                                (rid,account,day)).fetchone()
            if previous:
                return day + f"{previous[0]:02d}"
            latest=db.execute("SELECT COALESCE(MAX(seq),0) FROM sales_po_reservations WHERE account=? AND day=?",
                              (account,day)).fetchone()[0]
            local=db.execute("""SELECT DISTINCT json_extract(data,'$.order.customer_po') FROM records
                WHERE json_extract(data,'$.order.account_number')=? AND json_extract(data,'$.order.customer_po') LIKE ?""",
                (account,day+"%"))
            taken=(int(s[0][8:]) for s in local if s[0][8:].isascii() and s[0][8:].isdigit() and len(s[0][8:])<=12)
            seq=max(latest,max(taken,default=0),max(occupied,default=0))+1
            db.execute("INSERT INTO sales_po_reservations VALUES(?,?,?,?)",(account,day,seq,rid))
        return day + f"{seq:02d}"

    def get(self, rid):
        with self.connect() as c:
            row = c.execute("SELECT data FROM records WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ValueError("记录不存在。")
        return json.loads(row[0])

    def save(self, data, event, details=None):
        old = data["revision"]
        updated = {**data, "revision": old + 1}
        with self.connect() as c:
            result = c.execute("UPDATE records SET revision=?, data=? WHERE id=? AND revision=?",
                               (old + 1, json.dumps(updated, ensure_ascii=False), data["id"], old))
            if result.rowcount != 1:
                raise ValueError("记录已被另一操作修改，请重新获取后再操作。")
            c.execute("INSERT INTO audit(record_id,at,event,data) VALUES(?,?,?,?)",
                      (data["id"], now(), event, json.dumps(details or {}, ensure_ascii=False)))
        return updated

    def claim(self, key, rid):
        with self.connect() as c:
            try:
                c.execute("INSERT INTO submissions VALUES(?,?,?,?)", (key, rid, "submitting", "{}"))
            except sqlite3.IntegrityError:
                row = c.execute("SELECT record_id,state,data FROM submissions WHERE business_key=?", (key,)).fetchone()
                raise ValueError(f"该客户 PO 已提交或结果待核查，禁止重复建单。记录 {row[0]}，状态 {row[1]}。")

    def begin_unique(self, key, record, event):
        # One local commit for the business-key lock, state transition and audit.
        old = record["revision"]
        updated = dict(record, revision=old+1)
        with self.connect() as db:
            try:
                db.execute("INSERT INTO submissions VALUES(?,?,?,?)", (key,record["id"],"submitting","{}"))
            except sqlite3.IntegrityError:
                raise ValueError("同一业务单据已提交或正在核查，禁止重复建单或归档。") from None
            count=db.execute("UPDATE records SET revision=?,data=? WHERE id=? AND revision=?",
                             (old+1,json.dumps(updated,ensure_ascii=False),record["id"],old)).rowcount
            if count!=1:
                raise ValueError("记录已变化，请重新核对后再操作。")
            db.execute("INSERT INTO audit(record_id,at,event,data) VALUES(?,?,?,?)",(record["id"],now(),event,"{}"))
        return updated

    def submission(self, key, state, data):
        with self.connect() as c:
            c.execute("UPDATE submissions SET state=?,data=? WHERE business_key=?", (state, json.dumps(data, ensure_ascii=False), key))

    def submission_checkpoint(self, record):
        with self.connect() as db:
            row = db.execute("SELECT data FROM submissions WHERE business_key=? AND record_id=?",
                             (record.get("business_key"), record["id"])).fetchone()
        return json.loads(row[0]) if row else {}

    def checkpoint_order(self, key, rid, receipt):
        with self.connect() as db:
            count = db.execute("UPDATE submissions SET data=? WHERE business_key=? AND record_id=? AND state='submitting'",
                               (json.dumps(receipt,ensure_ascii=False),key,rid)).rowcount
            if count != 1:
                raise ValueError("本次提交结果待核查，请查询已有回执。")

    def settle_order(self, record, state, event, *, previous_state="submitting"):
        """Commit the draft, audit and PO claim together; retain the last confirmed checkpoint."""
        key, rid, rev = record["business_key"], record["id"], record["revision"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM submissions WHERE business_key=? AND record_id=? AND state=?",
                             (key,rid,previous_state)).fetchone()
            if not row:
                raise ValueError("提交状态已变化，请重新查询回执。")
            checkpoint = json.loads(row[0])
            receipt = {k: checkpoint[k] for k in ("stage","status") if k in checkpoint}
            if str(checkpoint.get("batch_number","")).isdigit():
                receipt["batch_number"] = str(checkpoint["batch_number"])
            record = {**record, "revision":rev+1, "submission_receipt":receipt}
            if state == "rejected":
                record.pop("business_key")
                db.execute("DELETE FROM submissions WHERE business_key=? AND record_id=?", (key,rid))
            else:
                db.execute("UPDATE submissions SET state=?,data=? WHERE business_key=? AND record_id=?",
                           (state,json.dumps(record.get("oracle_result", receipt),ensure_ascii=False),key,rid))
            changed = db.execute("UPDATE records SET revision=?,data=? WHERE id=? AND revision=?",
                                 (rev+1,json.dumps(record,ensure_ascii=False),rid,rev)).rowcount
            if changed != 1:
                raise ValueError("提交状态已变化，请重新查询回执。")
            db.execute("INSERT INTO audit(record_id,at,event,data) VALUES(?,?,?,?)",
                       (rid,now(),event,json.dumps(record.get("oracle_result",receipt),ensure_ascii=False)))
        return record

    def audit(self, rid):
        with self.connect() as c:
            rows = c.execute("SELECT at,event,data FROM audit WHERE record_id=? ORDER BY id", (rid,)).fetchall()
        return [{"at": at, "event": event, "data": json.loads(d)} for at, event, d in rows]
