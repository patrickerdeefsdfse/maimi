"""Show Oracle-assigned sales orders and record the employee's decision in Maimi."""
from uuid import uuid4

from .contracts import card, invoke_action, notice, table

KEY = "sales-order-approval"
REFERENCE = {"type":"string","pattern":"^[a-f0-9]{32}$"}
TOOLS = [
    ("pending", "查看 Oracle 分派给本人的待审核销售订单",
     {"type":"object","properties":{"after":REFERENCE},"additionalProperties":False}),
    ("detail", "打开待审核订单的单头与明细，供本人核对和确认",
     {"type":"object","properties":{"order_id":REFERENCE},"required":["order_id"],"additionalProperties":False}),
]


async def invoke(svc, ctx, action, args):
    from .service import ProjectFailure, object_args
    from app.sales_order_transport import OrderCallFailed
    if action == "approve" and ctx.source != "human":
        raise ProjectFailure(403,"project_human_authorization_required")
    allowed = {"pending": ["after"], "detail": ["order_id"], "approve": ["review_id"]}
    if action not in allowed:
        raise ProjectFailure(404,"project_action_not_found")
    object_args(args, allowed[action], [] if action == "pending" else allowed[action])
    if not svc.sales_orders:
        raise ProjectFailure(503,"project_source_review_pending")
    sections, actions = [], []
    try:
        result = await svc.sales_orders({"pending":"approval_pending","detail":"approval_detail","approve":"approval_decide"}[action],args)
    except OrderCallFailed as error:
        # OrderCallFailed has already removed raw database and transport messages.
        value = card(KEY,uuid4().hex,"failed","审核操作未完成。",sections=[notice(str(error))])
        return await svc._save_result(ctx,value)
    if action == "pending":
        if not result["configured"]:
            summary = "还缺 Oracle 待审订单与审核工号的分派查询。接通后这里会显示分派给你的订单。"
            status = "failed"
        else:
            items = result["items"]
            summary = "请选择订单查看明细。" if items else "当前没有分派给你的麦米待审销售订单。"
            status = "needs_input" if items else "completed"
            if items:
                sections.append(table("我的待审订单",[{"key":"order_number","label":"销售订单号"},{"key":"business_entity","label":"业务实体"}],items))
                actions.extend(invoke_action("查看 "+x["order_number"], KEY,"detail",{"order_id":x["order_id"]}) for x in items)
            if result.get("next"):
                actions.append(invoke_action("下一页",KEY,"pending",{"after":result["next"]}))
    elif action == "detail":
        order = result["order"]
        summary = "请核对订单 "+result["order_number"]+"。点击确认会向 Oracle 提交审核，成功后才进入鸿翼档案收集流程。"
        status = "needs_input"
        header = {"business_entity":"销售主体","customer_name":"客户","account_number":"客户账号",
                  "customer_po":"客户采购单号","order_type":"订单类型","currency":"币种","total_amount":"总金额",
                  "total_basis":"总额口径","price_list_name":"价目表","order_date":"订单日期",
                  "salesrep_name":"销售员","head_remark":"订单备注"}
        sections.append(table("订单资料",[{"key":"label","label":"字段"},{"key":"value","label":"内容"}],
                              [{"label":label,"value":order.get(key)} for key,label in header.items() if order.get(key) is not None]))
        line_fields = (("source_line","来源行号"), ("item_code","物料"), ("customer_item","客户物料"),
                       ("description","名称"), ("specification","规格"), ("quantity","数量"), ("unit","单位"),
                       ("unit_price","单价"), ("price_basis","单价口径"), ("tax_rate","税率"), ("amount","行金额"),
                       ("amount_basis","金额口径"), ("request_date","交货日期"), ("remark","行备注"))
        columns = [{"key":key,"label":label} for key,label in line_fields
                   if any(line.get(key) is not None for line in order["lines"])]
        for start in range(0,len(order["lines"]),50):
            stop=min(start+50,len(order["lines"]))
            sections.append(table(f"订单明细（{start+1}–{stop}）", columns, order["lines"][start:stop]))
        if result.get("booking_state","pending") == "pending":
            actions.append(invoke_action("确认并提交 Oracle 审核",KEY,"approve",{"review_id":result["review_id"]}))
        else:
            summary = result.get("message") or "这笔订单的 Oracle 审核回执需要核对。"
            status = "needs_input"
    elif not result.get("approved") or result.get("oracle_status") != "S":
        summary = result.get("message") or "Oracle 审核尚未确认成功，请按建单批次核查。"
        sections.append(notice("该订单尚未进入鸿翼档案收集。"))
        status = "failed"
    else:
        summary = "销售订单 "+result["order_number"]+" 已审核通过。"
        if result.get("archive_status") == "archived":
            summary += "原件已归档。"
        elif result.get("archive_status") == "collection_accepted":
            summary += "鸿翼档案收集已受理，等待正式入库。"
        elif result.get("archive_status") == "collection_duplicate":
            summary += "鸿翼标为重复，请档案管理员在档案收集中核对。"
        elif result.get("archive_status") == "ecm_uploaded":
            summary += "原件已上传企业库，档案收集回执尚待核对。"
        elif result.get("archive_status") == "archive_unknown":
            summary += "归档回执待核对。"
        else:
            summary += "原件已进入自动归档队列。" if result["archive_configured"] else "原件已列入待归档，等待鸿翼接口接通。"
        status = "completed"
    value = card(KEY,uuid4().hex,status,summary,sections=sections,actions=actions)
    value["approval_stage"] = action
    return await svc._save_result(ctx,value)
