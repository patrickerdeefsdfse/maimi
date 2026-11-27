"""Private records per authenticated conversation and a company-wide order ledger."""
import json
import time
import uuid
from .store import Store


class CloudStore(Store):
    def business_approval(self, record):
        from .order_approvals import local_decision
        return local_decision(record)

    def __init__(self, root, employee, conversation):
        super().__init__(root)
        self.scope = (employee, conversation)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scopes (
                    id TEXT PRIMARY KEY, employee TEXT NOT NULL, conversation TEXT NOT NULL,
                    next_check INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS due_scopes ON scopes(next_check);
                CREATE INDEX IF NOT EXISTS record_archive_status ON records(
                    json_extract(data,'$.status'),json_extract(data,'$.archive_status'));
            """)

    def create(self, data):
        record = dict(data, id=uuid.uuid4().hex, revision=1)
        with self.connect() as db:
            db.execute("INSERT INTO records VALUES(?,?,?)", (record["id"], 1, json.dumps(record, ensure_ascii=False)))
            db.execute("INSERT INTO scopes(id,employee,conversation) VALUES(?,?,?)", (record["id"], *self.scope))
        return record

    def get(self, rid):
        with self.connect() as db:
            row = db.execute("""SELECT data FROM records JOIN scopes USING(id)
                WHERE id=? AND employee=? AND conversation=?""", (rid, *self.scope)).fetchone()
        if row is None:
            raise ValueError("当前会话中没有这份订单。")
        return json.loads(row[0])

    def pending(self, limit=8):
        # Reserve a bounded page across gateway replicas, including after restart.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            rows = db.execute("""SELECT scopes.id,employee,conversation FROM scopes JOIN records USING(id)
                WHERE next_check<=? AND json_extract(data,'$.status')='submitted'
                    AND json_extract(data,'$.archive_status')='awaiting_archive'
                    AND json_extract(data,'$.approval_decision.oracle_status')='S'
                    AND CAST(json_extract(data,'$.approval_decision.batch_number') AS TEXT)
                        =CAST(json_extract(data,'$.oracle_result.batch_number') AS TEXT)
                ORDER BY next_check,scopes.id LIMIT ?""", (now, limit)).fetchall()
            db.executemany("UPDATE scopes SET next_check=? WHERE id=?", ((now + 120, row[0]) for row in rows))
        return [dict(order_id=rid, employee=employee, conversation=conversation) for rid,employee,conversation in rows]

