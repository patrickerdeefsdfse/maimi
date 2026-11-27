"""Managed search for authenticated connector callers; native authority is separate."""
from __future__ import annotations

import asyncio
import logging
import httpx

from .desktop_auth import TrustedActor, validate_trusted_actor
from .iam_oauth import IAMFailure
from . import personal_model_proxy as models
from .personal_search import SearchFailure, fail, official_messages, public_url, QUERY_PREFIX, SEARCH_MODEL

FALLBACK_CODES = frozenset((
    'SEARCH_MODEL_UNAVAILABLE_FOR_ACCOUNT', 'SEARCH_UPSTREAM_UNSUPPORTED',
    'SEARCH_UPSTREAM_UNAVAILABLE', 'SEARCH_RESPONSE_INVALID', 'SEARCH_TIMEOUT',
    'SEARCH_BUSY', 'SEARCH_NO_RESULT',
))
LOGGER = logging.getLogger('megmeet.managed-search')


def observation(provider, code='NONE', status=0):
    # Fixed enums only: no identities, queries, credentials or provider text.
    provider = provider if provider in ('deepseek-official', 'bing') else 'unknown'
    code = code if code in FALLBACK_CODES | {'NONE', 'SEARCH_AUTH_REQUIRED', 'SEARCH_SCOPE_REVOKED', 'SEARCH_BING_FAILED'} else 'SEARCH_FAILED'
    status = status if type(status) is int and 100 <= status <= 599 else 0
    LOGGER.warning('MEGMEET_MANAGED_SEARCH provider=%s code=%s upstream_status=%s', provider, code, status)


def official_sources(value, count):
    citations, items, seen = {}, [], set()
    for block in value['content']:
        if block['type'] == 'text':
            for cite in block.get('citations', []):
                citations.setdefault(cite['url'], []).append(cite['cited_text'])
    for block in value['content']:
        if block['type'] != 'web_search_tool_result':
            continue
        for entry in block['content']:
            if entry['url'] in seen:
                continue
            seen.add(entry['url'])
            items.append({'url': entry['url'], 'title': entry.get('title', ''),
                          'snippet': '\n'.join(citations.get(entry['url'], []))[:2000],
                          **({'page_age': entry['page_age']} if 'page_age' in entry else {})})
    if not items:
        fail('SEARCH_NO_RESULT')
    return items[:count], len(items) > count


