"""Private text only. No service identity, file tools or employee bearer in IM responses."""
import asyncio
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, StrictStr

from app import iam_oauth as iam
from app.desktop_auth import require_trusted_actor
from app.personal_cloud import issue_lease, lease_actor
from app.personal_model_proxy import cloud_chat_complete
from routing import Refused

NO_STORE={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"}
SCOPE="dingtalk-private-text-v1"
PREFIX="/v1/im/dingtalk"
LEASE_RE=re.compile(r"mcl_[A-Za-z0-9_-]{43}")


class ModelDelegation:
    def __init__(self, store, channel, connect, key, model):
        if len(key)!=32 or not isinstance(model,str) or not 1<=len(model)<=120:
            raise ValueError("MODEL_CONSENT_CONFIG_REQUIRED")
        self.store=store
        self.channel=channel
        self.connect=connect
        self.cipher=AESGCM(key)
        self.model=model

    def associated_data(self, employee, generation, expires):
        return "\n".join((SCOPE,self.store._account(self.channel),employee,str(generation),str(expires))).encode()

    def enroll(self, employee, generation, expires, lease):
        if not LEASE_RE.fullmatch(lease) or not time.time()<expires<=time.time()+900:
            raise Refused("USER_AUTHORIZATION_REQUIRED")
        iv=secrets.token_bytes(12)
        data=self.associated_data(employee,generation,expires)
        encrypted=iv+self.cipher.encrypt(iv,lease.encode(),data)
        with self.connect() as db:
            row=db.execute("""SELECT generation FROM maimi_im.binding WHERE account=%s AND employee=%s
                              AND generation=%s AND valid_until>%s FOR UPDATE""",
                           (self.store._account(self.channel),employee,generation,time.time())).fetchone()
            if not row:
                raise Refused("EMPLOYEE_BINDING_INACTIVE")
            db.execute("""INSERT INTO maimi_im.model_consent VALUES(%s,%s,%s,%s)
                ON CONFLICT(generation) DO UPDATE SET lease_ciphertext=EXCLUDED.lease_ciphertext,
                expires_at=EXCLUDED.expires_at""",(generation,employee,encrypted,expires))

    def generation_for(self, employee):
        with self.connect() as db:
            row=db.execute("""SELECT generation FROM maimi_im.binding WHERE account=%s AND employee=%s AND valid_until>%s""",
                           (self.store._account(self.channel),employee,time.time())).fetchone()
            if not row:
                raise Refused("EMPLOYEE_BINDING_REQUIRED")
            return str(row[0])

    def credential(self, employee, generation, receipt=None):
        with self.connect() as db:
            row=db.execute("""SELECT c.lease_ciphertext,c.expires_at FROM maimi_im.model_consent c
                JOIN maimi_im.binding b ON b.generation=c.generation AND b.employee=c.employee
                WHERE c.generation=%s AND c.employee=%s AND b.account=%s AND c.expires_at>%s AND b.valid_until>%s""",
                (generation,employee,self.store._account(self.channel),time.time(),time.time())).fetchone()
            if not row:
                raise Refused("USER_AUTHORIZATION_REQUIRED")
            if receipt is not None and not db.execute("""SELECT 1 FROM maimi_im.receipt WHERE receipt_key=%s
                    AND generation=%s AND state='reserved'""",(receipt,generation)).fetchone():
                raise Refused("INVALID_RECEIPT")
        raw,expires=row; raw=bytes(raw)
        try:
            return self.cipher.decrypt(raw[:12],raw[12:],self.associated_data(employee,generation,expires)).decode()
        except Exception:
            raise Refused("USER_AUTHORIZATION_REQUIRED") from None

    async def actor(self, employee, generation, receipt=None):
        lease=await asyncio.to_thread(self.credential,employee,generation,receipt)
        try:
            actor=await lease_actor(lease)
        except iam.IAMFailure:
            raise Refused("USER_AUTHORIZATION_REQUIRED") from None
        if actor.user_no != employee:
            raise Refused("USER_AUTHORIZATION_REQUIRED")
        return actor

    async def binding_active(self,binding):
        await self.actor(binding.employee,binding.generation)
        return True

    async def dispatch(self, route, text):
        actor=await self.actor(route.employee,route.binding_generation,route.receipt_key)
        result=await cloud_chat_complete(actor,[{'role':'user','content':text}],self.model,route.receipt_key)
        await self.actor(route.employee,route.binding_generation,route.receipt_key)
        return result['text']

    def revoke(self, employee):
        with self.connect() as db:
            self.store._lock_employee(db,self.store._account(self.channel),employee)
            db.execute("""DELETE FROM maimi_im.model_consent WHERE employee=%s AND generation IN
                        (SELECT generation FROM maimi_im.binding WHERE employee=%s AND account=%s)""",
                       (employee,employee,self.store._account(self.channel)))
            self.store.revoke(db,self.channel,employee)

    @staticmethod
    def cleanup(db,now):
        db.execute("""DELETE FROM maimi_im.model_consent WHERE expires_at<=%s OR NOT EXISTS (
            SELECT 1 FROM maimi_im.binding WHERE binding.generation=model_consent.generation
              AND binding.employee=model_consent.employee AND valid_until>%s)""",(now,now))


class Input(BaseModel):
    class Config:
        extra="forbid"


class Confirmation(Input):
    ticket: uuid.UUID
    proof: StrictStr


class Consent(Input):
    scope: StrictStr


def install_browser_routes(app, service, origin):
    """Mount only behind the same production IAM origin, with a working IAM login path."""
    if not origin.startswith("https://") or origin.endswith("/"):
        raise ValueError("EXPECTED_HTTPS_ORIGIN")
    @app.get("/maimi/im/dingtalk/")
    @app.get("/maimi/im/dingtalk/{name}")
    async def assets(name:str="pair.html"):
        types={"pair.html":"text/html","pair.js":"text/javascript","pair.css":"text/css"}
        if name not in types: return Response(status_code=404,headers=NO_STORE)
        return Response(Path(__file__).with_name(name).read_bytes(),media_type=types[name],
            headers={**NO_STORE,"X-Content-Type-Options":"nosniff","Content-Security-Policy":
                "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
    async def current(request):
        if (request.url.query or request.headers.get("origin") != origin or
            request.headers.get("sec-fetch-site")!="same-origin" or request.headers.get("authorization") or
            request.headers.get("content-type","").split(";")[0].strip()!="application/json" or
            not request.cookies.get(iam.SESSION_COOKIE)):
            raise Refused("ORIGIN_OR_SESSION_DENIED")
        # This candidate never accepts or promotes IM headers as an IAM identity.
        return await require_trusted_actor(request)
    def reply(obj,status=200):
        return JSONResponse(obj,status_code=status,headers=NO_STORE)
    async def controlled(request,action):
        try:
            actor=await current(request)
            return reply(await action(actor))
        except (iam.IAMFailure,Refused):
            return reply({"detail":"EMPLOYEE_AUTHORIZATION_REFUSED"},403)
    @app.post(PREFIX+"/pair/start")
    async def start(request:Request, form:Input):
        async def action(actor):
            def begin():
                with service.connect() as db:
                    service.store._lock_employee(db,service.store._account(service.channel),actor.user_no)
                    db.execute("DELETE FROM maimi_im.bind_attempt WHERE employee=%s AND account=%s",
                               (actor.user_no,service.store._account(service.channel)))
                    return service.store.begin(db,service.channel,actor.user_no,int(time.time()))
            ticket,claim=await asyncio.to_thread(begin)
            return {'ticket':ticket,'command':'麦米 绑定 '+claim,'expires_in_seconds':300}
        return await controlled(request,action)
    @app.post(PREFIX+"/pair/confirm")
    async def confirm(request:Request, form:Confirmation):
        async def action(actor):
            def finish():
                with service.connect() as db:
                    service.store.confirm(db,service.channel,actor.user_no,str(form.ticket),form.proof,int(time.time()))
            await asyncio.to_thread(finish)
            return {'paired':True}
        return await controlled(request,action)
    @app.post(PREFIX+"/consent")
    async def consent(request:Request, form:Consent):
        async def action(actor):
            if form.scope!=SCOPE:
                raise Refused("SCOPE_DENIED")
            generation=await asyncio.to_thread(service.generation_for,actor.user_no)
            lease,deadline=await issue_lease(request,actor)
            expires=min(int(deadline),int(time.time())+900)
            await asyncio.to_thread(service.enroll,actor.user_no,generation,expires,lease)
            return {'scope':SCOPE,'expires_at':expires}
        return await controlled(request,action)
    @app.post(PREFIX+"/revoke")
    async def revoke(request:Request, form:Input):
        async def action(actor):
            await asyncio.to_thread(service.revoke,actor.user_no)
            return {'revoked':True}
        return await controlled(request,action)

