"""Dedicated private FastAPI app; do not register on a public anonymous gateway."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from identity_store import IdentityStore
from routing import Channel, Refused, dingtalk_text

PATH = "/internal/maimi-im/v1/dingtalk"
LIMIT = 65536
SAFE = {"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"}


def response(state, text, status):
    return JSONResponse({"state":state,"reply":text}, status_code=status, headers=SAFE)


def signature(key, timestamp, nonce, raw):
    material = "\n".join(("POST", PATH, timestamp, nonce, hashlib.sha256(raw).hexdigest()))
    return hmac.new(key, material.encode(), hashlib.sha256).hexdigest()


def verify(key, headers, raw, now):
    timestamp = headers.get("x-maimi-time","")
    nonce = headers.get("x-maimi-nonce","")
    supplied = headers.get("x-maimi-sign","")
    if not re.fullmatch(r"[0-9]{10}",timestamp) or abs(int(timestamp)-now)>60:
        raise Refused("FORWARDER_AUTH_REQUIRED")
    try:
        if str(UUID(nonce)) != nonce:
            raise ValueError
    except ValueError:
        raise Refused("FORWARDER_AUTH_REQUIRED") from None
    if not re.fullmatch(r"[0-9a-f]{64}",supplied) or not hmac.compare_digest(signature(key,timestamp,nonce,raw),supplied):
        raise Refused("FORWARDER_AUTH_REQUIRED")


def create_app(*, channel, forward_key, handle):
    if len(forward_key)<32 or not channel.bot or not channel.tenant:
        raise ValueError("INCOMPLETE_SERVER_CONFIG")
    app=FastAPI(openapi_url=None,docs_url=None,redoc_url=None)

    @app.post(PATH)
    async def receive(request: Request):
        if request.url.query or request.headers.get("content-type","").split(";")[0].strip()!="application/json":
            return response("refused","请求格式不支持。",415)
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>LIMIT:
                return response("refused","消息过长。",413)
            raw.extend(chunk)
        now=int(time.time())
        try:
            verify(forward_key,request.headers,raw,now)
        except Refused:
            return response("forbidden","转发身份未通过验证。",403)
        try:
            payload=json.loads(raw)
            if not isinstance(payload,dict):
                raise ValueError
            event=dingtalk_text(channel,payload)
            if (not isinstance(event.text,str) or not event.text.strip() or len(event.text)>8000
                or not now-300<=event.created_at<=now+30):
                raise Refused("INVALID_TEXT_OR_EXPIRED")
            return await handle(event, now)
        except Refused as e:
            if str(e)=="DUPLICATE_OR_PREVIOUS_OUTCOME_UNKNOWN":
                return response("duplicate","",200)
            if str(e) in ("EMPLOYEE_BINDING_REQUIRED","EMPLOYEE_BINDING_INACTIVE"):
                return response("binding_required","当前钉钉账号尚未开通麦米私聊，请联系麦米管理员。",200)
            if str(e)=="USER_AUTHORIZATION_REQUIRED":
                return response("consent_required","请登录麦米，在本人账号下授权钉钉私聊文字问答，然后重新发送。",200)
            return response("refused","无法处理此私聊消息。",403)
        except (ValueError, UnicodeDecodeError, TypeError):
            return response("bad_request","消息格式不支持。",400)
        except Exception:
            return response("temporarily_unavailable","麦米服务暂时不可用。",503)
    return app


class Handler:
    def __init__(self, store, routing_key, connect, active_employee, dispatch=None, delegation=None, jobs=None):
        self.store=store
        self.routing_key=routing_key
        self.connect=connect
        self.active_employee=active_employee
        self.dispatch=dispatch
        self.delegation=delegation
        self.jobs=jobs

    async def __call__(self,event,now):
        # Binding codes are not accepted until both ends of the IAM pairing UI exist.
        if event.text.startswith("绑定 ") or event.text=="绑定":
            if self.delegation is not None and re.fullmatch(r"绑定 [A-Za-z0-9_-]{27}",event.text):
                def claim():
                    with self.connect() as db:
                        return self.store.claim(db,event.channel,event.sender,event.text[3:],now)
                proof=await asyncio.to_thread(claim)
                return response("pair_claimed","请将此确认码填入本人已登录的麦米配对页面："+proof,200)
            return response("binding_not_available","账号绑定请联系麦米管理员。",200)
        if event.text not in ("状态","帮助") and self.dispatch is None:
            return response("dispatch_not_configured","此机器人已接入麦米入口，聊天服务尚待开通。",503)
        def find_owner():
            with self.connect() as db:
                return self.store.get_locked(db,event.channel,event.sender,now)
        initial=await asyncio.to_thread(find_owner)
        async def active():
            if self.delegation is not None:
                return await self.delegation.binding_active(initial)
            return await self.active_employee(initial.employee)
        if not await active():
            raise Refused("EMPLOYEE_BINDING_INACTIVE")
        def reserve():
            with self.connect() as db:
                bound=self.store.get_locked(db,event.channel,event.sender,now)
                if bound != initial: raise Refused("BINDING_CHANGED")
                route=self.store.reserve_inbound(db,event,lambda o:o==initial.employee,now,self.routing_key)
                handoff=None
                if self.jobs is not None and event.text not in ("帮助","状态"):
                    handoff=self.jobs.submit_or_read(db,route,event.text,now)
                    if not handoff[0]: self.store.finish(db,route,"finished")
                return route,handoff
        route,handoff=await asyncio.to_thread(reserve)
        if handoff is not None:
            if not await active() or await asyncio.to_thread(find_owner)!=initial:
                raise Refused("BINDING_CHANGED")
            queued,text=handoff
            return response("queued" if queued else "answered",text,200)
        def finish(state):
            with self.connect() as db: self.store.finish(db,route,state)
        try:
            if event.text=="状态":
                text="麦米身份绑定已验证。"+("聊天服务已接入。" if self.dispatch is not None else "聊天服务尚待开通。")
            elif event.text=="帮助":
                text="发送“麦米 状态”查看连接；“麦米 任务”查看自己的任务；“麦米 结果 任务编号”取回复。"
            else:
                # Dispatch must authorize holder, current generation and target workspace before execution.
                text=await self.dispatch(route,event.text)
            if not isinstance(text,str) or not 0<len(text)<=8000:
                raise Refused("BAD_RESULT")
            if not await active():
                raise Refused("EMPLOYEE_BINDING_INACTIVE")
            if await asyncio.to_thread(find_owner)!=initial:
                raise Refused("BINDING_CHANGED")
            await asyncio.to_thread(finish,"finished")
            return response("answered",text,200)
        except Exception:
            await asyncio.to_thread(finish,"uncertain")
            raise


def create_local_service():
    """Uvicorn --factory entry. Requires explicit local provisioning, IAM lookup and PG schema."""
    import psycopg
    from app.iam_directory import resolve_account, DirectoryError
    def readkey(var):
        raw=Path(os.environ[var]).read_bytes()
        if not 32<=len(raw)<=512: raise ValueError("INVALID_KEY_FILE")
        return raw
    forward=readkey("MAIMI_IM_FORWARD_KEY_FILE")
    identity=readkey("MAIMI_IM_BINDING_KEY_FILE")
    routing=readkey("MAIMI_IM_ROUTING_KEY_FILE")
    if len({forward,identity,routing})!=3: raise ValueError("DISTINCT_KEYS_REQUIRED")
    channel=Channel("dingtalk",os.environ["MAIMI_IM_ROBOT_CODE"],os.environ["MAIMI_IM_CORP_ID"])
    async def active(employee):
        try:
            await resolve_account(employee)
            return True
        except DirectoryError:
            return False
    def connect():
        return psycopg.connect(os.environ["MAIMI_IM_DATABASE_URL"],connect_timeout=3,
            options="-c statement_timeout=3000 -c lock_timeout=800")
    store=IdentityStore(identity)
    delegation=None
    jobs=None
    if os.getenv("MAIMI_IM_MODEL_DELEGATION_ENABLED")=="true":
        from qa_delegation import ModelDelegation, install_browser_routes
        key=readkey("MAIMI_IM_CONSENT_KEY_FILE")
        if key in (identity,routing,forward): raise ValueError("DISTINCT_KEYS_REQUIRED")
        delegation=ModelDelegation(store,channel,connect,key,os.environ["MAIMI_IM_MODEL"])
        from qa_jobs import Jobs
        job_key=readkey("MAIMI_IM_JOB_KEY_FILE")
        if job_key in (identity,routing,forward,key): raise ValueError("DISTINCT_KEYS_REQUIRED")
        jobs=Jobs(connect,job_key,delegation.dispatch)
    app=create_app(channel=channel,forward_key=forward,
        handle=Handler(store,routing,connect,active,delegation.dispatch if delegation else None,delegation,jobs))
    if delegation is not None:
        install_browser_routes(app,delegation,os.environ["IAM_PUBLIC_ORIGIN"])
    async def purge():
        def once():
            with connect() as db:
                if not db.execute("SELECT pg_try_advisory_xact_lock(1060111)").fetchone()[0]:
                    return
                now=int(time.time())
                store.cleanup(db,now)
                if delegation is not None: delegation.cleanup(db,now)
                if jobs is not None: jobs.cleanup(db,now)
        while True:
            try: await asyncio.to_thread(once)
            except Exception:
                app.state.im_cleanup_status="unavailable"
            else: app.state.im_cleanup_status="available"
            await asyncio.sleep(300)
    @app.on_event("startup")
    async def start_purge():
        app.state.im_purge=asyncio.create_task(purge())
        if jobs is not None: app.state.im_worker=asyncio.create_task(jobs.worker_loop())
    @app.on_event("shutdown")
    async def stop_purge():
        app.state.im_purge.cancel()
        if jobs is not None:
            app.state.im_worker.cancel()
            try: await app.state.im_worker
            except asyncio.CancelledError: pass
        try: await app.state.im_purge
        except asyncio.CancelledError: pass
    return app

