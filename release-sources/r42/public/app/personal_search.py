"""Fixed personal TokenKey Messages search. No credentials leave this gateway.

Candidate router is intentionally separate from chat/completions. Integrators
must add only INTERNAL_PATHS to the existing IAM internal-route allowlist.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import personal_cloud as cloud
from . import personal_model_proxy as models
from .iam_oauth import IAMFailure

PREFIX = '/internal/personal-cloud/search/'
INTERNAL_PATHS = frozenset(PREFIX + name for name in ('messages', 'bing'))
SID = re.compile(r'(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', re.I)
TICKET = re.compile(r'mst_[A-Za-z0-9_-]{43}')
QUERY_PREFIX = 'Perform a web search for the query: '
HEADERS = {'Cache-Control': 'no-store'}
MAX_RESPONSE = 512 * 1024
# Search model confirmed by the operator. The old name is accepted only on the
# private worker-to-gateway wire; it is never a permitted upstream alternative.
SEARCH_MODEL = 'deepseek-v4.1-flash'
LEGACY_SEARCH_MODEL = 'deepseek-flash-v4.1'
SEARCH_INPUT_MODELS = frozenset((SEARCH_MODEL, LEGACY_SEARCH_MODEL))


class SearchFailure(Exception):
    def __init__(self, code, status=502, upstream_status=0):
        self.code, self.status, self.upstream_status = code, status, upstream_status
        super().__init__(code)


def fail(code, status=502, upstream_status=0):
    raise SearchFailure(code, status, upstream_status)


def fields(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def validate_search(body):
    if (not fields(body, ('model', 'max_tokens', 'messages', 'tools'))
            or not isinstance(body['model'], str) or body['model'] not in SEARCH_INPUT_MODELS
            or type(body['max_tokens']) is not int or body['max_tokens'] != 2048
            or not isinstance(body['messages'], list) or len(body['messages']) != 1
            or not fields(body['messages'][0], ('role', 'content')) or body['messages'][0]['role'] != 'user'
            or not isinstance(body['messages'][0]['content'], list) or len(body['messages'][0]['content']) != 1
            or not fields(body['messages'][0]['content'][0], ('type', 'text'))
            or body['messages'][0]['content'][0]['type'] != 'text'
            or not isinstance(body['messages'][0]['content'][0]['text'], str)
            or not body['messages'][0]['content'][0]['text'].startswith(QUERY_PREFIX)
            or body['tools'] != [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 1}]
            or type(body['tools'][0]['max_uses']) is not int):
        fail('SEARCH_REQUEST_INVALID', 400)
    query = body['messages'][0]['content'][0]['text'][len(QUERY_PREFIX):]
    if not query.strip() or len(query) > 400:
        fail('SEARCH_REQUEST_INVALID', 400)
    return query


def public_url(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 2048 or any(ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or '\\' in value or parsed.hostname.lower() == 'localhost'
                or parsed.hostname.lower().endswith(('.local', '.internal', '.localhost'))):
            return False
        try:
            return ipaddress.ip_address(parsed.hostname).is_global
        except ValueError:
            return '.' in parsed.hostname and re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname) is not None
    except ValueError:
        return False


FAILURE_BODY_LIMIT = 8192
FAILURE_BODY_SECONDS = 1.0
FAILURE_TYPES = frozenset((
    'invalid_request_error', 'authentication_error', 'permission_error',
    'not_found_error', 'rate_limit_error', 'api_error', 'overloaded_error',
    'timeout_error', 'internal_server_error', 'server_error', 'upstream_error',
    'unsupported_error',
))
FAILURE_CODES = frozenset((
    'bad_gateway', 'internal_server_error', 'upstream_error', 'upstream_timeout',
    'timeout', 'rate_limit_exceeded', 'insufficient_quota', 'model_not_found',
    'model_not_supported', 'unsupported_model', 'unsupported_feature',
    'invalid_request', 'invalid_api_key', 'authentication_error',
    'permission_denied', 'service_unavailable', 'overloaded_error',
    'no_available_channel', 'channel_unavailable', 'invalid_response',
))


def failure_enum(value, allowed):
    if value is None:
        return 'ABSENT'
    if isinstance(value, str) and value in allowed:
        return value.upper()
    return 'OTHER'


async def observe_http_failure(response, headers_started):
    """Inspect this already-failed response only; never retry or expose text."""
    headers_ms = max(0, min(600000, round((time.monotonic()-headers_started)*1000)))
    media = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media == 'application/json' or re.fullmatch(r'application/[a-z0-9.-]+\+json', media):
        kind = 'JSON'
    elif media == 'text/html':
        kind = 'HTML'
    elif not media:
        kind = 'MISSING'
    else:
        kind = 'OTHER'
    state, error_type, error_code = 'READ_ERROR', 'ABSENT', 'ABSENT'
    raw = bytearray()
    try:
        # This budget is part of the existing search request, not another call.
        async with asyncio.timeout(FAILURE_BODY_SECONDS):
            async for chunk in response.aiter_bytes(chunk_size=1024):
                remaining = FAILURE_BODY_LIMIT-len(raw)
                if len(chunk) >= remaining:
                    raw.extend(chunk[:remaining])
                    # Reaching the cap is conservatively incomplete, even if
                    # the entire body happens to be exactly this many bytes.
                    state = 'BYTE_LIMIT'
                    break
                raw.extend(chunk)
            else:
                if not raw:
                    state = 'EMPTY'
                elif kind == 'HTML':
                    state = 'HTML'
                else:
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        state = 'INVALID_JSON' if kind == 'JSON' else 'NON_JSON'
                    else:
                        state = 'JSON_OBJECT' if isinstance(value, dict) else 'JSON_OTHER'
                        if isinstance(value, dict) and isinstance(value.get('error'), dict):
                            error_type = failure_enum(value['error'].get('type'), FAILURE_TYPES)
                            error_code = failure_enum(value['error'].get('code'), FAILURE_CODES)
    except (TimeoutError, httpx.TimeoutException):
        state = 'TIMEOUT'
    except httpx.HTTPError:
        state = 'READ_ERROR'
    except Exception:
        # Diagnostic failure must preserve the already-received HTTP status.
        state = 'READ_ERROR'
    # CancelledError deliberately propagates so scope revocation stays terminal.
    status = response.status_code if type(response.status_code) is int and 100 <= response.status_code <= 599 else 0
    logging.getLogger('megmeet.managed-search').warning(
        'MEGMEET_OFFICIAL_SEARCH_FAILURE endpoint_kind=messages upstream_status=%s headers_ms=%s '
        'content_type=%s body_state=%s error_type=%s error_code=%s',
        status, headers_ms, kind, state, error_type, error_code)


SEARCH_DETAIL_REASONS = frozenset((
    'NATIVE_RESULT', 'NATIVE_PARTIAL_RESULT', 'NATIVE_EMPTY', 'NO_NATIVE_RESULT_BLOCK',
    'SOURCE_URL_FILTERED', 'SOURCE_PAYLOAD_MISSING', 'TOOL_LIMIT', 'TOOL_RATE_LIMIT', 'TOOL_UNAVAILABLE',
    'TOOL_INPUT_INVALID', 'TOOL_UNKNOWN_ERROR', 'ENVELOPE_INVALID', 'BLOCK_INVALID',
    'RESULT_LIST_INVALID', 'RESULT_ITEM_INVALID', 'CITATIONS_INVALID',
    'JSON_INVALID', 'RESPONSE_TOO_LARGE',
))
SEARCH_TOOL_ERRORS = {
    'max_uses_exceeded': ('TOOL_LIMIT', 'SEARCH_NO_RESULT', 502),
    'too_many_requests': ('TOOL_RATE_LIMIT', 'SEARCH_BUSY', 429),
    'unavailable': ('TOOL_UNAVAILABLE', 'SEARCH_UPSTREAM_UNAVAILABLE', 502),
    'invalid_tool_input': ('TOOL_INPUT_INVALID', 'SEARCH_RESPONSE_INVALID', 502),
    'query_too_long': ('TOOL_INPUT_INVALID', 'SEARCH_RESPONSE_INVALID', 502),
    'request_too_large': ('TOOL_INPUT_INVALID', 'SEARCH_RESPONSE_INVALID', 502),
}

NATIVE_CODES = frozenset((
    'NONE', 'SEARCH_REQUEST_INVALID', 'SEARCH_AUTH_REQUIRED', 'SEARCH_SCOPE_REVOKED',
    'SEARCH_MODEL_UNAVAILABLE_FOR_ACCOUNT', 'SEARCH_UPSTREAM_UNSUPPORTED',
    'SEARCH_UPSTREAM_UNAVAILABLE', 'SEARCH_RESPONSE_INVALID', 'SEARCH_TIMEOUT',
    'SEARCH_BUSY', 'SEARCH_NO_RESULT', 'SEARCH_UNAVAILABLE', 'SEARCH_BING_FAILED',
))


def native_observation(code, upstream_status=0):
    # Request terminal observation also covers failures before provider dispatch.
    # Occurrences can overlap the upstream observation; these are not request IDs.
    safe = code if isinstance(code, str) and code in NATIVE_CODES else 'SEARCH_UNAVAILABLE'
    status = upstream_status if type(upstream_status) is int and 100 <= upstream_status <= 599 else 0
    logging.getLogger('megmeet.managed-search').warning(
        'MEGMEET_OFFICIAL_SEARCH code=%s upstream_status=%s', safe, status)


def search_detail(reason, upstream_status=200):
    # A second fixed-enum marker refines failures without changing the existing
    # worker fallback contract or copying upstream error bodies into logs.
    reason = reason if reason in SEARCH_DETAIL_REASONS else 'ENVELOPE_INVALID'
    status = upstream_status if type(upstream_status) is int and 100 <= upstream_status <= 599 else 0
    logging.getLogger('megmeet.managed-search').warning(
        'MEGMEET_OFFICIAL_SEARCH_DETAIL reason=%s upstream_status=%s', reason, status)


def safe_response(value):
    """Project only native source/citation blocks, including partial success."""
    def invalid(reason):
        search_detail(reason)
        fail('SEARCH_RESPONSE_INVALID', 502, 200)

    if not isinstance(value, dict) or not isinstance(value.get('content'), list) or len(value['content']) > 128:
        invalid('ENVELOPE_INVALID')
    blocks, tool_errors = [], []
    native_blocks = accepted = filtered = missing_payloads = 0
    for block in value['content']:
        if not isinstance(block, dict):
            invalid('BLOCK_INVALID')
        if block.get('type') == 'web_search_tool_result':
            native_blocks += 1
            content = block.get('content')
            # The pinned official provider treats absent/null payloads as empty.
            # This is compatibility, never evidence that any source was found.
            if content is None:
                missing_payloads += 1
                blocks.append({'type': 'web_search_tool_result', 'content': []})
                continue
            if isinstance(content, dict) and content.get('type') == 'web_search_tool_result_error':
                code = content.get('error_code')
                if not isinstance(code, str) or code not in SEARCH_TOOL_ERRORS:
                    invalid('TOOL_UNKNOWN_ERROR')
                tool_errors.append(SEARCH_TOOL_ERRORS[code])
                continue
            if not isinstance(content, list) or len(content) > 100:
                invalid('RESULT_LIST_INVALID')
            items = []
            for item in content:
                if not isinstance(item, dict):
                    invalid('RESULT_ITEM_INVALID')
                if item.get('type') != 'web_search_result':
                    continue
                if not public_url(item.get('url')):
                    filtered += 1
                    continue
                saved = {'type': 'web_search_result', 'url': item['url']}
                for key, limit in (('title', 300), ('page_age', 100)):
                    if isinstance(item.get(key), str):
                        saved[key] = item[key][:limit]
                items.append(saved)
                accepted += 1
            blocks.append({'type': 'web_search_tool_result', 'content': items})
        elif block.get('type') == 'text':
            citations = []
            # The official provider accepts absent/null citations as no quotes.
            raw_citations = block.get('citations')
            if raw_citations is None:
                raw_citations = []
            if not isinstance(raw_citations, list) or len(raw_citations) > 100:
                invalid('CITATIONS_INVALID')
            for cite in raw_citations:
                if isinstance(cite, dict) and public_url(cite.get('url')) and isinstance(cite.get('cited_text'), str):
                    citations.append({'url': cite['url'], 'cited_text': cite['cited_text'][:2000]})
            blocks.append({'type': 'text', 'text': '', 'citations': citations})
    warnings = {entry[0] for entry in tool_errors}
    if missing_payloads:
        warnings.add('SOURCE_PAYLOAD_MISSING')
    for reason in sorted(warnings):
        search_detail(reason)
    if tool_errors and not accepted:
        # Keep the existing closed worker/connector error vocabulary.
        _, code, status = tool_errors[0]
        fail(code, status, 200)
    if accepted:
        reason = 'NATIVE_PARTIAL_RESULT' if warnings else 'NATIVE_RESULT'
    elif missing_payloads:
        reason = 'SOURCE_PAYLOAD_MISSING'
    elif not native_blocks:
        reason = 'NO_NATIVE_RESULT_BLOCK'
    else:
        reason = 'SOURCE_URL_FILTERED' if filtered else 'NATIVE_EMPTY'
    if reason not in warnings:
        search_detail(reason)
    usage = value.get('usage', {})
    result = {'content': blocks, 'usage': {key: usage[key] for key in ('input_tokens', 'output_tokens')
        if isinstance(usage, dict) and type(usage.get(key)) is int and 0 <= usage[key] <= 10**9}}
    if warnings:
        result.update(search_partial=bool(accepted), search_warnings=sorted(warnings))
    return result



@dataclass(frozen=True)
class Access:
    actor: object
    lease: str
    session: str
    invocation: str
    host_digest: str
    permit: dict


class WorkerAuthority:
    def __init__(self, *, lease_actor=cloud.lease_actor, manager=cloud.manager, transport=None):
        self.lease_actor, self.manager, self.transport = lease_actor, manager, transport

    async def inspect(self, lease, session, invocation):
        # Availability of the authority is not a search-provider failure. Never
        # permit a fallback when the task/lease can no longer be proved.
        try:
            return await self._inspect(lease, session, invocation)
        except (SearchFailure, asyncio.CancelledError):
            raise
        except Exception:
            fail('SEARCH_SCOPE_REVOKED', 403)

    async def _inspect(self, lease, session, invocation):
        if not SID.fullmatch(session or '') or not TICKET.fullmatch(invocation or ''):
            fail('SEARCH_SCOPE_REVOKED', 403)
        actor = await self.lease_actor(lease)
        host = await self.manager('GET', '/v1/hosts/' + cloud.owner_key(actor))
        if (not re.fullmatch(r'http://[a-z0-9-]+:3084', host.get('origin', ''))
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', host.get('route_token', ''))):
            fail('SEARCH_SCOPE_REVOKED', 403)
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False, transport=self.transport) as client:
            async with client.stream('POST', host['origin'] + '/__cloud/search/context', headers={
                    'Authorization': 'Bearer ' + host['route_token'], 'X-Megmeet-Client-Lease': lease},
                    json={'native_session_id': session, 'invocation_id': invocation}) as response:
                if response.status_code != 200:
                    fail('SEARCH_SCOPE_REVOKED', 403)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8192:
                        fail('SEARCH_SCOPE_REVOKED', 403)
                permit = json.loads(raw)
        # R42 makes native web tools available to every authenticated task.
        # Accept the new versioned capability authority, plus the frozen old
        # explicit-selection authority during a rolling worker transition.
        # Never accept mixed schemas or synthesize either authority here.
        common = ('native_session_id', 'agent_session_id', 'invocation_id', 'generation', 'deadline', 'purpose')
        selected = (fields(permit, (*common, 'selected', 'selection_revision'))
                    and permit['selected'] == ['web-search']
                    and type(permit['selection_revision']) is int and permit['selection_revision'] >= 0)
        native = (fields(permit, (*common, 'capabilities', 'policy'))
                  and permit['capabilities'] == ['web-search'] and permit['policy'] == 'signed-in-native-web-v1')
        if (not (selected or native)
                or permit['native_session_id'] != session or permit['invocation_id'] != invocation
                or not SID.fullmatch(permit['agent_session_id'] or '')
                or type(permit['generation']) is not int or permit['generation'] < 1
                or type(permit['deadline']) is not int or not time.time() < permit['deadline'] <= time.time() + 95
                or permit['purpose'] != 'web-search'):
            fail('SEARCH_SCOPE_REVOKED', 403)
        digest = hashlib.sha256((host['origin'] + ':' + host['route_token']).encode()).hexdigest()
        return Access(actor, lease, session, invocation, digest, permit)

    async def verify(self, access):
        current = await self.inspect(access.lease, access.session, access.invocation)
        if (current.actor != access.actor or current.host_digest != access.host_digest or current.permit != access.permit):
            fail('SEARCH_SCOPE_REVOKED', 403)


async def official_messages(actor, body, *, actor_validator, broker_factory=models.get_broker, transport=None):
    """One fixed native-search Messages call with the current employee's key.

    Both native WorkerAuthority and the authenticated connector path use this
    transport. The caller supplies its own unchanged revocation authority.
    No endpoint/model/header/credential override is accepted from tool input.
    """
    validate_search(body)
    # Normalize before authorization, leaving the caller's request unchanged.
    # A catalog entry for the old alias alone must not authorize the new model.
    upstream_body = deepcopy(body)
    upstream_body['model'] = SEARCH_MODEL
    broker = broker_factory()

    async def validate(current):
        verified = await actor_validator(current)
        if current != actor or verified != actor:
            fail('SEARCH_AUTH_REQUIRED', 401)
        return verified

    broker.actor_validator = validate
    _, credential = await broker.credential(actor)
    if SEARCH_MODEL not in {model.code for model in broker.catalog(credential)}:
        fail('SEARCH_MODEL_UNAVAILABLE_FOR_ACCOUNT', 503)
    await validate(actor)
    async with httpx.AsyncClient(timeout=httpx.Timeout(55, connect=8), verify=True, trust_env=False,
                                 follow_redirects=False, transport=transport) as client:
        headers_started = time.monotonic()
        async with client.stream('POST', broker.base_url + '/messages', json=upstream_body, headers={
                'Authorization': 'Bearer ' + credential.api_key, 'x-api-key': credential.api_key,
                'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}) as response:
            async def rejected(code, status=502):
                logging.getLogger('megmeet.managed-search').warning(
                    'MEGMEET_OFFICIAL_SEARCH code=%s upstream_status=%s', code, response.status_code)
                await observe_http_failure(response, headers_started)
                fail(code, status, response.status_code)
            if response.status_code in (401, 403): await rejected('SEARCH_AUTH_REQUIRED', 401)
            if response.status_code in (400, 404, 405, 422, 501): await rejected('SEARCH_UPSTREAM_UNSUPPORTED')
            if response.status_code == 429: await rejected('SEARCH_BUSY', 429)
            if response.status_code != 200: await rejected('SEARCH_UPSTREAM_UNAVAILABLE')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    search_detail('RESPONSE_TOO_LARGE')
                    fail('SEARCH_RESPONSE_INVALID', 502, 200)
            if time.time() >= credential.expires_at: fail('SEARCH_AUTH_REQUIRED', 401)
            await validate(actor)
            try:
                decoded = json.loads(raw)
            except (ValueError, UnicodeError):
                search_detail('JSON_INVALID')
                fail('SEARCH_RESPONSE_INVALID', 502, 200)
            projected = safe_response(decoded)
            has_sources = any(block.get('type') == 'web_search_tool_result' and block.get('content')
                              for block in projected['content'])
            logging.getLogger('megmeet.managed-search').warning(
                'MEGMEET_OFFICIAL_SEARCH code=%s upstream_status=200',
                'NONE' if has_sources else 'SEARCH_NO_RESULT')
            return projected


class PersonalSearch:
    def __init__(self, *, authority=None, broker_factory=models.get_broker, budget=None, bing=None,
                 transport=None, parallel=4, total_timeout=65):
        self.authority = authority or WorkerAuthority()
        self.broker_factory, self.budget, self.bing, self.transport = broker_factory, budget, bing, transport
        self.gate, self.total_timeout = asyncio.Semaphore(parallel), total_timeout

    async def guarded(self, access, operation):
        async def watch():
            while True:
                await asyncio.sleep(0.5)
                await self.authority.verify(access)
        work, guard = asyncio.create_task(operation()), asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait((work, guard), return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                await guard
                fail('SEARCH_SCOPE_REVOKED', 403)
            result = await work
            await self.authority.verify(access)
            return result
        finally:
            work.cancel(); guard.cancel()
            await asyncio.gather(work, guard, return_exceptions=True)

    async def dispatch(self, lease, envelope, operation):
        if operation not in ('messages', 'bing') or not fields(envelope, ('native_session_id', 'invocation_id', 'body')):
            fail('SEARCH_REQUEST_INVALID', 400)
        body = envelope['body']
        if operation == 'messages':
            validate_search(body)
        elif (not fields(body, ('query', 'count')) or not isinstance(body['query'], str) or not body['query'].strip()
              or len(body['query']) > 400 or type(body['count']) is not int or not 1 <= body['count'] <= 10):
            fail('SEARCH_REQUEST_INVALID', 400)
        acquired = False
        try:
            async with asyncio.timeout(self.total_timeout):
                access = await self.authority.inspect(lease, envelope['native_session_id'], envelope['invocation_id'])
                try:
                    await asyncio.wait_for(self.gate.acquire(), 1)
                    acquired = True
                except TimeoutError:
                    fail('SEARCH_BUSY', 429)
                await self.authority.verify(access)
                if self.budget:
                    await self.budget(access.actor.user_no)
                if operation == 'bing':
                    if not self.bing:
                        fail('SEARCH_BING_FAILED')
                    async def read_bing():
                        try:
                            value = await self.bing(body)
                        except (SearchFailure, asyncio.CancelledError):
                            raise
                        except Exception:
                            fail('SEARCH_BING_FAILED')
                        if not isinstance(value, dict) or not isinstance(value.get('sources'), list):
                            fail('SEARCH_BING_FAILED')
                        sources = []
                        for entry in value['sources'][:body['count']]:
                            if not isinstance(entry, dict) or not public_url(entry.get('url')):
                                continue
                            saved = {'url': entry['url']}
                            for key, limit in (('title', 300), ('snippet', 2000)):
                                if isinstance(entry.get(key), str): saved[key] = entry[key][:limit]
                            sources.append(saved)
                        return {'sources': sources, 'truncated': len(value['sources']) > body['count']}
                    return await self.guarded(access, read_bing)
                async def validate(actor):
                    await self.authority.verify(access)
                    if actor != access.actor: fail('SEARCH_AUTH_REQUIRED', 401)
                    return actor
                async def messages():
                    return await official_messages(access.actor, body, actor_validator=validate,
                        broker_factory=self.broker_factory, transport=self.transport)
                return await self.guarded(access, messages)
        except SearchFailure:
            raise
        except IAMFailure as exc:
            # Scope authority has its own fail-closed wrapper above. A valid
            # personal account's credential/catalog service can be unavailable;
            # this is distinct from a provider rejecting its authentication.
            if getattr(exc, 'status', 0) >= 500:
                fail('SEARCH_UPSTREAM_UNAVAILABLE', 503)
            fail('SEARCH_AUTH_REQUIRED', 401)
        except (TimeoutError, httpx.TimeoutException):
            fail('SEARCH_TIMEOUT', 504)
        except httpx.HTTPError:
            fail('SEARCH_UPSTREAM_UNAVAILABLE')
        except (ValueError, TypeError, KeyError, AttributeError):
            fail('SEARCH_RESPONSE_INVALID')
        finally:
            if acquired: self.gate.release()


router = APIRouter()
_service = None


def service():
    global _service
    if _service is None:
        from .enterprise_connectors import service as connectors
        from .connector_search_bing import search_bing
        _service = PersonalSearch(budget=connectors().store.budget, bing=search_bing)
    return _service


@router.post(PREFIX + '{operation}')
async def search(request: Request, operation: str):
    try:
        if request.url.path not in INTERNAL_PATHS:
            fail('SEARCH_REQUEST_INVALID', 404)
        header = request.headers.get('authorization', '')
        lease = header[7:] if header.startswith('Bearer ') else ''
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192: fail('SEARCH_REQUEST_INVALID', 413)
        result = await service().dispatch(lease, json.loads(raw), operation)
        if operation == 'messages':
            has_sources = any(block.get('type') == 'web_search_tool_result' and block.get('content')
                              for block in result.get('content', []))
            native_observation('NONE' if has_sources else 'SEARCH_NO_RESULT', 200)
        return JSONResponse(result, headers=HEADERS)
    except SearchFailure as exc:
        if operation == 'messages': native_observation(exc.code, exc.upstream_status)
        return JSONResponse({'code': exc.code, 'error': {'message': exc.code}}, status_code=exc.status, headers=HEADERS)
    except (ValueError, TypeError):
        if operation == 'messages': native_observation('SEARCH_REQUEST_INVALID')
        return JSONResponse({'code': 'SEARCH_REQUEST_INVALID'}, status_code=400, headers=HEADERS)
    except Exception:
        if operation == 'messages': native_observation('SEARCH_UNAVAILABLE')
        return JSONResponse({'code': 'SEARCH_UNAVAILABLE'}, status_code=503, headers=HEADERS)
