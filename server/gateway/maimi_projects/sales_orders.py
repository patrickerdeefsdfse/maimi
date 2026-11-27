"""Order result cards and the confirmed native attachment boundary."""
import base64
import json
from uuid import uuid4
from .contracts import card, invoke_action, notice, table

KEY = "sales-orders"
REF = {"type":"string","maxLength":128}
CHAT = {"type":"string","maxLength":2000}
REV = {"type":"integer","minimum":1}
MULTILINE_NOTICE = "当前 Oracle 接口尚未启用多行建单。可继续复核、生成预览和下载 XML，启用后请重新预览确认。"
REVIEW_HANDOFF = "请一次列出待补字段，直接回复用户并结束本轮。用户可稍后在原对话补充；收到回复后先 review 同一草稿，再按当前版本 revise。不要调用 ask_user_question 等交互工具挂起本轮。"
REQUIRED_LABELS = {"business_entity": "业务实体", "customer_name": "客户名称", "account_number": "客户账号",
                   "customer_po": "客户 PO 号", "currency": "币种", "item_code": "物料编码",
                   "quantity": "订购数量", "unit_price": "单价", "request_date": "交货日期"}


def tool(action, description, fields, required=None):
    return action, description, {"type":"object","properties":fields,"required":list(fields if required is None else required),"additionalProperties":False}


TOOLS = [
    tool("status","核对销售订单接入状态、Oracle 测试或正式环境及配置缺项",{}),
    tool("stage_file","识别本条消息的一个原件并暂存提取结果",{"batch_id":REF,"index":{"type":"integer","minimum":0,"maximum":19},"count":{"type":"integer","minimum":1,"maximum":20},"attachment_id":REF,"chat_text":CHAT},["batch_id","index","count","attachment_id"]),
    tool("finish_batch","按单据整理已识别的附件，生成待员工复核的草稿",{"batch_id":REF,"skipped":{"type":"array","items":{"type":"integer","minimum":0,"maximum":19},"maxItems":20}},["batch_id"]),
    tool("recognize","使用已配置的识别服务提取本会话上传的图片、PDF 或 Excel。附件已识别时用 review 续办。缺少业务字段时，" + REVIEW_HANDOFF,
         {"attachment_id":REF,"chat_text":CHAT},["attachment_id"]),
    tool("review","查看当前草稿和识别依据。缺少业务字段时，" + REVIEW_HANDOFF,{"order_id":REF}),
    tool("lookup_customer","按本会话订单的客户名称查询 Oracle 客户账号候选，核对后再更正订单",{"order_id":REF}),
    tool("lookup_price","保留原件识别或员工填写的单价。仅在本行缺少单价时，查询同客户、主体、物料、币种的最近历史订单单价及已配置的客户价目表。无候选时请用户补充本行单价。",
         {"order_id":REF,"line_index":{"type":"integer","minimum":0,"maximum":499}}),
    tool("generate_po","原件确无客户 PO 时生成内部编号，待提交人复核",{"order_id":REF,"revision":REV}),
    tool("revise","按提交人的更正修改字段并记录原因",{"order_id":REF,"revision":REV,"changes":{"type":"object"},"reason":{"type":"string","maxLength":1000}}),
    tool("query","核对 Oracle 订单关联的 ECM 归档回执",{"order_id":REF})
]
PREPARE = tool("prepare","员工复核后预览本版本的订单",{"order_id":REF,"revision":REV})
CONFIRM_ORDER = tool("confirm_order","保存本人复核的更改并确认创建 Oracle 订单",{"order_id":REF,"revision":REV,"changes":{"type":"object"}},["order_id","revision"])
TEST = tool("recognize_test","本人确认本附件为联调测试案例",{"attachment_id":REF,"chat_text":CHAT},["attachment_id"])
PROD = tool("recognize_production","本人选择正式识别服务",{"attachment_id":REF,"chat_text":CHAT},["attachment_id"])
SUBMIT = tool("submit","确认按已预览版本创建 Oracle 订单",{"order_id":REF,"revision":REV,"confirmation_token":REF})


