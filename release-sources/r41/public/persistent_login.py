"""Company-owned remembered login; credentials never leave encrypted Redis.

Zero is an explicit no-expiry marker on session/device records only. Individual
API grants and model leases remain short-lived and revocation checked.
"""
import json
import time
import hmac
import os,re,stat
from pathlib import Path
from . import iam_oauth as iam

VERSION = 4
POLICIES = frozenset(('web-r40', 'device-r40'))
LEASE_SECONDS = 3600
COOKIE_SECONDS = 34560000  # Browser cap; refreshed on authenticated entry.

def enabled():
    """Don't introduce new session records before the forward-only commit."""
    name=os.getenv('PERSONAL_LOGIN_ACTIVATION_FILE','')
    release=os.getenv('PERSONAL_LOGIN_RELEASE_SHA','')
    if not name or not re.fullmatch('[a-f0-9]{64}',release):
        return False
    try:
        path=Path(name)
        if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
            return False
        with path.open('rb') as f:
            before=os.fstat(f.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)&0o022 or before.st_size>4096:
                return False
            raw=f.read(4097);after=os.fstat(f.fileno())
        if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            return False
        value=json.loads(raw)
        return (set(value)=={'schema','runtime','release_manifest_sha256','image_id','status'}
                and value['schema']=='megmeet-runtime-activation-r41-v1' and value['runtime']=='0.2.0-rc.2'
                and value['release_manifest_sha256']==release and value['status']=='ACTIVE'
                and isinstance(value['image_id'],str) and re.fullmatch('sha256:[a-f0-9]{64}',value['image_id']) is not None)
    except (OSError,ValueError,TypeError):
        return False

def policy_ttl(policy):
    if policy in POLICIES:
        return 0 if enabled() else (604800 if policy=='web-r40' else 2592000)
    return iam.LOGIN_POLICIES.get(policy)

def persistent(record):
    return (record.get('version') == VERSION and record.get('login_policy') in POLICIES
            and type(record.get('expires_at')) is int and record['expires_at'] == 0)

def expiry_valid(record):
    return persistent(record) or (type(record.get('expires_at')) is int and record['expires_at'] > time.time())

def deadline(record):
    return int(time.time()) + LEASE_SECONDS if persistent(record) else record['expires_at']

def cache_key(account):
    return 'megmeet:tokenkey:account-cache:r41:' + iam._digest(account)

async def cached_credential(store, account, expected_epoch=None):
    from .personal_model_proxy import PersonalCredential, _catalog, validate_credential
    redis = getattr(store, 'redis', None)
    if redis is None:
        return None
    raw = await redis.get(cache_key(account))
    if raw is None:
        return None
    try:
        if not isinstance(raw,str) or not 1 <= len(raw) <= iam.MAX_PERSONAL_CIPHERTEXT:
            raise ValueError()
        value = json.loads(iam._token_cipher().decrypt(raw.encode('ascii')))
        if (set(value) != {'purpose','account','revocation_version','api_key','models'}
                or value['purpose'] != 'tokenkey-account-cache-r41' or value['account'] != account
                or type(value['revocation_version']) is not int):
            raise ValueError()
        epoch = await store.account_revocation_version(account)
        if value['revocation_version'] != epoch or expected_epoch is not None and epoch != expected_epoch:
            return None
        credential = PersonalCredential(account, value['api_key'], int(time.time()) + LEASE_SECONDS,
                                        _catalog(value['models']))
        validate_credential(credential, account)
        # Do not return an entry replaced or invalidated during the read.
        if await redis.get(cache_key(account)) != raw or await store.account_revocation_version(account) != epoch:
            return None
        return credential
    except iam.IAMFailure:
        raise
    except Exception:
        raise iam.IAMFailure(503, 'personal_key_cache_invalid') from None

