"""MCP client used only by the authenticated project port and archive worker."""
import asyncio
import base64
import json
import logging
import os
from urllib.parse import urlsplit
from uuid import NAMESPACE_OID, uuid5

import httpx

LOG = logging.getLogger("maimi.sales_orders")


def configuration():
    url = os.getenv("MAIMI_ORDER_MCP_URL", "")
    secret = os.getenv("MAIMI_ORDER_MCP_SECRET", "")
    endpoint = urlsplit(url)
    if (not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path != "/mcp" or len(secret)<32 or
            not (endpoint.scheme=="https" or endpoint.scheme=="http" and endpoint.hostname in {"localhost","127.0.0.1","::1","sales-orders.maimi-production.svc.cluster.local"})):
        return None
    return url,secret


class OrderCallFailed(Exception):
    pass


async def rpc(action, arguments, *, source, employee="", conversation="", display_name="", authorize=None):
    config = configuration()
    if not config:
        raise OrderCallFailed("订单 MCP 尚未配置。")
    url, secret = config
    headers = {"Authorization":"Bearer "+secret, "Accept":"application/json, text/event-stream",
               "x-maimi-order-source":source,"x-maimi-order-employee":employee,"x-maimi-order-session":conversation}
    if display_name:
        headers["x-maimi-order-name"] = base64.b64encode(display_name.encode("utf8")).decode("ascii")
    async with httpx.AsyncClient(trust_env=False,timeout=httpx.Timeout(520,connect=5),follow_redirects=False,headers=headers) as client:
        async def send(message):
            async with client.stream("POST",url,json=message) as response:
                if response.status_code not in {200,202}:
                    raise OrderCallFailed("订单 MCP 暂不可用。")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body)+len(chunk)>86 * 1024 * 1024:
                        raise OrderCallFailed("订单返回内容过大，请分单处理。")
                    body.extend(chunk)
                if "id" not in message:
                    return None
                payload = json.loads(body)
                if payload.get("id")!=message["id"] or "result" not in payload:
                    raise OrderCallFailed("订单 MCP 协议返回异常。")
                return payload["result"]
        init = await send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25",
                       "capabilities":{},"clientInfo":{"name":"maimi-project-gateway","version":"1"}}})
        version = init.get("protocolVersion")
        if version not in {"2025-11-25","2025-06-18","2025-03-26"}:
            raise OrderCallFailed("订单 MCP 协议版本不兼容。")
        client.headers["mcp-protocol-version"] = version
        await send({"jsonrpc":"2.0","method":"notifications/initialized"})
        if authorize:
            await authorize()
        result = await send({"jsonrpc":"2.0","id":2,"method":"tools/call",
                             "params":{"name":"sales_order","arguments":{"action":action,"arguments":arguments}}})
        if result.get("isError"):
            # MCP may embed driver/model/validation errors. Never relay raw bodies.
            candidate = result.get("content",[{}])[0].get("text","")
            known = ("当前会话中没有这份订单","字段或金额校验失败","确认凭据失效","必填字段或金额校验尚未通过",
                     "请由麦米注入","Oracle 写入未配置启用","Oracle 连接尚未配置","Oracle 多行 XML 合同尚未确认",
                     "同一业务单据已提交或正在核查","Oracle 结果待核查","归档结果待核查","销售订单 FastAPI 尚未配置平台认证","销售订单识别未返回有效订单行","销售订单识别未完成","销售订单上传接口返回 HTTP 401",
                     "请先核对订单中的客户名称，再查询客户账号。","订单行编号无效，请从当前订单选择一行。",
                     "请先核对客户账号、客户名称和本行订购项目，再查询价格。","查询未返回有效价格，请向提交人核对价格。",
                     "Oracle 连接尚未配置。","原订单已有 PO，不可覆盖。","版本已变化或订单已提交，无法修改。",
                     "记录已被另一操作修改，请重新获取后再操作。","明细结构无效，请重新打开复核表。",
                     "更改明细行须由员工在复核表中操作。","更正内容无效，请检查字段和版本。",
                     "Oracle 环境或目标尚未核对，请管理员核对环境、地址、端口和服务名。",
                     "销售订单上传回执无效。","销售订单识别流格式无效。","销售订单识别流提前结束。",
                     "销售订单服务响应超时，请稍后重新识别。","销售订单识别服务暂时无法连接。")
            known += ("该订单当前未分派给你，或已经不在待审清单。","请重新打开审核详情。",
                      "审核预览已过期，请重新打开。","订单或审核分派已变化，请重新打开审核详情。",
                      "此审核操作须由获分派的员工本人在详情页确认。")
            hint = next((item for item in known if item in candidate),None)
            raise OrderCallFailed(hint or "订单处理未确认成功，请查看当前订单和企业接口配置。")
        data = result.get("structuredContent")
        if data is None:
            parts = result.get("content")
            if isinstance(parts,list) and len(parts)==1 and parts[0].get("type")=="text":
                data = json.loads(parts[0]["text"])
        if not isinstance(data,dict):
            raise OrderCallFailed("订单 MCP 数据格式异常。")
        return data


