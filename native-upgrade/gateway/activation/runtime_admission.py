"""Trusted manager-only admission of brand-new native021 workspaces.

Existing volumes are NEVER inferred to be new from their contents. A durable
intent precedes creation. Interrupted creation is fail-closed for operator
recovery; successful receipts bind both volume identities and this release.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import quote

LABEL = 'com.megmeet.personal-cloud.manager'
OWNER_LABEL = 'com.megmeet.personal-cloud.owner'
ADMISSION_LABEL = 'com.megmeet.personal-cloud.new-admission'


class AdmissionRefused(RuntimeError):
    pass


def directory(path):
    p = Path(path)
    if not p.is_absolute() or any(x.is_symlink() for x in (p, *p.parents)):
        raise AdmissionRefused('UNSAFE_ADMIN_DIRECTORY')
    s = p.stat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
        raise AdmissionRefused('UNSAFE_ADMIN_DIRECTORY')
    return p


def read_receipt(path, *, maximum_bytes=16384):
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 4 * 1024 * 1024:
        raise AdmissionRefused('INVALID_RECEIPT_LIMIT')
    p = Path(path)
    directory(p.parent)
    if p.is_symlink(): raise AdmissionRefused('UNSAFE_ADMIN_RECEIPT')
    before = p.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
            or before.st_mode & 0o022 or before.st_size > maximum_bytes):
        raise AdmissionRefused('UNSAFE_ADMIN_RECEIPT')
    with os.fdopen(os.open(p, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as f:
        opened = os.fstat(f.fileno()); raw = f.read(maximum_bytes + 1); after = os.fstat(f.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if len(raw) > maximum_bytes or identity(before) != identity(opened) or identity(opened) != identity(after):
        raise AdmissionRefused('ADMIN_RECEIPT_CHANGED')
    return json.loads(raw)


def write_receipt(path, data):
    p = Path(path); directory(p.parent)
    with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(json.dumps(data, sort_keys=True, separators=(',', ':')).encode())
        f.flush(); os.fsync(f.fileno())
    fd = os.open(p.parent, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def identity(volume, name, owner, namespace):
    if (not isinstance(volume, dict) or volume.get('Name') != name
            or not isinstance(volume.get('CreatedAt'), str) or not volume['CreatedAt']
            or (volume.get('Labels') or {}).get(LABEL) != namespace
            or (volume.get('Labels') or {}).get(OWNER_LABEL) != owner):
        raise AdmissionRefused('VOLUME_OWNERSHIP_MISMATCH')
    return {'name': name, 'created_at': volume['CreatedAt']}


def new_receipt_matches(path, *, owner, namespace, release_sha, image_id, state, files):
    try:
        data = read_receipt(path)
        return data == dict(schema='megmeet-runtime-new-owner-r65-v1', status='NEW_EMPTY_VERIFIED',
            owner=owner, manager_namespace=namespace, runtime='0.2.1-rc.2',
            release_manifest_sha256=release_sha, image_id=image_id,
            state_volume=state, files_volume=files)
    except (OSError, ValueError, TypeError, AdmissionRefused): return False


async def initialize_empty(docker, image, volumes, namespace, owner, *, verify=False):
    """Fixed code only. No workspace content executes and no network is attached."""
    script = """const fs=require('node:fs');
