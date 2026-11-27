"""One-use desktop-to-browser DingTalk OAuth bridge, not a second IAM login.

Only a digest of the existing desktop grant is retained. The ticket is put in
the URL fragment, cleared by the browser, and consumed with a same-origin POST.
The exact desktop grant and IAM session must survive issue, consume, callback
and the atomic provider-grant commit. No IAM browser session is minted here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.responses import HTMLResponse

from . import iam_oauth as iam
from .desktop_auth import TOKEN, TrustedActor

PATH = '/v1/connectors/dingtalk/desktop-handoff'
CALLBACK = '/v1/connectors/dingtalk/callback'
COOKIE = 'megmeet_dingtalk_desktop'
BUCKET = 'desktop-dingtalk-handoff'
PURPOSE = 'desktop-dingtalk-handoff-v1'
OPAQUE = re.compile(r'^[A-Za-z0-9_-]{43}$')
HEX = re.compile(r'^[a-f0-9]{64}$')
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff'}


def public_handoff_request(request):
    # The callback handler verifies either existing IAM auth or the narrowly
    # scoped bridge cookie. No other connector or POST callback becomes public.
    return (request.method, request.url.path) in {
        ('GET', PATH), ('POST', PATH + '/consume'), ('GET', CALLBACK)}


def failure(code='dingtalk_desktop_handoff_expired', status=401):
    from .enterprise_connectors import ConnectorFailure
    return ConnectorFailure(status, code)


def source_contract(source):
    if (not isinstance(source, dict) or set(source) != {'grant_ref', 'sid', 'user_no', 'account_no'}
            or not isinstance(source['grant_ref'], str) or not HEX.fullmatch(source['grant_ref'])
            or any(not isinstance(source[k], str) or not 1 <= len(source[k]) <= 256
                   for k in ('sid', 'user_no', 'account_no'))):
        raise failure()


async def validate_source(source):
    source_contract(source)
    async def run(service):
        redis = getattr(service.store, 'redis', None)
        if redis is None:
            raise failure('connector_storage_unavailable', 503)
        raw = await redis.get('megmeet:desktop:grant:' + source['grant_ref'])
        record = json.loads(raw) if raw else None
        if (not isinstance(record, dict) or record.get('sid') != source['sid']
                or type(record.get('expires_at')) not in {int, float} or record['expires_at'] <= time.time()):
            raise failure()
        user = await service.authenticate(source['sid'])
        if not user or user.get('user_no') != source['user_no'] or user.get('account_no') != source['account_no']:
            raise failure()
        return TrustedActor(user['user_no'], user['account_no'], user.get('display_name', ''),
                            tuple(user.get('organization_codes', ())), source['sid'])
    return await iam._with_service(run)


async def commit_grant(connector, source, owner, sealed, ttl):
    # Revalidate account/organization first, then bind the final Redis SET to
    # both still-existing source grants atomically. Logout cannot resurrect it.
    actor = await validate_source(source)
    if actor.user_no != owner:
        raise failure()
    redis = getattr(connector.store, 'redis', None)
    if redis is None:
        raise failure('connector_storage_unavailable', 503)
    script = """local raw=redis.call('GET',KEYS[1]);if not raw then return 0 end