def missing_required(view):
    labels = []
    by_line = {}
    for issue in view.get("issues", []):
        if issue.get("code") != "required":
            continue
        path = issue["path"].split(".")
        label = REQUIRED_LABELS.get(path[-1], path[-1])
        if len(path) == 3 and path[0] == "lines" and path[1].isdigit():
            by_line.setdefault(label, []).append(int(path[1]) + 1)
        else:
            labels.append(label)
    for label, rows in by_line.items():
        if len(rows) == len(view["order"]["lines"]):
            where = f"全部 {len(rows)} 行"
        else:
            where = "第 " + "、".join(map(str, rows[:12])) + " 行"
            if len(rows) > 12:
                where += f"等共 {len(rows)} 行"
        labels.append(where + "的" + label)
    return "、".join(labels)


def order_editor(view, confirmation_error=None):
    order = view["order"]
    filtered = lambda fields: {key: value for key, value in fields.items() if value is not None}
    evidence = order.get("evidence") or {}
    prefix = "原识别数值待核："
    observations = {path: note[len(prefix):] for path,note in (view.get("provenance") or {}).items()
                    if isinstance(note,str) and note.startswith(prefix)}
    editor = {"schema": "sales-order-editor-v1", "order_id": view.get("id") or view["order_id"],
            "revision": view["revision"], "can_prepare": view.get("can_prepare", True),
            "header": filtered({k:v for k,v in order.items() if k not in {"lines","evidence"}}),
            "lines": [filtered(line) for line in order["lines"]],
            "issues": view.get("issues", []), "observations": observations,
            "defaults": {k:{f:v for f,v in item.items() if f in {"value","previous","reason"}}
                         for k,item in (view.get("business_defaults") or {}).items()},
            "confidence": {path:item["confidence"] for path,item in evidence.items()
                           if isinstance(item,dict) and type(item.get("confidence")) in {int,float}}}
    warehouse = (view.get("normalized") or {}).get("ship_org_code")
    if isinstance(warehouse,str) and warehouse.isalnum() and len(warehouse)<=10:
        editor["ship_org_code"] = warehouse
    if isinstance(view.get("item_candidates"), dict):
        options = {}
        for index in range(len(editor["lines"])):
            key = f"lines.{index}.item_code"
            codes = view["item_candidates"].get(key)
            if isinstance(codes, list):
                options[key] = list(dict.fromkeys(code for code in codes if isinstance(code,str) and 0 < len(code) <= 240))[:20]
        editor["item_candidates"] = options
    if isinstance(view.get("customer_candidates"), list):
        editor["customer_candidates"] = [
            {"party_name": row["party_name"], "account_number": row["account_number"]}
            for row in view["customer_candidates"] if isinstance(row,dict)
            and all(isinstance(row.get(k), str) and 0 < len(row[k].strip()) <= 500
                    for k in ("party_name", "account_number"))]
    if isinstance(view.get("business_entities"), list):
        editor["business_entities"] = list(dict.fromkeys(name for name in view["business_entities"]
            if isinstance(name, str) and 0 < len(name.strip()) <= 500))
    rejected = view.get("submission_error") if view.get("status") == "review" else None
    error = confirmation_error or rejected
    if error:
        batch = str((view.get("submission_receipt") or {}).get("batch_number", ""))
        if rejected == error and batch.isascii() and batch.isdecimal() and 0 < len(batch) <= 38:
            error = error.rstrip("。") + f"。Oracle 接口批次号 {batch}。"
        editor["submission_error"] = error
    return editor


