"""Read-only Windows distribution. No IAM, Gateway imports or secret access."""
from __future__ import annotations

import json
from html import escape
import os
from pathlib import Path
import re
import stat

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

SITE = Path(os.getenv('DIST_SITE_ROOT', '/srv/megmeet-distribution'))
PREFIX = '/maimi'
FILENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.exe$')
VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?$')
SHA256 = re.compile(r'^[a-f0-9]{64}$')
ASSETS = {'app-icon.png': 'image/png', 'app.css': 'text/css', 'app.js': 'text/javascript'}
HEADERS = {'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
           'X-Frame-Options': 'DENY'}
CSP = "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def strict_filename(value):
    return isinstance(value, str) and FILENAME.fullmatch(value) is not None and '..' not in value


def safe_file(*parts):
    """All callers use fixed segments or one strictly validated EXE basename."""
    root = SITE
    if not root.is_absolute():
        raise ValueError('site_root_invalid')
    for item in (*reversed(root.parents), root):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError('site_directory_invalid')
    target = root
    for index, segment in enumerate(parts):
        if not segment or segment in ('.', '..') or '/' in segment or '\\' in segment:
            raise ValueError('site_path_invalid')
        target = target / segment
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('site_symlink_denied')
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError('site_directory_invalid')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('site_file_invalid')
    return target, info


def release():
    path, info = safe_file('release.json')
    if info.st_size > 4096:
        raise ValueError('release_limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('release_invalid')
    if value.get('status') == 'unpublished':
        if set(value) != {'schema_version', 'status'}:
            raise ValueError('unpublished_release_invalid')
        return value
    if (set(value) != {'schema_version', 'status', 'version', 'filename', 'size_bytes', 'sha256'}
            or value['status'] != 'published' or not isinstance(value['version'], str)
            or not VERSION.fullmatch(value['version']) or not strict_filename(value['filename'])
            or type(value['size_bytes']) is not int or not 1 <= value['size_bytes'] <= 4 * 1024**3
            or not isinstance(value['sha256'], str) or not SHA256.fullmatch(value['sha256'])):
        raise ValueError('release_invalid')
    _, installer = safe_file('releases', value['filename'])
    if installer.st_size != value['size_bytes']:
        raise ValueError('installer_size_mismatch')
    return value


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def static_headers(request, call_next):
    byte_range = request.headers.get('range')
    if request.url.path.startswith(PREFIX + '/releases/') and byte_range is not None:
        # Browser resumptions need a single bounded byte range. Reject multipart
        # range expansion before the framework parses it or opens a file.
        if len(byte_range) > 64 or not re.fullmatch(r'bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})', byte_range):
            return JSONResponse({'detail': 'range_not_satisfiable'}, status_code=416,
                                headers={**HEADERS, 'Cache-Control': 'no-store'})
    response = await call_next(request)
    for name, value in HEADERS.items():
        response.headers[name] = value
    return response


def file_response(parts, media, *, filename=None, immutable=False):
    try:
        path, info = safe_file(*parts)
    except (OSError, ValueError):
        raise HTTPException(404, 'not_found') from None
    return FileResponse(path, media_type=media, filename=filename, stat_result=info,
        headers={'Cache-Control': 'public, max-age=31536000, immutable' if immutable else 'no-store',
                 **({'Content-Security-Policy': CSP} if media == 'text/html' else {})})


@app.api_route(PREFIX, methods=['GET', 'HEAD'])
async def canonical():
    return RedirectResponse(PREFIX + '/', status_code=308, headers={'Cache-Control': 'no-store'})


@app.api_route(PREFIX + '/', methods=['GET', 'HEAD'])
async def index():
    try:
        path, info = safe_file('index.html')
        if info.st_size > 64 * 1024:
            raise ValueError('index_limit')
        template = path.read_text(encoding='utf-8')
    except (OSError, ValueError):
        raise HTTPException(503, 'page_unavailable') from None
    status_code = 200
    try:
        current = release()
    except (OSError, ValueError, KeyError, TypeError):
        current = None
        status_code = 503
    button = '<a id="download" class="download disabled" aria-disabled="true">'
    details = '<details id="checksum-details" hidden><summary>查看安装包校验值</summary><code id="checksum"></code></details>'
    state = '安装包正在准备中，请稍后再来。' if current else '暂时无法读取下载信息，请稍后刷新页面。'
    rendered_button, rendered_details = button, details
    if current and current['status'] == 'published':
        filename = escape(current['filename'], quote=True)
        rendered_button = ('<a id="download" class="download" href="' + PREFIX + '/releases/'
                           + filename + '" download="' + filename + '">')
        state = '版本 ' + current['version'] + ' · ' + format(current['size_bytes'] / 1000 / 1000, '.1f') + ' MB'
        rendered_details = ('<details id="checksum-details"><summary>查看安装包校验值</summary>'
                            '<code id="checksum">' + escape(current['sha256']) + '</code></details>')
    # Render the existing release template without requiring browser JavaScript.
    # Keep the asset route for older pages; current pages never need a second
    # metadata request before offering the validated installer.
    replacements = {
        '<script src="/maimi/assets/app.js" defer></script>': '',
        button: rendered_button,
        '正在读取版本信息…': escape(state),
        details: rendered_details,
        '<noscript><p class="release-state">请启用浏览器 JavaScript 后查看可下载版本。</p></noscript>': '',
    }
    for before, after in replacements.items():
        if template.count(before) != 1:
            raise HTTPException(503, 'page_unavailable') from None
        template = template.replace(before, after, 1)
    return HTMLResponse(template, status_code=status_code,
                        headers={'Cache-Control': 'no-store', 'Content-Security-Policy': CSP})


@app.api_route(PREFIX + '/assets/{name}', methods=['GET', 'HEAD'])
async def asset(name: str):
    if name not in ASSETS:
        raise HTTPException(404, 'not_found')
    return file_response((name,), ASSETS[name])


@app.api_route(PREFIX + '/release.json', methods=['GET', 'HEAD'])
async def metadata():
    try:
        value = release()
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'release_unavailable') from None
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@app.api_route(PREFIX + '/releases/{filename}', methods=['GET', 'HEAD'])
async def installer(filename: str):
    if not strict_filename(filename):
        raise HTTPException(404, 'not_found')
    return file_response(('releases', filename), 'application/octet-stream', filename=filename, immutable=True)


@app.api_route(PREFIX + '/updates/company-win-x64.json', methods=['GET', 'HEAD'])
async def updates():
    try:
        path, info = safe_file('updates', 'company-win-x64.json')
        if not 1 <= info.st_size <= 512 * 1024 or not isinstance(json.loads(path.read_bytes()), dict):
            raise ValueError('feed_invalid')
    except (OSError, ValueError, TypeError):
        raise HTTPException(404, 'not_found') from None
    # Preserve exact signed bytes; publication and signature validation are
    # performed by the release builder and installed desktop application.
    return FileResponse(path, media_type='application/json', stat_result=info, headers={'Cache-Control': 'no-store'})


@app.get(PREFIX + '/health')
async def health():
    try:
        current = release()
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'release_unavailable') from None
    return JSONResponse({'status': 'healthy', 'service': 'desktop-distribution',
                         'release_status': current['status']}, headers={'Cache-Control': 'no-store'})
