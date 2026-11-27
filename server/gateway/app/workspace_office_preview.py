"""A browser-initiated, read-only Office preview using the employee's execution pool."""
import asyncio
import base64
import hashlib
import json
import posixpath
import re
import time
import uuid
from contextlib import suppress

import anyio
import httpx

from . import execution_bridge, personal_cloud as cloud
from .iam_oauth import IAMFailure
from .personal_cloud_files import NativeFileReader, file_path

LIMIT = 50 * 1024**2
PDF_LIMIT = 32 * 1024**2
HELPER = '/opt/personal-cloud/office-kit-executor.mjs'


def check(ok, status=502):
    if not ok:
        raise IAMFailure(status, 'office_preview_unavailable')


class Preview:
    def __init__(self, reader, client):
        self.reader, self.client = reader, client
        self.prefix = '/v1/hosts/' + cloud.owner_key(reader.actor)
        self.host = self.queue = self.job = self.directory = None

    async def manager(self, op, data):
        return await execution_bridge.execution_manager('POST', self.prefix + '/' + op, data)

    async def acquire(self):
        while True:
            await self.reader.authorize()
            data = {'lease': self.reader.lease, 'expires_at': int(time.time()) + 120,
                    'catalog': {'data': [], 'default_model': None}, 'resource_class': 'office'}
            if self.queue:
                data['queue_request_id'] = self.queue
            value = await self.manager('open', data)
            if value.get('status') == 'ready':
                self.host = value
                check(type(value.get('generation')) is int and value['generation'] > 0)
                if 'origin' not in value:
                    self.host = await self.heartbeat()
                return
            check(value.get('status') in ('starting', 'queued'), 503)
            self.queue = value.get('request_id') or self.queue
            await asyncio.sleep(1)

    async def heartbeat(self):
        value = await self.manager('heartbeat', {'lease': self.reader.lease, 'expires_at': int(time.time()) + 120, 'activity': True})
        check(value.get('generation') == self.host['generation'], 409)
        return value

    async def rpc(self, op, args, *, cleanup=False):
        if not cleanup:
            await self.reader.authorize()
        host = self.host
        payload = {'operation': op, 'args': args, 'session_id': self.reader.session_id, 'request_id': str(uuid.uuid4())}
        async with self.client.stream('POST', host['origin'] + '/__cloud/execute', json=payload,
                                      headers={'Authorization': 'Bearer ' + host['route_token']}) as result:
            check(result.status_code == 200)
            raw = bytearray()
            async for chunk in result.aiter_bytes():
                check(len(raw) + len(chunk) <= 2 * 1024**2)
                raw.extend(chunk)
            response = json.loads(raw)
        check(isinstance(response, dict) and 'value' in response)
        return response['value']

    async def process(self, body, source=None, *, cleanup=False):
        job = self.job = str(uuid.uuid4())
        spec = {'argv': ['node', HELPER, json.dumps(body, ensure_ascii=False)], 'cwd': '/workspace',
                'env': {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}, 'graceMs': 2000,
                'stdio': {'stdin': 'pipe' if source else 'ignore', 'stdout': {'maxBytes': 65536}, 'stderr': {'maxBytes': 1024}}}
        await self.rpc('process.spawn', {'job_id': job, 'spec': spec}, cleanup=cleanup)
        if source:
            async for chunk in source.chunks():
                for pos in range(0, len(chunk), 65536):
                    await self.rpc('process.stdin', {'job_id': job, 'data': base64.b64encode(chunk[pos:pos+65536]).decode(), 'end': False})
            await self.rpc('process.stdin', {'job_id': job, 'data': '', 'end': True})
        parts, offset = [], 0
        renew = time.monotonic() + 30
        while True:
            if not cleanup and time.monotonic() > renew:
                self.host = await self.heartbeat()
                renew = time.monotonic() + 30
            value = await self.rpc('process.poll', {'job_id': job, 'offsets': {'stdout': offset, 'stderr': 0}}, cleanup=cleanup)
            output = value['stdout']
            check(not output['lossy'] and offset <= output['nextOffset'] <= 65536 and not value.get('error'))
            parts.append(output['text'])
            offset = output['nextOffset']
            if value['finished'] and value['empty']:
                break
            await asyncio.sleep(.3)
        await self.rpc('process.forget', {'job_id': job}, cleanup=cleanup)
        self.job = None
        result = json.loads(''.join(parts))
        check(result.get('ok') is True)
        return result['value']

    async def render(self, path, source, generation):
        if source:
            version, size = source.info['version'], source.info['bytes']
        else:
            target = await self.rpc('fs.resolve', {'path': path})
            check(target.get('displayPath') == path and target.get('targetKey') == path, 403)
            stat = await self.rpc('fs.stat', {'target': target})
            check(stat and stat['type'] == 'file', 404)
            version, size = stat['version'], stat['size']
        check(isinstance(version, str) and type(size) is int and 0 < size <= LIMIT, 413)
        ext = path.rsplit('.', 1)[1].lower()
        result = await self.process({'mode': 'preview', 'session': self.reader.session_id, 'extension': ext,
            'version': version, 'path': 'input.' + ext if source else path, 'fromStdin': bool(source), 'bytes': size}, source)
        prefix = '/workspace/.maimi-office-kit-' + hashlib.sha256(self.reader.session_id.encode()).hexdigest()[:24]
        output = result.get('path', '')
        check(re.fullmatch(re.escape(prefix) + r'/out-[0-7]/preview\.pdf', output))
        self.directory = posixpath.dirname(output)
        length = result['bytes']
        check(type(length) is int and 0 < length <= PDF_LIMIT)
        target = await self.rpc('fs.resolve', {'path': output})
        check(target.get('displayPath') == output and target.get('targetKey') == output)
        pdf = bytearray()
        while len(pdf) < length:
            count = min(512 * 1024, length - len(pdf))
            chunk = await self.rpc('fs.readByteRange', {'target': target, 'offset': len(pdf), 'length': count})
            raw = base64.b64decode(chunk['base64'], validate=True)
            check(len(raw) == count)
            pdf.extend(raw)
        check(pdf.startswith(b'%PDF-') and hashlib.sha256(pdf).hexdigest() == result['sha256'])
        await self.reader.authorize()
        return {'absolutePath': path, 'version': version, 'offset': 0, 'eof': True, 'bytes': length,
                'data': base64.b64encode(pdf).decode(), 'missingFonts': result['missingFonts'], 'generation': generation}

    async def close(self):
        if self.host:
            if self.job:
                with suppress(Exception):
                    await self.rpc('process.terminate', {'job_id': self.job}, cleanup=True)
            if self.directory:
                with suppress(Exception):
                    await self.process({'mode': 'cleanup', 'session': self.reader.session_id, 'directory': self.directory}, cleanup=True)
            await self.manager('release-hint', {'generation': self.host['generation']})
        elif self.queue:
            await self.manager('queue/cancel', {'request_id': self.queue})