async def save_credential(store, account, credential, epoch, *, only_if_absent=False):
    from .personal_model_proxy import validate_credential
    redis = getattr(store, 'redis', None)
    if redis is None:
        return
    validate_credential(credential, account)
    value = {'purpose':'tokenkey-account-cache-r41','account':account,
        'revocation_version':epoch,'api_key':credential.api_key,
        'models':[{'model_code':m.code,'model_name':m.name,
                   **({'inputModalities':list(m.input_modalities)} if m.input_modalities is not None else {})}
                  for m in credential.models]}
    encrypted = iam._token_cipher().encrypt(json.dumps(value,separators=(',',':')).encode()).decode('ascii')
    if len(encrypted) > iam.MAX_PERSONAL_CIPHERTEXT:
        raise iam.IAMFailure(503, 'personal_key_cache_invalid')
    script = """if (redis.call('GET',KEYS[2]) or '0')~=ARGV[2] then return 0 end
    if ARGV[3]=='1' then redis.call('SET',KEYS[1],ARGV[1],'NX') else redis.call('SET',KEYS[1],ARGV[1]) end;return 1"""
    if not await redis.eval(script,2,cache_key(account),iam.account_revocation_key(account),encrypted,epoch,'1' if only_if_absent else '0'):
        raise iam.IAMFailure(401,'iam_session_revoked')

async def invalidate_credential(store, account, rejected):
    redis = getattr(store,'redis',None)
    if redis is None:
        return
    raw = await redis.get(cache_key(account))
    if raw is None:
        return
    try:
        if not isinstance(raw,str) or not 1 <= len(raw) <= iam.MAX_PERSONAL_CIPHERTEXT:
            raise ValueError()
        value = json.loads(iam._token_cipher().decrypt(raw.encode('ascii')))
        if value.get('purpose') != 'tokenkey-account-cache-r41' or value.get('account') != account:
            raise ValueError()
        key = value.get('api_key')
        if not isinstance(key,str) or not hmac.compare_digest(key.encode(),rejected.api_key.encode()):
            return  # A concurrent fresh login already replaced the failed key.
        await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",1,cache_key(account),raw)
    except Exception:
        raise iam.IAMFailure(503,'personal_key_cache_invalid') from None

async def promote_session(service, sid, record):
    """CAS only a live, decryptable previous remembered session; never resurrect."""
    if not enabled() or persistent(record) or record.get('version') != iam.REMEMBERED_SESSION_VERSION or record.get('login_policy') not in POLICIES:
        return record
    redis = getattr(service.store, 'redis', None)
    if redis is None:
        return record
    await service._check_account_revocation(record)
    credential = iam._decrypt_personal_credential(record,sid)
    key = 'megmeet:iam:session:' + iam._digest(sid)
    raw = await redis.get(key)
    if raw is None or json.loads(raw) != record:
        raise iam.IAMFailure(401,'iam_session_expired')
    remaining = await redis.ttl(key)
    if remaining <= 0:
        raise iam.IAMFailure(401,'iam_session_expired')
    if remaining < record['expires_at'] - int(time.time()) - 2:
        return record  # Preserve an administrator-shortened deadline.
    updated = {**record,'version':VERSION,'expires_at':0,
        'personal_credential_encrypted':iam._encrypt_personal_credential(sid,record['account_no'],record['user_no'],
            credential,0,policy=record['login_policy'],revocation_version=record['revocation_version'])}
    script = """local now=tonumber(redis.call('TIME')[1])
    if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('TTL',KEYS[1])<=0
      or redis.call('TTL',KEYS[1])<tonumber(ARGV[4])-now-2
      or tonumber(ARGV[4])<=now or (redis.call('GET',KEYS[2]) or '0')~=ARGV[3] then return 0 end
    redis.call('SET',KEYS[1],ARGV[2]);return 1"""
    ok = await redis.eval(script,2,key,iam.account_revocation_key(record['account_no']),raw,
        json.dumps(updated,separators=(',',':')),record['revocation_version'],record['expires_at'])
    if not ok:
        actual = await service.store.session_read(sid)
        if actual is None or not persistent(actual):
            raise iam.IAMFailure(401,'iam_session_expired')
        iam._decrypt_personal_credential(actual,sid)
        await service._check_account_revocation(actual)
        return actual
    await save_credential(service.store,record['account_no'],credential,record['revocation_version'],only_if_absent=True)
    return updated

def refresh_cookie(response, request, secure):
    sid = request.cookies.get(iam.SESSION_COOKIE,'')
    if iam.OPAQUE.fullmatch(sid):
        response.set_cookie(iam.SESSION_COOKIE,sid,max_age=COOKIE_SECONDS,
            httponly=True,secure=secure,samesite='lax',path='/')
    return response
