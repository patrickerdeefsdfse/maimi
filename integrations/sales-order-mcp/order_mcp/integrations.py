"""Configurable company API adapter. No invented ECM endpoint is enabled by default."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .store import digest, now
from .hongyi import HongyiClient
from .hongyi_collection import file_type

def value_at(data, path):
    for part in path.split("."):
        data = data[part]
    return data


def archive_state(record):
    state = record.get("archive_status","awaiting_submission")
    result = record.get("archive_result") or {}
    # Before V6 integration a completed enterprise-library upload was called archived.
    if state == "archived" and result.get("document_id") and not result.get("collection_status"):
        return "ecm_uploaded"
    return state

def archive_sources(record):
    primary = {field:record[field] for field in ("file_name","sha256","original_path")}
    originals = record.get("originals",[primary])
    if (not isinstance(originals,list) or not originals or not isinstance(originals[0],dict)
            or any(originals[0].get(k)!=v for k,v in primary.items())):
        raise ValueError("订单原件清单与首次登记的原件不一致，请核查。")
    for item in originals:
        if not isinstance(item,dict) or any(not isinstance(item.get(k),str) or not item[k] for k in primary):
            raise ValueError("订单原件清单缺少文件名、位置或校验值。")
        name=item["file_name"]
        if any(c in name for c in "/\\\r\n") or name in (".",".."):
            raise ValueError("销售订单原件文件名无效。")
        with Path(item["original_path"]).open("rb") as f:
            h=hashlib.sha256()
            for chunk in iter(lambda:f.read(1024*1024),b""):
                h.update(chunk)
        if h.hexdigest()!=item["sha256"]:
            raise ValueError("原件内容已变更，拒绝归档。")
    return originals

class CompanyApis:
    def __init__(self, path: Path):
        self.config = json.loads(path.read_text("utf-8")) if path.exists() else {}
        cfg = self.config.get("hongyi", {})
        self.hongyi = HongyiClient(cfg) if cfg.get("enabled") else None

    def endpoint(self, name):
        cfg = self.config.get(name, {})
        if not cfg.get("enabled"):
            raise ValueError(f"{name} 接口尚未配置。请按已确认的公司接口契约填写 integrations.json。")
        url = cfg["url"]
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("公司接口必须使用 HTTPS。")
        headers = {}
        for header, env_name in cfg.get("header_env", {}).items():
            secret = os.getenv(env_name)
            if not secret:
                raise ValueError(f"{name} 接口缺少运行时凭据。")
            headers[header] = secret
        return cfg, headers

    async def request(self, name, values, file_path=None, idempotency_key=None):
        cfg, headers = self.endpoint(name)
        payload = dict(cfg.get("static_fields", {}))
        for local, remote in cfg.get("request_fields", {}).items():
            if local not in values:
                raise ValueError(f"{name} 缺少参数 {local}。")
            payload[remote] = values[local]
        if idempotency_key:
            if not cfg.get("idempotency_header"):
                raise ValueError("归档接口须配置已由服务端支持的幂等头，防止重复归档。")
            headers[cfg["idempotency_header"]] = idempotency_key
        async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
            method = cfg.get("method", "GET").upper()
            if file_path:
                if method != "POST":
                    raise ValueError("归档上传仅支持 POST multipart。")
                with Path(file_path).open("rb") as f:
                    response = await client.post(cfg["url"], headers=headers, data=payload,
                                                 files={cfg["file_field"]: (values["file_name"], f, "application/octet-stream")})
            elif method == "GET":
                response = await client.get(cfg["url"], headers=headers, params=payload)
            elif method == "POST":
                response = await client.post(cfg["url"], headers=headers, json=payload)
            else:
                raise ValueError("只支持 GET/POST 接口。")
        if response.status_code != 200:
            raise ValueError(f"{name} 接口 HTTP {response.status_code}；未确认成功。")
        data = response.json()
        actual = value_at(data, cfg["success_path"])
        if type(actual) is not type(cfg["success_value"]) or actual != cfg["success_value"]:
            raise ValueError(f"{name} 返回业务失败；未确认成功。")
        return {key: value_at(data, path) for key, path in cfg["response_fields"].items()}

    async def approval(self, order_number, business_entity):
        result = await self.request("approval", {"order_number": order_number, "business_entity": business_entity})
        if str(result.get("order_number")) != str(order_number):
            raise ValueError("审核接口返回了其他订单，拒绝归档。")
        if result.get("business_entity") != business_entity:
            raise ValueError("审核接口业务实体不匹配，拒绝跨实体归档。")
        cfg = self.config["approval"]
        result["approved"] = result.get("status") in cfg["approved_values"]
        result["checked_at"] = now()
        return result

class ArchiveService:
    def __init__(self, service, apis=None):
        self.service = service
        self.apis = apis or CompanyApis(service.settings.integration_config)
        self.locks = {}

    async def sync(self, rid):
        import asyncio
        lock = self.locks.setdefault(rid, asyncio.Lock())
        async with lock:
            return await self._sync(rid)

    async def _sync(self, rid):
        store = self.service.store
        record = store.get(rid)
        if record["status"] != "submitted":
            raise ValueError("须先成功建单，再查询审核和归档。")
        state = archive_state(record)
        if state in {"archived", "collection_accepted", "collection_duplicate", "ecm_uploaded"}:
            return {**record["archive_result"], "archive_status":state}
        if record.get("archive_status") in {"archiving", "archive_unknown"}:
            raise ValueError("归档结果待核查，请按幂等号查询 ECM，禁止重复上传。")
        oracle = record["oracle_result"]
        approval = (store.business_approval(record) if hasattr(store, "business_approval")
                    else await self.apis.approval(oracle["order_number"], record["order"]["business_entity"]))
        old_approval = record.get("approval",{})
        record["approval"] = approval
        if not approval["approved"]:
            record["archive_status"] = "awaiting_approval"
            if {k:v for k,v in old_approval.items() if k!="checked_at"} != {k:v for k,v in approval.items() if k!="checked_at"}:
                store.save(record, "approval_checked", approval)
            return {"archive_status": "awaiting_approval", "approval": approval}
        key = digest(["archive", record["business_key"], record["sha256"]])
        values = {"order_number": oracle["order_number"], "batch_number": oracle["batch_number"],
                  "customer_po": record["order"]["customer_po"], "customer_name": record["order"]["customer_name"],
                  "account_number": record["order"]["account_number"], "business_entity": record["order"]["business_entity"],
                  "file_name": record["file_name"], "sha256": record["sha256"], "idempotency_key": key,
                  "order_json": json.dumps(record["order"], ensure_ascii=False)}
        # V6 rules choose their own outbound fields. Make confirmed order and human approval fields available.
        for name in ("order_date","currency","salesrep_name","order_type","total_amount","head_remark"):
            if record["order"].get(name) is not None:
                values[name] = record["order"][name]
        if approval.get("approved_by"):
            values["approved_by"] = approval["approved_by"]
        if approval.get("checked_at"):
            values["approved_at"] = approval["checked_at"]
        hongyi = getattr(self.apis, "hongyi", None)
        if hongyi:
            hongyi.check(values)
        else:
            if isinstance(self.apis, CompanyApis) and hasattr(store,"business_approval"):
                raise ValueError("请为客户销售订单启用鸿翼 V6 ECM 收集规则，原件仍待归档。")
            cfg, _ = self.apis.endpoint("archive_upload")
            if not cfg.get("idempotency_header"):
                raise ValueError("通用归档接口必须支持已配置的幂等头。")
        sources=archive_sources(record)
        if len(sources)>1:
            if not hongyi:
                raise ValueError("多原件订单需要配置鸿翼 V6 收集接口。")
            for item in sources:
                file_type(hongyi.config["collection"],item["file_name"])
            return await self._archive_many(record,sources,values,hongyi)
        original = Path(sources[0]["original_path"])
        # A lookup can be retried. Reserve the right to write before the first ECM mutation.
        claimed = False
        def reserve(receipt=None):
            nonlocal record, claimed
            pending = {**record, "archive_status": "archiving", "archive_key": key}
            if receipt:
                pending["archive_receipt"] = receipt
            record = store.begin_unique("archive:" + key, pending, "archive_started")
            claimed = True

        try:
            if hongyi:
                def checkpoint(receipt):
                    nonlocal record
                    record["archive_receipt"] = receipt
                    record = store.save(record, "archive_checkpoint", receipt)
                result = await hongyi.archive(original, values, key, checkpoint, before_create=reserve)
            else:
                reserve()
                result = await self.apis.request("archive_upload", values, original, key)
            if not result.get("document_id"):
                raise ValueError("归档接口没有返回文档 ID。")
            stages = {"accepted":"collection_accepted", "duplicate":"collection_duplicate", "archived":"archived"}
            outcome = result.get("collection_status")
            if outcome is not None and outcome not in stages:
                raise ValueError("档案收集回执状态待核查。")
            state = stages.get(outcome,"ecm_uploaded")
            record.update(archive_status=state, archive_result=result)
            store.save(record, state, result)
            store.submission("archive:" + key, state, result)
            return {**result,"archive_status":state}
        except BaseException:
            if not claimed:
                raise
            record["archive_status"] = "archive_unknown"
            store.save(record, "archive_unknown")
            store.submission("archive:" + key, "unknown", {})
            raise ValueError("归档结果待核查，未标记成功；请按 archive_key 查询 ECM。")

    async def _archive_many(self,record,sources,values,hongyi):
        store=self.service.store
        key=digest(["archive-order-v2",record["business_key"]])
        values={**values,"idempotency_key":key}
        manifest=digest([[s["file_name"],s["sha256"]] for s in sources])
        progress=record.get("archive_receipt") or {}
        if record.get("archive_key"):
            if (record["archive_key"]!=key or progress.get("manifest")!=manifest or
                    progress.get("stage")!="ready" or not isinstance(progress.get("documents"),list)):
                raise ValueError("归档检查点与当前订单不符，请核查。")
        else:
            progress={"stage":"ready","manifest":manifest,"business_unique_id":key,"documents":[]}
            record.update(archive_key=key,archive_receipt=progress,archive_status="awaiting_archive")
            record=store.begin_unique("archive:"+key,record,"archive_started")
        # The store's revision CAS must complete before each remote mutation.
        # A completed upload is resumable, an unacknowledged mutation requires reconciliation.
        def checkpoint(stage,documents,extra):
            nonlocal record
            receipt={"manifest":manifest,"business_unique_id":key,"stage":stage,"documents":list(documents),**extra}
            record=store.save({**record,"archive_status":"awaiting_archive" if stage=="ready" else "archiving",
                               "archive_receipt":receipt},"archive_checkpoint",{"stage":stage,"completed_files":len(documents)})
        try:
            result=await hongyi.archive_many(sources,values,key,progress,checkpoint)
        except BaseException:
            if record["archive_status"]=="archiving":
                store.save({**record,"archive_status":"archive_unknown"},"archive_unknown")
                store.submission("archive:"+key,"unknown",{})
            raise
        state={"accepted":"collection_accepted","duplicate":"collection_duplicate","archived":"archived"}[result["collection_status"]]
        store.save({**record,"archive_status":state,"archive_result":result},"archive_completed",{"status":state,"files":len(sources)})
        store.submission("archive:"+key,state,result)
        return {**result,"archive_status":state}

    async def query(self, rid):
        record = self.service.store.get(rid)
        if not record.get("archive_key"):
            attempt = record.get("booking_attempt") or {}
            return {"local_status":archive_state(record), "booking_state":attempt.get("state"),
                    "order_number":record.get("oracle_result",{}).get("order_number"),"approval":record.get("approval")}
        hongyi = getattr(self.apis, "hongyi", None)
        if hongyi:
            receipt = record.get("archive_result") or record.get("archive_receipt", {})
            if isinstance(receipt.get("documents"),list):
                docs=list(receipt["documents"])
                if receipt.get("current"):
                    docs.append(receipt["current"])
                remote=[await hongyi.query(d, d.get("filename")) for d in docs]
                return {"local_status":archive_state(record),"remote_result":{"documents":remote,
                        "stage":receipt.get("stage"),"collection_status":receipt.get("collection_status","unconfirmed"),
                        "collection_receipt":receipt.get("collection_receipt")}}
            result = await hongyi.query(receipt, receipt.get("filename"))
            return {"local_status": archive_state(record), "remote_result": result}
        result = await self.apis.request("archive_query", {"idempotency_key": record.get("archive_key", ""),
                                         "order_number": record.get("oracle_result", {}).get("order_number", ""),
                                         "document_id": record.get("archive_result", {}).get("document_id", "")})
        # Query doesn't silently resolve unknown outcomes; review authoritative receipt first.
        return {"local_status": archive_state(record), "remote_result": result}
