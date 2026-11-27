"""Read only bounded, explicitly allowed local attachments. Never fetch arbitrary URLs."""
import base64
import hashlib
import io
import json
import os
from tempfile import NamedTemporaryFile
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from PIL import Image, ImageOps
from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel
from pypdf import PdfReader
import pypdfium2 as pdfium
from .config import Settings

Image.MAX_IMAGE_PIXELS = 45_000_000
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".pdf", ".xlsx", ".xlsm", ".docx"}

@dataclass
class Prepared:
    text: str
    images: list[tuple[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

def allowed_file(path: str, settings: Settings) -> Path:
    p = Path(path).expanduser().resolve()
    roots = (*settings.input_roots, settings.data_dir.resolve() / "uploads")
    if not any(p.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("文件不在允许读取的订单目录内。请放入上传目录或配置 ORDER_INPUT_ROOTS。")
    if not p.is_file() or p.suffix.lower() not in EXTENSIONS:
        raise ValueError("支持图片、PDF、XLSX、DOCX；旧 XLS/DOC 请另存为新格式。")
    if p.stat().st_size > settings.max_bytes:
        raise ValueError("文件超过 30 MB，请拆分后上传。")
    return p

def upload(name: str, content: str, settings: Settings) -> Path:
    suffix = Path(name).suffix.lower()
    if suffix not in EXTENSIONS or len(content) > settings.max_bytes * 4 // 3 + 8:
        raise ValueError("上传类型不支持或超过 30 MB。")
    raw = base64.b64decode(content, validate=True)
    if len(raw) > settings.max_bytes:
        raise ValueError("文件超过 30 MB。")
    folder = settings.data_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / (hashlib.sha256(raw).hexdigest() + suffix)
    publish_bytes(p,raw)
    return p

def publish_bytes(path: Path, raw: bytes):
    if path.exists():
        return
    with NamedTemporaryFile(dir=path.parent,delete=False) as tmp:
        name=Path(tmp.name)
    try:
        name.write_bytes(raw)
        os.replace(name,path)
    finally:
        name.unlink(missing_ok=True)

def image_url(im: Image.Image) -> str:
    if im.width * im.height > 45_000_000:
        raise ValueError("单张图片超过 4500 万像素，请缩小或分割后上传。")
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((3072, 3072))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()

def prepare(path: Path, settings: Settings, sheets: list[str] | None = None) -> Prepared:
    suffix = path.suffix.lower()
    result = Prepared(text="")
    if suffix == ".pdf":
        with path.open("rb") as f:
            if not f.read(8).startswith(b"%PDF-"):
                raise ValueError("不是可读取的标准 PDF（可能带企业加密封装），请导出解密后的 PDF。")
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请提供可读取的副本。")
        if len(reader.pages) > settings.max_pages:
            raise ValueError("PDF 超过 30 页，请按订单拆分；不会静默截断。")
        doc = pdfium.PdfDocument(str(path))
        try:
            for index, page in enumerate(reader.pages):
                source = f"page:{index + 1}"
                result.sources.append(source)
                result.text += f"\n[{source}]\n{page.extract_text() or ''}\n"
                rendered = doc[index]
                scale = min(3, 3072 / max(rendered.get_size()))
                bitmap = rendered.render(scale=scale)
                try:
                    result.images.append((source, image_url(bitmap.to_pil())))
                finally:
                    bitmap.close()
                    rendered.close()
        finally:
            doc.close()
    elif suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > 100 * 1024 * 1024:
                raise ValueError("Word 解压体积过大。")
            source = "document:body"
            result.sources.append(source)
            root = ET.fromstring(z.read("word/document.xml"))
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            result.text = "[document:body]\n" + "\n".join(" | ".join(n.text or "" for n in p.iter(ns + "t")) for p in root.iter(ns + "p"))
            for name in z.namelist():
                if name.startswith("word/media/") and not name.endswith("/"):
                    with Image.open(io.BytesIO(z.read(name))) as im:
                        result.images.append((source, image_url(im)))
        result.warnings.append("Word 使用正文及内嵌图片提取；复杂浮动布局请另存 PDF 复核。")
    elif suffix in {".xlsx", ".xlsm"}:
        # Bound expanded bytes before parsing XML or embedded images.
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > 100 * 1024 * 1024:
                raise ValueError("Excel 解压体积过大。")
        wb = load_workbook(path, data_only=True, read_only=False)
        formulas = load_workbook(path, data_only=False, read_only=True)
        try:
            if sheets and not set(sheets).issubset(wb.sheetnames):
                raise ValueError("指定的工作表不存在。可用：" + ", ".join(wb.sheetnames))
            selected = sheets or wb.sheetnames
            for name in selected:
                ws = wb[name]
                if ws.max_row * ws.max_column > 100_000:
                    raise ValueError("工作表过大，请仅导出订单区域。")
                cells = []
                for row in ws:
                    for cell in row:
                        value = cell.value
                        formula = formulas[name][cell.coordinate]
                        if formula.data_type == "f" and value is None:
                            result.warnings.append(f"{name}!{cell.coordinate} 公式无缓存值，请用 Excel 重算并保存。")
                        if value is None:
                            continue
                        if isinstance(value, (datetime, date)):
                            value = value.isoformat()[:10]
                        elif isinstance(value, (int, float)):
                            # Header context helps interpret unformatted Excel serial dates.
                            headers = " ".join(str(ws.cell(r, cell.column).value or "") for r in range(1, min(cell.row, 9)))
                            if 20000 < value < 80000 and re.search(r"日期|交期|DEL DATE", headers, re.I):
                                value = {"raw": value, "excel_date": from_excel(value, wb.epoch).date().isoformat()}
                            elif re.fullmatch(r"0+", cell.number_format) and cell.number_format != "0":
                                value = str(int(value)).zfill(len(cell.number_format))
                            elif isinstance(value, float) and value.is_integer():
                                value = str(int(value))
                        cells.append({"cell": cell.coordinate, "value": value})
                if not cells and not ws._images:
                    continue
                source = f"sheet:{name}"
                result.sources.append(source)
                result.text += f"\n[{source}]\n" + json.dumps(cells, ensure_ascii=False, default=str)
                for idx, img in enumerate(ws._images):
                    with Image.open(io.BytesIO(img._data())) as im:
                        result.images.append((source, image_url(im)))
            if len(selected) > 1:
                result.warnings.append("包含多个工作表；每个独立 PO 分别返回，严禁合并客户、旧模板或送货单。")
        finally:
            wb.close()
            formulas.close()
    else:
        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1)
            if frames > settings.max_pages:
                raise ValueError("图片页数超过 30，请拆分。")
            for i in range(frames):
                im.seek(i)
                source = f"page:{i + 1}"
                result.sources.append(source)
                result.images.append((source, image_url(im)))
    if len(result.text) > 160_000 or len(result.images) > 30:
        raise ValueError("内容超出单次识别限制，请拆分文件或指定工作表；不会截断。")
    if not result.text.strip() and not result.images:
        raise ValueError("文件为空。")
    return result
