"""Native browser preview protocol backed by the isolated shared Office service.

Read authorization and file versions still come from the employee's native
workspace. Conversion must not load LibreOffice WASM into the small chat worker.
"""
import asyncio
import base64
import hashlib
import json
import re
from contextlib import suppress

import anyio
import httpx
from fastapi.responses import StreamingResponse

from . import iam_oauth as iam
from . import personal_cloud as cloud
from . import office_remote as office
from .personal_cloud_files import NativeFileReader, file_path, SESSION_ID

MAX_SOURCE = 50 * 1024 * 1024
MAX_PDF = 32 * 1024 * 1024
TIMEOUT = 240
HEARTBEAT = 8
POLL_INTERVAL = .5
MAX_PENDING = 10
_pending = 0
_slots = asyncio.Semaphore(2)
_transport = None  # Synthetic transport only; production uses fixed private service.


class PreviewFailure(Exception):
    def __init__(self, message):
        self.message = message


def failure(message):
    raise PreviewFailure(message)


async def job_request(client, method, url, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        if response.status_code == 429:
            failure('文档预览服务正忙，请稍后重试，或先下载原文件。')
        if response.status_code not in (200, 201, 202):
            failure('文档预览暂时不可用，可以先下载原文件。')
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > office.MAX_JSON:
                failure('文档预览返回异常，可以先下载原文件。')
            data.extend(chunk)
    try:
        value = office.strict_json(data)
        office.validate_job(value, 'status')
    except (ValueError, TypeError):
        failure('文档预览返回异常，可以先下载原文件。')
    return value


async def convert(request, args):
    reader = await NativeFileReader.open(request, args['path'],
        session_id=args['workspaceFileScopeId'], max_bytes=MAX_SOURCE,
        activity=args.get('priority', 'foreground') == 'foreground')
    job_id = None
    client = None
    try:
        # Preserve the native provider generation used by the UI cache.
        generation = await reader.rpc('officeToPdf/generation', {})
        if not isinstance(generation, str) or not 1 <= len(generation) <= 128:
            failure('文档预览初始化失败，可以先下载原文件。')
        base, secret = office.service_configuration()
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5),
            trust_env=False, follow_redirects=False, transport=_transport,
            headers={**office.worker_headers(reader.actor, secret), 'Accept-Encoding': 'identity'})
        url = base + '/v1/office/jobs'
        extension = args['path'].rsplit('.', 1)[-1].lower()
        name = 'input.' + extension
        job = await job_request(client, 'POST', url, json={'protocol': office.PROTOCOL,
            'request': {'operation': 'convert', 'input': name, 'output': 'output.pdf', 'options': {}},
            'inputs': [{'path': name, 'size_bytes': reader.info['bytes']}]})
        job_id = job['job_id']
        job_url = url + '/' + job_id
        await job_request(client, 'PUT', job_url + '/inputs/0', content=reader.chunks(),
            headers={'Content-Length': str(reader.info['bytes']), 'Content-Type': 'application/octet-stream'})
        await reader.authorize()
        job = await job_request(client, 'POST', job_url + '/run', content=b'{}')
        while job['state'] in ('queued', 'running'):
            await asyncio.sleep(POLL_INTERVAL)
            await reader.authorize()
            job = await job_request(client, 'GET', job_url)
            if job['job_id'] != job_id:
                failure('文档预览返回异常，可以先下载原文件。')
        if job['state'] != 'succeeded':
            failure('文档转换失败，可以先下载原文件后查看。')
        outputs = job['outputs']
        if (len(outputs) != 1 or outputs[0]['id'] != '0'
                or outputs[0]['path'] != 'output.pdf' or not 5 <= outputs[0]['size_bytes'] <= MAX_PDF):
            failure('预览文件过大或格式异常，请下载原文件查看。')
        output = outputs[0]
        data = bytearray()
        async with client.stream('GET', job_url + '/outputs/0') as response:
            if response.status_code != 200:
                failure('预览文件读取失败，可以先下载原文件。')
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > output['size_bytes']:
                    failure('预览文件校验失败，可以先下载原文件。')
                data.extend(chunk)
        if (len(data) != output['size_bytes'] or not data.startswith(b'%PDF-')
                or hashlib.sha256(data).hexdigest() != output['sha256']):
            failure('预览文件校验失败，可以先下载原文件。')
        await reader.authorize()
        return {'absolutePath': reader.info['absolutePath'], 'version': reader.info['version'],
            'offset': 0, 'eof': True, 'bytes': len(data), 'data': base64.b64encode(data).decode('ascii'),
            'missingFonts': [], 'generation': generation}
    finally:
        # A disconnect cancels the entire ASGI scope. Shield this bounded
        # cleanup so a closed tab does not leave a conversion running.
        with anyio.move_on_after(4, shield=True):
            with suppress(Exception):
                await reader.close()
            if client is not None:
                if job_id:
                    with suppress(Exception):
                        await asyncio.wait_for(client.delete(base + '/v1/office/jobs/' + job_id), 3)
                await client.aclose()

