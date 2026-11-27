"""Bind worker capabilities to the admitted conversation without exposing them to the browser."""
import base64
import asyncio
import hashlib
import hmac
import json
import re

from .iam_oauth import IAMFailure

SESSION = re.compile(r'(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',re.I)
LEASE = 'megmeet:personal-cloud:lease:'
MARK = 'megmeet:task-network:v1:'

def session_id(value):
    return value if isinstance(value,str) and SESSION.fullmatch(value) else None

def request_session(path,query,body):
    if path.startswith('/__megmeet/tools/'):
        return session_id(body.get('session_id') if isinstance(body,dict) and 'session_id' in body else query.get('session_id'))
    if path.startswith('/api/'):
        if isinstance(body,dict):
            args=(body.get('payload') or {}).get('args') if isinstance(body.get('payload'),dict) else None
            if isinstance(args,dict):
                req=args.get('request')
                sid=session_id(req.get('sessionId')) if isinstance(req,dict) else None
                return sid or session_id(args.get('agentId'))
            return session_id(body.get('sessionId'))
        return session_id(query.get('sessionId'))
    return None

def marker(owner,sid):
    return MARK + owner + ':' + sid.lower().removeprefix('session-')

def restricted_lease(parent,owner,sid):
    key=marker(owner,sid)
    signature=hmac.digest(parent.encode(),key.encode(),'sha256')
    return 'mcl_'+base64.urlsafe_b64encode(signature).rstrip(b'=').decode()

def order_message_key(owner,sid,request_id):
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', request_id, re.I):
        return None
    return marker(owner,sid) + ':order-text:' + request_id.lower()


async def scope_for_request(redis,parent,owner,sid,selected,path,request_id=None,prompt=None):
    from .skill_network_policy import reviewed_confidential_names
    key=marker(owner,sid)+':'+hashlib.sha256(parent.encode()).hexdigest()
    names=set(selected)
    if path=='/__megmeet/tools/selection':
        # DSH expires the corresponding toolbar selection after 30 minutes.
        await redis.set(key+':selection',json.dumps(sorted(names)),ex=1800)
    elif path=='/api/session/prompt' or path.startswith('/__megmeet/tools/'):
        names.update(json.loads(await redis.get(key+':selection') or '[]'))
    private=bool(await asyncio.to_thread(reviewed_confidential_names,names)) if names else False
    if path=='/api/session/prompt' and isinstance(request_id,str) and re.fullmatch('[a-zA-Z0-9_-]{1,160}',request_id):
        replay=key+':request:'+request_id
        if private:
            await redis.set(replay,'1',ex=86400)
        else:
            private=bool(await redis.exists(replay))
    lease = await select_lease(redis,parent,owner,sid,private)
    if path == '/api/session/prompt' and 'sales-orders' in names and isinstance(prompt,dict):
        parts = prompt.get('content')
        cache_key = order_message_key(owner,sid,request_id)
        if cache_key and isinstance(parts,list) and any(isinstance(p,dict) and p.get('type') in ('file','image') for p in parts):
            fragments = []
            left = 2000
            for p in parts:
                if not isinstance(p,dict) or p.get('type') != 'text' or not isinstance(p.get('text'),str):
                    continue
                if left <= 0:
                    break
                part = p['text'][:left].strip()
                if part:
                    fragments.append(part)
                    left -= len(part) + 1
            if fragments:
                # 20 sequential attachment reads may outlive an hour. A retried request keeps its original wording.
                await redis.set(cache_key,'\n'.join(fragments),ex=4*3600,nx=True)
    return lease

async def select_lease(redis,parent,owner,sid,restrict):
    """The original task always keeps its immutable capability; the next turn is decided afresh."""
    if not restrict:return parent
    token=restricted_lease(parent,owner,sid)
    prefix=hashlib.sha256(parent.encode()).hexdigest()
    dest=LEASE+hashlib.sha256(token.encode()).hexdigest()
    script="""
    local parent=redis.call('GET',KEYS[1])
    local ttl=redis.call('PTTL',KEYS[1])
    if not parent or ttl<=0 then return -1 end
    local data=cjson.decode(parent)
    data.parent_digest=ARGV[1]
    data.task_network_mode='restricted'
    data.task_session_id=ARGV[2]
    redis.call('SET',KEYS[2],cjson.encode(data),'PX',ttl)
    return 1
    """
    if await redis.eval(script,2,LEASE+prefix,dest,prefix,sid)!=1:
        raise IAMFailure(401,'personal_cloud_lease_expired')
    return token