async def granted(employee, connector="sales-orders"):
    from .plugin_admin import project_granted
    return await asyncio.to_thread(project_granted,employee,connector)


def bind(access, authorize):
    async def check(connector):
        await authorize(access)
        if not await granted(access.actor.user_no,connector):
            raise OrderCallFailed("当前账号未获销售订单权限。")
    async def call(action, arguments):
        approval = action in {"approval_pending","approval_detail","approval_decide"}
        connector = "sales-order-approval" if approval else "sales-orders"
        if approval and access.permit.get("connector") != connector:
            raise OrderCallFailed("请在销售订单审核技能中操作。")
        async def verify():
            await check(connector)
        await verify()
        if action == "approval_decide" and access.permit["source"] != "human":
            raise OrderCallFailed("此审核操作须由获分派的员工本人在详情页确认。")
        if action in {"confirm_order","prepare","submit","recognize_test","recognize_production"} and access.permit["source"]!="human":
            raise OrderCallFailed("此确认操作须由员工在页面点击；普通识别请调用 recognize。")
        # The Office service only sees this IAM-scoped attachment. A sender label in a
        # chat screenshot may be missing from the standard FastAPI order fields.
        if (action in {"recognize","recognize_test","recognize_production","stage_file"}
                and str(arguments.get("name","")).lower().endswith((".jpg",".jpeg",".png"))
                and getattr(access.actor,"network_mode","")=="public"
                and getattr(access.actor,"task_network_mode","public")=="public"):
            from .drawing_office import read_drawing_text,MAX_INPUT
            from maimi_projects.heatcost_flow import DrawingTextUnavailable
            encoded=arguments.get("data","")
            if isinstance(encoded,str) and 0<len(encoded)<=4*((MAX_INPUT+2)//3):
                data=base64.b64decode(encoded,validate=True)
                try:
                    async with asyncio.timeout(15):
                        pages=await read_drawing_text(access.actor,{"name":arguments["name"],"data":data},1,verify)
                    if pages and pages[0].get("text"):
                        arguments={**arguments,"source_text":pages[0]["text"][:6000]}
                except (DrawingTextUnavailable,TimeoutError):
                    pass
        # A recreated workspace receives a fresh epoch even if the client reused a UUID.
        if action == 'stage_file':
            from . import iam_oauth
            from .task_network_scope import order_message_key
            cache_key = order_message_key(access.owner,access.session_id,arguments.get('batch_id'))
            if cache_key:
                async def read_text(service):
                    return await service.store.redis.get(cache_key)
                text = await iam_oauth._with_service(read_text)
                if text:
                    arguments = {**arguments, 'chat_text':text.decode() if isinstance(text,bytes) else text}
        conversation = str(uuid5(NAMESPACE_OID, access.owner+":"+access.session_id+":"+access.epoch))
        return await rpc(action,arguments,source=access.permit["source"],employee=access.actor.user_no,
                         conversation=conversation, display_name=access.actor.display_name if action in {"recognize","recognize_test","recognize_production","stage_file"} else "", authorize=verify)
    return call


async def sweep():
    work = await rpc("pending",{},source="scheduler")
    for row in work.get("items",[]):
        employee = row["employee"]
        async def check():
            if not await granted(employee):
                raise OrderCallFailed("员工授权已撤销。")
        try:
            await check()
            await rpc("archive",{"order_id":row["order_id"]},source="archive",employee=employee,
                      conversation=row["conversation"],authorize=check)
        except (OrderCallFailed, httpx.HTTPError):
            LOG.warning("ORDER_ARCHIVE_PENDING_CHECK")


async def archive_loop(stop):
    while not stop.is_set():
        try:
            if configuration():
                await sweep()
        except Exception:
            LOG.warning("ORDER_ARCHIVE_SWEEP_INCOMPLETE")
        try:
            await asyncio.wait_for(stop.wait(),timeout=60)
        except asyncio.TimeoutError:
            pass