async def render(request):
    if request.headers.get('authorization'):
        raise iam.IAMFailure(403, 'web_session_required')
    actor = await cloud.actor_for(request)
    raw = await office.limited_body(request, 64 * 1024)
    try:
        body = office.strict_json(raw)
        if (not isinstance(body, dict) or body.get('type') != 'client-request'
                or body.get('method') != 'officeToPdf/render'
                or not isinstance(body.get('rpcId'), str) or not 1 <= len(body['rpcId']) <= 256):
            raise ValueError()
        args = body['payload']['args']
        if (not isinstance(args, dict) or set(args) - {'workspaceFileScopeId', 'path', 'priority'}
                or not isinstance(args.get('workspaceFileScopeId'), str)
                or not SESSION_ID.fullmatch(args['workspaceFileScopeId'])
                or args.get('priority', 'foreground') not in ('foreground', 'background')):
            raise ValueError()
        path = args.get('path')
        if not isinstance(path, str):
            raise ValueError()
        # Native resource addresses are commonly workspace-relative. The reader
        # resolves these only after verifying this real session's /workspace cwd.
        file_path(path, allow_relative=True)
        if not re.search(r'\.(docx?|xlsx?|pptx?)$', args['path'], re.I):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise iam.IAMFailure(400, 'personal_cloud_preview_request_invalid') from None

    async def stream():
        global _pending
        task = None
        counted = False
        acquired = False
        async def bounded_convert():
            nonlocal acquired
            await _slots.acquire()
            acquired = True
            return await convert(request, args)

        async def response_body():
            nonlocal task, counted
            global _pending
            try:
                if _pending >= MAX_PENDING:
                    failure('文档预览服务正忙，请稍后重试，或先下载原文件。')
                _pending += 1
                counted = True
                task = asyncio.create_task(asyncio.wait_for(bounded_convert(), TIMEOUT))
                # JSON whitespace keeps the existing reverse proxy alive. The
                # native client still receives one ordinary server-response.
                yield b' \n'
                while not task.done():
                    await asyncio.wait({task}, timeout=HEARTBEAT)
                    current = await cloud.actor_for(request)
                    if office.binding(current) != office.binding(actor):
                        raise iam.IAMFailure(401, 'personal_cloud_identity_changed')
                    await cloud.touch_host(request, actor, activity=args.get('priority', 'foreground') == 'foreground')
                    if not task.done():
                        yield b' \n'
                result = {'ok': True, 'value': task.result()}
            except PreviewFailure as exc:
                result = {'ok': False, 'error': {'code': 'document-render/cloud-failed', 'details': {}, 'message': exc.message}}
            except asyncio.TimeoutError:
                result = {'ok': False, 'error': {'code': 'document-render/cloud-failed', 'details': {},
                    'message': '文档预览生成超时，可以先下载原文件。'}}
            except iam.IAMFailure as exc:
                message = ('登录或云端连接已失效，请重新进入后重试。' if exc.status in (401, 409)
                    else '文件无法读取或超过预览限制，可以先下载原文件。')
                result = {'ok': False, 'error': {'code': 'document-render/cloud-failed', 'details': {}, 'message': message}}
            except (httpx.HTTPError, OSError):
                result = {'ok': False, 'error': {'code': 'document-render/cloud-failed', 'details': {},
                    'message': '文档预览暂时不可用，可以先下载原文件。'}}
            yield json.dumps({'type': 'server-response', 'rpcId': body['rpcId'], 'result': result},
                ensure_ascii=False, separators=(',', ':')).encode('utf-8')

        try:
            async for chunk in response_body():
                yield chunk
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                with anyio.move_on_after(5, shield=True):
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            # Hold the slot until response transmission completes, so slow
            # clients cannot accumulate unbounded completed PDF buffers.
            if acquired:
                _slots.release()
            if counted:
                _pending -= 1

    return StreamingResponse(stream(), media_type='application/json',
        headers={**cloud.HEADERS, 'X-Content-Type-Options': 'nosniff', 'X-Accel-Buffering': 'no'})