class ManagedSearch:
    def __init__(self, *, broker_factory=models.get_broker, transport=None, bing=None,
                 parallel=4, official_timeout=60, total_timeout=85, watch_interval=0.5):
        self.broker_factory, self.transport, self.bing = broker_factory, transport, bing
        self.gate = asyncio.Semaphore(parallel)
        self.official_timeout, self.total_timeout, self.watch_interval = official_timeout, total_timeout, watch_interval

    async def run(self, actor, args, *, actor_validator=None):
        # The route supplies its revocable desktop grant or worker lease check.
        validator = actor_validator or validate_trusted_actor
        if not isinstance(actor, TrustedActor):
            fail('SEARCH_AUTH_REQUIRED', 401)
        if (not isinstance(args, dict) or set(args) - {'query', 'count', 'freshness'}
                or not isinstance(args.get('query'), str) or not args['query'].strip()
                or len(args['query']) > 400 or any(ord(c) < 32 for c in args['query'])
                or type(args.get('count')) is not int or not 1 <= args['count'] <= 10):
            fail('SEARCH_REQUEST_INVALID', 400)
        if args.get('freshness') is not None:
            fail('web_search_freshness_unsupported', 400)

        async def validate(current):
            try:
                if current != actor or await validator(current) != actor:
                    fail('SEARCH_SCOPE_REVOKED', 403)
                return actor
            except asyncio.CancelledError:
                raise
            except Exception:
                fail('SEARCH_SCOPE_REVOKED', 403)

        async def watch():
            while True:
                await asyncio.sleep(self.watch_interval)
                await validate(actor)

        async def operation():
            body = {'model': SEARCH_MODEL, 'max_tokens': 2048,
                    'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': QUERY_PREFIX + args['query']}]}],
                    'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 1}]}
            try:
                async with asyncio.timeout(self.official_timeout):
                    value = await official_messages(actor, body, actor_validator=validate,
                        broker_factory=self.broker_factory, transport=self.transport)
                    sources, truncated = official_sources(value, args['count'])
                observation('deepseek-official')
                partial = value.get('search_partial') is True
                return {'query': args['query'], 'sources': sources, 'truncated': truncated or partial,
                        'untrusted_external_content': True, 'search_provider': 'deepseek-official',
                        'fallback_reason': None, 'search_warnings': value.get('search_warnings', []),
                        'search_notice': '本次使用 DeepSeek 官方联网搜索；部分检索未完成，已保留成功取得的来源。' if partial else '本次使用 DeepSeek 官方联网搜索。'}
            except SearchFailure as exc:
                reason, status = exc.code, getattr(exc, 'upstream_status', 0)
                if reason not in FALLBACK_CODES:
                    observation('deepseek-official', reason, status)
                    raise
            except IAMFailure as exc:
                if getattr(exc, 'status', 0) < 500:
                    fail('SEARCH_AUTH_REQUIRED', 401)
                reason, status = 'SEARCH_UPSTREAM_UNAVAILABLE', 0
            except (TimeoutError, httpx.TimeoutException):
                reason, status = 'SEARCH_TIMEOUT', 0
            except httpx.HTTPError:
                reason, status = 'SEARCH_UPSTREAM_UNAVAILABLE', 0
            except (ValueError, TypeError, KeyError, AttributeError):
                reason, status = 'SEARCH_RESPONSE_INVALID', 0
            observation('deepseek-official', reason, status)
            await validate(actor)
            try:
                if not self.bing:
                    fail('SEARCH_BING_FAILED')
                value = await self.bing(dict(args))
                if not isinstance(value, dict) or not isinstance(value.get('sources'), list):
                    fail('SEARCH_BING_FAILED')
                sources, seen = [], set()
                for entry in value['sources']:
                    if not isinstance(entry, dict) or not public_url(entry.get('url')) or entry['url'] in seen:
                        continue
                    seen.add(entry['url'])
                    source = {'url': entry['url']}
                    for key, limit in (('title', 300), ('snippet', 2000)):
                        if isinstance(entry.get(key), str): source[key] = entry[key][:limit]
                    sources.append(source)
                if not sources:
                    fail('SEARCH_BING_FAILED')
                observation('bing')
                return {'query': args['query'], 'sources': sources[:args['count']],
                        'truncated': len(sources) > args['count'], 'untrusted_external_content': True,
                        'search_provider': 'bing', 'fallback_reason': reason,
                        'search_notice': 'DeepSeek 官方联网搜索暂不可用，本次结果来自 Bing。'}
            except asyncio.CancelledError:
                raise
            except Exception:
                observation('bing', 'SEARCH_BING_FAILED')
                fail('SEARCH_BING_FAILED')

        acquired = False
        work = guard = None
        try:
            async with asyncio.timeout(self.total_timeout):
                await validate(actor)
                try:
                    await asyncio.wait_for(self.gate.acquire(), 1)
                    acquired = True
                except TimeoutError:
                    fail('SEARCH_BUSY', 429)
                await validate(actor)
                work, guard = asyncio.create_task(operation()), asyncio.create_task(watch())
                done, _ = await asyncio.wait((work, guard), return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    await guard
                    fail('SEARCH_SCOPE_REVOKED', 403)
                result = await work
                await validate(actor)
                return result
        except TimeoutError:
            fail('SEARCH_TIMEOUT', 504)
        finally:
            tasks = [task for task in (work, guard) if task is not None]
            for task in tasks: task.cancel()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            if acquired: self.gate.release()
