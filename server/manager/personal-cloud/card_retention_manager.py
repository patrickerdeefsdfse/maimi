"""Card-v1-only expiry adapter for the existing single-manager Docker runtime.

No legacy namespace purge, host source bind, image override, or DSH startup.
The image pin is administrator configuration after a new image is qualified.
This is not the future Kubernetes lifecycle/HA reservation implementation.
"""
import asyncio
import json
import re
import secrets
from urllib.parse import quote

PROTOCOL = 'card-retention-v1'
OWNER_LABEL = 'com.megmeet.personal-cloud.owner'
OP_LABEL = 'com.megmeet.personal-cloud.card-expiry-operation'
MAX_BYTES = 65536


def validate_refs(refs):
    if not isinstance(refs, list) or not 1 <= len(refs) <= 64:
        raise ValueError('card_cleanup_invalid')
    seen = set()
    for row in refs:
        if (not isinstance(row, dict) or set(row) != {'storage_id','sha256','bytes','expires_at'}
            or not isinstance(row['storage_id'], str) or not re.fullmatch(r'cfs_[a-f0-9]{32}', row['storage_id'])
            or row['storage_id'] in seen or not isinstance(row['sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])
            or type(row['bytes']) is not int or not 1 <= row['bytes'] <= 134217728
            or type(row['expires_at']) is not int or row['expires_at'] < 172800):
            raise ValueError('card_cleanup_invalid')
        seen.add(row['storage_id'])
    # Detach the acknowledged identities from the caller's mutable object.
    return {'protocol': PROTOCOL, 'refs': [dict(row) for row in refs]}


def validate_result(value, refs):
    expected = {row['storage_id']:row for row in refs}
    if (not isinstance(value, dict) or set(value) != {'status','items'}
        or value['status'] not in ('COMPLETE','PARTIAL') or not isinstance(value['items'], list)
        or len(value['items']) != len(refs)):
        raise ValueError('card_cleanup_response_invalid')
    seen = set()
    for row in value['items']:
        if (not isinstance(row, dict) or not isinstance(row.get('storage_id'), str)
            or row['storage_id'] not in expected or row['storage_id'] in seen
            or type(row.get('reclaimed_bytes')) is not int
            or not 0 <= row['reclaimed_bytes'] <= expected[row['storage_id']]['bytes']):
            raise ValueError('card_cleanup_response_invalid')
        seen.add(row['storage_id'])
        keys = {'storage_id','status','reclaimed_bytes'}
        if row.get('status') == 'DEFERRED':
            if (set(row) != keys|{'reason'} or row['reclaimed_bytes'] != 0
                or not isinstance(row['reason'], str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}', row['reason'])):
                raise ValueError('card_cleanup_response_invalid')
        elif row.get('status') == 'DELETED':
            if (set(row) != keys|{'already_deleted'} or type(row['already_deleted']) is not bool
                or (row['already_deleted'] and row['reclaimed_bytes'] != 0)):
                raise ValueError('card_cleanup_response_invalid')
        elif row.get('status') != 'ABSENT' or set(row) != keys or row['reclaimed_bytes'] != 0:
            raise ValueError('card_cleanup_response_invalid')
    if (value['status'] == 'PARTIAL') != any(row['status'] == 'DEFERRED' for row in value['items']):
        raise ValueError('card_cleanup_response_invalid')
    return value


def selected_image(m, owner):
    image, overlay, runtime = m.worker_selection(owner)
    if (not isinstance(m.CARD_LIFECYCLE_IMAGE, str)
        or not re.fullmatch(r'sha256:[a-f0-9]{64}', m.CARD_LIFECYCLE_IMAGE)
        or image != m.CARD_LIFECYCLE_IMAGE or runtime != m.NATIVE021_RUNTIME or overlay):
        raise m.HTTPException(503, 'card_cleanup_qualified_image_required')
    return image


def check_identity(m, info, owner, image, *, identifier=None, operation=None):
    labels = (info.get('Config') or {}).get('Labels') or {} if isinstance(info, dict) else {}
    if (not isinstance(info, dict) or not isinstance(info.get('Id'), str)
        or not re.fullmatch('[a-f0-9]{64}', info['Id'])
        or (identifier is not None and info['Id'] != identifier)
        or info.get('Image') != image or labels.get(m.LABEL) != m.INSTANCE
        or labels.get(OWNER_LABEL) != owner
        or (operation is not None and labels.get(OP_LABEL) != operation)):
        raise m.HTTPException(503, 'card_cleanup_identity_mismatch')
    return info['Id']


async def request(m, origin, token, body):
    try:
        async with m.httpx.AsyncClient(timeout=75, trust_env=False, follow_redirects=False) as client:
            async with client.stream('POST', origin+'/__cloud/projects/card-expire',
                headers={'Authorization':'Bearer '+token}, json=body) as response:
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_BYTES:
                        raise ValueError('card_cleanup_response_limit')
                if response.status_code != 200:
                    raise m.HTTPException(409 if response.status_code in (409,429) else 503,
                                          'card_cleanup_unavailable')
                return validate_result(json.loads(raw), body['refs'])
    except (m.httpx.HTTPError, ValueError, TypeError):
        raise m.HTTPException(503, 'card_cleanup_unavailable') from None


def helper_config(m, owner, image, volume_name, token, operation, alias):
    return {'Image':image, 'User':'1000:1000', 'WorkingDir':'/tmp',
        # Cmd alone would inherit the native-worker ENTRYPOINT and start DSH.
        'Entrypoint':['python3','-I','-B','/opt/personal-cloud/card_retention_server.py'], 'Cmd':[],
        'Env':['CLOUD_ROUTE_TOKEN='+token, 'CLOUD_OWNER_KEY='+owner, 'CLOUD_MAINTENANCE='+PROTOCOL],
        'Labels':{m.LABEL:m.INSTANCE, OWNER_LABEL:owner, OP_LABEL:operation},
        'HostConfig':{'NetworkMode':m.NETWORK, 'ReadonlyRootfs':True, 'CapDrop':['ALL'],
            'SecurityOpt':['no-new-privileges:true'], 'Memory':128*1024*1024, 'MemorySwap':128*1024*1024,
            'NanoCpus':500000000, 'PidsLimit':16, 'Init':True, 'AutoRemove':False,
            'Tmpfs':{'/tmp':'rw,nosuid,noexec,size=16777216,uid=1000,gid=1000'},
            'LogConfig':{'Type':'none'},
            'Mounts':[{'Type':'volume','Source':volume_name,'Target':'/state','VolumeOptions':{'NoCopy':True}}]},
        'NetworkingConfig':{'EndpointsConfig':{m.NETWORK:{'Aliases':[alias]}}}}


async def wait_ready(m, origin, token):
    async with m.httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as client:
        for _ in range(30):
            try:
                response = await client.get(origin+'/__cloud/ready', headers={'Authorization':'Bearer '+token})
                if response.status_code == 200 and response.json() == {'mode':PROTOCOL}:
                    return
            except (m.httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(.2)
    raise m.HTTPException(503, 'card_cleanup_helper_unavailable')


async def remove_helper(m, owner, image, operation, name, identifier):
    # A lost create response may still have created a container. Inspect only
    # our fresh unpredictable name, and recheck all identity labels before delete.
    target = identifier or name
    info = await m.docker('GET', '/containers/'+target+'/json', missing_ok=True)
    if info is None:
        if identifier is None:
            # A timed-out create could complete later; absence is not final proof.
            raise m.HTTPException(503, 'card_cleanup_create_outcome_unknown')
        return
    found = check_identity(m, info, owner, image, identifier=identifier, operation=operation)
    await m.docker('DELETE', '/containers/'+found+'?force=true&v=false')
    if await m.docker('GET', '/containers/'+found+'/json', missing_ok=True) is not None:
        raise m.HTTPException(503, 'card_cleanup_termination_unconfirmed')


async def offline(m, owner, body, image, state):
    name = m.owner_key(owner)
    volume_name = m.volume_names(owner)['state']
    volume = await m.docker('GET', '/volumes/'+volume_name, missing_ok=True)
    if volume is None:
        return {'status':'COMPLETE', 'items':[{'storage_id':r['storage_id'], 'status':'ABSENT',
                                             'reclaimed_bytes':0} for r in body['refs']]}
    labels = volume.get('Labels') or {}
    if volume.get('Name') != volume_name or labels.get(m.LABEL) != m.INSTANCE or labels.get(OWNER_LABEL) != owner:
        raise m.HTTPException(503, 'personal_cloud_volume_ownership_mismatch')
    filters = quote(json.dumps({'volume':[volume_name]}, separators=(',',':')))
    if await m.docker('GET', '/containers/json?all=true&filters='+filters):
        raise m.HTTPException(409, 'personal_cloud_storage_busy')
    operation = secrets.token_hex(16)
    helper_name = name+'-card-expiry-'+operation
    alias = 'card-expiry-'+secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    identifier = None
    state['confirmed'] = False
    try:
        created = await m.docker('POST', '/containers/create?name='+helper_name,
            helper_config(m, owner, image, volume_name, token, operation, alias))
        if not isinstance(created, dict) or not re.fullmatch('[a-f0-9]{64}', created.get('Id','')):
            raise m.HTTPException(503, 'card_cleanup_helper_identity')
        identifier = created['Id']
        info = await m.docker('GET', '/containers/'+identifier+'/json', missing_ok=True)
        check_identity(m, info, owner, image, identifier=identifier, operation=operation)
        await m.docker('POST', '/containers/'+identifier+'/start')
        origin = 'http://'+alias+':3084'
        await wait_ready(m, origin, token)
        return await request(m, origin, token, body)
    finally:
        await remove_helper(m, owner, image, operation, helper_name, identifier)
        state['confirmed'] = True


async def expire(m, owner, refs):
    m.owner_key(owner)
    try:
        body = validate_refs(refs)
    except ValueError:
        raise m.HTTPException(400, 'card_cleanup_arguments') from None
    image = selected_image(m, owner)
    async with m.lock:
        if owner in m.starting or owner in m.migrations:
            raise m.HTTPException(409, 'personal_cloud_storage_busy')
        record = m.records.get(owner)
        if record is not None:
            generation = tuple(record[k] for k in ('container_id','origin','route_token'))
        else:
            state = {'confirmed':True}  # Becomes false before any create request.
            async def run_owned():
                try:
                    return {'result':await offline(m, owner, body, image, state)}
                finally:
                    async with m.lock:
                        if state['confirmed'] and m.migrations.get(owner) is asyncio.current_task():
                            m.migrations.pop(owner, None)
                        # Unknown cleanup deliberately leaves this task as an
                        # admission reservation. No automatic timeout releases it.
            task = asyncio.create_task(run_owned())
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            m.migrations[owner] = task
    if record is None:
        # HTTP cancellation cannot cancel the helper owner or free its slot.
        return await asyncio.shield(task)
    info = await m.docker('GET', '/containers/'+generation[0]+'/json', missing_ok=True)
    check_identity(m, info, owner, image, identifier=generation[0])
    value = await request(m, generation[1], generation[2], body)
    async with m.lock:
        current = m.records.get(owner)
        if current is not record or tuple(current[k] for k in ('container_id','origin','route_token')) != generation:
            raise m.HTTPException(409, 'card_cleanup_worker_generation_changed')
    return {'result':value}
