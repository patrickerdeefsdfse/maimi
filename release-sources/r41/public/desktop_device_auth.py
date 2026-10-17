"""Revocable remembered desktop device; no IAM refresh grant is assumed.

Resume credentials are independent of application access Bearers. The client
durably saves its next random resume token before rotation. Redis retains only
token/proof hashes and an encrypted response usable for a 120-second retry
window. That last encrypted response remains in the device record until the
next rotation or device revocation, but cannot issue access past its own expiry.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import iam_oauth as iam
from . import desktop_auth as desktop

DEVICE_TTL = 0
from . import persistent_login as persistent
RETRY_SECONDS = 120
RESUME = re.compile(r'^mdr_[A-Za-z0-9_-]{43}$')


def fail(code, status=401):
    raise iam.IAMFailure(status, code)


def device_key(device_id):
    return 'megmeet:desktop:device:' + desktop._hash(device_id)


def cancellation_key(device_id):
    return 'megmeet:desktop:device-cancel:' + desktop._hash(device_id)


def _proof(body):
    desktop._instance(body.device_id)
    desktop._instance(body.instance_id)
    if not isinstance(body.resume_token, str) or not RESUME.fullmatch(body.resume_token):
        fail('desktop_device_expired')
    return desktop.proof_challenge(body.code_verifier)


async def action_payload(request, allow_empty=False):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            fail('desktop_request_invalid', 400)
    if allow_empty and not raw:
        return {}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        fail('desktop_request_invalid', 400)


def action_body(value, kind, model):
    copied = dict(value)
    if kind is not None and copied.pop('kind', None) != kind:
        fail('desktop_request_invalid', 400)
    try:
        return model.model_validate(copied, strict=True)
    except Exception:
        # A validation response must not echo the submitted resume credential.
        fail('desktop_request_invalid', 400)


def _redis(service):
    redis = getattr(service.store, 'redis', None)
    if redis is None or service.config.identity_mode != 'sso_tokenkey':
        fail('desktop_remembered_login_unavailable', 503)
    return redis


def _json(raw):
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        fail('desktop_device_store_unavailable', 503)


def _bind(record, body, challenge):
    if (record.get('instance') != body.instance_id
            or not isinstance(record.get('challenge'), str)
            or not hmac.compare_digest(record['challenge'], challenge)):
        fail('desktop_device_proof_denied', 403)


async def _session(service, sid):
    user = await service.authenticate(sid)
    if user is None:
        fail('desktop_device_expired')
    raw = await _redis(service).get('megmeet:iam:session:' + iam._digest(sid))
    if raw is None:
        fail('desktop_device_expired')
    record = _json(raw)
    if (record.get('version') not in (iam.REMEMBERED_SESSION_VERSION,persistent.VERSION)
            or record.get('login_policy') != 'device-r40'
            or type(record.get('expires_at')) is not int
            or not persistent.expiry_valid(record)
            or record.get('account_no') != user['account_no']
            or record.get('user_no') != user['user_no']):
        fail('desktop_device_expired')
    await service._check_account_revocation(record)
    return user, raw, record


def _seal(sid, body, response, expires_at):
    # Fernet authenticates the whole payload, including the application binding.
    value = {'sid': iam._digest(sid), 'device': body.device_id,
        'instance': body.instance_id, 'request': body.request_id,
        'expires_at': expires_at, 'response': response}
    return iam._token_cipher().encrypt(json.dumps(value, separators=(',', ':')).encode()).decode('ascii')


def _reply(ciphertext, sid, body):
    try:
        value = json.loads(iam._token_cipher().decrypt(ciphertext.encode('ascii')))
        if (set(value) != {'sid', 'device', 'instance', 'request', 'expires_at', 'response'}
                or value['sid'] != iam._digest(sid) or value['device'] != body.device_id
                or value['instance'] != body.instance_id or value['request'] != body.request_id
                or type(value['expires_at']) is not int or value['expires_at'] <= time.time()):
            raise ValueError()
        response = value['response']
        if not isinstance(response, dict) or not desktop.TOKEN.fullmatch(response.get('access_token', '')):
            raise ValueError()
        response['expires_in'] = max(0, value['expires_at'] - int(time.time()))
        return JSONResponse(response, headers=desktop.HEADERS)
    except iam.IAMFailure:
        raise
    except Exception:
        fail('desktop_device_store_unavailable', 503)


def _response(user, sid, body, expires_at, device_expires):
    token = 'mdt_' + secrets.token_urlsafe(32)
    response = {'access_token': token, 'token_type': 'Bearer',
        'expires_in': expires_at - int(time.time()), 'renewal_supported': True,
        'resume_supported': True, 'device_id': body.device_id,
        'device_expires_at': device_expires, 'user': desktop._actor(user, sid).public()}
    return token, _seal(sid, body, response, expires_at)


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    device_id: str = Field(max_length=36)
    instance_id: str = Field(max_length=36)
    code_verifier: str = Field(min_length=43, max_length=128)
    resume_token: str = Field(min_length=47, max_length=47)
    next_resume_token: str = Field(min_length=47, max_length=47)
    request_id: str = Field(max_length=36)


class DeviceLogoutBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    device_id: str = Field(max_length=36)
    instance_id: str = Field(max_length=36)
    code_verifier: str = Field(min_length=43, max_length=128)
    resume_token: str = Field(min_length=47, max_length=47)


async def capabilities(request: Request):
    desktop._check_mode(request)
    enabled = iam.IAMConfig.from_env().identity_mode == 'sso_tokenkey'
    return JSONResponse({'remembered_login': enabled, 'protocol': 1,
        'device_ttl_seconds': DEVICE_TTL, 'web_ttl_seconds': 0,
        'retry_window_seconds': RETRY_SECONDS}, headers=desktop.HEADERS)


async def exchange_remembered(service, body, challenge):
    if not all(isinstance(v, str) for v in (body.device_id,body.request_id,body.resume_token)):
        fail('desktop_request_invalid', 400)
    _proof(body)
    desktop._instance(body.request_id)
    redis, store = _redis(service), desktop.desktop_store(service)
    flow_key = store.key('flow', body.flow_id)
    cache_key = store.key('exchange-retry', body.flow_id)
    cached = await redis.get(cache_key)
    if cached is not None:
        cache = _json(cached)
        _bind(cache, body, challenge)
        if (cache.get('device') != body.device_id or cache.get('request') != body.request_id
                or cache.get('current') != desktop._hash(body.resume_token)):
            fail('desktop_resume_conflict', 409)
        sid = cache['sid']
        _, session_raw, session_record = await _session(service, sid)
        # The cached exchange cannot restore a revoked/rotated device or session.
        script = """local d=redis.call('GET',KEYS[2]);if not d then return false end
        local dev=cjson.decode(d)
        if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('GET',KEYS[3])~=ARGV[2]
          or (redis.call('GET',KEYS[4]) or '0')~=ARGV[3]
          or dev.current~=ARGV[4] or dev.sid~=ARGV[5] then return false end
        return ARGV[1]"""
        valid = await redis.eval(script, 4, cache_key, device_key(body.device_id),
            'megmeet:iam:session:'+iam._digest(sid), iam.account_revocation_key(session_record['account_no']),
            cached, session_raw, session_record['revocation_version'], desktop._hash(body.resume_token), sid)
        if not valid:
            fail('desktop_device_expired')
        return _reply(cache['response'], sid, body)
    raw = await redis.get(flow_key)
    if raw is None:
        fail('desktop_flow_unavailable', 410)
    flow = _json(raw)
    _bind(flow, body, challenge)
    if flow.get('remember_device') is not True:
        fail('desktop_device_exchange_required', 400)
    if flow.get('status') != 'ready':
        return JSONResponse({'status': 'pending'}, status_code=202, headers=desktop.HEADERS)
    sid = flow['sid']
    user, session_raw, session_record = await _session(service, sid)
    device_expires = session_record['expires_at']
    access_expires = min(int(time.time()) + desktop._lifetime(), await service._live_personal_deadline(sid, session_record))
    token, sealed = _response(user, sid, body, access_expires, device_expires)
    device = {'sid': sid, 'instance': body.instance_id, 'challenge': challenge,
        'current': desktop._hash(body.resume_token), 'expires_at': device_expires,
        'generation': 0, 'last_request': '', 'previous': '', 'retry_until': 0, 'response': ''}
    grant = {'sid': sid, 'instance': body.instance_id, 'renewal_challenge': challenge,
        'expires_at': access_expires}
    cache = {'sid': sid, 'instance': body.instance_id, 'challenge': challenge,
        'device': body.device_id, 'request': body.request_id, 'current': device['current'], 'response': sealed}
    script = """local now=tonumber(redis.call('TIME')[1]);local expires=tonumber(ARGV[6])
    if redis.call('EXISTS',KEYS[7])==1 or redis.call('GET',KEYS[2])~=ARGV[2]
      or (redis.call('GET',KEYS[6]) or '0')~=ARGV[3] or expires<=now
      or (redis.call('TTL',KEYS[2])<=0 and not (redis.call('TTL',KEYS[2])==-1 and cjson.decode(ARGV[2]).version==4 and cjson.decode(ARGV[2]).expires_at==0)) then return {0} end
    local cached=redis.call('GET',KEYS[5]);local actual=redis.call('GET',KEYS[3])
    if cached and actual then
      local c=cjson.decode(cached);local wanted=cjson.decode(ARGV[7]);local dev=cjson.decode(actual)
      if c.sid==wanted.sid and c.instance==wanted.instance and c.challenge==wanted.challenge
        and c.device==wanted.device and c.request==wanted.request and c.current==wanted.current
        and dev.sid==c.sid and dev.current==c.current then return {2,c.response} end
      return {-1}
    end
    if redis.call('GET',KEYS[1])~=ARGV[1] then return {0} end
    if redis.call('EXISTS',KEYS[3])==1 or redis.call('EXISTS',KEYS[4])==1 then return {-1} end
    local d=cjson.decode(ARGV[4]);local grant=cjson.decode(ARGV[5])
    local session=cjson.decode(ARGV[2]);local persistent=session.version==4 and session.expires_at==0
    local remaining=redis.call('TTL',KEYS[2]);local ttl=persistent and (remaining==-1 and 3600 or remaining) or math.min(d.expires_at-now,remaining)
    if ttl<=0 or grant.expires_at>now+ttl then return {0} end
    if persistent and remaining==-1 then redis.call('SET',KEYS[3],ARGV[4]) else redis.call('SET',KEYS[3],ARGV[4],'EX',ttl) end
    redis.call('SET',KEYS[4],ARGV[5],'EX',grant.expires_at-now)
    redis.call('SET',KEYS[5],ARGV[7],'EX',math.min(120,ttl))
    redis.call('DEL',KEYS[1]);return {1,ARGV[8]}"""
    result = await redis.eval(script, 7, flow_key, 'megmeet:iam:session:'+iam._digest(sid),
        device_key(body.device_id), store.key('grant', token), cache_key,
        iam.account_revocation_key(session_record['account_no']), cancellation_key(body.device_id), raw, session_raw,
        session_record['revocation_version'], json.dumps(device), json.dumps(grant),
        access_expires, json.dumps(cache), sealed)
    status = int(result[0])
    if status == -1:
        fail('desktop_resume_conflict', 409)
    if status not in (1, 2):
        fail('desktop_device_expired')
    if await service.authenticate(sid) is None:
        fail('desktop_device_expired')
    return _reply(result[1], sid, body)


async def resume(request: Request, body: ResumeBody):
    desktop._check_mode(request)
    challenge = _proof(body)
    desktop._instance(body.request_id)
    if not RESUME.fullmatch(body.next_resume_token) or hmac.compare_digest(body.resume_token, body.next_resume_token):
        fail('desktop_resume_request_invalid', 400)
    async def run(service):
        redis = _redis(service)
        key = device_key(body.device_id)
        raw = await redis.get(key)
        if raw is None:
            fail('desktop_device_expired')
        device = _json(raw)
        _bind(device, body, challenge)
        current, nxt = desktop._hash(body.resume_token), desktop._hash(body.next_resume_token)
        repeated = (device.get('previous') == current and device.get('current') == nxt
            and device.get('last_request') == body.request_id)
        if current != device.get('current') and not repeated:
            fail('desktop_resume_conflict', 409)
        sid = device['sid']
        user, session_raw, session_record = await _session(service, sid)
        if (device.get('expires_at') != session_record['expires_at']
                and not (persistent.persistent(session_record) and type(device.get('expires_at')) is int and device['expires_at'] > time.time())):
            fail('desktop_device_expired')
        access_expires = min(int(time.time()) + desktop._lifetime(), await service._live_personal_deadline(sid, session_record))
        token, sealed = _response(user, sid, body, access_expires, session_record['expires_at'])
        grant = {'sid': sid, 'instance': body.instance_id, 'renewal_challenge': challenge,
            'expires_at': access_expires}
        # Atomic CAS verifies device, immutable login snapshot and revocation
        # epoch together. It never creates or extends the underlying session.
        script = """local now=tonumber(redis.call('TIME')[1])
        local raw=redis.call('GET',KEYS[1]);if not raw then return {0} end
        if redis.call('GET',KEYS[2])~=ARGV[2] or (redis.call('TTL',KEYS[2])<=0 and not (redis.call('TTL',KEYS[2])==-1 and cjson.decode(ARGV[2]).version==4 and cjson.decode(ARGV[2]).expires_at==0))
          or (redis.call('GET',KEYS[4]) or '0')~=ARGV[3] then return {0} end
        local d=cjson.decode(raw);local expected=cjson.decode(ARGV[1]);local grant=cjson.decode(ARGV[7])
        if d.sid~=expected.sid or d.instance~=expected.instance or d.challenge~=expected.challenge then return {-1} end
        if (d.expires_at~=0 and d.expires_at<=now) or grant.expires_at<=now then return {0} end
        if d.previous==ARGV[4] and d.current==ARGV[5] and d.last_request==ARGV[6] then
          if d.retry_until<=now then return {-2} end
          return {2,d.response}
        end
        if raw~=ARGV[1] or d.current~=ARGV[4] or d.last_request==ARGV[6] then return {-1} end
        local session=cjson.decode(ARGV[2]);local persistent=session.version==4 and session.expires_at==0
    local remaining=redis.call('TTL',KEYS[2]);local ttl=persistent and (remaining==-1 and 3600 or remaining) or math.min(d.expires_at-now,remaining)
        if ttl<=0 or grant.expires_at>now+ttl or redis.call('EXISTS',KEYS[3])==1 then return {0} end
        d.previous=d.current;d.current=ARGV[5];d.last_request=ARGV[6]
        if persistent then d.expires_at=0 end
        d.retry_until=persistent and now+120 or math.min(now+120,d.expires_at);d.response=ARGV[8];d.generation=d.generation+1
        if persistent and remaining==-1 then redis.call('SET',KEYS[1],cjson.encode(d)) else redis.call('SET',KEYS[1],cjson.encode(d),'EX',ttl) end
        redis.call('SET',KEYS[3],ARGV[7],'EX',grant.expires_at-now)
        return {1,ARGV[8]}"""
        result = await redis.eval(script, 4, key, 'megmeet:iam:session:'+iam._digest(sid),
            desktop.desktop_store(service).key('grant', token),
            iam.account_revocation_key(session_record['account_no']), raw, session_raw,
            session_record['revocation_version'], current, nxt, body.request_id, json.dumps(grant), sealed)
        status = int(result[0])
        if status == -2:
            fail('desktop_resume_request_stale', 409)
        if status == -1:
            fail('desktop_resume_conflict', 409)
        if status not in (1, 2):
            fail('desktop_device_expired')
        if await service.authenticate(sid) is None:
            fail('desktop_device_expired')
        return _reply(result[1], sid, body)
    return await iam._with_service(run)


async def device_logout(request: Request, body: DeviceLogoutBody):
    desktop._check_mode(request)
    challenge = _proof(body)
    async def run(service):
        redis = _redis(service); key = device_key(body.device_id)
        raw = await redis.get(key)
        if raw is None:
            # Cancellation can race the first exchange before a device exists.
            # 600 seconds covers the 300-second original enrollment flow; an
            # expired flow cannot later create a device, even if a request was
            # paused. Never return success while allowing a late enrollment.
            script = """if redis.call('EXISTS',KEYS[1])==1 then return 0 end
            redis.call('SET',KEYS[2],ARGV[1],'EX',600);return 1"""
            canceled = await redis.eval(script, 2, key, cancellation_key(body.device_id),
                json.dumps({'instance':body.instance_id,'challenge':challenge}))
            if canceled == 1:
                return JSONResponse({'revoked': True}, headers=desktop.HEADERS)
            raw = await redis.get(key)
            if raw is None:
                # A concurrent logout already deleted the device/session.
                return JSONResponse({'revoked': True}, headers=desktop.HEADERS)
        device = _json(raw)
        _bind(device, body, challenge)
        token_hash = desktop._hash(body.resume_token)
        if token_hash not in (device.get('current'), device.get('previous')):
            fail('desktop_resume_conflict', 409)
        # The bound previous token can only revoke, never issue a grant. This
        # also permits durable offline logout after a response-loss rotation.
        script = """local raw=redis.call('GET',KEYS[1]);if not raw then return 1 end
        local d=cjson.decode(raw)
        if d.instance~=ARGV[1] or d.challenge~=ARGV[2] or d.sid~=ARGV[4]
          or (d.current~=ARGV[3] and d.previous~=ARGV[3]) then return 0 end
        redis.call('DEL',KEYS[1],KEYS[2]);return 1"""
        done = await redis.eval(script, 2, key, 'megmeet:iam:session:'+iam._digest(device['sid']),
            body.instance_id, challenge, token_hash, device['sid'])
        if done != 1:
            fail('desktop_resume_conflict', 409)
        return JSONResponse({'revoked': True}, headers=desktop.HEADERS)
    return await iam._with_service(run)
