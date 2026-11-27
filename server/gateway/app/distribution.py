"""Public, read-only desktop releases. The publisher switches latest.yml last."""
import os
import re
import stat
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter()
ROOT = Path('/opt/maimi-desktop-releases')
PREFIX = '/maimi/updates/windows/'
VERSION = r'(?:0|[1-9]\d{0,4})(?:\.(?:0|[1-9]\d{0,4})){2}'
PACKAGE = re.compile(r'Maimi-Setup-(' + VERSION + r')-x64\.exe(?:\.blockmap)?')
SINGLE_RANGE = re.compile(r'bytes=(?:\d{1,20}-\d{0,20}|-\d{1,20})')

FEEDS = frozenset(('latest.yml', 'stable.yml', 'nightly.yml'))

def public_download(request):
    if request.method not in ('GET', 'HEAD'):
        return False
    path = request.url.path
    if not path.startswith(PREFIX):
        return False
    name = path[len(PREFIX):]
    return name in FEEDS or PACKAGE.fullmatch(name) is not None

def file_info(name):
    path = ROOT / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise ValueError('release_file_invalid')
    return path, info

def current_release(feed='latest.yml'):
    if feed not in FEEDS:
        return None
    try:
        path, info = file_info(feed)
        if info.st_size > 16384:
            return None
        with path.open('rb') as stream:
            data = stream.read(16385).decode('utf-8')
        version = re.search(r'(?m)^version: (' + VERSION + r')\s*$', data)
        if not version:
            return None
        version = version[1]
        name = f'Maimi-Setup-{version}-x64.exe'
        if not re.search(r'(?m)^path: ' + re.escape(name) + r'\s*$', data):
            return None
        file_info(name + '.blockmap')
        _, package = file_info(name)
        return version, name, package.st_size
    except (OSError, UnicodeError, ValueError):
        return None

@router.api_route(PREFIX + '{name}', methods=['GET', 'HEAD'], include_in_schema=False)
def download(request: Request, name: str):
    if os.getenv('MAIMI_DEPLOYMENT_MODE') != 'production-ha' or not public_download(request):
        raise HTTPException(404)
    try:
        path, info = file_info(name)
    except (OSError, ValueError):
        raise HTTPException(404) from None
    if name in FEEDS and not current_release(name):
        raise HTTPException(503, 'release_not_ready')
    ranges = request.headers.get('range')
    if ranges and not SINGLE_RANGE.fullmatch(ranges):
        raise HTTPException(416, headers={'Content-Range': f'bytes */{info.st_size}'})
    cache = 'no-store' if name in FEEDS else 'public, max-age=31536000, immutable'
    return FileResponse(path, stat_result=info, filename=name if name.endswith('.exe') else None,
        media_type='text/yaml' if name in FEEDS else 'application/octet-stream',
        headers={'Cache-Control': cache, 'X-Content-Type-Options': 'nosniff'})

