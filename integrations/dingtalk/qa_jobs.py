"""Bounded private Q&A with pull-only results, no stored webhook or automatic resends."""
import asyncio
import os
import re
import time
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from routing import Refused, Route

QUEUE_TTL=90
CALL_LIMIT=100
RESULT_TTL=1800
PAGE_CHARS=1200
STATE={'queued':'等待中','running':'处理中','finished':'已完成','uncertain':'结果待核对，不会自动重试','expired':'已过期，未执行'}


class Jobs:
    def __init__(self, connect, key, dispatch):
        if len(key)!=32:
            raise ValueError("JOB_KEY_REQUIRED")
        self.connect=connect
        self.cipher=AESGCM(key)
        self.dispatch=dispatch

    def crypt(self, job, route, field, data, *, decrypt=False):
        aad="\n".join(("maimi-im-job/v1",str(job),route.employee,route.binding_generation,
                       route.receipt_key,route.conversation_key,field)).encode()
        if decrypt:
            raw=bytes(data)
            return self.cipher.decrypt(raw[:12],raw[12:],aad).decode()
        iv=os.urandom(12)
        return iv+self.cipher.encrypt(iv,data.encode(),aad)

    def submit_or_read(self, db, route, text, now):
        """Caller has already locked the employee binding and claimed this inbound receipt."""
        records=(route.employee,route.binding_generation,route.conversation_key)
        if text=="任务":
            rows=db.execute("""SELECT job_id,state FROM maimi_im.question_job
                WHERE employee=%s AND generation=%s AND conversation_key=%s AND retain_until>%s
                ORDER BY created_at DESC,job_id DESC LIMIT 6""",(*records,now)).fetchall()
            if not rows:return False,"本私聊最近没有可查任务。"
            return False,"最近任务：\n"+"\n".join(f"{job} · {STATE[state]}" for job,state in rows)
        if text=="结果" or text.startswith("结果 "):
            spec=text.split()
            if len(spec) not in (2,3) or not re.fullmatch(r"[0-9a-f-]{36}",spec[1]) or (len(spec)==3 and not re.fullmatch(r"[1-9][0-9]?",spec[2])):
                return False,"请输入：麦米 结果 任务编号 [页码]"
            try:job=uuid.UUID(spec[1])
            except ValueError:return False,"任务编号格式不正确。"
            row=db.execute("""SELECT state,answer,receipt_key FROM maimi_im.question_job WHERE job_id=%s
                AND employee=%s AND generation=%s AND conversation_key=%s AND retain_until>%s""",(job,*records,now)).fetchone()
            if not row:return False,"此私聊没有该任务，或结果已过期。"
            state,encrypted,original_receipt=row
            if state!='finished':return False,"任务 "+str(job)+" · "+STATE[state]
            original=Route(route.employee,route.conversation_key,original_receipt,route.binding_generation)
            result=self.crypt(job,original,"answer",encrypted,decrypt=True)
            page=int(spec[2]) if len(spec)==3 else 1
            pages=(len(result)+PAGE_CHARS-1)//PAGE_CHARS
            if page>pages:return False,f"这项结果共有 {pages} 页。"
            part=result[(page-1)*PAGE_CHARS:page*PAGE_CHARS]
            return False,f"第 {page}/{pages} 页\n{part}"+(f"\n下一页：麦米 结果 {job} {page+1}" if page<pages else "")
        # Serialize admission across both APs; short transaction only.
        db.execute("SELECT pg_advisory_xact_lock(1060112)")
        total,own=db.execute("""SELECT count(*),count(*) FILTER (WHERE employee=%s)
            FROM maimi_im.question_job WHERE state IN ('queued','running')""",(route.employee,)).fetchone()
        if own>=2 or total>=32:
            return False,"问答队列已满，请先查看已有任务。"
        job=uuid.uuid4()
        db.execute("""INSERT INTO maimi_im.question_job(job_id,receipt_key,generation,employee,conversation_key,
                 state,created_at,deadline,retain_until,question) VALUES(%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s)""",
                 (job,route.receipt_key,route.binding_generation,route.employee,route.conversation_key,
                  now,now+QUEUE_TTL,now+RESULT_TTL,self.crypt(job,route,"question",text)))
        return True,f"已收件。查询回复请发：麦米 结果 {job}\n也可发送“麦米 任务”查看最近任务。"

    def claim(self,now):
        with self.connect() as db:
            row=db.execute("""SELECT job_id,employee,conversation_key,receipt_key,generation,question FROM maimi_im.question_job
                WHERE state='queued' AND deadline>%s ORDER BY created_at,job_id LIMIT 1 FOR UPDATE SKIP LOCKED""",(now,)).fetchone()
            if row is None:return None
            job,employee,conversation,receipt,gen,encrypted=row
            route=Route(employee,conversation,receipt,str(gen))
            db.execute("UPDATE maimi_im.question_job SET state='running',started_at=%s,question=NULL WHERE job_id=%s",(now,job))
            return job,route,bytes(encrypted)

    def finish(self,job,route,result):
        with self.connect() as db:
            state="uncertain" if result is None else "finished"
            encrypted=None if result is None else self.crypt(job,route,"answer",result)
            changed=db.execute("""UPDATE maimi_im.question_job SET state=%s,answer=%s WHERE job_id=%s
               AND receipt_key=%s AND state='running' RETURNING receipt_key""",(state,encrypted,job,route.receipt_key)).fetchone()
            if changed:
                db.execute("UPDATE maimi_im.receipt SET state=%s WHERE receipt_key=%s AND state='reserved'",(state,route.receipt_key))

    async def work_once(self):
        claimed=await asyncio.to_thread(self.claim,int(time.time()))
        if claimed is None:return False
        job,route,encrypted=claimed
        result=None
        try:
            question=self.crypt(job,route,"question",encrypted,decrypt=True)
            value=await asyncio.wait_for(self.dispatch(route,question),timeout=CALL_LIMIT)
            if isinstance(value,str) and 0<len(value)<=16000: result=value
        finally:
            await asyncio.to_thread(self.finish,job,route,result)
        return True

    def cleanup(self,db,now):
        # No transition out of running can re-queue a call.
        for state,old,column,cutoff,receipt in (('expired','queued','deadline',now,'finished'),
                    ('uncertain','running','started_at',now-CALL_LIMIT-30,'uncertain')):
            db.execute(f"""WITH closed AS (UPDATE maimi_im.question_job SET state=%s,question=NULL
                    WHERE state=%s AND {column}<=%s RETURNING receipt_key)
                    UPDATE maimi_im.receipt SET state=%s FROM closed WHERE maimi_im.receipt.receipt_key=closed.receipt_key
                    AND maimi_im.receipt.state='reserved'""",(state,old,cutoff,receipt))
        db.execute("""DELETE FROM maimi_im.question_job WHERE retain_until<=%s OR NOT EXISTS (
            SELECT 1 FROM maimi_im.binding WHERE binding.generation=question_job.generation AND
            binding.employee=question_job.employee AND valid_until>%s)""",(now,now))

    async def worker_loop(self):
        while True:
            try:worked=await self.work_once()
            except Exception:worked=False
            if not worked:await asyncio.sleep(1)

