"""鸿翼 ECM adapter, based on the supplied 21-page V3 document.

No undocumented retry/idempotency API is assumed. ArchiveService owns a durable
local ledger and blocks retries for ambiguous results and duplicate names.
"""
import os
import re
import uuid
import hashlib
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryFile
from urllib.parse import urlparse
import httpx
from . import hongyi_collection

# Confirmed by the business owner on 2026-11-17. Browser references identify
# the intended location; API folder IDs must be verified separately.
SALES_ORDER_DESTINATION = {
    "name": "销售订单专用归档",
    "parent_name": "100 示例销售档案",
    "parent_directory_reference": "9000001",
    "reference_url": "https://archive.example.invalid/folders/9000001",
    "parent_folder_id": 0,
    "folders": [
        {"name": "01 示例部门甲", "folder_id": 0, "parent_folder_id": 0,
         "business_entities": ["示例制造乙有限公司"]},
        {"name": "02 示例部门乙", "folder_id": 0, "parent_folder_id": 0,
         "business_entities": ["示例制造丙有限公司"]},
        {"name": "03 示例部门丙", "folder_id": 0, "parent_folder_id": 0,
         "business_entities": ["示例制造甲有限公司"]},
        {"name": "04 示例部门丁", "folder_id": 0, "parent_folder_id": 0, "business_entities": []},
        {"name": "05 示例部门戊", "folder_id": 0, "parent_folder_id": 0, "business_entities": []},
        {"name": "06 示例部门己", "folder_id": 0, "parent_folder_id": 0, "business_entities": []},
        {"name": "07 示例部门庚", "folder_id": 0, "parent_folder_id": 0, "business_entities": []},
        {"name": "08 示例部门辛", "folder_id": 0, "parent_folder_id": 0, "business_entities": []},
    ],
}


def verified_parent_reference():
    return os.getenv("ORDER_ECM_VERIFIED_PARENT_REFERENCE", SALES_ORDER_DESTINATION["parent_directory_reference"])


def positive_id(value):
    return type(value) is int and value > 0


@contextmanager
def verified_stream(source):
    # Upload the same bytes that were checked against the reviewed order.
    with TemporaryFile("w+b") as snapshot:
        checksum = hashlib.sha256()
        with Path(source["original_path"]).open("rb") as f:
            for block in iter(lambda: f.read(1024*1024), b""):
                checksum.update(block)
                snapshot.write(block)
        if checksum.hexdigest()!=source["sha256"]:
            raise ValueError("原件内容已变更，拒绝归档。")
        size=snapshot.tell()
        snapshot.seek(0)
        yield snapshot,size


def archive_target_status(config=None):
    """Return only destination facts, without integration tokens or form data."""
    destination = (config or {}).get("destination") or SALES_ORDER_DESTINATION
    folders = destination.get("folders", [])
    parent = destination.get("parent_folder_id")
    collection = hongyi_collection.status((config or {}).get("collection") or {})
    cfg = config or {}
    ready_credentials = bool(os.getenv(cfg.get("token_env","ORDER_ECM_TOKEN")) or (
        os.getenv(cfg.get("login_name_env","ORDER_ECM_LOGIN_NAME")) and
        os.getenv(cfg.get("integration_key_env","ORDER_ECM_INTEGRATION_KEY")) and cfg.get("ip_address")))
    try:
        HongyiClient(cfg).check_connection()
        connection_ready = True
    except ValueError:
        connection_ready = False
    possible_route = positive_id(parent) and str(destination.get("parent_directory_reference","")) == verified_parent_reference() and any(
        positive_id(x.get("folder_id")) and x["folder_id"] != parent and x.get("parent_folder_id") == parent
        and x.get("business_entities") for x in folders)
    return {
        "name": destination.get("name", "销售订单专用归档"),
        "parent_name": destination.get("parent_name", ""),
        "parent_directory_reference": destination.get("parent_directory_reference", ""),
        "reference_url": destination.get("reference_url", ""),
        "collection": collection,
        "ready": bool(cfg.get("enabled") and connection_ready and ready_credentials and possible_route and collection["ready"]),
        "ecm_connection_configured": connection_ready,
        "ecm_credentials_configured": ready_credentials,
        "parent_folder_id_configured": positive_id(parent),
        "folders": [{"name": x.get("name", ""), "business_entities": x.get("business_entities", []),
                     "folder_id_configured": positive_id(x.get("folder_id")) and x.get("parent_folder_id") == parent}
                    for x in folders],
    }


