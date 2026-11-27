"""Employee-owned DingTalk AIHub Streamable HTTP connections.

Only the official endpoint is reachable. URLs remain encrypted in the existing
connector store; model calls receive opaque action IDs, never connection keys.
No tool call is retried. A timed-out write has an unknown outcome.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import uuid4

import httpx

HOST = 'mcp-gw.dingtalk.com'
MARKET = 'https://aihub.dingtalk.com/#/mcp-market/mcp'
VERSIONS = {'2025-06-18', '2025-03-26'}
MAX_CONNECTIONS = 16
TTL = 30 * 86400
MAX_RESPONSE = 2 * 1024**2
MAX_REGISTRY = 1024**2
MAX_PUBLIC = 512 * 1024
MAX_TOOL_RESULT = 60 * 1024
MAX_ARGUMENTS = 64 * 1024
MAX_TOOLS = 128
OPERATION_TIMEOUT = 40
ID = re.compile(r'^[a-f0-9]{32}$')
ACTION = re.compile(r'^mcp_([a-f0-9]{32})_([a-f0-9]{24})$')
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff'}
MESSAGES = {
    'dingtalk_mcp_url_invalid': '请粘贴钉钉 AI 应用服务市场提供的完整 StreamableHttp 地址。',
    'dingtalk_mcp_name_invalid': '请输入 1 至 60 字的连接名称，不要在名称中填写连接地址。',
    'dingtalk_mcp_input_invalid': '连接信息格式不正确，请检查后重试。',
    'dingtalk_mcp_authorization_denied': '此钉钉连接已失效或权限不足，请在钉钉市场重新获取地址。',
    'dingtalk_mcp_unavailable': '暂时无法连接钉钉服务，请稍后重试。',
    'dingtalk_mcp_protocol_invalid': '钉钉服务返回了不兼容的 MCP 响应，请稍后刷新连接。',
    'dingtalk_mcp_version_unsupported': '此连接的 MCP 协议暂不兼容，请使用 StreamableHttp 地址。',
    'dingtalk_mcp_response_too_large': '钉钉服务返回内容过多，请缩小查询范围。',
    'dingtalk_mcp_connection_limit': '最多保存 16 个钉钉连接，请先移除不用的连接。',
    'dingtalk_mcp_connection_missing': '此钉钉连接已移除或过期，请重新连接。',
    'dingtalk_mcp_connection_busy': '此连接正在处理操作，请稍后重试。',
    'dingtalk_mcp_tools_changed': '钉钉工具或授权已变化，请刷新连接后重新发起操作。',
    'dingtalk_mcp_tool_unknown': '所选钉钉工具已不存在，请刷新连接。',
    'dingtalk_mcp_rate_limit': '钉钉服务暂时限流，请稍后重试。',
    'dingtalk_mcp_write_result_unknown': '发送结果尚不能确认，请先到钉钉核对，避免重复操作。',
}


def failure(code, status=400):
    # Lazy import keeps the protocol module independent of router import order.
    from .enterprise_connectors import ConnectorFailure
    return ConnectorFailure(status, code)


def packed(value, maximum=MAX_RESPONSE):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError, RecursionError):
        raise failure('dingtalk_mcp_input_invalid') from None
    if len(raw) > maximum:
        raise failure('dingtalk_mcp_response_too_large', 413)
    return raw


def checksum(value):
    return hashlib.sha256(packed(value)).hexdigest()


def endpoint(value):
    try:
        if not isinstance(value, str) or not 16 <= len(value) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError()
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname != HOST or url.netloc not in {HOST, HOST + ':443'}
                or url.username or url.password or url.fragment or '\\' in value
                or not re.fullmatch(r'/server/[A-Za-z0-9_-]{1,128}', url.path)):
            raise ValueError()
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        if len(pairs) != 1 or pairs[0][0] != 'key':
            raise ValueError()
        key = pairs[0][1]
        if not 8 <= len(key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError()
        return 'https://' + HOST + url.path + '?key=' + quote(key, safe=''), key
    except (ValueError, TypeError):
        raise failure('dingtalk_mcp_url_invalid') from None


def label(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 60
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r'https?://|key\s*=', value, re.I)):
        raise failure('dingtalk_mcp_name_invalid')
    return value.strip()


def redact(value, url):
    """Provider-controlled text must not reflect this connection's credential."""
    _, key = endpoint(url)
    def visit(item, depth=0):
        if depth > 30:
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        if isinstance(item, str):
            item = item.replace(url, '[钉钉连接凭据]').replace(key, '[凭据]').replace(quote(key, safe=''), '[凭据]')
            return re.sub(r'https://mcp-gw\.dingtalk\.com/[^\s"<>]+', '[钉钉连接地址]', item)
        if isinstance(item, list):
            return [visit(x, depth + 1) for x in item]
        if isinstance(item, dict):
            return {visit(k, depth + 1): visit(v, depth + 1) for k, v in item.items()}
        return item
    return visit(value)