for(const p of ['/state','/workspace']){
 if(fs.readdirSync(p).length)process.exit(21);
 if(process.argv[1]==='initialize'){fs.chownSync(p,1000,1000);fs.chmodSync(p,0o700)}
 const s=fs.statSync(p);if(s.uid!==1000||s.gid!==1000||(s.mode&0o777)!==0o700)process.exit(22);
}
"""
    name = 'r65-admit-' + uuid.uuid4().hex
    config = {'Image': image, 'User': '1000:1000' if verify else '0:0', 'Entrypoint': ['node'],
        'Cmd': ['-e', script, 'verify' if verify else 'initialize'],
        'Labels': {LABEL: namespace, OWNER_LABEL: owner, 'com.megmeet.personal-cloud.helper': 'new-owner'},
        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True,
            'CapDrop': ['ALL'], 'CapAdd': [] if verify else ['CHOWN', 'FOWNER', 'DAC_OVERRIDE'],
            'SecurityOpt': ['no-new-privileges:true'], 'Memory': 128*1024*1024,
            'MemorySwap': 128*1024*1024, 'PidsLimit': 32, 'Init': True,
            'Mounts': [{'Type': 'volume', 'Source': volumes[k]['name'],
                'Target': '/state' if k == 'state' else '/workspace', 'ReadOnly': verify,
                'VolumeOptions': {'NoCopy': True}}
                for k in ('state', 'files')]}}
    cid = None
    try:
        created = await docker('POST', '/containers/create?name='+name, config); cid = created['Id']
        await docker('POST', '/containers/'+cid+'/start')
        result = await asyncio.wait_for(docker('POST', '/containers/'+cid+'/wait?condition=not-running'), 20)
        if result.get('StatusCode') != 0 or result.get('Error'):
            raise AdmissionRefused('EMPTY_VOLUME_PROOF_FAILED')
    finally:
        # Named request allows cleanup when create succeeded but its response was lost.
        await asyncio.shield(docker('DELETE', '/containers/'+(cid or name)+'?force=true', missing_ok=True))


async def admit_if_new(*, docker, owner, namespace, image_id, release_sha, receipts_directory,
                       snapshot_directory):
    if (not re.fullmatch('[a-f0-9]{32}', owner) or not re.fullmatch('[a-z0-9][a-z0-9-]{1,28}[a-z0-9]', namespace)
            or not re.fullmatch('[a-f0-9]{64}', release_sha)
            or not re.fullmatch('sha256:[a-f0-9]{64}', image_id)):
        raise AdmissionRefused('ADMISSION_SCOPE_INVALID')
    root = directory(receipts_directory); snapshots = directory(snapshot_directory)
    receipt = root/(owner+'.json'); pending = root/(owner+'.pending.json')
    names = {k: namespace+'-'+owner+'-'+k for k in ('state', 'files')}
    old = {k: await docker('GET', '/volumes/'+v, missing_ok=True) for k, v in names.items()}
    if any(v is not None for v in old.values()):
        if not all(v is not None for v in old.values()): raise AdmissionRefused('PARTIAL_EXISTING_VOLUMES')
        return False  # Existing owners must pass their existing receipt checks.
    if any(p.exists() or p.is_symlink() for p in (receipt, pending, snapshots/(owner+'.json'))):
        raise AdmissionRefused('MISSING_ADMITTED_VOLUMES_OR_INTERRUPTED_CREATION')
    nonce = uuid.uuid4().hex
    write_receipt(pending, {'schema': 'r65-new-owner-intent-v1', 'owner': owner,
        'manager_namespace': namespace, 'image_id': image_id, 'release_manifest_sha256': release_sha,
        'nonce': nonce, 'volumes': names})
    volumes = {}
    for kind, name in names.items():
        await docker('POST', '/volumes/create', {'Name': name,
            'Labels': {LABEL: namespace, OWNER_LABEL: owner, ADMISSION_LABEL: nonce}})
        value = await docker('GET', '/volumes/'+name)
        volumes[kind] = identity(value, name, owner, namespace)
        if value['Labels'].get(ADMISSION_LABEL) != nonce: raise AdmissionRefused('VOLUME_CREATION_RACE')
        filters = quote(json.dumps({'volume': [name]}, separators=(',', ':')))
        if await docker('GET', '/containers/json?all=true&filters='+filters):
            raise AdmissionRefused('NEW_VOLUME_REFERENCED')
    await initialize_empty(docker, image_id, volumes, namespace, owner)
    await initialize_empty(docker, image_id, volumes, namespace, owner, verify=True)
    for kind, name in names.items():
        value = await docker('GET', '/volumes/'+name)
        if identity(value, name, owner, namespace) != volumes[kind] or value['Labels'].get(ADMISSION_LABEL) != nonce:
            raise AdmissionRefused('NEW_VOLUME_CHANGED')
        filters = quote(json.dumps({'volume': [name]}, separators=(',', ':')))
        if await docker('GET', '/containers/json?all=true&filters='+filters):
            raise AdmissionRefused('NEW_VOLUME_REFERENCED')
    write_receipt(receipt, dict(schema='megmeet-runtime-new-owner-r65-v1', status='NEW_EMPTY_VERIFIED',
        owner=owner, manager_namespace=namespace, runtime='0.2.1-rc.2',
        release_manifest_sha256=release_sha, image_id=image_id,
        state_volume=volumes['state'], files_volume=volumes['files']))
    pending.unlink()
    return True