class HongyiClient:
    def __init__(self, config: dict, transport=None):
        self.config = config
        self.base = config.get("base_url", "").rstrip("/")
        self.transport = transport

    def check_connection(self):
        parsed = urlparse(self.base)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and self.config.get("allow_internal_http") is True):
            raise ValueError("鸿翼地址须配置 HTTPS；既有内网 HTTP 须管理员显式启用 allow_internal_http。")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("鸿翼 base_url 不合法。")
    def folder(self, business_entity=None):
        destination = self.config.get("destination")
        if destination:
            parent = destination.get("parent_folder_id")
            if not positive_id(parent):
                raise ValueError("请先核验销售订单归档父目录的 API folderId。")
            matches = [x for x in destination.get("folders", [])
                       if business_entity and business_entity in x.get("business_entities", [])]
            if len(matches) != 1:
                raise ValueError("业务实体尚未唯一对应销售订单归档子目录，不会自动归入其他主体。")
            selected = matches[0]
            folder_id = selected.get("folder_id")
            if not positive_id(folder_id) or folder_id == parent or selected.get("parent_folder_id") != parent:
                raise ValueError("请先核验该业务实体子目录的 API folderId 及父目录关系。")
            return folder_id, selected.get("name", "")
        folder_id = self.config.get("folder_id")
        if not positive_id(folder_id):
            raise ValueError("请配置鸿翼订单归档文件夹 folder_id。")
        return folder_id, ""

    def check_metadata(self):
        fields = self.config.get("metadata_fields")
        if not self.config.get("form_id") or not isinstance(fields, dict) or not fields:
            raise ValueError("请配置鸿翼订单元数据 form_id 及实际 eform 字段映射。")
        if any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", x) for x in fields.values()):
            raise ValueError("鸿翼元数据字段映射须使用真实字段名。")
        if len(set(fields.values())) != len(fields):
            raise ValueError("不同订单字段不能映射到同一个鸿翼元数据字段。")
        if any("REPLACE" in x for x in fields.values()):
            raise ValueError("请将鸿翼示例字段替换为实际元数据字段名。")

    def check(self, values=None):
        collection = self.config.get("collection") or {}
        hongyi_collection.validate(collection, values)
        self.check_connection()
        if str((self.config.get("destination") or {}).get("parent_directory_reference", "")) != verified_parent_reference():
            raise ValueError("请配置并核验 100 示例销售档案及主体子目录。")
        self.folder((values or {}).get("business_entity"))
        if not os.getenv(self.config.get("token_env","ORDER_ECM_TOKEN")) and not (
            os.getenv(self.config.get("login_name_env","ORDER_ECM_LOGIN_NAME")) and
            os.getenv(self.config.get("integration_key_env","ORDER_ECM_INTEGRATION_KEY")) and self.config.get("ip_address")
        ):
            raise ValueError("请配置鸿翼企业库访问凭据 token_env，或企业库集成登录配置。")

    async def _send(self, client, method, path, *, body=None, form=None, params=None, files=None, base=None, wrapped=True):
        url = (base or self.base) + path
        kwargs = {"params": params} if params is not None else {}
        if body is not None:kwargs["json"] = body
        if form is not None:
            # Explicit multipart even when no file part is present (WebCore requirement).
            kwargs["files"] = [(k, (None, str(v))) for k, v in form.items()] + (files or [])
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            # Download/metadata APIs carry tokens in query parameters; do not echo URLs.
            raise ValueError("鸿翼网络请求失败，结果待核查。") from None
        if response.status_code != 200:
            raise ValueError(f"鸿翼接口 HTTP {response.status_code}，结果未确认。")
        data = response.json()
        if wrapped and (type(data.get("result")) is not int or data["result"] != 0):
            raise ValueError("鸿翼返回业务错误，未确认成功。")
        return data

    async def _token(self, client):
        token = os.getenv(self.config.get("token_env", "ORDER_ECM_TOKEN"), "")
        if token:return token
        login = os.getenv(self.config.get("login_name_env", "ORDER_ECM_LOGIN_NAME"), "")
        key = os.getenv(self.config.get("integration_key_env", "ORDER_ECM_INTEGRATION_KEY"), "")
        ip = self.config.get("ip_address", "")
        if not login or not key or not ip:
            raise ValueError("请由宿主提供鸿翼 token，或配置登录名、集成 key 和 IP。")
        response = await self._send(client, "POST", "/api/services/Org/UserLoginIntegrationByUserLoginName",
                                    body={"loginName": login, "ipAddress": ip, "integrationKey": key})
        token = response.get("data")
        if not isinstance(token, str) or not token:
            raise ValueError("鸿翼未返回有效 token。")
        return token

    def _region(self, value):
        if not value:return self.base
        region = str(value).rstrip("/")
        allowed = {self.base, *(str(x).rstrip("/") for x in self.config.get("allowed_region_urls", []))}
        if region not in allowed:
            raise ValueError("鸿翼返回未配置的分区域地址；为避免令牌发往错误地址，请管理员确认加入允许列表。")
        return region

    async def _search(self, client, token, filename, folder_id):
        result = await self._send(client, "POST", "/api/services/DocSearch/SearchFileList", body={
            "token": token, "keyWord": filename, "searchType": "1", "folderId": folder_id,
            "searchPattern": True, "pageIndex": 1, "pageSize": 100, "startNum": 0, "lang": "zh-cn", "metaDataSearch": False})
        info = result["data"]["DocListInfo"]
        return {"files": info.get("FilesInfo", []), "total": info.get("Settings", {}).get("searchTotalCount", 0)}

    def _metadata_values(self, values):
        parts = []
        for local, field in self.config["metadata_fields"].items():
            if local not in values or values[local] is None:
                raise ValueError("鸿翼元数据映射引用了缺失的订单字段，请核对实际字段映射。")
            value = str(values[local])
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field) or any(x in value for x in "|=\r\n") or value != value.strip():
                raise ValueError("鸿翼 dataset 不支持该字段名或包含 |、=、换行/首尾空格的值，请确认映射和原文。")
            if field in {"EntityId", "EntityType", "metaStrategy", "id"}:
                raise ValueError("元数据映射不能覆盖鸿翼保留字段。")
            parts.append(field + "=" + value)
        return parts

    async def archive(self, path: Path, values: dict, key: str, checkpoint, *, before_create=None):
        self.check(values)
        source_name = str(values.get("file_name") or path.name)
        if not source_name or source_name != Path(source_name).name or any(c in source_name for c in "/\\\r\n"):
            raise ValueError("销售订单原件文件名无效。")
        collection = self.config["collection"]
        hongyi_collection.file_type(collection,source_name)
        with verified_stream({"original_path": path, "sha256": values["sha256"]}) as snapshot:
            original = await self._upload_file(path, values, key, checkpoint,
                                               before_create=before_create, verified=snapshot)
        checkpoint({**original,"stage":"collection_submitting","business_unique_id":key})
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, transport=self.transport) as client:
            result = await hongyi_collection.collect(client, collection, values, key, [(source_name, original["document_id"],key)])
        receipt = {**original, **result}
        checkpoint({**receipt,"stage":"collection_"+result["collection_status"]})
        return receipt

    async def _upload_file(self, path: Path, values: dict, key: str, checkpoint, before_create=None, verified=None):
        folder_id, folder_name = self.folder(values.get("business_entity"))
        safe_order = re.sub(r"[^A-Za-z0-9_-]", "_", str(values["order_number"]))[:50]
        filename = f"SO-{safe_order}-{key[:24]}{path.suffix.lower()}"
        size = verified[1] if verified is not None else path.stat().st_size
        upload_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "megmeet-order:" + key))
        async with httpx.AsyncClient(timeout=120, follow_redirects=False, transport=self.transport) as client:
            token = await self._token(client)
            found = await self._search(client, token, filename, folder_id)
            if found["files"] or found["total"]:
                raise ValueError("鸿翼已存在同名归档文件，请核查既有文档；不会自动覆盖或新建版本。")
            if before_create:
                before_create({"stage":"file_creating","filename":filename,"folder_id":folder_id})
            created = await self._send(client, "POST", "/WebCore", form={"token": token, "module": "RegionDocOperationApi",
                "fun": "CheckAndCreateDocInfo", "fileModel": "UPLOAD", "filename": filename, "size": size, "folderId": folder_id})
            data = created["data"]
            if data.get("ExistsFileId"):
                raise ValueError("鸿翼返回重名文件，停止上传。")
            file_id, version = data["FileId"], data["FileVerId"]
            if any(type(value) not in (str,int) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",str(value))
                   or str(value) == "0" for value in (file_id, version)):
                raise ValueError("鸿翼未返回文件 ID 和版本 ID。")
            checkpoint({"stage": "file_created", "document_id": str(file_id), "file_version_id": str(version), "filename": filename,
                        "upload_id": upload_id, "folder_id": folder_id, "folder_name": folder_name})
            region = self._region(data.get("RegionUrl"))
            with (nullcontext(verified[0]) if verified is not None else path.open("rb")) as stream:
                result = await self._send(client, "POST", "/document/upload", base=region, wrapped=False,
                    form={"token": token, "uploadId": upload_id, "regionHash": data["RegionHash"], "regionId": data["RegionId"],
                          "size": size, "blockSize": size}, files=[("file", (filename, stream, "application/octet-stream"))])
            # HTTP 200 / errorCode=0 while Uploading is NOT a completed upload.
            if type(result.get("errorCode")) is not int or result["errorCode"] != 0 or result.get("status") != "End":
                raise ValueError("鸿翼文件上传尚未确认结束。")
            receipt = {"document_id":str(file_id),"file_version_id":str(version),"filename":filename,
                    "folder_id":folder_id,"folder_name":folder_name,"preview_path":f"/preview.html?fileid={file_id}",
                    "preview_requires_host_token":True}
            checkpoint({**receipt,"stage":"file_uploaded"})
            return receipt

    async def archive_many(self, sources, values, key, progress, checkpoint):
        from .store import digest
        documents = list(progress["documents"])
        for index, source in enumerate(sources):
            file_key = digest([key, index, source["sha256"]])
            if index < len(documents):
                doc = documents[index]
                if (doc.get("original_key") != file_key or doc.get("source_name") != source["file_name"]
                        or not doc.get("document_id")):
                    raise ValueError("已上传原件的回执与本订单不符，请核查。")
                continue
            def note(receipt):
                stage = receipt["stage"]
                detail = {k:v for k,v in receipt.items() if k!="stage"}
                detail.update(source_name=source["file_name"],original_key=file_key,sha256=source["sha256"])
                if stage == "file_uploaded":
                    documents.append(detail)
                    checkpoint("ready", documents, {})
                else:
                    checkpoint(stage, documents, {"current":detail})
            with verified_stream(source) as snapshot:
                await self._upload_file(Path(source["file_name"]),values,file_key,note,before_create=note,verified=snapshot)
        if len(documents)!=len(sources) or len({d["document_id"] for d in documents})!=len(sources):
            raise ValueError("原件的企业库回执数量或 fileId 异常，请核查。")
        checkpoint("collection_submitting",documents,{})
        payload = [(doc["source_name"],doc["document_id"],doc["original_key"]) for doc in documents]
        async with httpx.AsyncClient(timeout=60,follow_redirects=False,transport=self.transport) as client:
            result = await hongyi_collection.collect(client,self.config["collection"],values,key,payload)
        return {**documents[0],**result,"documents":documents}

    async def upload_ecm_with_metadata(self, path: Path, values: dict, key: str, checkpoint):
        """Support legacy ECM-only uploads without reporting EDRMS acceptance."""
        self.check_connection()
        self.check_metadata()
        metadata = self._metadata_values(values)
        receipt = await self._upload_file(path,values,key,checkpoint)
        file_id, version = receipt["document_id"], receipt["file_version_id"]
        file_id = int(file_id) if file_id.isdecimal() else file_id
        version = int(version) if version.isdecimal() else version
        record_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":record"))
        map_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":map"))
        dataset = "|".join([f"EntityId={file_id}", "EntityType=2", "metaStrategy=0", *metadata])
        async with httpx.AsyncClient(timeout=120, follow_redirects=False, transport=self.transport) as client:
            token = await self._token(client)
            result = await self._send(client, "POST", "/api/services/MetaData/UpdateEntityMetaMapEx", body={
                "token": token, "fileId": file_id, "fileVerId": version, "fileType": 2, "operateType": "manage",
                "formId": self.config["form_id"], "formVer": "", "recordId": record_id, "dataset": dataset, "mapId": map_id})
            detail = result.get("data", {})
            if detail.get("code") != 0 or detail.get("result") != 0 or str(detail.get("entityId")) != str(file_id):
                raise ValueError("文件已上传，但订单元数据未确认写入，不能标记归档完成。")
            # Never put the ECM token into logs, return values, or chat links.
            return {**receipt,"collection_status":"ecm_only","map_id":detail.get("entityMetaMap", {}).get("id", map_id),
                    "meta_record_id":detail.get("metaRecordId",record_id)}

    async def query(self, receipt: dict, filename=None):
        self.check_connection()
        folder_id = None
        if not receipt.get("document_id"):
            if not filename:
                raise ValueError("缺少可查询的文档 ID 或归档文件名。")
            folder_id = receipt.get("folder_id")
            if not positive_id(folder_id):
                folder_id, _ = self.folder(receipt.get("business_entity"))
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, transport=self.transport) as client:
            token = await self._token(client)
            if not receipt.get("document_id"):
                return await self._search(client, token, filename, folder_id)
            result = await self._send(client, "GET", "/api/services/MetaData/GetMetaDatasByFileId", params={
                "token": token, "fileId": receipt["document_id"], "docType": 2, "fileVerId": receipt.get("file_version_id", "")})
            return {"document_id": receipt["document_id"], "metadata": result["data"],
                    "collection_status":receipt.get("collection_status", "unconfirmed"),
                    "collection_receipt":receipt.get("collection_receipt"),"collection_source":"saved_receipt",
                    "preview_path": f"/preview.html?fileid={receipt['document_id']}", "preview_requires_host_token": True}
