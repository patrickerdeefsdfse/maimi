"""Browser session idle deadline shared by both application replicas."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()
IDLE_SECONDS = 7200
PROGRAM_URL = '/maimi/web/web-idle.js'
NO_STORE = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}

# Retain the IAM record's own expiry and revocation rules. Only a browser
# session has this second deadline. Workers and passive polls never renew it.
READ_SESSION = """
local last=redis.call('GET',KEYS[2])
if last and tonumber(redis.call('TIME')[1]) >= tonumber(last) then
    redis.call('DEL',KEYS[1],KEYS[2])
    return false
end
return redis.call('GET',KEYS[1])
"""
WINDOW = """
if redis.call('EXISTS',KEYS[1]) == 0 then return -1 end
local now=tonumber(redis.call('TIME')[1])
local deadline=tonumber(redis.call('GET',KEYS[2]))
if deadline and now >= deadline then
    redis.call('DEL',KEYS[1],KEYS[2]); return -1
end
if not deadline or ARGV[1]=='1' then
    deadline=now+tonumber(ARGV[2])
    local ttl=redis.call('PTTL',KEYS[1])
    if ttl==-1 then redis.call('SET',KEYS[2],deadline)
    elseif ttl>0 then redis.call('SET',KEYS[2],deadline,'PX',ttl)
    else return -1 end
end
return deadline-now
"""

def keys(sid):
    from . import iam_oauth as iam
    suffix = iam._digest(sid)
    return 'megmeet:iam:session:' + suffix, 'megmeet:iam:web-idle:' + suffix


async def window(store, sid, active=False):
    return int(await store.redis.eval(WINDOW, 2, *keys(sid), int(active), IDLE_SECONDS))


@router.api_route('/v1/iam/web-activity', methods=['GET', 'POST'])
async def activity(request: Request):
    from . import iam_oauth as iam
    sid = request.cookies.get(iam.SESSION_COOKIE, '')
    if request.headers.get('authorization') or not iam.OPAQUE.fullmatch(sid):
        return Response(status_code=401, headers=NO_STORE)
    active = request.method == 'POST'
    if active and request.headers.get('origin') != iam.IAMConfig.from_env().public_origin:
        return Response(status_code=403, headers=NO_STORE)

    async def check(service):
        if not await service.authenticate(sid):
            return -1
        return await window(service.store, sid, active)

    remaining = await iam._with_service(check)
    if remaining < 0:
        return Response(status_code=401, headers=NO_STORE)
    return JSONResponse({'remaining_seconds': remaining}, headers=NO_STORE)


@router.get(PROGRAM_URL)
async def program():
    return Response(Path(__file__).with_name('web_idle.js').read_bytes(),
                    media_type='text/javascript', headers=NO_STORE)
