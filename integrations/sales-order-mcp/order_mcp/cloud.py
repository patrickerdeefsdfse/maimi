"""Private stateless MCP transport behind the authenticated 麦米 gateway."""
import asyncio
import base64
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from .cloud_store import CloudStore
from .approval_assignments import OracleAssignments
from .config import Settings
from .ingest import EXTENSIONS, upload
from .integrations import ArchiveService
from .hongyi import archive_target_status
from .service import OrderService
from .store import digest

EMPLOYEE = re.compile(r"M[0-9]{8}")
SESSION = re.compile(r"(?:session-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ORDER_ID = re.compile(r"[0-9a-f]{32}")


def archive_ready(config):
    hongyi = config.get("hongyi",{})
    return archive_target_status(hongyi)["ready"]


def create_app(settings=None, *, auth_key=None, provider=None, oracle=None, archive_apis=None):
    base = settings or Settings.from_env()
    root = base.data_dir.resolve()
    key = auth_key or os.getenv("MAIMI_ORDER_MCP_SECRET", "")
    if len(key) < 32:
        raise ValueError("订单 MCP 缺少服务端认证配置。")
    # The middleware enforces the caller before MCP parses any payload.
    server = FastMCP("maimi-sales-orders", stateless_http=True, json_response=True,
                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
                     max_request_body_size=42 * 1024 * 1024)
    slots = asyncio.Semaphore(2)

    @server.tool()
    async def sales_order(action: str, arguments: dict, ctx: Context) -> dict:
        """麦米销售订单服务。员工与会话身份仅接收已鉴权企业网关发送的请求头。"""
        h = ctx.request_context.request.headers
        role = h.get("x-maimi-order-source")
        if role == "scheduler" and action == "pending" and not arguments:
            return {"items": CloudStore(root, "", "").pending()}
        employee, sid = h.get("x-maimi-order-employee", ""), h.get("x-maimi-order-session", "")
        if not EMPLOYEE.fullmatch(employee) or not SESSION.fullmatch(sid) or role not in {"model","human","archive"}:
            raise ValueError("订单操作缺少合法的员工或会话身份。")
        if (role == "archive") != (action == "archive"):
            raise ValueError("归档任务不允许此操作。")
        store = CloudStore(root, employee, sid)
        scoped = replace(base, data_dir=root / "documents" / digest([employee,sid]), input_roots=(),
                         user_code=employee)
        scoped.data_dir.mkdir(parents=True, exist_ok=True)
        service = OrderService(scoped, provider=provider, oracle=oracle, store=store)
        archive = ArchiveService(service, archive_apis)
        if action in {"approval_pending", "approval_detail", "approval_decide"}:
            from .order_approvals import OrderApprovals
            approvals = OrderApprovals(store, OracleAssignments(scoped, getattr(archive.apis, "config", {})))
            if action == "approval_pending" and not (set(arguments) - {"after"}):
                return await asyncio.to_thread(approvals.pending, arguments.get("after", ""))
            if action == "approval_detail" and set(arguments) == {"order_id"}:
                return await asyncio.to_thread(approvals.detail, arguments["order_id"])
            if action == "approval_decide" and role == "human" and set(arguments) == {"review_id"}:
                decision = await asyncio.to_thread(approvals.approve, arguments["review_id"])
                conf = getattr(archive.apis, "config", {})
                decision["archive_configured"] = archive_ready(conf)
                return decision
            raise ValueError("此审核操作须由获分派的员工本人在详情页确认。")
        if action == "status" and not arguments:
            from .oracle import OracleGateway
            cfg = getattr(archive.apis, "config", {})
            recognition_status=getattr(service.provider,"target_status",None)
            return {"recognition": recognition_status() if callable(recognition_status) else {"environment":"injected_provider","configured":True},
                    "oracle": OracleGateway(scoped).target_status(),
                    "approval_configured": OracleAssignments(scoped, cfg).configured,
                    "approval_procedure": "CUX_BLUE_AI_OE_ORDER_PKG.BOOK_ORDER",
                    "archive_configured": archive_ready(cfg),
                    "archive_target": archive_target_status(cfg.get("hongyi", {})),
                    "message": "订单由本人复核确认后建单。Oracle 分派的审核人确认后，麦米以建单批次调用 BOOK_ORDER。审核成功后才向鸿翼提交原件并跟踪收集回执。"}
        if action == "finish_batch":
            from .order_batch import finish
            if set(arguments)-{"batch_id","skipped"}:
                raise ValueError("批次参数无效。")
            return await asyncio.to_thread(finish,store,arguments.get("batch_id"),arguments.get("skipped",[]))
        if action in {"recognize","recognize_test","recognize_production","stage_file"}:
            if action in {"recognize_test","recognize_production"}:
                if role!="human":
                    raise ValueError("旧版识别环境按钮须由员工本人点击；普通识别使用服务端配置。")
                from .fastapi_extract import TEST, PRODUCTION, FastAPIExtractor
                service.provider=(FastAPIExtractor(TEST+"/noauth") if action=="recognize_test" else
                    FastAPIExtractor(PRODUCTION,os.getenv("ORDER_EXTRACT_PLATFORM_TOKEN","")))
            keys={"name","data"} | ({"batch_id","index","count","origin_id"} if action=="stage_file" else set())
            chat_text=arguments.get("chat_text","")
            source_text=arguments.get("source_text","")
            if (set(arguments)-{"chat_text","source_text"}!=keys or not isinstance(chat_text,str) or len(chat_text)>2000
                    or not isinstance(source_text,str) or len(source_text)>6000):
                raise ValueError("识别参数无效。")
            name = arguments.get("name","")
            if (not isinstance(name,str) or not 0 < len(name) <= 240 or re.search(r"[\\/\x00-\x1f]",name)
                    or Path(name).suffix.lower() not in EXTENSIONS):
                raise ValueError("上传文件名或文件类型不受支持。")
            try:
                display_name=base64.b64decode(h.get("x-maimi-order-name",""),validate=True).decode("utf8")
            except (UnicodeError,ValueError):
                raise ValueError("员工姓名格式不正确。") from None
            if not display_name.strip() or len(display_name)>120:
                raise ValueError("缺少可信员工姓名。")
            async with slots:
                source = upload(name, arguments.get("data",""), scoped)
                if action=="stage_file":
                    from .order_batch import accept
                    origin=arguments["origin_id"]
                    if not isinstance(origin,str) or not re.fullmatch(r"pjf_[A-Za-z0-9_-]{32}",origin):
                        raise ValueError("附件标识无效。")
                    batch=arguments["batch_id"]
                    idx,total=arguments["index"],arguments["count"]
                    key=digest([origin,name,source.stem])
                    if not await asyncio.to_thread(accept,store,batch,idx,total,key):
                        drafts,_=await service.extract_drafts(source,name,employee,display_name,allow_blank=True,
                                                               chat_text=chat_text,source_text=source_text,single_attachment=total==1)
                        await asyncio.to_thread(accept,store,batch,idx,total,key,drafts)
                    return {"staged":True,"index":idx}
                return await service.recognize(source,name,employee,display_name,chat_text=chat_text,source_text=source_text)
        rid = arguments.get("order_id", "")
        if not isinstance(rid,str) or not ORDER_ID.fullmatch(rid):
            raise ValueError("订单编号无效。")
        if action == "review":
            return await asyncio.to_thread(service.review,rid)
        if action == "confirm_order":
            if role != "human" or set(arguments) not in ({"order_id","revision"},{"order_id","revision","changes"}):
                raise ValueError("此操作须由提交人本人在复核表中确认。")
            return await service.confirm_order(rid,arguments["revision"],arguments.get("changes",{}))
        if action == "lookup_customer":
            return await service.lookup_customer(rid)
        if action == "lookup_price":
            return await service.lookup_price(rid, arguments.get("line_index"))
        if action == "generate_po":
            return service.generate_po(rid, arguments["revision"])
        if action == "revise":
            return service.revise(rid, arguments["revision"], arguments["changes"], arguments["reason"], human=role=="human")
        if action == "prepare" and role=="human":
            preview=service.prepare_submission(rid, arguments["revision"])
            preview["oracle_xml"]=service.export(rid, expected_revision=preview["revision"])["xml"]
            from .oracle import OracleGateway
            preview["oracle_target"]=OracleGateway(scoped).target_status()
            return preview
        if action == "submit" and role == "human":
            if oracle is None and not (scoped.dsn and scoped.db_user and scoped.db_password):
                raise ValueError("Oracle 连接尚未配置，订单仍为待确认。")
            return await service.submit(rid, arguments["revision"], arguments["confirmation_token"], True)
        if action == "archive":
            return await archive.sync(rid)
        if action == "query":
            record = store.get(rid)
            if record.get("status") in {"submitting","submission_unknown","cancelled"}:
                return await service.query_submission(rid)
            result = await archive.query(rid)
            if record.get("status") == "submitted":
                result = {**service.confirmation_result(rid), **result}
            return result
        raise ValueError("当前请求未获准执行此操作。")

    inner = server.streamable_http_app()
    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            from starlette.datastructures import Headers
            headers = Headers(scope=scope)
            supplied = headers.get("authorization","").encode()
            if ("origin" in headers or "sec-fetch-site" in headers or
                    not secrets.compare_digest(supplied, ("Bearer "+key).encode())):
                return await Response(status_code=403)(scope,receive,send)
        await inner(scope,receive,send)
    return guarded
