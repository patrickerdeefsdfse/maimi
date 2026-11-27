import io
import json

import httpx
import pytest
from openpyxl import Workbook

from order_mcp.fastapi_extract import ExtractionError, FastAPIExtractor, PRODUCTION, TEST
from order_mcp.fastapi_rows import mapped_orders


class Socket:
    def __init__(self, frames):
        self.frames = frames
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        async def stream():
            for frame in self.frames:
                yield frame
        return stream()


async def test_documented_upload_stream_download_keeps_distinct_order_lines():
    row = {"业务实体": "示例制造乙有限公司", "客户名称": "示例员甲(购方)", "客户代号": "910001",
           "客户PO号": "QA-001", "订购项目": "89000000DEMO400", "订单数量": 50, "含税单价": 79,
           "请求日期": "2026-12-30"}
    rows = [row, row | {"订单数量": 60}]
    ws = Socket(["data: 0|[WORK]", "data: 0|[DONE]", "data: [ALL_DONE]" + json.dumps(rows), "data: [DONE]"])
    output = io.BytesIO()
    wb = Workbook()
    wb.active.title = "销货订单导入"
    wb.active.append(list(row))
    wb.active.append(list(row.values()))
    wb.save(output)
    calls = []

    def request(req):
        calls.append(req.url.path)
        assert req.headers["authorization"] == "Bearer synthetic-only"
        if req.url.path.endswith("/fileUpload"):
            assert "multipart/form-data" in req.headers["content-type"]
            return httpx.Response(200, json={"fileHash": "a" * 64, "filename": "synthetic.v2.md"})
        assert req.url.path.endswith("/download")
        assert json.loads(req.content) == {"userNo": "M90000001", "username": "示例员甲测试员工", "fileHash": "a" * 64}
        return httpx.Response(200, content=output.getvalue())

    def socket(uri, **options):
        assert uri == PRODUCTION.replace("https://", "wss://") + "/ws/stream-extract"
        assert options["additional_headers"]["Authorization"] == "Bearer synthetic-only"
        return ws

    extractor = FastAPIExtractor(PRODUCTION, "synthetic-only", transport=httpx.MockTransport(request), socket=socket)
    data = await extractor.extract("synthetic.pdf", b"synthetic-only", "M90000001", "示例员甲测试员工")
    assert calls == ["/v1/mc-saleorder-extract/fileUpload", "/v1/mc-saleorder-extract/download"]
    assert ws.sent == [{"file": [{"fileHash": "a" * 64, "filename": "synthetic.v2.md"}]}]
    groups = mapped_orders(data["rows"])
    assert len(groups) == 1
    order, evidence, positions = groups[0]
    assert order.account_number == "910001"
    assert [line.customer_item for line in order.lines] == ["89000000DEMO400"] * 2
    assert all(line.item_code is None for line in order.lines)
    assert [str(line.quantity) for line in order.lines] == ["50", "60"]
    assert positions == [1, 2] and "第 2 行" in evidence["lines.1.quantity"]


@pytest.mark.parametrize("frames", [["data: [ALL_DONE]{"], ["data: [DONE]"], ["data: [INTERRUPTED]"],
                                  ["data: 0|[ERROR]"], ["data: [ALL_DONE][]"]])
async def test_incomplete_or_invalid_stream_never_returns_orders(frames):
    with pytest.raises(ExtractionError):
        await FastAPIExtractor.consume(Socket(frames))


@pytest.mark.parametrize("body", [b"[1]", b"not-json"])
async def test_bad_upload_receipt_has_actionable_message(body):
    extractor = FastAPIExtractor(PRODUCTION, "synthetic-only", transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
    with pytest.raises(ExtractionError, match="上传回执无效"):
        await extractor.extract("synthetic.pdf", b"synthetic-only", "M90000001", "示例员甲测试员工")


async def test_timeout_is_reported_without_internal_connection_detail():
    def request(req):
        raise httpx.ReadTimeout("private internal diagnostic")
    extractor = FastAPIExtractor(PRODUCTION, "synthetic-only", transport=httpx.MockTransport(request))
    with pytest.raises(ExtractionError, match="服务响应超时") as error:
        await extractor.extract("synthetic.pdf", b"synthetic-only", "M90000001", "示例员甲测试员工")
    assert "private" not in str(error.value)


@pytest.mark.parametrize("download_status", [404, 503, 403])
async def test_completed_row_stream_and_missing_optional_workbook(download_status):
    socket = Socket(["data: [ALL_DONE][]", "data: [DONE]"])
    def respond(req):
        if req.url.path.endswith("/fileUpload"):
            return httpx.Response(200, json={"fileHash":"a"*64,"filename":"qa.md"})
        return httpx.Response(download_status)
    provider = FastAPIExtractor(TEST+"/noauth", transport=httpx.MockTransport(respond),socket=lambda *a,**kw:socket)
    if download_status == 403:
        with pytest.raises(ExtractionError,match="403"):
            await provider.extract("qa.png",b"QA","M90000001","示例员甲")
    else:
        result = await provider.extract("qa.png",b"QA","M90000001","示例员甲")
        assert result["rows"] == [] and "workbook" not in result
