"""Original sales-order FastAPI contract, verified against 销售订单接口(1).docx."""
import asyncio
import io
import json
import os
import re
import zipfile

import httpx
from openpyxl import load_workbook
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

TEST = "https://service-3.example.invalid/v1/mc-saleorder-extract"
PRODUCTION = "https://service-4.example.invalid/v1/mc-saleorder-extract"
EXCEL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LIMIT = 30 * 1024 * 1024


class ExtractionError(ValueError):
    pass


class FastAPIExtractor:
    def __init__(self, base, platform_token="", *, transport=None, socket=connect):
        if base not in (TEST,PRODUCTION,TEST+"/noauth"):
            raise ExtractionError("识别服务地址不在正式接口文档的可用配置中。")
        self.base, self.token = base, platform_token
        self.transport, self.socket = transport, socket

    @classmethod
    def configured(cls):
        return cls(os.getenv("ORDER_EXTRACT_URL",PRODUCTION).rstrip("/"),os.getenv("ORDER_EXTRACT_PLATFORM_TOKEN",""))

    def check_config(self):
        if not self.base.endswith("/noauth") and not self.token:
            raise ExtractionError("销售订单 FastAPI 尚未配置平台认证。")

    def target_status(self):
        """Return administrator-selected environment and readiness, never a token."""
        noauth = self.base == TEST + "/noauth"
        return {"environment": "test" if self.base in {TEST, TEST + "/noauth"} else "production",
                "configured": noauth or bool(self.token),
                "authentication": "test_noauth" if noauth else "platform_token",
                "selection": "administrator_configured"}

    async def extract(self, name, raw, employee, display_name):
        try:
            return await self._extract(name, raw, employee, display_name)
        except (TimeoutError, httpx.TimeoutException):
            raise ExtractionError("销售订单服务响应超时，请稍后重新识别。") from None
        except (httpx.HTTPError, ConnectionClosedError, InvalidHandshake):
            raise ExtractionError("销售订单识别服务暂时无法连接。") from None

    async def _extract(self, name, raw, employee, display_name):
        self.check_config()
        if not re.fullmatch(r"M[0-9]{8}",employee) or not isinstance(display_name,str) or not display_name.strip():
            raise ExtractionError("缺少来自麦米登录状态的工号或姓名。")
        if not 0<len(raw)<=LIMIT:
            raise ExtractionError("文件为空或超过 30 MiB。")
        identity={"userNo":employee,"username":display_name}
        headers={"Authorization":"Bearer "+self.token} if self.token and not self.base.endswith("/noauth") else {}
        async with asyncio.timeout(360),httpx.AsyncClient(trust_env=False,follow_redirects=False,headers=headers,
                    transport=self.transport,timeout=httpx.Timeout(120,connect=12)) as client:
            async with client.stream("POST",self.base+"/fileUpload",data=identity,files={"file":(name,raw,"application/octet-stream")}) as response:
                try:
                    meta=json.loads(await self.read_response(response,65536,"上传"))
                except (json.JSONDecodeError, UnicodeError):
                    raise ExtractionError("销售订单上传回执无效。") from None
            if not isinstance(meta,dict):
                raise ExtractionError("销售订单上传回执无效。")
            fid,md=meta.get("fileHash"),meta.get("filename")
            if (not isinstance(fid,str) or not re.fullmatch(r"[0-9a-fA-F]{64}",fid) or not isinstance(md,str)
                    or not md.endswith(".md") or len(md)>255 or re.search(r"[\\/\x00-\x1f]",md) or md in (".","..")):
                raise ExtractionError("销售订单上传回执无效。")
            uri=self.base.replace("https://","wss://",1)+"/ws/stream-extract"
            async with self.socket(uri,additional_headers=headers,open_timeout=15,close_timeout=3,max_size=4*1024*1024,proxy=None) as ws:
                await ws.send(json.dumps({"file":[{"fileHash":fid,"filename":md}]},ensure_ascii=False))
                rows=await self.consume(ws)
            result = {"rows": rows, "source_hash": fid}
            try:
                async with asyncio.timeout(30), client.stream("POST",self.base+"/download",json={**identity,"fileHash":fid}) as response:
                    if response.status_code in {404, 410} or 500 <= response.status_code < 600:
                        return result
                    workbook=await self.read_response(response,LIMIT,"下载")
                    disposition=response.headers.get("content-disposition","").lower()
            except (TimeoutError,httpx.TransportError):
                return result
            try:
                kind=self.verify_workbook(workbook,disposition)
            except (ExtractionError,zipfile.BadZipFile):
                return result
            return result | {"workbook":workbook,"workbook_kind":kind}

    @staticmethod
    async def read_response(response, maximum, phase):
        if response.status_code!=200:
            raise ExtractionError(f"销售订单{phase}接口返回 HTTP {response.status_code}。")
        data=bytearray()
        async for chunk in response.aiter_bytes():
            if len(data)+len(chunk)>maximum:
                raise ExtractionError("销售订单服务返回内容过大。")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    async def consume(ws):
        result=None
        async for frame in ws:
            if not isinstance(frame,str):
                raise ExtractionError("销售订单识别流格式无效。")
            for line in frame.splitlines():
                line=line.strip()
                if not line.strip():continue
                if line.startswith("{"):
                    try:
                        obj=json.loads(line)
                    except json.JSONDecodeError:
                        raise ExtractionError("销售订单识别流格式无效。") from None
                    if obj.get("type")=="error":raise ExtractionError("销售订单服务报告识别失败。")
                    continue
                if not line.startswith("data:"):continue
                msg=line[5:].strip()
                if msg=="[INTERRUPTED]" or re.fullmatch(r"\d+\|\[ERROR\].*",msg):
                    raise ExtractionError("销售订单识别未完成。")
                if msg.startswith("[ALL_DONE]"):
                    try:
                        rows=json.loads(msg.removeprefix("[ALL_DONE]").strip())
                    except json.JSONDecodeError:
                        raise ExtractionError("销售订单识别流格式无效。") from None
                    if result is not None or not isinstance(rows,list) or len(rows)>500 or any(not isinstance(r,dict) for r in rows):
                        raise ExtractionError("销售订单行结构无效。")
                    result=rows
                elif msg=="[DONE]":
                    if result is None:raise ExtractionError("服务结束前没有返回最终订单行。")
                    return result
        raise ExtractionError("销售订单识别流提前结束。")

    @staticmethod
    def verify_workbook(data,disposition=""):
        if data.startswith(b"%TSD-Header") and len(data)>=512 and re.search(r"filename\*?=(?:utf-8''|\x22)?result\.xlsx(?:\x22|$)",disposition):
            return "protected_xlsx"
        if not data.startswith(b"PK"):
            raise ExtractionError("销售订单下载响应不是 Excel 文件。")
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            if sum(x.file_size for x in bundle.infolist())>100*1024*1024:
                raise ExtractionError("销售订单工作簿内容过大。")
        wb=load_workbook(io.BytesIO(data),read_only=True,data_only=True)
        try:
            if "销货订单导入" not in wb.sheetnames:
                raise ExtractionError("销售订单工作簿缺少标准工作表。")
        finally:
            wb.close()
        return "xlsx"