class _HideCredentialURLs(logging.Filter):
    def filter(self, record):
        # HTTPX logs full request URLs at INFO. Suppress this fixed host only.
        return HOST not in record.getMessage()


logging.getLogger('httpx').addFilter(_HideCredentialURLs())


class MCPClient:
    def __init__(self, url, transport=None):
        self.url, _ = endpoint(url)
        self.transport = transport
        self.session_id = None
        self.version = None
        self.next_id = 0
        self.client = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(transport=self.transport, trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(15, connect=5))
        return self

    async def __aexit__(self, *_):
        try:
            if self.session_id:
                # Session disposal is not a tool operation and never retries it.
                async with asyncio.timeout(2):
                    self.client.cookies.clear()
                    async with self.client.stream('DELETE', self.url, headers=self.headers()):
                        pass
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            try:
                await self.client.aclose()
            except (Exception, asyncio.CancelledError):
                # Disposal cannot replace a known/unknown tool result. The
                # remote session expiry still bounds abandoned sessions.
                pass

    def headers(self):
        result = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if self.session_id:
            result['Mcp-Session-Id'] = self.session_id
        if self.version:
            result['MCP-Protocol-Version'] = self.version
        return result

    async def _post(self, body, notification=False, initializing=False):
        raw = packed(body, MAX_ARGUMENTS + 8192)
        try:
            self.client.cookies.clear()
            async with self.client.stream('POST', self.url, content=raw, headers=self.headers()) as response:
                if response.status_code in {401, 403}:
                    raise failure('dingtalk_mcp_authorization_denied', 403)
                if response.status_code == 429:
                    raise failure('dingtalk_mcp_rate_limit', 429)
                if not 200 <= response.status_code < 300:
                    raise failure('dingtalk_mcp_unavailable', 502)
                if initializing:
                    sid = response.headers.get('mcp-session-id')
                    if sid is not None:
                        if not 1 <= len(sid) <= 1024 or any(not 0x21 <= ord(c) <= 0x7e for c in sid):
                            raise failure('dingtalk_mcp_protocol_invalid', 502)
                        self.session_id = sid
                if notification:
                    if response.status_code not in {200, 202, 204}:
                        raise failure('dingtalk_mcp_protocol_invalid', 502)
                    return None
                media = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if media not in {'application/json', 'text/event-stream'}:
                    raise failure('dingtalk_mcp_protocol_invalid', 502)
                total, buffer, data_lines, events = 0, b'', [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE:
                        raise failure('dingtalk_mcp_response_too_large', 502)
                    buffer += chunk
                    if media == 'application/json':
                        continue
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        line = line.rstrip(b'\r')
                        if line.startswith(b'data:'):
                            data_lines.append(line[5:].lstrip(b' '))
                        elif not line and data_lines:
                            events += 1
                            if events > 64:
                                raise failure('dingtalk_mcp_protocol_invalid', 502)
                            message = json.loads(b'\n'.join(data_lines)); data_lines = []
                            value = await self._message(message, body['id'])
                            if value is not None:
                                return value
                if media == 'application/json':
                    value = await self._message(json.loads(buffer), body['id'])
                    if value is not None:
                        return value
                raise failure('dingtalk_mcp_protocol_invalid', 502)
        except (httpx.HTTPError, TimeoutError):
            raise failure('dingtalk_mcp_unavailable', 502) from None
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise failure('dingtalk_mcp_protocol_invalid', 502) from None

    async def _message(self, value, request_id):
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        if 'method' in value:
            if 'id' not in value:  # Notifications are untrusted and not executed.
                return None
            reply = {'jsonrpc': '2.0', 'id': value['id']}
            if value['method'] == 'ping':
                reply['result'] = {}
            else:
                reply['error'] = {'code': -32601, 'message': 'Client capability unavailable'}
            await self._post(reply, notification=True)
            return None
        if value.get('id') != request_id or type(value.get('id')) is not int:
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        if 'error' in value:
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        if not isinstance(value.get('result'), dict):
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        return value['result']

    async def request(self, method, params):
        self.next_id += 1
        return await self._post({'jsonrpc': '2.0', 'id': self.next_id, 'method': method,
                                 'params': params}, initializing=method == 'initialize')

    async def initialize(self):
        result = await self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'megmeet-maimi', 'version': '1.0.0'}})
        if result.get('protocolVersion') not in VERSIONS:
            raise failure('dingtalk_mcp_version_unsupported', 502)
        if not isinstance(result.get('capabilities'), dict) or not isinstance(result['capabilities'].get('tools'), dict):
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        self.version = result['protocolVersion']
        await self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, notification=True)

    async def tools(self):
        found, seen, cursor = [], set(), None
        for _ in range(16):
            result = await self.request('tools/list', {'cursor': cursor} if cursor else {})
            rows = result.get('tools')
            if not isinstance(rows, list):
                raise failure('dingtalk_mcp_protocol_invalid', 502)
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', row['name'])
                        or row['name'] in seen or not isinstance(row.get('inputSchema'), dict)
                        or row['inputSchema'].get('type') != 'object'):
                    raise failure('dingtalk_mcp_protocol_invalid', 502)
                seen.add(row['name'])
                if endpoint(self.url)[1] in row['name']:
                    raise failure('dingtalk_mcp_protocol_invalid', 502)
                packed(row['inputSchema'], 32768)
                annotation = row.get('annotations', {})
                text = row.get('description', row.get('title', row['name']))
                if not isinstance(text, str):
                    raise failure('dingtalk_mcp_protocol_invalid', 502)
                clean = redact({'description': text[:2000], 'schema': row['inputSchema']}, self.url)
                tool = {'name': row['name'], 'description': clean['description'], 'input_schema': clean['schema'],
                        'read_only': isinstance(annotation, dict) and annotation.get('readOnlyHint') is True}
                tool['key'] = hashlib.sha256(row['name'].encode()).hexdigest()[:24]
                tool['revision'] = checksum(tool)
                found.append(tool)
                if len(found) > MAX_TOOLS:
                    raise failure('dingtalk_mcp_response_too_large', 413)
            cursor = result.get('nextCursor')
            if cursor is None:
                packed(found, MAX_PUBLIC)
                return found
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise failure('dingtalk_mcp_protocol_invalid', 502)
        raise failure('dingtalk_mcp_response_too_large', 413)

    async def call(self, tool, arguments):
        result = await self.request('tools/call', {'name': tool['name'], 'arguments': arguments})
        if not isinstance(result.get('content', []), list) or ('isError' in result and type(result['isError']) is not bool):
            raise failure('dingtalk_mcp_protocol_invalid', 502)
        public = {key: result[key] for key in ('content', 'structuredContent', 'isError') if key in result}
        public = redact(public, self.url)
        packed(public, MAX_RESPONSE)
        if len(packed(public)) > MAX_TOOL_RESULT:
            # Native tool output has a 90k-character limit. Report bounded,
            # explicit partial output rather than let that outer layer silently
            # cut a JSON document or invite a duplicate write.
            preview = []
            for item in public.get('content', []):
                if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str):
                    preview.append({'type': 'text', 'text': item['text'][:2000]})
                if len(preview) == 5:
                    break
            public = {'content': preview, 'isError': public.get('isError', False),
                'truncated': True, 'note': '钉钉已返回结果，但内容过长，仅展示部分文字。请缩小查询范围或分批读取；写操作请先到钉钉核对，不要重复提交。'}
            packed(public, MAX_TOOL_RESULT)
        public['untrusted_external_content'] = True
        return public


