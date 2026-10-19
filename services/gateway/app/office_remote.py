"""Authenticated streaming bridge to the shared, isolated office service.

The Gateway derives task ownership from the current IAM session. Neither
employee identifiers nor company credentials are accepted from job payloads.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .iam_oauth import IAMFailure
from . import personal_cloud as cloud
from . import usage_producer as metering

router = APIRouter()
PREFIX = '/v1/office'
INTERNAL = '/internal/personal-cloud/office'
PROTOCOL = 'megmeet-office-remote-v1'
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache',
           'X-Content-Type-Options': 'nosniff'}
MAX_JSON = 2 * 1024 * 1024
MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
JOB = r'[a-f0-9]{32}'
STATES = {'uploading', 'queued', 'running', 'succeeded', 'failed', 'cancelled'}
_transport = None  # Explicit synthetic transport injection in isolated tests.


def strict_json(raw):
    def reject_constant(_value):
        raise ValueError('invalid JSON number')
    return json.loads(raw, parse_constant=reject_constant)


def operation(method, suffix):
    if method == 'GET' and suffix == '/capabilities':
        return 'capabilities'
    if method == 'POST' and suffix == '/jobs':
        return 'create'
    if method in ('GET', 'DELETE') and re.fullmatch('/jobs/' + JOB, suffix):
        return 'status' if method == 'GET' else 'cancel'
    if method == 'POST' and re.fullmatch('/jobs/' + JOB + '/run', suffix):
        return 'run'
    if method == 'PUT' and re.fullmatch('/jobs/' + JOB + '/inputs/(?:0|[1-9]|1[0-9])', suffix):
        return 'upload'
    if method == 'GET' and re.fullmatch('/jobs/' + JOB + '/outputs/(?:0|[1-9][0-9]?)', suffix):
        return 'download'
    return None


def internal_route(request):
    path = request.url.path
    return path.startswith(INTERNAL + '/') and operation(request.method, path[len(INTERNAL):]) is not None


def service_configuration():
    base = os.getenv('OFFICE_SERVICE_URL', 'http://office-service:8093')
    if not re.fullmatch(r'http://[a-z0-9-]+:8093', base):
        raise IAMFailure(503, 'office_service_configuration')
    try:
        path = Path(os.environ['OFFICE_SERVICE_SECRET_FILE'])
        info = path.lstat()
        if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 257):
            raise ValueError()
        secret = path.read_text(encoding='utf-8').strip()
        if not 32 <= len(secret) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError()
    except (OSError, KeyError, ValueError, UnicodeError):
        raise IAMFailure(503, 'office_service_configuration') from None
    return base, secret


async def actor_for(request, internal):
    if internal:
        value = request.headers.get('authorization', '')
        if not value.startswith('Bearer '):
            raise IAMFailure(401, 'personal_cloud_lease_invalid')
        return await cloud.lease_actor(value[7:])
    return await cloud.actor_for(request)


def binding(actor):
    return actor.user_no, actor.account_no, actor.auth_session_id


def worker_headers(actor, secret):
    return {
        'X-Office-Service-Key': secret,
        'X-Office-Owner': hashlib.sha256(('megmeet-office-owner-v1:' + actor.user_no).encode()).hexdigest(),
        'X-Office-Scope': hashlib.sha256(('megmeet-office-scope-v1:' + actor.auth_session_id).encode()).hexdigest(),
    }


async def limited_body(request, maximum):
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > maximum:
            raise IAMFailure(413, 'office_request_too_large')
    return bytes(result)


def validate_job(value, action):
    if action == 'capabilities':
        if not isinstance(value, dict) or value.get('protocol') != 'megmeet-office-v1':
            raise ValueError()
        return
    if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
            or not re.fullmatch(JOB, value.get('job_id', '')) or value.get('state') not in STATES
            or not isinstance(value.get('outputs'), list) or len(value['outputs']) > 20):
        raise ValueError()
    total = 0
    ids = set()
    for output in value['outputs']:
        if (not isinstance(output, dict) or set(output) != {'id', 'path', 'size_bytes', 'sha256'}
                or not re.fullmatch(r'(?:0|[1-9][0-9]?)', output.get('id', ''))
                or output['id'] in ids or not isinstance(output.get('path'), str)
                or not 1 <= len(output['path']) <= 1024 or output['path'].startswith(('/', '\\'))
                or any(c in output['path'] for c in ('\\', ':', '\x00', '\r', '\n'))
                or any(part in ('', '.', '..') for part in output['path'].split('/'))
                or type(output.get('size_bytes')) is not int or not 0 <= output['size_bytes'] <= MAX_OUTPUT
                or not re.fullmatch('[a-f0-9]{64}', output.get('sha256', ''))):
            raise ValueError()
        ids.add(output['id'])
        total += output['size_bytes']
    if total > MAX_OUTPUT:
        raise ValueError()


async def bridge(request: Request, suffix: str, internal=False):
    action = operation(request.method, suffix)
    if action is None or request.url.query:
        raise IAMFailure(404, 'office_route_not_found')
    actor = await actor_for(request, internal)
    expected = binding(actor)
    base, secret = service_configuration()
    headers = worker_headers(actor, secret)

    async def revalidate():
        if binding(await actor_for(request, internal)) != expected:
            raise IAMFailure(401, 'office_session_changed')

    if action == 'upload':
        length = request.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length) > MAX_INPUT):
            raise IAMFailure(413, 'office_request_too_large')
        if length is not None:
            headers['Content-Length'] = length
        headers['Content-Type'] = 'application/octet-stream'

        async def upload():
            total, checked = 0, time.monotonic()
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_INPUT:
                    raise IAMFailure(413, 'office_request_too_large')
                if time.monotonic() - checked >= 5:
                    await revalidate()
                    checked = time.monotonic()
                yield chunk
            await revalidate()
        content = upload()
    else:
        content = await limited_body(request, 600 * 1024 if action == 'create' else 2)
        if action == 'create':
            try:
                body = strict_json(content)
                if not isinstance(body, dict) or set(body) != {'protocol', 'request', 'inputs'}:
                    raise ValueError()
                if body.get('protocol') != PROTOCOL:
                    raise ValueError()
            except (ValueError, TypeError):
                raise IAMFailure(400, 'office_request_invalid') from None
            headers['Content-Type'] = 'application/json'
        elif content not in (b'', b'{}'):
            raise IAMFailure(400, 'office_request_body_denied')

    client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5, pool=5),
        trust_env=False, follow_redirects=False, transport=_transport)
    response = None
    handed_off = False
    requested_at = time.monotonic()
    try:
        response = await client.send(client.build_request(request.method, base + PREFIX + suffix,
            headers=headers, content=content), stream=True)
        if response.status_code not in {200, 201, 202}:
            status = response.status_code
            code = {400: 'office_request_invalid', 404: 'office_job_not_found',
                    409: 'office_job_state_conflict', 413: 'office_request_too_large',
                    429: 'office_capacity_busy'}.get(status, 'office_service_unavailable')
            raise IAMFailure(status if status in {400, 404, 409, 413, 429} else 503, code)
        await revalidate()
        if action == 'download':
            size = response.headers.get('content-length', '')
            if not size.isdigit() or not 0 <= int(size) <= MAX_OUTPUT:
                raise IAMFailure(502, 'office_response_invalid')
            expected_size = int(size)

            async def stream():
                total, checked = 0, time.monotonic()
                try:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > expected_size:
                            raise IAMFailure(502, 'office_response_invalid')
                        if time.monotonic() - checked >= 5:
                            await revalidate()
                            checked = time.monotonic()
                        yield chunk
                    if total != expected_size:
                        raise IAMFailure(502, 'office_response_invalid')
                finally:
                    await response.aclose()
                    await client.aclose()

            async def close():
                await response.aclose()
                await client.aclose()
            handed_off = True
            return StreamingResponse(stream(), media_type='application/octet-stream',
                headers={**HEADERS, 'Content-Length': size}, background=BackgroundTask(close))
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_JSON:
                raise IAMFailure(502, 'office_response_invalid')
        try:
            value = strict_json(raw)
            validate_job(value, action)
            if action != 'create' and action != 'capabilities' and value['job_id'] != suffix.split('/')[2]:
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            raise IAMFailure(502, 'office_response_invalid') from None
        await revalidate()
        # Only validated, owner-bound service receipts establish a job result.
        # Capabilities/uploads/downloads are not separate Office jobs.
        if action != "capabilities":
            if action == "run":
                metering.office_started(actor, value["job_id"], requested_at)
            metering.office_observed(actor, value["job_id"], value["state"])
        return JSONResponse(value, status_code=response.status_code, headers=HEADERS)
    except httpx.HTTPError:
        raise IAMFailure(503, 'office_service_unavailable') from None
    finally:
        if not handed_off:
            if response is not None:
                await response.aclose()
            await client.aclose()


@router.api_route(PREFIX + '/{suffix:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def public_office(request: Request, suffix: str):
    return await bridge(request, '/' + suffix)


@router.api_route(INTERNAL + '/{suffix:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def internal_office(request: Request, suffix: str):
    return await bridge(request, '/' + suffix, True)