def review_sections(view):
    order = view["order"]
    header = [{"field":k,"value":v} for k,v in order.items() if k not in {"lines","evidence"} and v is not None]
    lines = order["lines"]
    # No clipping the approval preview. Reject an unrenderable field before issuing a button.
    def values(title,rows):
        if any(len(str(v))>500 for row in rows for v in row.values()):
            raise ValueError("订单字段过长，请拆分后复核。")
        return [table(title,[{"key":"field","label":"字段"},{"key":"value","label":"值"}], rows[i:i+100]) for i in range(0,len(rows),100)]
    sections = []
    if view.get("status") in {"review", "prepared"}:
        if view.get("can_prepare") is False:
            missing = missing_required(view)
            if missing:
                sections.append(notice("待补字段：" + missing))
            sections.append(notice("可随时在本对话回复更正内容。涉及明细时注明行号，有多份草稿时注明是哪一份。"))
        if len(lines) > 1 and view.get("multi_line_confirmed") is False:
            sections.append(notice(MULTILINE_NOTICE))
    sections += values("订单字段",header)
    for index,line in enumerate(lines):
        sections += values(f"订单行 {index+1}",[{"field":k,"value":v} for k,v in line.items() if v is not None])
    sections += values("识别依据",[{"field":k,"value":v} for k,v in view.get("provenance",{}).items()])
    norm = view.get("normalized",{})
    for index,line in enumerate(norm.get("rows",[])):
        sections += values(f"Oracle 核算行 {index+1}",[{"field":k,"value":v} for k,v in line.items() if v is not None])
    sections += values("问题与警告",[{"field":i.get("path",""),"value":i["message"]} for i in view.get("issues",[])]
                       + [{"field":"识别","value":w} for w in view.get("warnings",[])])
    return sections