class MCPConnections:
    def __init__(self, service, transport=None):
        self.svc, self.store, self.transport = service, service.store, transport

    async def _load(self, owner, touch=True):
        raw = await self.store.get('dingtalk_mcp', owner, 'connections')
        value = self.svc.unseal(owner, raw) if raw else {'version': 1, 'connections': {}}
        if (not isinstance(value, dict) or value.get('version') != 1
                or not isinstance(value.get('connections'), dict) or len(value['connections']) > MAX_CONNECTIONS):
            raise failure('dingtalk_mcp_protocol_invalid', 503)
        if raw and touch:
            await self.store.replace_if_equal('dingtalk_mcp', owner, 'connections', raw, raw, TTL)
        return raw, value

    async def _change(self, owner, mutate):
        for _ in range(4):
            raw, value = await self._load(owner, touch=False)
            result = mutate(value['connections'])
            packed(value, MAX_REGISTRY)
            sealed = self.svc.seal(owner, value)
            success = await self.store.replace_if_equal('dingtalk_mcp', owner, 'connections', raw, sealed, TTL) if raw else await self.store.put('dingtalk_mcp', owner, 'connections', sealed, TTL, nx=True)
            if success:
                return result
        raise failure('dingtalk_mcp_connection_busy', 409)

    async def _connection(self, owner, connection_id):
        if not isinstance(connection_id, str) or not ID.fullmatch(connection_id):
            raise failure('dingtalk_mcp_input_invalid')
        _, value = await self._load(owner)
        found = value['connections'].get(connection_id)
        if not found:
            raise failure('dingtalk_mcp_connection_missing', 404)
        return found

    @asynccontextmanager
    async def _lock(self, owner, connection_id):
        nonce = secrets.token_urlsafe(32)
        if not await self.store.put('dingtalk_mcp_lock', owner, connection_id, nonce, 60, nx=True):
            raise failure('dingtalk_mcp_connection_busy', 409)
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT):
                yield
        except TimeoutError:
            raise failure('dingtalk_mcp_unavailable', 502) from None
        finally:
            try:
                await self.store.replace_if_equal('dingtalk_mcp_lock', owner, connection_id, nonce, None)
            except (Exception, asyncio.CancelledError):
                # A lock cleanup failure must not disguise a completed write
                # as retryable. The 60-second lease expires without renewal.
                pass

    @staticmethod
    def public(connection):
        read = sum(tool['read_only'] for tool in connection['tools'])
        return {'id': connection['id'], 'name': connection['name'], 'status': 'ready' if connection['tools'] else 'empty',
                'tool_count': len(connection['tools']), 'read_tool_count': read,
                'write_tool_count': len(connection['tools']) - read,
                'last_checked_at': connection['checked_at'],
                'status_message': '已连接，实际权限由钉钉服务校验。' if connection['tools'] else '已连接，当前服务未提供工具。'}

    async def listing(self, owner):
        _, value = await self._load(owner)
        return {'connections': [self.public(c) for c in value['connections'].values()],
                'maximum_connections': MAX_CONNECTIONS, 'market_url': MARKET,
                'account_binding_verified': False}

    async def create(self, owner, name, url, commit_guard=None):
        name, (url, _) = label(name), endpoint(url)
        await self.store.budget(owner)
        _, saved = await self._load(owner)
        if len(saved['connections']) >= MAX_CONNECTIONS:
            raise failure('dingtalk_mcp_connection_limit', 409)
        try:
            async with asyncio.timeout(35), MCPClient(url, self.transport) as client:
                await client.initialize()
                tools = await client.tools()
        except TimeoutError:
            raise failure('dingtalk_mcp_unavailable', 502) from None
        record = {'id': uuid4().hex, 'name': name, 'url': url, 'revision': secrets.token_urlsafe(32),
                  'tools': tools, 'checked_at': int(time.time())}
        if commit_guard:
            await commit_guard()
        def add(rows):
            if len(rows) >= MAX_CONNECTIONS:
                raise failure('dingtalk_mcp_connection_limit', 409)
            rows[record['id']] = record
        await self._change(owner, add)
        return {'status': 'connected', 'connection': self.public(record)}

    async def refresh(self, owner, connection_id, commit_guard=None):
        await self.store.budget(owner)
        async with self._lock(owner, connection_id):
            record = await self._connection(owner, connection_id)
            async with MCPClient(record['url'], self.transport) as client:
                await client.initialize()
                tools = await client.tools()
            fresh = {**record, 'tools': tools, 'revision': secrets.token_urlsafe(32), 'checked_at': int(time.time())}
            if commit_guard:
                await commit_guard()
            def update(rows):
                if rows.get(connection_id, {}).get('revision') != record['revision']:
                    raise failure('dingtalk_mcp_tools_changed', 409)
                rows[connection_id] = fresh
            await self._change(owner, update)
            return {'status': 'refreshed', 'connection': self.public(fresh)}

    async def disconnect(self, owner, connection_id):
        async with self._lock(owner, connection_id):
            await self._connection(owner, connection_id)
            await self._change(owner, lambda rows: rows.pop(connection_id, None))
        return {'status': 'disconnected', 'connection_id': connection_id}

    async def catalog(self, owner):
        listing = await self.listing(owner)
        available = any(item['tool_count'] for item in listing['connections'])
        tools = [
            {'name': 'mcp_list_tools', 'description': '分页发现本人已连接钉钉服务的真实工具和参数；可按连接或关键词筛选，依 next_cursor 继续读取。',
             'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': {
                 'connection_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
                 'query': {'type': 'string', 'maxLength': 100}, 'cursor': {'type': 'integer', 'minimum': 0},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}},
            {'name': 'mcp_call_tool', 'description': '调用 mcp_list_tools 返回的本人钉钉工具，参数必须遵守其 schema。非只读操作会先展示确认卡，用户确认后才执行。',
             'input_schema': {'type': 'object', 'additionalProperties': False,
                 'required': ['connection_id', 'tool_name', 'arguments'], 'properties': {
                 'connection_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
                 'tool_name': {'type': 'string', 'maxLength': 128}, 'arguments': {'type': 'object'}}}},
        ]
        for tool in tools:
            tool.update(available=available, requires_confirmation=False, provider='dingtalk_aihub_mcp')
        return tools, listing

    async def list_tools(self, owner, args):
        if not isinstance(args, dict) or set(args) - {'connection_id', 'query', 'cursor', 'limit'}:
            raise failure('dingtalk_mcp_input_invalid')
        connection_id, query = args.get('connection_id'), args.get('query', '')
        cursor, limit = args.get('cursor', 0), args.get('limit', 5)
        if (connection_id is not None and (not isinstance(connection_id, str) or not ID.fullmatch(connection_id))
                or not isinstance(query, str) or len(query) > 100
                or type(cursor) is not int or not 0 <= cursor <= MAX_CONNECTIONS * MAX_TOOLS
                or type(limit) is not int or not 1 <= limit <= 10):
            raise failure('dingtalk_mcp_input_invalid')
        _, value = await self._load(owner)
        if connection_id is not None and connection_id not in value['connections']:
            raise failure('dingtalk_mcp_connection_missing', 404)
        found = []
        for record in value['connections'].values():
            if connection_id and record['id'] != connection_id:
                continue
            for tool in record['tools']:
                if query.casefold() not in (record['name'] + ' ' + tool['name'] + ' ' + tool['description']).casefold():
                    continue
                found.append({'connection_id': record['id'], 'connection_name': record['name'],
                    'tool_name': tool['name'], 'description': tool['description'], 'input_schema': tool['input_schema'],
                    'requires_confirmation': not tool['read_only']})
        selected = []
        for tool in found[cursor:cursor + limit]:
            if len(packed(selected + [tool])) > 60 * 1024:
                break
            selected.append(tool)
        if not selected and cursor < len(found):
            raise failure('dingtalk_mcp_response_too_large', 413)
        end = cursor + len(selected)
        return {'tools': selected, 'next_cursor': end if end < len(found) else None, 'total': len(found),
                'untrusted_external_content': True}

    async def _resolve(self, owner, action):
        match = ACTION.fullmatch(action or '')
        if not match:
            raise failure('dingtalk_mcp_tool_unknown', 404)
        record = await self._connection(owner, match[1])
        tool = next((x for x in record['tools'] if x['key'] == match[2]), None)
        if tool is None:
            raise failure('dingtalk_mcp_tool_unknown', 404)
        return record, tool

    async def _call(self, record, tool, arguments):
        async with MCPClient(record['url'], self.transport) as client:
            await client.initialize()
            current = next((x for x in await client.tools() if x['name'] == tool['name']), None)
            if current is None or current['revision'] != tool['revision']:
                raise failure('dingtalk_mcp_tools_changed', 409)
            return await client.call(tool, arguments)

    async def invoke(self, owner, body):
        if not isinstance(body.arguments, dict):
            raise failure('dingtalk_mcp_input_invalid')
        if body.action == 'mcp_list_tools':
            return {'status': 'completed', 'result': await self.list_tools(owner, body.arguments)}
        if body.action != 'mcp_call_tool' or set(body.arguments) != {'connection_id', 'tool_name', 'arguments'}:
            raise failure('dingtalk_mcp_input_invalid')
        args = body.arguments
        if not isinstance(args['tool_name'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', args['tool_name']):
            raise failure('dingtalk_mcp_input_invalid')
        record = await self._connection(owner, args['connection_id'])
        tool = next((x for x in record['tools'] if x['name'] == args['tool_name']), None)
        if tool is None:
            raise failure('dingtalk_mcp_tool_unknown', 404)
        from types import SimpleNamespace
        body = SimpleNamespace(connector='dingtalk', action='mcp_' + record['id'] + '_' + tool['key'],
                               arguments=args['arguments'], request_id=body.request_id)
        if not isinstance(body.arguments, dict):
            raise failure('dingtalk_mcp_input_invalid')
        packed(body.arguments, MAX_ARGUMENTS)
        record, tool = await self._resolve(owner, body.action)
        async with self._lock(owner, record['id']):
            fresh = await self._connection(owner, record['id'])
            if fresh['revision'] != record['revision']:
                raise failure('dingtalk_mcp_tools_changed', 409)
            if tool['read_only']:
                return {'status': 'completed', 'result': await self._call(record, tool, body.arguments)}
            # One request ID can publish at most one consumable confirmation.
            request_key = str(body.request_id)
            request_hash = checksum([body.connector, body.action, body.arguments, record['revision'], tool['revision']])
            raw = await self.store.get('request', owner, request_key)
            if raw:
                saved = self.svc.unseal(owner, raw)
                if saved['request_hash'] != request_hash:
                    raise failure('connector_request_immutable', 409)
                if not await self.store.get('pending', owner, saved['confirmation_id']):
                    raise failure('connector_confirmation_consumed_or_expired', 409)
                return saved['public']
            ticket = secrets.token_urlsafe(32)
            public = {'status': 'requires_confirmation', 'confirmation_id': ticket, 'expires_at': int(time.time()) + 300,
                'preview': {'action': body.action, 'summary': record['name'] + ' · ' + tool['description'],
                    'arguments': redact(body.arguments, record['url']), 'recipients': [],
                    'connection': self.public(record), 'provider': 'dingtalk_aihub_mcp'}}
            pending = {'provider': 'dingtalk_aihub_mcp', 'action': body.action, 'arguments': body.arguments,
                'connection_id': record['id'], 'connection_revision': record['revision'], 'tool_revision': tool['revision'],
                'expires_at': public['expires_at'], 'request_id': request_key}
            if not await self.store.put('request', owner, request_key, self.svc.seal(owner, {
                    'request_hash': request_hash, 'confirmation_id': ticket, 'public': public}), 86400, nx=True):
                raise failure('connector_request_in_progress', 409)
            await self.store.put('pending', owner, ticket, self.svc.seal(owner, pending), 300)
            return public

    async def confirm(self, owner, ticket, raw, pending):
        async with self._lock(owner, pending['connection_id']):
            record, tool = await self._resolve(owner, pending['action'])
            if (pending['expires_at'] <= time.time() or pending['connection_revision'] != record['revision']
                    or pending['tool_revision'] != tool['revision'] or tool['read_only']):
                raise failure('connector_confirmation_scope_changed', 403)
            if await self.store.pop('pending', owner, ticket) != raw:
                raise failure('connector_confirmation_consumed_or_expired', 409)
            try:
                result = await self._call(record, tool, pending['arguments'])
            except asyncio.CancelledError:
                # An HTTP disconnect/overall timeout does not prove that the
                # provider cancelled the write. The ticket remains consumed.
                raise failure('dingtalk_mcp_write_result_unknown', 502) from None
            except Exception as exc:
                from .enterprise_connectors import ConnectorFailure
                if isinstance(exc, ConnectorFailure) and exc.status < 500:
                    raise
                raise failure('dingtalk_mcp_write_result_unknown', 502) from None
            return {'status': 'completed', 'result': result}


async def management_body(request, fields):
    """Do not let FastAPI's validation response echo a credential-bearing input."""
    if (request.query_params or request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'):
        raise failure('dingtalk_mcp_input_invalid')
    payload = bytearray()
    async for part in request.stream():
        if len(payload) + len(part) > 12288:
            raise failure('dingtalk_mcp_input_invalid')
        payload.extend(part)
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise failure('dingtalk_mcp_input_invalid') from None
    if not isinstance(value, dict) or set(value) != set(fields) or any(not isinstance(v, str) for v in value.values()):
        raise failure('dingtalk_mcp_input_invalid')
    return value
