"""Exact pinned frontend code only; never cache owner state or rendered HTML."""
from __future__ import annotations
import hashlib
import logging
import time
from .iam_oauth import IAMFailure

# Exact built assets from deployed DSH versions, including retained workspaces.
# The rendered index, plugin responses and workspace data use separate routes.
ASSETS = {
    '/assets/index-Ciz7DZxv.js': (1093870, '65411dabad4de33466b12643cf30376d3d41ea1b7ce10ce329e97c89992d308b', 'text/javascript'),
    '/assets/vendor-BUMdD3V7.js': (741528, '7675048a90d0419365a342c4eb81713aa8582b82a76b08ba2a8fc27160575265', 'text/javascript'),
    '/assets/vendor-DV2921Zo.css': (27312, '4cea59483551208ce61ff27251da83d5f7a0893ef34cfe4b808d61be6e13ebc7', 'text/css'),
    '/assets/index-CSAdOBx6.css': (92252, 'd487cca8d8a97f1e0cc5eca02343528c873e544eb7ec3a5b323a0f035553551d', 'text/css'),
    '/assets/index-5SrrfWpU.js': (633282, 'af3ff948cf3f132edbddebea93e2f9f02233b390a94bd4c9842182300be811bb', 'text/javascript'),
    '/assets/index-BPHePDI_.css': (76338, '44a24dd9a31c4e74281562aca1d3d43141aa44c6f38c0fd343f576ce476d405d', 'text/css'),
    '/assets/vendor-BNsW4eBh.css': (29288, '9860041e810431da5a6b83e63f883ab8ff4fc49b4af3fc76681b46f778783a9f', 'text/css'),
    '/assets/vendor-CCJJTK99.js': (740575, '38f95ea3ff85b49dc4d8f237db208bbaedd2cb8c31611215c246dde41786ff21', 'text/javascript'),
}
PHASES = ('auth', 'workspace', 'retention', 'catalog', 'lease', 'resume', 'manager', 'recheck', 'host', 'body', 'upstream', 'static')


class PhaseTimings:
    def __init__(self, group):
        self.group = group if group in ('open', 'index', 'static', 'plugins') else 'other'
        self.started = self.previous = time.monotonic()
        self.values = {}

    def mark(self, phase):
        now = time.monotonic()
        if phase in PHASES: self.values[phase] = max(0, min(600000, round((now-self.previous)*1000)))
        self.previous = now

    def emit(self, *, cached=False):
        if self.group == 'other': return
        fields = ' '.join(phase+'_ms='+str(self.values.get(phase, 0)) for phase in PHASES)
        total = max(0, min(600000, round((time.monotonic()-self.started)*1000)))
        logging.getLogger('megmeet.web-entry').warning(
            'MEGMEET_WEB_ENTRY group=%s total_ms=%s cacheable=%s %s', self.group, total, int(cached), fields)

    def headers(self):
        """Closed timing names/numbers only; never expose a path or identity."""
        if self.group == 'other': return {}
        values = [phase+';dur='+str(self.values[phase]) for phase in PHASES if phase in self.values]
        total = max(0, min(600000, round((time.monotonic()-self.started)*1000)))
        values.append('total;dur='+str(total))
        return {'Server-Timing': ', '.join(values)}


def exact_asset(method, path, query):
    return method == 'GET' and not query and path in ASSETS


async def read_verified_asset(response, path, validate):
    """Buffer one pinned static asset, authenticating during and after reading."""
    body = bytearray()
    checked = 0
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        if len(body) > 2*1024*1024:
            raise IAMFailure(502, 'personal_cloud_static_response_invalid')
        if time.monotonic()-checked >= 5:
            await validate()
            checked = time.monotonic()
    await validate()
    size, digest, mime = ASSETS[path]
    actual_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    cacheable = (response.status_code == 200 and len(body) == size and actual_type == mime
                 and response.headers.get('content-encoding', 'identity').lower() == 'identity'
                 and hashlib.sha256(body).hexdigest() == digest)
    return bytes(body), cacheable