local ok,d=pcall(cjson.decode,raw);local now=tonumber(redis.call('TIME')[1])
if not ok or type(d)~='table' or d.sid~=ARGV[1]
or type(d.expires_at)~='number' or d.expires_at<=now then return 0 end
local iam=redis.call('GET',KEYS[2]);if not iam or redis.call('PTTL',KEYS[2])<=0 then return 0 end
local good,s=pcall(cjson.decode,iam)
if not good or type(s)~='table' or s.user_no~=ARGV[4] or s.account_no~=ARGV[5]
or (s.expires_at~=nil and (type(s.expires_at)~='number' or s.expires_at<=now)) then return 0 end
redis.call('SET',KEYS[3],ARGV[2],'EX',ARGV[3]);return 1"""
    accepted = await redis.eval(script, 3, 'megmeet:desktop:grant:' + source['grant_ref'],
        'megmeet:iam:session:' + iam._digest(source['sid']), connector.store.key('grant', owner, 'dingtalk'),
        source['sid'], sealed, ttl, source['user_no'], source['account_no'])
    if not accepted:
        raise failure()


class DingTalkHandoff:
    def __init__(self, connector, *, validate=validate_source, commit=commit_grant, clock=time.time):
        self.connector, self.validate, self.committer, self.clock = connector, validate, commit, clock

    async def issue(self, actor, token):
        if not isinstance(actor, TrustedActor) or not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise failure('dingtalk_desktop_session_required')
        c = self.connector
        c.config.require_dingtalk()
        c.config.binding(actor.user_no)
        source = {'grant_ref': hashlib.sha256(token.encode('ascii')).hexdigest(), 'sid': actor.auth_session_id,
                  'user_no': actor.user_no, 'account_no': actor.account_no}
        await self.validate(source)
        await c.store.budget(actor.user_no)
        ticket = secrets.token_urlsafe(32)
        saved = {'purpose': PURPOSE, 'source': source, 'expires': self.clock() + 60,
                 'config_hash': c.binding_hash(actor.user_no)}
        if not await c.store.put('ticket', BUCKET, ticket, c.seal(BUCKET, saved), 60, nx=True):
            raise failure('connector_storage_unavailable', 503)
        return {'connector': 'dingtalk', 'authorization_url': c.config.public_origin + PATH + '#' + ticket,
                'expires_in': 60, 'handoff': True}

    async def _validate_record(self, saved):
        if (not isinstance(saved, dict) or saved.get('purpose') != PURPOSE
                or type(saved.get('expires')) not in {int, float} or saved['expires'] <= self.clock()):
            raise failure()
        source_contract(saved.get('source'))
        actor = await self.validate(saved['source'])
        self.connector.config.require_dingtalk()
        if (not isinstance(actor, TrustedActor) or actor.user_no != saved['source']['user_no']
                or actor.account_no != saved['source']['account_no']
                or actor.auth_session_id != saved['source']['sid']
                or saved.get('config_hash') != self.connector.binding_hash(actor.user_no)):
            raise failure()
        return actor

    async def consume(self, ticket):
        if not isinstance(ticket, str) or not OPAQUE.fullmatch(ticket):
            raise failure()
        c = self.connector
        raw = await c.store.pop('ticket', BUCKET, ticket)  # Atomic GETDEL, exactly once.
        saved = c.unseal(BUCKET, raw) if raw else {}
        actor = await self._validate_record(saved)
        binding = secrets.token_urlsafe(32)
        url = await c.authorize(actor, binding)
        state = parse_qs(urlsplit(url).query)['state'][0]
        saved.update(expires=self.clock() + 300, state=state)
        if not await c.store.put('browser', BUCKET, binding, c.seal(BUCKET, saved), 300, nx=True):
            raise failure('connector_storage_unavailable', 503)
        return url, binding

    async def callback(self, marker, binding, state):
        if (not all(isinstance(v, str) and OPAQUE.fullmatch(v) for v in (marker, binding, state))
                or not hmac.compare_digest(marker, binding)):
            raise failure()
        raw = await self.connector.store.get('browser', BUCKET, binding)
        saved = self.connector.unseal(BUCKET, raw) if raw else {}
        if saved.get('state') != state:
            raise failure()
        return await self._validate_record(saved), saved

    async def commit(self, saved, owner, sealed, ttl):
        actor = await self._validate_record(saved)
        if actor.user_no != owner:
            raise failure()
        await self.committer(self.connector, saved['source'], owner, sealed, ttl)

    async def clear(self, binding):
        if isinstance(binding, str) and OPAQUE.fullmatch(binding):
            await self.connector.store.delete('browser', BUCKET, binding)


BROWSER_SCRIPT = """(async()=>{const status=document.getElementById('status');const ticket=location.hash.slice(1);
history.replaceState(null,'',location.pathname);
try{if(!/^[A-Za-z0-9_-]{43}$/.test(ticket)||location.search)throw Error();
const response=await fetch('/v1/connectors/dingtalk/desktop-handoff/consume',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket})});
if(!response.ok)throw Error();const data=await response.json();const url=new URL(data.authorization_url);
if(url.origin!=='https://login.dingtalk.com'||url.pathname!=='/oauth2/auth'||url.username||url.password||url.hash)throw Error();
location.replace(url.href);}catch{status.textContent='连接链接已过期、已使用或桌面登录已结束。请返回麦米桌面重新点击连接钉钉。';}})();"""


def browser_page():
    digest = base64.b64encode(hashlib.sha256(BROWSER_SCRIPT.encode()).digest()).decode()
    body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>连接钉钉</title><h1>连接钉钉</h1><p id="status">正在连接，请稍候…</p>'
            '<noscript>请允许本页运行 JavaScript，再从麦米桌面重新连接。</noscript><script>' + BROWSER_SCRIPT + '</script></html>')
    return HTMLResponse(body, headers={**HEADERS, 'Content-Security-Policy':
        "default-src 'none'; script-src 'sha256-" + digest + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
