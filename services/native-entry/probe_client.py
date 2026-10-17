#!/usr/bin/env python3
"""Offline integration support only; never used as the production entrypoint.

The server mode injects a narrowly scoped synthetic SSO transport into the real
entry factory. Native HTTP and WebSocket upstreams remain real DSH processes.
No environment flag or authentication bypass is added to entry_app.py.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import io
import re
import uuid

import httpx
from websockets.legacy.client import connect

CONFIG = Path('/run/megmeet-entry/config.json')
IDENTITIES = Path('/run/megmeet-entry/probe-identities.json')
EVENTS = Path('/tmp/probe-ws-events.jsonl')
WS_DIAGNOSTICS = Path('/tmp/probe-ws-diagnostics.jsonl')
HTTP_EVENTS = Path('/tmp/probe-http-events.jsonl')
TRACE = ContextVar('offline_probe_http_trace', default='')
SAVED = Path('/tmp/probe-initial.json')
PUBLIC = 'https://platform-ai.megmeet.com'
BASE = 'http://127.0.0.1:8080'
HTTP_ERROR_CLASSES = {
    'ConnectError', 'ConnectTimeout', 'ReadError', 'ReadTimeout', 'WriteError',
    'WriteTimeout', 'PoolTimeout', 'RemoteProtocolError', 'LocalProtocolError',
    'ProxyError', 'UnsupportedProtocol', 'TooManyRedirects', 'DecodingError',
    'HTTPStatusError', 'NetworkError', 'TimeoutException', 'TransportError', 'HTTPError',
}
WS_ERROR_CLASSES = {
    'InvalidStatusCode', 'InvalidStatus', 'InvalidHandshake', 'InvalidHeader',
    'InvalidHeaderValue', 'InvalidHeaderFormat', 'InvalidUpgrade',
    'NegotiationError', 'SecurityError', 'InvalidMessage', 'InvalidURI',
    'TimeoutError', 'ConnectionRefusedError', 'ConnectionResetError',
    'ConnectionAbortedError', 'OSError', 'gaierror',
}
WS_CASES = {'INITIAL_A', 'INITIAL_B', 'RESTORED_A', 'RESTORED_B'}


class Failure(Exception):
    pass


def require(value, code):
    if not value:
        raise Failure(code)


def passed(code):
    print(code + ': PASS', flush=True)


def safe_http_error(exc):
    name = type(exc).__name__
    return name if isinstance(exc, httpx.HTTPError) and name in HTTP_ERROR_CLASSES else 'OTHER_ERROR'


def safe_ws_error(exc):
    name = type(exc).__name__
    return name if name in WS_ERROR_CLASSES else 'OTHER_ERROR'


def ws_error_status(exc):
    status = getattr(exc, 'status_code', None)
    if status is None:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
    return status if type(status) is int and 100 <= status <= 599 else None


def ws_event(event, *, status=None, error=None):
    """Private handshake trace; route success is recorded separately."""
    trace = TRACE.get()
    if not trace:
        return
    require(re.fullmatch(r'[0-9a-f]{32}', trace) is not None
            and event in {'ENTER', 'ATTEMPT', 'SUCCESS', 'FAIL'}, 'WS_DIAGNOSTIC_EVENT_INVALID')
    value = {'trace': trace, 'event': event}
    if status is not None:
        require(type(status) is int and 100 <= status <= 599, 'WS_DIAGNOSTIC_STATUS_INVALID')
        value['status'] = status
    if error is not None:
        require(error in WS_ERROR_CLASSES | {'OTHER_ERROR'}, 'WS_DIAGNOSTIC_ERROR_INVALID')
        value['error'] = error
    require(not WS_DIAGNOSTICS.exists() or WS_DIAGNOSTICS.stat().st_size < 65536,
            'WS_DIAGNOSTIC_TRACE_FULL')
    with WS_DIAGNOSTICS.open('a', encoding='utf-8') as output:
        output.write(json.dumps(value) + '\n')


def http_event(stage, event, *, status=None, error=None):
    """Private test-only trace: no URL, identity, body, header or scalar data."""
    trace = TRACE.get()
    if not trace:
        return
    require(stage in {'SSO', 'BOOTSTRAP', 'UPSTREAM'}
            and event in {'REQUEST', 'RESPONSE', 'RESOLVED', 'READ_OK', 'READ_ERROR', 'ERROR'},
            'DIAGNOSTIC_EVENT_INVALID')
    value = {'trace': trace, 'stage': stage, 'event': event}
    if status is not None:
        require(type(status) is int and 100 <= status <= 599, 'DIAGNOSTIC_STATUS_INVALID')
        value['status'] = status
    if error is not None:
        require(error in HTTP_ERROR_CLASSES | {'OTHER_ERROR'}, 'DIAGNOSTIC_ERROR_INVALID')
        value['error'] = error
    require(not HTTP_EVENTS.exists() or HTTP_EVENTS.stat().st_size < 65536, 'DIAGNOSTIC_TRACE_FULL')
    with HTTP_EVENTS.open('a', encoding='utf-8') as output:
        output.write(json.dumps(value) + '\n')


class TracedASGI:
    """Observability only, outside the unchanged production application."""
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        trace = ''
        if scope['type'] in {'http', 'websocket'}:
            supplied = [value for name, value in scope.get('headers', [])
                        if name.lower() == b'x-offline-probe-trace']
            if len(supplied) == 1:
                value = supplied[0].decode('ascii', errors='ignore')
                if re.fullmatch(r'[0-9a-f]{32}', value):
                    trace = value
        token = TRACE.set(trace)
        try:
            await self.application(scope, receive, send)
        finally:
            TRACE.reset(token)


def inputs():
    from entry_app import Config
    return Config.parse(json.loads(CONFIG.read_text())), json.loads(IDENTITIES.read_text())


def read_ready(binding):
    value = json.loads(Path(binding.bootstrap).read_text())
    require(value.get('ready') is True and value.get('owner_id') == binding.user_id,
            'READY_OWNER_MISMATCH')
    require(str(uuid.UUID(value['generation'])) == value['generation'], 'READY_GENERATION')
    require(isinstance(value['workspace_id'], str) and value['workspace_id'], 'READY_WORKSPACE')
    require(isinstance(value['cookie'], str) and value['cookie'], 'READY_COOKIE')
    return value


class OfflineTransport(httpx.AsyncBaseTransport):
    """Synthetic SSO only; forward requests solely to fixed native authorities."""
    def __init__(self, config, identities):
        self.config = config
        self.identities = identities
        self.real = httpx.AsyncHTTPTransport(trust_env=False)
        self.native = {(httpx.URL(b.upstream).host, httpx.URL(b.upstream).port or 80)
                       for b in config.bindings}

    async def handle_async_request(self, request):
        if str(request.url) == self.config.sso_url:
            require(request.method == 'POST', 'SSO_METHOD')
            http_event('SSO', 'REQUEST')
            cookie = request.headers.get('cookie', '')
            for item in self.identities:
                if cookie == 'access_token=' + item['cookie']:
                    http_event('SSO', 'RESPONSE', status=200)
                    return httpx.Response(200, json={'code': 0, 'data': {'user_no': item['user_no']}}, request=request)
            http_event('SSO', 'RESPONSE', status=403)
            return httpx.Response(403, json={'code': 403}, request=request)
        authority = (request.url.host, request.url.port or 80)
        if request.url.scheme != 'http' or authority not in self.native:
            raise httpx.ConnectError('offline probe forbids non-native traffic', request=request)
        http_event('UPSTREAM', 'REQUEST')
        try:
            response = await self.real.handle_async_request(request)
        except Exception as exc:
            http_event('UPSTREAM', 'ERROR', error=safe_http_error(exc))
            raise
        http_event('UPSTREAM', 'RESPONSE', status=response.status_code)
        return response

    async def aclose(self):
        await self.real.aclose()


def make_recorded_connect(config, actual_connect=connect):
    from entry_app import NATIVE_AUTHORITY, NATIVE_ORIGIN

    @asynccontextmanager
    async def recorded_connect(uri, **kwargs):
        ws_event('ENTER')
        matches = [b for b in config.bindings
                   if httpx.URL(b.upstream).host == kwargs.get('host')
                   and (httpx.URL(b.upstream).port or 80) == kwargs.get('port')]
        require(len(matches) == 1, 'WS_FIXED_TARGET')
        binding = matches[0]
        ready = read_ready(binding)
        require(kwargs.get('extra_headers') == {'Cookie': ready['cookie']}, 'WS_SERVER_COOKIE')
        require(kwargs.get('origin') == NATIVE_ORIGIN, 'WS_INTERNAL_ORIGIN')
        ws_url = httpx.URL(uri)
        require(ws_url.scheme == 'ws' and ws_url.netloc.decode() == NATIVE_AUTHORITY
                and ws_url.path == '/api/remote.mux', 'WS_INTERNAL_AUTHORITY')
        ws_event('ATTEMPT')
        established = False
        try:
            async with actual_connect(uri, **kwargs) as connection:
                require(connection.open, 'WS_UPSTREAM_NOT_OPEN')
                established = True
                ws_event('SUCCESS', status=101)
                event = {'owner_id': binding.user_id, 'generation': ready['generation'], 'cookie_match': True}
                with EVENTS.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(event) + '\n')
                yield connection
        except Exception as exc:
            if not established:
                try:
                    ws_event('FAIL', status=ws_error_status(exc), error=safe_ws_error(exc))
                except Exception:
                    pass  # Diagnostics cannot replace the actual handshake failure.
            raise

    return recorded_connect


def serve():
    import uvicorn
    from entry_app import create_app, private_file, resolve_identity
    config, identities = inputs()
    transport = OfflineTransport(config, identities)

    async def recorded_resolver(client, cfg, cookie):
        binding = await resolve_identity(client, cfg, cookie)
        http_event('SSO', 'RESOLVED')
        return binding

    bootstrap_paths = {binding.bootstrap for binding in config.bindings}

    def recorded_reader(path, limit):
        try:
            value = private_file(path, limit)
        except Exception:
            if path in bootstrap_paths:
                http_event('BOOTSTRAP', 'READ_ERROR')
            raise
        if path in bootstrap_paths:
            http_event('BOOTSTRAP', 'READ_OK')
        return value

    app = create_app(config, transport=transport, resolver=recorded_resolver,
                     reader=recorded_reader, websocket_connect=make_recorded_connect(config), reauth_seconds=1)
    # This separate offline executable never logs exception strings or headers.
    logging.disable(logging.CRITICAL)
    uvicorn.run(TracedASGI(app), host='0.0.0.0', port=8080, access_log=False, log_config=None, log_level='critical')


def envelope(rpc_id):
    return {'type': 'client-request', 'rpcId': rpc_id, 'method': 'workspace/create',
            'payload': {'args': {'request': {'path': '/state/work'}}}}


def validate_workspace(response, rpc_id, expected):
    require(response.status_code == 200, 'NATIVE_HTTP_STATUS')
    data = response.json()
    result = data.get('result', {})
    value = result.get('value', {})
    workspace = value.get('workspace', {})
    require(data.get('type') == 'server-response' and data.get('rpcId') == rpc_id
            and result.get('ok') is True and value.get('created') is False
            and workspace.get('workspaceId') == expected
            and workspace.get('path') == '/state/work', 'PRECREATED_WORKSPACE_REUSED')


def private_response(response, readies):
    require('set-cookie' not in response.headers, 'BROWSER_RECEIVED_HOST_COOKIE')
    body = response.content
    for ready in readies:
        require(ready['cookie'].encode() not in body, 'HOST_COOKIE_IN_BODY')
        for pair in ready['cookie'].split('; '):
            value = pair.partition('=')[2]
            require(value and value.encode() not in body, 'HOST_COOKIE_VALUE_IN_BODY')


def summarize_http_trace(trace):
    """Return only fixed categories and HTTP status, never arbitrary values."""
    events = []
    if HTTP_EVENTS.exists():
        require(HTTP_EVENTS.stat().st_size <= 70000, 'DIAGNOSTIC_TRACE_TOO_LARGE')
        for line in HTTP_EVENTS.read_text().splitlines():
            event = json.loads(line)
            if event.get('trace') == trace:
                events.append(event)
    upstream = [e for e in events if e.get('stage') == 'UPSTREAM']
    sso = [e for e in events if e.get('stage') == 'SSO']
    bootstrap = [e for e in events if e.get('stage') == 'BOOTSTRAP']
    if upstream:
        stage = 'UPSTREAM'
    elif any(e.get('event') == 'RESOLVED' for e in sso):
        # Request-body and bootstrap validation both occur after identity.
        # Do not claim a parsed-bootstrap failure from a file-read event alone.
        stage = 'BOOTSTRAP_OR_LOCAL_GATE'
    elif sso:
        stage = 'SSO'
    else:
        stage = 'LOCAL_GATE'
    print('PROBE_DIAGNOSTIC_STAGE: ' + stage, flush=True)
    for name, selected in [('SSO', sso), ('UPSTREAM', upstream)]:
        statuses = [e.get('status') for e in selected if e.get('event') == 'RESPONSE']
        if statuses and type(statuses[-1]) is int and 100 <= statuses[-1] <= 599:
            print('PROBE_' + name + '_HTTP_STATUS: ' + str(statuses[-1]), flush=True)
        errors = [e.get('error') for e in selected if e.get('event') == 'ERROR']
        if errors and errors[-1] in HTTP_ERROR_CLASSES | {'OTHER_ERROR'}:
            print('PROBE_' + name + '_ERROR_CLASS: ' + errors[-1], flush=True)
    if bootstrap and bootstrap[-1].get('event') == 'READ_ERROR':
        print('PROBE_BOOTSTRAP_FILE: READ_ERROR', flush=True)


async def diagnose_native_failure(response, trace, binding, ready, rpc_id, case):
    """Informative direct comparisons never replace the failed primary result."""
    print('PROBE_NATIVE_HTTP_STATUS: ' + str(response.status_code), flush=True)
    print('PROBE_DIAGNOSTIC_CASE: ' + case, flush=True)
    try:
        summarize_http_trace(trace)
    except Exception:
        print('PROBE_DIAGNOSTIC_TRACE: UNAVAILABLE', flush=True)
    pairs = [('PUBLIC', 'platform-ai.megmeet.com', PUBLIC, False),
             ('PUBLIC_FORWARDED_PROTO', 'platform-ai.megmeet.com', PUBLIC, True),
             ('LOOPBACK', '127.0.0.1:3085', 'http://127.0.0.1:3085', False)]
    public_status = None
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as direct:
        for label, host, origin, forwarded in pairs:
            if forwarded and public_status == 200:
                continue
            try:
                headers = {'Cookie': ready['cookie'], 'Host': host, 'Origin': origin,
                           'Accept-Encoding': 'identity'}
                if forwarded:
                    headers['X-Forwarded-Proto'] = 'https'
                result = await direct.post(binding.upstream + '/api/workspace/create',
                    headers=headers, json=envelope(rpc_id))
                print('PROBE_DIRECT_' + label + '_HTTP_STATUS: ' + str(result.status_code), flush=True)
                if label == 'PUBLIC':
                    public_status = result.status_code
            except Exception as exc:
                print('PROBE_DIRECT_' + label + '_ERROR_CLASS: ' + safe_http_error(exc), flush=True)
    print('PROBE_PRIMARY_RESULT: FAIL_PRESERVED', flush=True)


async def require_native_workspace(response, trace, binding, ready, rpc_id, case):
    if response.status_code != 200:
        try:
            await diagnose_native_failure(response, trace, binding, ready, rpc_id, case)
        except Exception:
            print('PROBE_DIAGNOSTIC_TRACE: UNAVAILABLE', flush=True)
        raise Failure('NATIVE_HTTP_STATUS')
    validate_workspace(response, rpc_id, ready['workspace_id'])


def summarize_ws_trace(trace):
    events = []
    if WS_DIAGNOSTICS.exists():
        require(WS_DIAGNOSTICS.stat().st_size <= 70000, 'WS_DIAGNOSTIC_TRACE_TOO_LARGE')
        for line in WS_DIAGNOSTICS.read_text().splitlines():
            event = json.loads(line)
            if event.get('trace') == trace and event.get('event') in {'ENTER', 'ATTEMPT', 'SUCCESS', 'FAIL'}:
                events.append(event)
    stages = {'ENTER': 'CONNECTOR_ENTERED', 'ATTEMPT': 'UPSTREAM_ATTEMPT',
              'SUCCESS': 'UPSTREAM_SUCCESS', 'FAIL': 'UPSTREAM_FAILED'}
    stage = stages[events[-1]['event']] if events else 'CONNECTOR_NOT_REACHED'
    print('PROBE_WS_DIAGNOSTIC_STAGE: ' + stage, flush=True)
    if events:
        last = events[-1]
        status = last.get('status')
        if type(status) is int and 100 <= status <= 599:
            print('PROBE_WS_UPSTREAM_HTTP_STATUS: ' + str(status), flush=True)
        if last.get('error') in WS_ERROR_CLASSES | {'OTHER_ERROR'}:
            print('PROBE_WS_UPSTREAM_ERROR_CLASS: ' + last['error'], flush=True)
    return bool(events)


async def diagnose_ws_failure(exc, trace, binding, ready, case):
    """Same-binding direct comparison is diagnostic only, never a fallback."""
    from entry_app import NATIVE_AUTHORITY, NATIVE_ORIGIN
    require(case in WS_CASES, 'WS_DIAGNOSTIC_CASE_INVALID')
    print('PROBE_WS_DIAGNOSTIC_CASE: ' + case, flush=True)
    status = ws_error_status(exc)
    if status is not None:
        print('PROBE_WS_BROWSER_HTTP_STATUS: ' + str(status), flush=True)
    print('PROBE_WS_BROWSER_ERROR_CLASS: ' + safe_ws_error(exc), flush=True)
    try:
        if not summarize_ws_trace(trace):
            summarize_http_trace(trace)
    except Exception:
        print('PROBE_WS_DIAGNOSTIC_TRACE: UNAVAILABLE', flush=True)
    target = httpx.URL(binding.upstream)
    try:
        async with connect('ws://' + NATIVE_AUTHORITY + '/api/remote.mux',
                           host=target.host, port=target.port or 80, origin=NATIVE_ORIGIN,
                           extra_headers={'Cookie': ready['cookie']},
                           open_timeout=5, close_timeout=2, ping_interval=None,
                           max_size=2 * 1024 * 1024, compression=None) as connection:
            require(connection.open, 'WS_DIRECT_NOT_OPEN')
            print('PROBE_WS_DIRECT_HTTP_STATUS: 101', flush=True)
    except Exception as direct_exc:
        direct_status = ws_error_status(direct_exc)
        if direct_status is not None:
            print('PROBE_WS_DIRECT_HTTP_STATUS: ' + str(direct_status), flush=True)
        print('PROBE_WS_DIRECT_ERROR_CLASS: ' + safe_ws_error(direct_exc), flush=True)


async def websocket_check(item, binding, ready, *, case, forged=False):
    require(case in WS_CASES, 'WS_DIAGNOSTIC_CASE_INVALID')
    previous = EVENTS.read_text().splitlines() if EVENTS.exists() else []
    trace = uuid.uuid4().hex
    headers = {'Cookie': 'access_token=' + item['cookie'], 'X-Offline-Probe-Trace': trace}
    if forged:
        headers.update({'X-User-ID': 'forged-owner', 'X-Forwarded-Host': 'forged.invalid'})
    established = False
    try:
        async with connect('ws://127.0.0.1:8080/api/remote.mux', origin=PUBLIC,
                           extra_headers=headers, open_timeout=10, close_timeout=3,
                           ping_interval=None, max_size=2 * 1024 * 1024) as connection:
            require(connection.open, 'WS_UPSTREAM_HANDSHAKE')
            established = True
            # Entry accepts only after the real selected DSH handshake succeeds.
            await asyncio.sleep(0.1)
    except Failure:
        raise
    except Exception as exc:
        if established:
            raise Failure('WS_AFTER_HANDSHAKE') from None
        try:
            await diagnose_ws_failure(exc, trace, binding, ready, case)
        except Exception:
            print('PROBE_WS_DIAGNOSTIC_TRACE: UNAVAILABLE', flush=True)
        print('PROBE_WS_PRIMARY_RESULT: FAIL_PRESERVED', flush=True)
        raise Failure('WS_BROWSER_HANDSHAKE') from None
    events = EVENTS.read_text().splitlines()
    require(len(events) == len(previous) + 1, 'WS_ROUTE_EVENT_COUNT')
    event = json.loads(events[-1])
    require(event == {'owner_id': binding.user_id, 'generation': ready['generation'],
                      'cookie_match': True}, 'WS_IDENTITY_ROUTE')


async def denied_websocket(headers, origin=PUBLIC):
    before = len(EVENTS.read_text().splitlines()) if EVENTS.exists() else 0
    before_attempts = len(WS_DIAGNOSTICS.read_text().splitlines()) if WS_DIAGNOSTICS.exists() else 0
    headers = {**headers, 'X-Offline-Probe-Trace': uuid.uuid4().hex}
    try:
        async with connect('ws://127.0.0.1:8080/api/remote.mux', origin=origin,
                           extra_headers=headers, open_timeout=5, close_timeout=2,
                           ping_interval=None):
            raise Failure('UNAUTHORIZED_WS_ACCEPTED')
    except Failure:
        raise
    except Exception as exc:
        require(getattr(exc, 'status_code', None) == 403, 'WS_DENIAL_WAS_NOT_403')
    after = len(EVENTS.read_text().splitlines()) if EVENTS.exists() else 0
    after_attempts = len(WS_DIAGNOSTICS.read_text().splitlines()) if WS_DIAGNOSTICS.exists() else 0
    require(before == after, 'UNAUTHORIZED_WS_REACHED_HOST')
    require(before_attempts == after_attempts, 'UNAUTHORIZED_WS_REACHED_CONNECTOR')


async def native_cookie_boundary(bindings, readies, phase):
    """Internal authority must still enforce each DSH Host's private cookie."""
    from entry_app import NATIVE_AUTHORITY, NATIVE_ORIGIN
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as direct:
        for i, binding in enumerate(bindings):
            for cookie in (None, readies[1-i]['cookie']):
                headers = {'Host': NATIVE_AUTHORITY, 'Origin': NATIVE_ORIGIN,
                           'Accept-Encoding': 'identity'}
                if cookie is not None:
                    headers['Cookie'] = cookie
                for method, path in (('GET', '/'), ('POST', '/api/workspace/create')):
                    args = {'json': envelope('boundary-' + phase)} if method == 'POST' else {}
                    # Explicit Request never merges an AsyncClient cookie jar.
                    response = await direct.send(httpx.Request(method, binding.upstream + path,
                                                                headers=headers, **args))
                    require(response.status_code == 401, 'NATIVE_PRIVATE_COOKIE_BOUNDARY')
    passed('NATIVE_ANONYMOUS_AND_FOREIGN_COOKIES_REJECTED_' + phase.upper())


