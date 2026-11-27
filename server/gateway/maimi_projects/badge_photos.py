"""Fixed company personnel endpoint, scoped to printable badge fields."""
import asyncio
import base64
import binascii
import http.client
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit


MAX_IMAGE = 8 * 1024 * 1024
MAX_RESPONSE = 12 * 1024 * 1024
TOKEN_FILE = Path('/run/maimi-badges/photo-token')


class PhotoServiceError(Exception):
    """Only fixed, user-readable diagnostic text belongs in this exception."""


def _person_record(payload):
    try:
        message = json.loads(payload)
    except (ValueError, RecursionError, UnicodeError):
        raise PhotoServiceError('照片服务返回的数据无法解析，请联系管理员。') from None
    if not isinstance(message, dict) or type(message.get('code')) is not int or message['code'] != 0:
        raise PhotoServiceError('照片服务未返回成功状态，请核对接口授权或补传照片。')
    person = message.get('data')
    if person is None:
        return None
    if not isinstance(person, dict):
        raise PhotoServiceError('照片服务返回的员工记录格式异常。')
    return person


def _decode_photo(encoded):
    if encoded is None or encoded == '':
        return None
    if not isinstance(encoded, str):
        raise PhotoServiceError('照片服务返回的图片格式异常。')
    if encoded.lower().startswith('data:'):
        prefix = re.match(r'data:image/(?:jpeg|jpg|png|webp|bmp);base64,', encoded, re.I)
        if not prefix:
            raise PhotoServiceError('照片服务的图片编码暂不支持。')
        encoded = encoded[prefix.end():]
    encoded = ''.join(encoded.split())
    if len(encoded) > (MAX_IMAGE + 2) // 3 * 4:
        raise PhotoServiceError('照片服务返回的照片超过 8 MB，请补传较小的照片。')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoServiceError('照片服务返回的图片编码无效。') from None
    if not 0 < len(data) <= MAX_IMAGE:
        raise PhotoServiceError('照片服务返回的照片大小异常。')
    return data


def decode_reply(payload):
    person = _person_record(payload)
    return _decode_photo(person.get('personPhoto')) if person is not None else None


@dataclass(frozen=True, repr=False)
class SelfBadgeDetails:
    name: str
    english_name: str
    photo: bytes | None
    photo_error: str = ''


def _english_print_name(value):
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFC', value.strip())
    if not value or len(value) > 64:
        return ''
    letters = [char for char in value if char not in " -.'’"]
    if not letters or any(not char.isalpha() or 'LATIN' not in unicodedata.name(char, '') for char in letters):
        return ''
    return value


def decode_self_details(payload, employee_no, *, include_photo):
    if not isinstance(employee_no, str) or not re.fullmatch(r'M[0-9]{8}', employee_no):
        raise ValueError('invalid employee number')
    person = _person_record(payload)
    if person is None:
        return None
    if person.get('pin') != '1' + employee_no[1:]:
        raise PhotoServiceError('人员接口返回的工号与本人工号不一致，请联系管理员核对。')
    name = person.get('name')
    photo, photo_error = None, ''
    if include_photo:
        try:
            photo = _decode_photo(person.get('personPhoto'))
        except PhotoServiceError as exc:
            photo_error = str(exc)
    return SelfBadgeDetails(name if isinstance(name, str) else '', _english_print_name(person.get('lastName')),
                            photo, photo_error)


def _fetch(employee_no, *, details=False, include_photo=True):
    if not isinstance(employee_no,str) or not re.fullmatch(r'M[0-9]{8}', employee_no):
        raise ValueError('invalid employee number')
    try:
        url = urlsplit(os.environ.get('MAIMI_PROJECT_PHOTO_URL', ''))
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError
        port = url.port
        token = TOKEN_FILE.read_text('utf-8').strip()
        if not token or len(token) > 8192:
            raise ValueError
    except (OSError, ValueError):
        raise PhotoServiceError('照片服务连接配置不完整，请联系管理员。') from None
    cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    connection = cls(url.hostname, port, timeout=8)
    # The upstream requires a query credential. No general-purpose HTTP client,
    # redirects, proxy environment or request logging should receive it.
    try:
        path = url.path.rstrip('/') + '/1' + employee_no[1:] + '?' + urlencode({'access_token': token})
        connection.request('GET', path, headers={'Accept':'application/json'})
        response = connection.getresponse()
        if response.status in (401,403):
            raise PhotoServiceError('照片服务授权失败，请联系管理员。')
        if response.status != 200:
            raise PhotoServiceError('照片服务暂时不可用，请稍后重试或补传照片。')
        size = response.getheader('Content-Length')
        if size is not None and (not size.isdecimal() or len(size)>9 or int(size)>MAX_RESPONSE):
            raise PhotoServiceError('照片服务返回的内容过大或长度异常。')
        limit = time.monotonic()+16
        body = bytearray()
        while len(body)<=MAX_RESPONSE and time.monotonic()<limit:
            chunk = response.read1(min(65536,MAX_RESPONSE-len(body)+1))
            if not chunk:
                if details:
                    return decode_self_details(body, employee_no, include_photo=include_photo)
                return decode_reply(body)
            body.extend(chunk)
        raise PhotoServiceError('照片服务响应超时或返回内容过大。')
    except (http.client.HTTPException, OSError, ValueError):
        raise PhotoServiceError('无法连接照片服务，请稍后重试或补传照片。') from None
    finally:
        connection.close()


async def fetch_photo(employee_no):
    return await asyncio.to_thread(_fetch, employee_no)


async def fetch_self_details(employee_no, *, include_photo):
    return await asyncio.to_thread(_fetch, employee_no, details=True, include_photo=include_photo)