async def invoke(svc, ctx, action, args):
    from .service import ProjectFailure, object_args
    from app.sales_order_transport import OrderCallFailed
    definition = next((t for t in [*TOOLS,PREPARE,SUBMIT,CONFIRM_ORDER,TEST,PROD] if t[0]==action),None)
    if not definition:
        raise ProjectFailure(400,"project_order_action_invalid")
    shape = definition[2]
    object_args(args,shape["properties"],shape["required"])
    if not svc.sales_orders:
        raise ProjectFailure(503,"project_order_mcp_not_configured")
    if action in {"confirm_order","prepare","submit","recognize_test","recognize_production"} and ctx.source!="human":
        raise ProjectFailure(403,"project_human_authorization_required")
    if action == "revise" and isinstance(args.get("changes"),dict) and "line_layout" in args["changes"] and ctx.source != "human":
        raise ProjectFailure(403,"project_human_authorization_required")
    # The ordinary catalog operation uses the administrator's fixed endpoint.
    # Legacy human-only buttons remain compatible without making them model tools.
    if action in {"recognize","recognize_test","recognize_production","stage_file"}:
        _, origin = await svc.files.record(args["attachment_id"])
        if origin != "native_message":
            raise ProjectFailure(400,"project_order_user_attachment_required")
        attached = await svc.files.read(ctx,args["attachment_id"],30*1024*1024)
        batch = {k:args[k] for k in ("batch_id","index","count")} if action=="stage_file" else {}
        if batch:
            batch["origin_id"]=args["attachment_id"]
        if "chat_text" in args:
            if not isinstance(args["chat_text"], str) or len(args["chat_text"]) > 2000:
                raise ProjectFailure(400,"project_arguments_invalid")
            batch["chat_text"] = args["chat_text"]
        args = {"name":attached["name"],"data":base64.b64encode(attached["data"]).decode("ascii"),**batch}
    try:
        result = await svc.sales_orders(action,args)
    except OrderCallFailed as exc:
        if action in {"confirm_order","revise","prepare","submit"} and any(label in str(exc) for label in ("版本","另一操作修改","重新预览")):
            refresh=invoke_action("查看最新草稿",KEY,"review",{"order_id":args["order_id"]})
            return await svc._save_result(ctx,card(KEY,uuid4().hex,"failed",str(exc),actions=[refresh]))
        return await svc._save_result(ctx,card(KEY,uuid4().hex,"failed",str(exc)))
    await svc._authorize(ctx,KEY)
    sections,actions,artifacts = [],[],[]
    editors = []
    needs_correction = False
    confirmation_error = None
    if action == "confirm_order" and result.get("status") == "needs_input":
        confirmation_error = result["message"]
        result = result["review"]
        action = "review"
    if action in {"query","review","confirm_order"} and result.get("status")=="cancelled":
        receipt=result.get("oracle_result") or result
        number=str(receipt.get("order_number") or "")
        text=f"Oracle 销售订单 {number} 已撤销。重新录入请上传原件并确认新草稿。"
        closed=card(KEY,uuid4().hex,"completed",text)
        closed["sales_order_refs"]=[args["order_id"]]
        value=await svc._save_result(ctx,closed)
        return {**value,"order_result":{"order_id":args["order_id"],"status":"cancelled","order_number":number},
                "untrusted_external_content":True}
    if action in {"stage_file","finish_batch"}:
        summary="附件已暂存。" if action=="stage_file" else "订单草稿已整理。"
        # Worker-only receipts are job scoped. They are never rendered as extra review cards.
        return {"status":"completed","project_result":card(KEY,uuid4().hex,"completed",summary),"order_result":result}
    if action=="status":
        state=result["oracle"]
        target=state.get("expected_target") or {}
        recognition=result.get("recognition") or {}
        rows=[{"field":"识别服务环境","value":{"test":"测试环境","production":"正式环境"}.get(recognition.get("environment"),"由服务端配置")},
              {"field":"识别配置完整","value":bool(recognition.get("configured"))},
              {"field":"Oracle 环境","value":{"test":"测试环境","production":"正式环境"}.get(state["environment"],"尚未配置")},
              {"field":"目标地址","value":target.get("host", "尚未配置")},
              {"field":"端口","value":target.get("port", "尚未配置")},
              {"field":"服务名","value":target.get("service_name", "尚未配置")},
              {"field":"连接与期望目标一致","value":state["target_matches"]},
              {"field":"Oracle 驱动模式","value":state.get("driver_mode", "thin")},
              {"field":"建单配置完整且目标一致","value":state["write_ready"]},
              {"field":"多行 XML 契约已确认","value":state["multi_line_confirmed"]},
              {"field":"Oracle 审核过程","value":result.get("approval_procedure","待核对")},
              {"field":"Oracle 审核人分派查询已配置","value":result["approval_configured"]},
              {"field":"鸿翼销售订单收集接口已配置","value":result["archive_configured"]}]
        sections.append(table("销售订单接入状态",[{"key":"field","label":"配置"},{"key":"value","label":"状态"}],rows))
        archive_target=result.get("archive_target") or {}
        if archive_target:
            sections.append(table("审核后归档目标",[{"key":"field","label":"配置"},{"key":"value","label":"状态"}],
                [{"field":"专用目录","value":archive_target.get("parent_name", "尚未配置")},
                 {"field":"目录记录号","value":archive_target.get("parent_directory_reference", "尚未配置")},
                 {"field":"父目录 API ID 已配置","value":bool(archive_target.get("parent_folder_id_configured"))}]))
            sections.append(table("按公司主体分类归档",[{"key":"name","label":"分类"},
                {"key":"business_entities","label":"公司主体"},{"key":"configured","label":"目录 API ID 已配置"}],
                [{"name":folder["name"],"business_entities":"、".join(folder.get("business_entities",[])),
                  "configured":bool(folder.get("folder_id_configured"))} for folder in archive_target.get("folders",[])]))
            collection = archive_target.get("collection") or {}
            if collection.get("missing"):
                sections.append(notice("客户销售订单收集规则待配置："+"、".join(collection["missing"])))
            sections.append(notice("019 目录用于按主体存放原件。上传完成后，麦米用电子附件 ID 推送档案收集；受理和正式入库分别记录。"))
        if state["missing_configuration"]:
            sections.append(notice("待管理员配置 "+"、".join(state["missing_configuration"])))
        sections.append(notice("员工核对并确认后，麦米创建 Oracle 订单。业务审核通过后，再推送鸿翼归档。"))
        sections.append(notice(result["message"]))
        summary="销售订单接入状态，请核对 Oracle 环境后测试。"
    elif action=="prepare":
        xml=result.pop("oracle_xml")
        xml_name="Oracle订单-"+result["order_id"][:12]+"-v"+str(result["revision"])+".xml"
        artifacts.append(await svc.files.write(ctx,xml_name,xml.encode("utf8"),"application/xml"))
        sections=review_sections(result)
        editors.append(order_editor(result))
        state=result.get("oracle_target",{})
        target=state.get("expected_target") or {}
        environment={"test":"测试环境","production":"正式环境"}.get(state.get("environment"),"环境尚未配置")
        sections.append(notice("Oracle 建单目标："+environment+"，"+str(target.get("host","待配置"))+":"+str(target.get("port","待配置"))+"/"+str(target.get("service_name","待配置"))))
        if len(result["order"]["lines"]) > 1 and state.get("multi_line_confirmed") is not True:
            summary = "已生成多行订单预览，当前仅供复核和下载。"
            sections.insert(0, notice(MULTILINE_NOTICE))
        else:
            actions=[invoke_action("确认创建 Oracle 订单",KEY,"submit",{k:result[k] for k in ("order_id","revision","confirmation_token")})]
            summary="请核对以下订单、核算价格与警告，确认后将写入 Oracle。"
        result={k:v for k,v in result.items() if k!="confirmation_token"}
    elif action in {"recognize","recognize_test","recognize_production","review","revise","generate_po"}:
        if action in {"recognize","recognize_test","recognize_production"} and result.get("workbook"):
            workbook=base64.b64decode(result.pop("workbook"),validate=True)
            generated=await svc.files.write(ctx,"销售订单识别结果.xlsx",workbook,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            artifacts.append(generated)
            if result.get("workbook_kind")=="protected_xlsx":
                sections.append(notice("识别服务返回了公司保护的 Excel 原件，可下载后在获授权的办公软件打开。"))
        views=result["orders"] if action in {"recognize","recognize_test","recognize_production"} else [result]
        for view in views:
            compact = bool(view.get("source_names")) and view["status"] in {"review","prepared"}
            if not compact:
                sections.append(notice("订单 "+view["id"]+"，版本 "+str(view["revision"])))
                sections += review_sections(view)
            if view["status"] in {"review","prepared"}:
                editor=order_editor(view, confirmation_error)
                if compact:
                    editor["source_names"]=view["source_names"]
                    editor["warnings"]=view.get("warnings",[])
                editors.append(editor)
                if view.get("can_prepare") is False:
                    needs_correction = True
                identity={"order_id":view["id"]}
                if not compact and view["order"].get("customer_name") and not view["order"].get("account_number"):
                    actions.append(invoke_action("查询客户账号",KEY,"lookup_customer",identity))
                if not compact and not view["order"].get("customer_po"):
                    actions.append(invoke_action("原件无 PO：生成内部编号待复核",KEY,"generate_po",identity | {"revision":view["revision"]}))
            if view["status"] in {"review","prepared"}:
                actions.append(invoke_action("确认创建 Oracle 订单",KEY,"confirm_order",{"order_id":view["id"],"revision":view["revision"]}))
        summary="订单已识别，可核对字段和原文依据。" if action in {"recognize","recognize_test","recognize_production"} else "订单复核结果"
        if needs_correction:
            summary = "草稿已保存，请核对待补字段及校验问题，可在本对话回复更正内容。"
        if confirmation_error:
            summary = confirmation_error
        if action=="review" and result.get("status")=="submitted":
            receipt=result.get("oracle_result") or {}
            summary="已在 Oracle 建单，销售订单号 "+str(receipt["order_number"])+"。"
            if receipt.get("batch_number"):
                sections.append(notice("Oracle 批次号 "+str(receipt["batch_number"])))
        elif action=="review" and result.get("status") in {"submitting","submission_unknown"}:
            summary=result.get("error") or "正在核对 Oracle 建单回执，请稍后查看状态。"
            actions.append(invoke_action("核对 Oracle 建单回执",KEY,"query",{"order_id":args["order_id"]}))
    elif action=="submit":
        summary="Oracle 订单 "+str(result["order_number"])+" 已创建，将在业务审核通过后归档。"
    elif action=="confirm_order" or (action=="query" and result.get("status") in {"submitting","submission_unknown","submitted"} and not result.get("local_status")):
        if result["status"]=="submitted":
            summary="已在 Oracle 建单，销售订单号 "+str(result["order_number"])+"。"
            if result.get("batch_number"):
                sections.append(notice("Oracle 批次号 "+str(result["batch_number"])))
            sections.append(notice("订单待业务审核，审核通过后进入归档流程。"))
        elif result["status"] in {"submitting","submission_unknown"}:
            summary=result["message"]
            if result.get("batch_number"):
                sections.append(notice("Oracle 批次号 "+str(result["batch_number"])))
            actions.append(invoke_action("核对 Oracle 建单回执",KEY,"query",{"order_id":args["order_id"]}))
        else:
            raise ProjectFailure(502,"project_order_result_invalid")
    elif action=="lookup_customer":
        candidates=result["candidates"]
        sections.append(table("客户账号候选",[{"key":"name","label":"客户名称"},{"key":"account_number","label":"客户账号"}],candidates))
        sections.append(notice(result["message"]))
        summary="查到 "+str(len(candidates))+" 个客户账号候选，请核对后更正订单。"
    elif action=="lookup_price":
        candidate=result.get("candidate")
        if result.get("price_present") is True:
            basis = {"inclusive": "含税", "exclusive": "未税"}.get(result.get("price_basis"), "税价口径待确认")
            summary = f"第 {result['line_index'] + 1} 行沿用已有单价 {result['current_price']}（{basis}）。"
        elif candidate:
            labels={"price":"价格","price_basis":"含税口径","source":"来源","price_date":"取价日期","price_list_name":"价目表"}
            descriptions={"inclusive":"含税","exclusive":"未税","unknown":"待确认","oracle_history":"历史订单","customer_price_list":"客户价目表"}
            rows=[{"field":label,"value":descriptions.get(str(candidate[key]),candidate[key])} for key,label in labels.items() if key in candidate]
            sections.append(table("订单行 "+str(result["line_index"]+1)+" 的候选价格",[{"key":"field","label":"字段"},{"key":"value","label":"值"}],rows))
            sections.append(notice(result["message"]))
            summary="价格查询结果，采用前须核对含税口径。"
        else:
            sections.append(notice(result["message"]))
            summary="未查到此行候选价。请核对原单，需补价时可在本对话回复行号、单价和含税口径。"
    else:
        stages={"awaiting_submission":"等待建单","awaiting_approval":"等待 Oracle 审核",
                "awaiting_archive":"Oracle 已审核，等待推送鸿翼","archiving":"正在向鸿翼提交",
                "archive_unknown":"推送结果待核查","ecm_uploaded":"原件已上传企业库，收集回执待核对",
                "collection_accepted":"档案收集已受理，等待正式入库","collection_duplicate":"鸿翼提示重复，请在档案收集中核对",
                "archived":"鸿翼已确认正式归档"}
        booking={"in_progress":"Oracle 审核已提交，等待回执","rejected":"Oracle 审核失败，请按原批次核查",
                 "unknown":"Oracle 审核结果待核查"}
        summary=booking.get(result.get("booking_state")) or stages.get(result.get("local_status"),"归档状态待核对")
        if action=="query" and result.get("order_number"):
            summary="Oracle 销售订单号 "+str(result["order_number"])+"。"+summary
    if editors and action in {"review","revise"} and result.get("source_names"):
        result={"id":result["id"],"revision":result["revision"],"full_review_in_card":True}
    if len(json.dumps([result,sections,editors],ensure_ascii=False).encode())>650000:
        raise ProjectFailure(413,"project_order_split_required")
    response_card = card(KEY,uuid4().hex,"needs_input" if actions or needs_correction else "completed",summary,sections=sections,actions=actions,artifacts=artifacts)
    if editors:
        response_card["order_editors"] = editors
    if action in {"submit","confirm_order"} or (action in {"query","review"} and result.get("status") in {"submitting","submission_unknown","submitted"}):
        response_card["sales_order_refs"] = [args["order_id"]]
    value = await svc._save_result(ctx,response_card)
    if needs_correction or (action == "lookup_price" and not result.get("candidate") and not result.get("price_present")):
        value["next_step"] = REVIEW_HANDOFF
    return {**value,"order_result":result,"untrusted_external_content":True}