async def check(phase):
    from probe_page import validated_page_paths, PageCheckError
    cfg, identities = inputs()
    require(len(identities) == 2, 'SYNTHETIC_IDENTITY_COUNT')
    bindings = cfg.bindings
    readies = [read_ready(binding) for binding in bindings]
    require(readies[0]['workspace_id'] != readies[1]['workspace_id'], 'DISTINCT_WORKSPACES')
    require(readies[0]['cookie'] != readies[1]['cookie'], 'DISTINCT_HOST_COOKIES')
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=12) as client:
        for i, (binding, identity, ready) in enumerate(zip(bindings, identities, readies)):
            case = phase.upper() + '_' + ('A' if i == 0 else 'B')
            require(identity['user_no'] == binding.user_no, 'SYNTHETIC_BINDING_ORDER')
            # The first native call precedes any page request and must reuse a
            # workspace already created by the new launcher before readiness.
            rpc_id = 'entry-probe-' + phase + '-' + str(i)
            trace = uuid.uuid4().hex
            headers = {'Cookie': 'access_token=' + identity['cookie'], 'Origin': PUBLIC,
                       'X-Offline-Probe-Trace': trace}
            response = await client.post(BASE + '/api/workspace/create', headers=headers, json=envelope(rpc_id))
            await require_native_workspace(response, trace, binding, ready, rpc_id,
                                           case)
            private_response(response, readies)
            passed('NATIVE_PREPAGE_HTTP_' + case)
            page = await client.get(BASE + '/', headers=headers)
            require(page.status_code == 200 and b'__DSH_BOOT__' in page.content, 'NATIVE_PAGE')
            private_response(page, readies)
            passed('NATIVE_PAGE_' + case)
            try:
                validated_page_paths(page.text, cfg.public_origin)
            except PageCheckError as exc:
                raise Failure(str(exc)) from None
            passed('NATIVE_PAGE_URLS_' + case)
            forged = dict(headers)
            trace = uuid.uuid4().hex
            forged['X-Offline-Probe-Trace'] = trace
            forged.update({'X-User-ID': bindings[1-i].user_id,
                           'X-User-No': bindings[1-i].user_no,
                           'X-Forwarded-Host': httpx.URL(bindings[1-i].upstream).host,
                           'Cookie': headers['Cookie'] + '; ' + readies[1-i]['cookie']})
            response = await client.post(BASE + '/api/workspace/create?owner_id=' + bindings[1-i].user_id,
                                         headers=forged, json=envelope(rpc_id))
            await require_native_workspace(response, trace, binding, ready, rpc_id,
                                           'FORGED_' + phase.upper() + '_' + ('A' if i == 0 else 'B'))
            private_response(response, readies)
            passed('NATIVE_FORGED_HTTP_' + case)
            await websocket_check(identity, binding, ready, case=case, forged=True)
            passed('NATIVE_WEBSOCKET_' + case)
        passed('PREPAGE_DEFAULT_WORKSPACES_AND_NATIVE_HTTP_' + phase.upper())
        passed('FORGED_OWNER_HEADERS_AND_FOREIGN_DSH_COOKIE_IGNORED_' + phase.upper())
        passed('REAL_NATIVE_WEBSOCKET_ROUTING_' + phase.upper())
        passed('NATIVE_PAGE_RESOURCE_URLS_SAME_ORIGIN_' + phase.upper())
        await native_cookie_boundary(bindings, readies, phase)
        for headers in ({}, {'X-User-ID': bindings[0].user_id}, {'Cookie': readies[0]['cookie']},
                        {'Cookie': 'access_token=unknown-probe-account'}):
            response = await client.get(BASE + '/', headers=headers)
            require(response.status_code == 403, 'UNAUTHENTICATED_HTTP_NOT_REJECTED')
        response = await client.post(BASE + '/api/workspace/create',
                                     headers={'Cookie': 'access_token=' + identities[0]['cookie'],
                                              'Origin': 'https://foreign.invalid'}, json=envelope('bad-origin'))
        require(response.status_code == 403, 'FOREIGN_ORIGIN_HTTP')
        await denied_websocket({'X-User-ID': bindings[0].user_id})
        await denied_websocket({'Cookie': 'access_token=' + identities[0]['cookie']}, 'https://foreign.invalid')
        passed('ANONYMOUS_FORGED_IDENTITY_AND_FOREIGN_ORIGIN_DENIED_' + phase.upper())
    state = [{'owner_id': r['owner_id'], 'workspace_id': r['workspace_id'], 'generation': r['generation']}
             for r in readies]
    if phase == 'initial':
        fd = os.open(SAVED, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(state, output)
    else:
        previous = json.loads(SAVED.read_text())
        require(state[0]['owner_id'] == previous[0]['owner_id']
                and state[0]['workspace_id'] == previous[0]['workspace_id']
                and state[0]['generation'] != previous[0]['generation'], 'A_NEW_GENERATION_SAME_WORKSPACE')
        require(state[1] == previous[1], 'B_BOOTSTRAP_UNCHANGED')
        passed('NEW_GENERATION_RECREATE_RECOVERY_AND_B_UNCHANGED')


def linux_guards():
    """Run POSIX cases skipped on Windows, with no provider or runtime calls."""
    from entry_app import private_file
    require(os.name == 'posix', 'LINUX_GUARDS_REQUIRE_POSIX')
    with tempfile.TemporaryDirectory(prefix='native-entry-tests-', dir='/tmp') as temporary:
        root = Path(temporary)
        (root / 'tests').mkdir()
        shutil.copyfile('/probe/host_launcher.py', root / 'host_launcher.py')
        shutil.copyfile('/probe/test_host_launcher.py', root / 'tests' / 'test_host_launcher.py')
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_host_launcher.py')
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
        require(result.testsRun >= 20 and not result.skipped and result.wasSuccessful(), 'LINUX_LAUNCHER_UNIT_GUARDS')
        real = root / 'ready.json'
        real.write_text('{}')
        link = root / 'linked.json'
        link.symlink_to(real)
        try:
            private_file(str(link), 128)
        except ValueError:
            pass
        else:
            raise Failure('ENTRY_SYMLINK_ACCEPTED')
        passed('LINUX_LAUNCHER_PERMISSION_AND_ENTRY_SYMLINK_GUARDS')


def main():
    os.umask(0o077)
    try:
        if sys.argv[1:] == ['serve']:
            serve()
        elif sys.argv[1:] == ['linux-guards']:
            linux_guards()
        elif len(sys.argv) == 3 and sys.argv[1] == 'check' and sys.argv[2] in ('initial', 'restored'):
            logging.disable(logging.CRITICAL)
            asyncio.run(check(sys.argv[2]))
        else:
            raise Failure('PROBE_CLIENT_ARGUMENTS')
        return 0
    except Exception as exc:
        print('PROBE_CLIENT_ERROR_CLASS: ' + type(exc).__name__, flush=True)
        if isinstance(exc, Failure):
            print('PROBE_CLIENT_CHECK: ' + str(exc), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