async def convert(request, args):
    session = args['workspaceFileScopeId']
    actor = await cloud.actor_for(request)
    active = args.get('priority', 'foreground') == 'foreground'
    upstream = await cloud.touch_host(request, actor, activity=active)
    lease, _ = await cloud.issue_lease(request, actor)
    path = file_path(args['path'], allow_relative=True)
    async with httpx.AsyncClient(timeout=45, trust_env=False, follow_redirects=False) as client:
        reader = NativeFileReader(request, actor, upstream, lease, client, path, session, LIMIT, active)
        listing = await reader.rpc('session/list', {'_request': {}})
        check(any(r.get('sessionId') == session and r.get('cwd') == '/workspace' for r in listing.get('items', []) if isinstance(r, dict)), 403)
        generation = await reader.rpc('officeKit/generation', {})
        source = None
        if path.startswith('/state/'):
            source = await NativeFileReader.open(request, path, session_id=session, max_bytes=LIMIT, activity=active)
        elif not path.startswith('/'):
            path = file_path('/workspace/' + path)
        preview = Preview(reader, client)
        try:
            await preview.acquire()
            return await preview.render(path, source, generation)
        except (KeyError, ValueError, TypeError) as error:
            raise IAMFailure(502, 'office_preview_unavailable') from error
        finally:
            with anyio.move_on_after(8, shield=True):
                if source:
                    await source.close()
                with suppress(Exception):
                    await preview.close()
