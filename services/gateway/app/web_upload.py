"""Bounded single-file browser multipart adapter for the native raw upload API.

No file paths, storage, decryption, network calls or authentication live here.
The caller retains its existing actor/Host/lease checks and native receipt.
"""
import logging
import re
import threading
import time

MAX_BODY = 32 * 1024 * 1024
MAX_HEADERS = 8192
MAX_CONTENT_TYPE = 512
TSD_HEADER = b'%TSD-Header-###%'
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
BOUNDARY = re.compile(r"[0-9A-Za-z'()+_,./:=?-]{1,70}")
FORMATS = frozenset(('ZIP', 'OLE', 'PDF', 'TSD', 'OTHER'))
PROTOCOLS = frozenset(('RAW', 'MULTIPART'))
RESULTS = frozenset(('ACCEPTED', 'INVALID_MULTIPART', 'STILL_ENCRYPTED'))


class UploadError(Exception):
    def __init__(self, code, status, protocol='MULTIPART', file_format='OTHER'):
        self.code = code
        self.status = status
        self.protocol = protocol
        self.file_format = file_format
        super().__init__(code)


def invalid():
    raise UploadError('personal_cloud_upload_invalid', 400)


def parameters(value):
    """Strict small MIME parameter grammar; no folding or duplicate keys."""
    if not isinstance(value, str) or not value or len(value) > MAX_HEADERS or any(ord(c) < 32 or ord(c) == 127 for c in value):
        invalid()
    first, separator, rest = value.partition(';')
    main = first.strip().lower()
    result = {}
    while separator:
        rest = rest.lstrip(' ')
        match = TOKEN.match(rest)
        if not match:
            invalid()
        key = match.group().lower()
        rest = rest[match.end():].lstrip(' ')
        if not rest.startswith('=') or key in result:
            invalid()
        rest = rest[1:].lstrip(' ')
        if rest.startswith('"'):
            text = []
            offset = 1
            while offset < len(rest):
                character = rest[offset]
                if character == '"':
                    offset += 1
                    break
                if character == '\\':
                    offset += 1
                    if offset >= len(rest):
                        invalid()
                    character = rest[offset]
                text.append(character)
                offset += 1
            else:
                invalid()
            result[key] = ''.join(text)
            rest = rest[offset:].lstrip(' ')
        else:
            match = TOKEN.match(rest)
            if not match:
                invalid()
            result[key] = match.group()
            rest = rest[match.end():].lstrip(' ')
        if rest:
            if not rest.startswith(';'):
                invalid()
            rest = rest[1:]
            separator = ';'
        else:
            separator = ''
    return main, result


def filename_parameter(params):
    # Filename is checked only as MIME metadata. Native query.name remains the
    # display-name authority; no filename is returned, logged or used as a path.
    # RFC 7578 section 4.2 forbids filename* for multipart/form-data. Browser
    # FormData uses filename, including UTF-8 or percent-encoded characters.
    if 'filename' not in params or 'filename*' in params:
        invalid()
    filename = params['filename']
    if not filename or len(filename) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        invalid()


def single_file(content_type, body):
    if len(content_type) > MAX_CONTENT_TYPE:
        invalid()
    media, options = parameters(content_type)
    if media != 'multipart/form-data' or set(options) != {'boundary'} or not BOUNDARY.fullmatch(options['boundary']):
        invalid()
    boundary = options['boundary'].encode('ascii')
    opening = b'--' + boundary + b'\r\n'
    if not body.startswith(opening):
        invalid()
    header_end = body.find(b'\r\n\r\n', len(opening), len(opening) + MAX_HEADERS + 4)
    if header_end < 0 or header_end - len(opening) > MAX_HEADERS:
        invalid()
    raw_headers = body[len(opening):header_end]
    try:
        text = raw_headers.decode('utf-8', 'strict')
    except UnicodeError:
        invalid()
    headers = {}
    for line in text.split('\r\n'):
        if not line or line[0] in ' \t' or '\r' in line or '\n' in line:
            invalid()
        key, separator, value = line.partition(':')
        key = key.lower()
        if not separator or key not in {'content-disposition', 'content-type'} or key in headers:
            invalid()
        headers[key] = value.strip(' ')
    if 'content-disposition' not in headers:
        invalid()
    disposition, options = parameters(headers['content-disposition'])
    if disposition != 'form-data' or options.get('name') != 'file' or set(options) - {'name', 'filename'}:
        invalid()
    filename_parameter(options)
    if 'content-type' in headers:
        media, _ = parameters(headers['content-type'])
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+/[!#$%&'*+.^_`|~0-9a-z-]+", media) or media.startswith('multipart/'):
            invalid()
    start = header_end + 4
    marker = b'\r\n--' + boundary
    end = body.find(marker, start)
    while end >= 0:
        tail = end + len(marker)
        if len(body) - tail in (2, 4) and body[tail:] in (b'--', b'--\r\n'):
            return body[start:end]
        if body[tail:tail + 2] in (b'\r\n', b'--'):
            # A second part, nested form or epilogue is not this protocol.
            invalid()
        end = body.find(marker, end + len(marker))
    invalid()


def file_format(data):
    prefix = data[:32]
    if prefix.startswith(TSD_HEADER):
        return 'TSD'
    if prefix[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
        return 'ZIP'
    if prefix.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        return 'OLE'
    if prefix.startswith(b'%PDF-'):
        return 'PDF'
    return 'OTHER'


def prepare_upload(content_type, body):
    """Return native bytes and fixed diagnostic labels; never enforce URL size."""
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BODY:
        raise UploadError('personal_cloud_request_too_large', 413, 'RAW')
    media = content_type.split(';', 1)[0].strip().lower()
    protocol = 'MULTIPART' if media == 'multipart/form-data' else 'RAW'
    if protocol == 'MULTIPART':
        body = single_file(content_type, body)
        content_type = 'application/octet-stream'
    category = file_format(body)
    if category == 'TSD':
        raise UploadError('personal_cloud_upload_still_encrypted', 422, protocol, 'TSD')
    return body, content_type, protocol, category


_rate_lock = threading.Lock()
_rate_start = 0.0
_rate_count = 0


def observe_upload(protocol, filename_present, size_present, category, result):
    """At most six anonymous fixed-field rows per minute; logging cannot fail work."""
    global _rate_start, _rate_count
    try:
        instant = time.monotonic()
        with _rate_lock:
            if instant - _rate_start >= 60 or instant < _rate_start:
                _rate_start, _rate_count = instant, 0
            if _rate_count >= 6:
                return
            _rate_count += 1
        logging.getLogger('megmeet.web-upload').warning(
            'MEGMEET_WEB_UPLOAD protocol=%s filename_present=%s size_present=%s format=%s result=%s',
            protocol if protocol in PROTOCOLS else 'RAW', int(filename_present is True), int(size_present is True),
            category if category in FORMATS else 'OTHER', result if result in RESULTS else 'INVALID_MULTIPART')
    except Exception:
        pass
