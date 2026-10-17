"""Restricted company photo client and safe, metadata-free image decoding."""

from __future__ import annotations

import base64
import binascii
import io
import logging
import re
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 6000
NORMALIZED_MAX_DIMENSION = 1600
SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}
EMPLOYEE_NUMBER = re.compile(r"M[0-9]{8}\Z")


class _RedactPhotoToken(logging.Filter):
    """httpx logs request URLs, while this legacy API requires a query token."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = re.sub(r"(?i)(access_token=)[^&\s\"'<>\\]*", r"\1[REDACTED]", message)
        record.args = ()
        return True


_TOKEN_FILTER = _RedactPhotoToken()


def _protect_transport_logs() -> None:
    # Filters on parent loggers do not run for propagated child records.
    names = {"httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith(("httpx.", "httpcore.")))
    for name in names:
        logger = logging.getLogger(name)
        if _TOKEN_FILTER not in logger.filters:
            logger.addFilter(_TOKEN_FILTER)


class PhotoError(ValueError):
    """Only static, non-sensitive messages may be returned to the caller."""


class _TemporaryPhotoError(PhotoError):
    """A safe transient failure eligible for exactly one automatic retry."""


def to_person_pin(employee_no: str) -> str:
    """Translate only the documented photo API numbering scheme."""
    if not isinstance(employee_no, str) or not EMPLOYEE_NUMBER.fullmatch(employee_no):
        raise PhotoError("工号格式不受支持，应为大写 M 加 8 位数字。")
    return "1" + employee_no[1:]


def normalize_photo(data: bytes) -> bytes:
    """Validate content, orient and shrink it, then remove metadata via PNG."""
    if not isinstance(data, bytes) or not data:
        raise PhotoError("照片内容为空，请重新上传。")
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoError("照片文件不能超过 8 MB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in SUPPORTED_FORMATS:
                    raise PhotoError("照片格式不受支持，请使用 JPEG、PNG、WebP 或 BMP。")
                width, height = source.size
                if width < 20 or height < 20:
                    raise PhotoError("照片尺寸过小，请使用至少 20 × 20 像素的图片。")
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                    raise PhotoError("照片像素过大，请将单边缩小到 6000 像素以内，且总像素不超过 2000 万。")
                if getattr(source, "n_frames", 1) > 1:
                    raise PhotoError("不支持动态或多帧照片，请上传单张静态图片。")
                source.verify()
            with Image.open(io.BytesIO(data)) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((NORMALIZED_MAX_DIMENSION, NORMALIZED_MAX_DIMENSION), Image.Resampling.LANCZOS)
                if oriented.mode == "RGBA" or "transparency" in oriented.info or oriented.mode in {"LA", "P"}:
                    foreground = oriented.convert("RGBA")
                    normalized = Image.new("RGB", foreground.size, "white")
                    normalized.paste(foreground, mask=foreground.getchannel("A"))
                else:
                    normalized = oriented.convert("RGB")
                # Fresh pixel-only image prevents inherited EXIF/text/ICC data.
                clean = Image.new("RGB", normalized.size)
                clean.paste(normalized)
                output = io.BytesIO()
                clean.save(output, format="PNG", optimize=True)
                return output.getvalue()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoError("照片损坏或无法识别，请换一张有效图片。") from None


def _decode_photo(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise PhotoError("未找到员工照片，请在网页补传。")
    if len(value) > MAX_RESPONSE_BYTES:
        raise PhotoError("照片接口返回的图片过大，请在网页补传。")
    if value.startswith("data:"):
        match = re.match(r"\Adata:image/(?:jpeg|jpg|png|webp|bmp);base64,", value, re.IGNORECASE)
        if match is None:
            raise PhotoError("照片接口返回的图片编码不受支持。")
        value = value[match.end() :]
    value = re.sub(r"[\r\n\t ]", "", value)
    if len(value) > ((MAX_PHOTO_BYTES + 2) // 3) * 4:
        raise PhotoError("照片接口返回的图片过大，请在网页补传。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoError("照片接口返回的图片编码无效，请重试或补传。") from None
    return normalize_photo(decoded)


def _mock_photo() -> bytes:
    """A clearly labelled, fictional placeholder; no fabricated face."""
    canvas = Image.new("RGB", (600, 800), "#e5f4f1")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((35, 35, 565, 765), radius=25, outline="#138a76", width=4)
    draw.ellipse((210, 180, 390, 360), fill="#a5c9c1")
    draw.rounded_rectangle((125, 395, 475, 610), radius=95, fill="#a5c9c1")
    font_path = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "SourceHanSansSC-Regular.otf"
    if font_path.is_file():
        font = ImageFont.truetype(str(font_path), 45)
        draw.text((300, 95), "演示照片", font=font, anchor="mm", fill="#096b5b")
        label_font = ImageFont.truetype(str(font_path), 25)
    else:
        label_font = ImageFont.load_default(size=28)
    draw.rectangle((70, 640, 530, 717), fill="#096b5b")
    draw.text((300, 678), "DEMO PHOTO", font=label_font, anchor="mm", fill="white")
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


class PhotoClient:
    def __init__(self, base_url: str, token: str, mode: str = "live", timeout: float = 8):
        if mode not in {"live", "mock"}:
            raise PhotoError("照片获取模式配置无效。")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise PhotoError("照片接口超时配置无效。")
        self.mode = mode
        self.timeout = timeout
        self._token = token
        self._url = base_url
        if mode == "live":
            try:
                parsed = urlsplit(base_url)
                valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                valid = valid and isinstance(token, str) and bool(token.strip())
                # Accessing port also validates malformed/non-numeric ports.
                _ = parsed.port
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise PhotoError("照片接口尚未正确配置，请联系管理员。")
        _protect_transport_logs()

    def fetch(self, employee_no: str) -> bytes:
        person_pin = to_person_pin(employee_no)
        if self.mode == "mock":
            return _mock_photo()
        for attempt in range(2):
            try:
                return self._fetch_once(person_pin)
            except _TemporaryPhotoError as error:
                if attempt:
                    raise PhotoError(str(error)) from None
                time.sleep(0.2)
        raise AssertionError("Unreachable retry state")

    def _fetch_once(self, person_pin: str) -> bytes:
        started = time.monotonic()
        try:
            # The complete fixed endpoint comes only from server configuration.
            # Disable proxies and redirects to keep credentials at that endpoint.
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", self._url.rstrip("/") + "/" + person_pin, params={"access_token": self._token}, headers={"Accept": "application/json"}) as response:
                    if response.status_code in {401, 403}:
                        raise PhotoError("照片接口认证失败，请联系管理员检查配置。")
                    if response.status_code == 404:
                        raise PhotoError("照片接口地址不存在，请联系管理员检查配置。")
                    if response.status_code in {502, 503, 504}:
                        raise _TemporaryPhotoError("照片接口暂时不可用，请重试或补传。")
                    if response.status_code != 200:
                        raise PhotoError("照片接口暂时不可用，请重试或补传。")
                    length = response.headers.get("Content-Length")
                    if length and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                        raise PhotoError("照片接口响应过大或异常，请重试或补传。")
                    payload = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if time.monotonic() - started > self.timeout * 2:
                            raise _TemporaryPhotoError("照片接口请求超时，请重试或补传。")
                        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise PhotoError("照片接口响应过大，请在网页补传。")
                        payload.extend(chunk)
                    # Construct a local response solely for bounded JSON parsing.
                    document = httpx.Response(200, content=bytes(payload)).json()
        except PhotoError:
            raise
        except httpx.TimeoutException:
            raise _TemporaryPhotoError("照片接口请求超时，请重试或补传。") from None
        except httpx.HTTPError:
            raise _TemporaryPhotoError("无法连接照片接口，请联系管理员或手工补传。") from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PhotoError("照片接口返回的数据格式异常，请重试或补传。") from None
        if not isinstance(document, dict) or type(document.get("code")) is not int or document["code"] != 0:
            raise PhotoError("照片接口未返回成功结果，请核对工号或手工补传。")
        employee_data = document.get("data")
        if not isinstance(employee_data, dict):
            raise PhotoError("未找到员工照片，请在网页补传。")
        return _decode_photo(employee_data.get("personPhoto"))
