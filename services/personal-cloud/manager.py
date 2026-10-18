"""Private fixed-policy Docker controller. Never accepts image/mount/command input.

Only this service has the Docker socket. Worker containers retain named volumes
on eviction. Docker labels constrain recovery to this manager's own namespace.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

OWNER=re.compile(r'^[a-f0-9]{32}$')
LABEL='com.megmeet.personal-cloud.manager'
INSTANCE=os.getenv('PERSONAL_CLOUD_MANAGER_NAMESPACE','megmeet-personal-v1')
# Worker DNS labels append '-' plus the 32-character owner digest. Keep the
# complete name within DNS's 63-byte label limit, including on Python clients.
if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,28}[a-z0-9]',INSTANCE):raise RuntimeError('invalid manager namespace (3-30 characters)')
SECRET_FILE=os.getenv('PERSONAL_CLOUD_MANAGER_SECRET_FILE','/run/secrets/personal-cloud-manager')
NETWORK=os.getenv('PERSONAL_CLOUD_WORKER_NETWORK','megmeet-dev-local_cloud-workers')
IMAGE=os.getenv('PERSONAL_CLOUD_WORKER_IMAGE','megmeet-personal-dsh:local')
MODEL_BASE=os.getenv('PERSONAL_CLOUD_MODEL_BASE_URL','http://gateway:8080/internal/personal-cloud/model/v1')
MAX_HOSTS=int(os.getenv('PERSONAL_CLOUD_MAX_ACTIVE_HOSTS','0'))  # zero means no account-slot ceiling
IDLE=int(os.getenv('PERSONAL_CLOUD_IDLE_SECONDS','0'))
CLOSED_SECONDS=int(os.getenv('PERSONAL_CLOUD_CLOSED_SECONDS','7200'))
PRESENCE_SECONDS=180
RESERVED_OWNERS=frozenset(filter(None,os.getenv('PERSONAL_CLOUD_RESERVED_OWNERS','').split(',')))
MIN_AVAILABLE_MIB=int(os.getenv('PERSONAL_CLOUD_MIN_AVAILABLE_MIB','2048'))
WORKER_OVERLAY_DIR=os.getenv('PERSONAL_CLOUD_WORKER_OVERLAY_DIR','')
WORKER_RUNTIME=os.getenv('PERSONAL_CLOUD_WORKER_RUNTIME','0.1.7-alpha.2')
CANARY_IMAGE=os.getenv('PERSONAL_CLOUD_CANARY_WORKER_IMAGE','')
CANARY_OWNERS=frozenset(filter(None,os.getenv('PERSONAL_CLOUD_CANARY_OWNERS','').split(',')))
CANARY_OVERLAY_DIR=os.getenv('PERSONAL_CLOUD_CANARY_OVERLAY_DIR','')
CANARY_RUNTIME='0.1.7-rc.2'
REVIEWED_RUNTIME='0.2.0-rc.2'
NATIVE021_RUNTIME='0.2.1-rc.2'
NATIVE021_ACTIVATION_PATH='/opt/personal-cloud/runtime-r65/activation.json'
NATIVE021_RECEIPTS_PATH='/opt/personal-cloud/runtime-r65/owners'
NATIVE021_RECEIPTS_DIR=os.getenv('PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR','')
RUNTIME_ACTIVATION_FILE=os.getenv('PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE','')
RUNTIME_RELEASE_SHA=os.getenv('PERSONAL_CLOUD_RUNTIME_RELEASE_SHA','')
FAST_ENTRY_OVERLAY_DIR=os.getenv('PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR','')
FAST_ENTRY_OVERLAY_PATH='/data/megmeet-service-entry-r62-20261016/native'
FAST_ENTRY_BRAND_SHA256='cb8b3b620a4f61da0773319f1cd746a027f000553876048726b054c4f609abb2'
FAST_ENTRY_NATIVE_FILES=(('cloud-brand-client.js', '/opt/dsh/node_modules/@deepseek-ai/dsh-client-ui-brand-official/lib/client.js', 'cb8b3b620a4f61da0773319f1cd746a027f000553876048726b054c4f609abb2', 1840580), ('logistics-index.mjs', '/opt/dsh/logistics-skill/index.mjs', '03d7684f9323296db7aca0ef68436b67f99536271c3fcc4d80404818319069db', 4777), ('logistics-SKILL.md', '/opt/dsh/logistics-skill/skills/logistics-query/SKILL.md', '93dd06858c36303de3f1ac107729226f68dc56123e2d58cdbb58ece0ebb845c7', 4873), ('runtime020-source-pins.json', '/opt/personal-cloud/runtime020-source-pins.json', '85d1b2d4a46ea7368eccfd958d44434f0b1d7f0504a6fe22a722f5865fe16a16', 28391), ('toolkit-adapter-index.mjs', '/opt/dsh/toolkit/adapter/index.mjs', 'bcecd0db76c000246ac45e23f24c7ee13da1fcb9fa2e0471e40b2e122e47090b', 5959), ('session-tools.mjs', '/opt/dsh/toolkit/session-tools.mjs', 'f1c30c1a3d19d90d07443ae2e89197165c806352b2ecaa0c5b301eb5f6ac041b', 15303), ('project-bridge.mjs', '/opt/dsh/toolkit/project-bridge.mjs', 'e425434fac415c90abde198c907a836e43372eaae2e251898e3243a77c016e2d', 17661), ('worker.mjs', '/opt/personal-cloud/worker.mjs', 'ee333f3f4eb5b45a600f3ede7df9be21b2615b3ba95de053e8b7326289149da9', 42404), ('company-ppt-index.mjs', '/opt/dsh/company-ppt-template/index.mjs', 'eb6f22367de289c128adc1a04850026f550cc5b50130f06c50e75c263c4e044c', 6325), ('company-ppt-SKILL.md', '/opt/dsh/company-ppt-template/skills/company-ppt-template/SKILL.md', 'aafacb6b24debf98e311c67f47666e3bee6b3ad6b40858dd800ddaa4f0bcf867', 4356), ('company-ppt-guide.md', '/opt/dsh/company-ppt-template/skills/company-ppt-template/references/template-guide.md', 'f993f7aa0eaf28bb19ce8a1060045977806919e9d153b1cc3ff8d45cda668b4a', 9324), ('company-ppt-manifest.json', '/opt/dsh/company-ppt-template/skills/company-ppt-template/template-manifest.json', 'a5912d89e13a5d375f52e8a4fd79bc66214cda5a0372a6e655d35dd934367d79', 333), ('company-ppt-template.pptx', '/opt/dsh/company-ppt-template/skills/company-ppt-template/assets/company-template-v5.3.pptx', '0e742339972cbfb37f9e5c10c8f907f0f6fd95cfcf5409118a076c7258869e99', 22275510), ('company-ppt-helper.py', '/opt/dsh/company-ppt-template/skills/company-ppt-template/scripts/company_ppt.py', '97de643035ddcbfaa005e9ce4b1f849989a7ff29b2b1d5b62ce3347eea398c48', 18975), ('company-ppt-preview.py', '/opt/dsh/company-ppt-template/skills/company-ppt-template/scripts/preview_pdf.py', '84e81357062e9a93b01f5d8192c0e85bf1b30f68abc1a4a97c120997bbc2e291', 4834), ('office-pptx-SKILL.md', '/opt/dsh/node_modules/@deepseek-ai/dsh-skill-office/assets/office-pptx/SKILL.md', '21711c61c44372e66c36f449fc22cd192ec5e76e6f17c272a9c46efc2bb9d9e0', 8253))
WORKER_OVERLAY_FILES=('worker.mjs','idle-control.mjs','catalog.mjs','vision-control-plugin.mjs','vision-admission.mjs','task-authority.mjs','web-control-plugin.mjs','web-bridge.mjs','managed-profile.mjs','community-profile.mjs')
MAINTENANCE_OVERLAY_FILES=('history-native.mjs','history-server.mjs','history-migration.mjs')
HISTORY_OVERLAY_DIR=os.getenv('PERSONAL_CLOUD_HISTORY_OVERLAY_DIR','')
HISTORY_OVERLAY_PATH='/data/megmeet-dsh-history-20260928-r29/public'
HISTORY_OVERLAY_FILES=(*MAINTENANCE_OVERLAY_FILES,'history-admission-rc2.mjs')
FOLLOWUP_OVERLAY='/data/megmeet-dsh-cli-20260928-r27/public'
TOOLKIT_OVERLAY_FILES=('project-routing.mjs','project-bridge.mjs','session-tools.mjs')
CHAT_OVERLAY_FILE='cloud-chat-client.js'
CHAT_OVERLAY_TARGET='/opt/dsh/node_modules/@deepseek-ai/dsh-client-ui-chat/lib/client.js'
BRAND_OVERLAY_FILE='cloud-brand-client.js'
BRAND_OVERLAY_TARGET='/opt/dsh/node_modules/@deepseek-ai/dsh-client-ui-brand-official/lib/client.js'
WORKFLOW_OVERLAY_DIR=os.getenv('PERSONAL_CLOUD_WORKFLOW_OVERLAY_DIR','')
WORKFLOW_OVERLAY_PATH='/data/megmeet-workflow-20260928-r30/public'
RUNTIME_FOLLOWUP_OVERLAY_DIR=os.getenv('PERSONAL_CLOUD_RUNTIME_FOLLOWUP_OVERLAY_DIR','')
RUNTIME_FOLLOWUP_OVERLAY_PATH='/data/megmeet-runtime-followup-20260929-r35/public'
RUNTIME_FOLLOWUP_BRAND_SHA256='851cac3c257019bd3e7ed0affdd57f8a39822facd0c3483c7a4c37987d2406d0'
WORKFLOW_OVERLAY_MOUNTS=(
    ('worker.mjs', '/opt/personal-cloud/worker.mjs'),
    ('idle-control.mjs', '/opt/personal-cloud/idle-control.mjs'),
    ('task-authority.mjs', '/opt/personal-cloud/task-authority.mjs'),
    ('task-control-plugin.mjs', '/opt/personal-cloud/task-control-plugin.mjs'),
    ('native-cancel-fence.mjs', '/opt/personal-cloud/native-cancel-fence.mjs'),
    ('native-request-receipt.mjs', '/opt/personal-cloud/native-request-receipt.mjs'),
    ('native-model-scope.mjs', '/opt/personal-cloud/native-model-scope.mjs'),
    ('native-scoped-credentials.mjs', '/opt/personal-cloud/native-scoped-credentials.mjs'),
    ('native-task-context.mjs', '/opt/personal-cloud/native-task-context.mjs'),
    ('task-tickets.mjs', '/opt/personal-cloud/task-tickets.mjs'),
    ('office-bridge.mjs', '/opt/personal-cloud/office-bridge.mjs'),
    ('web-bridge.mjs', '/opt/personal-cloud/web-bridge.mjs'),
    ('web-control-plugin.mjs', '/opt/personal-cloud/web-control-plugin.mjs'),
    ('native-search-connection.mjs', '/opt/personal-cloud/native-search-connection.mjs'),
    ('native-search-install.mjs', '/opt/personal-cloud/native-search-install.mjs'),
    ('official-search-plugin.mjs', '/opt/personal-cloud/official-search-plugin.mjs'),
    ('official-search-provider.mjs', '/opt/personal-cloud/official-search-provider.mjs'),
    ('search-host.mjs', '/opt/personal-cloud/search-host.mjs'),
    ('search-loopback.mjs', '/opt/personal-cloud/search-loopback.mjs'),
    ('search-permits.mjs', '/opt/personal-cloud/search-permits.mjs'),
    ('office-adapter-adapter.mjs', '/opt/dsh/office-adapter/adapter.mjs'),
    ('office-adapter-index.mjs', '/opt/dsh/office-adapter/index.mjs'),
    ('office-adapter-transport.mjs', '/opt/dsh/office-adapter/transport.mjs'),
    ('toolkit-adapter-index.mjs', '/opt/dsh/toolkit/adapter/index.mjs'),
    ('project-bridge.mjs', '/opt/dsh/toolkit/project-bridge.mjs'),
    ('project-routing.mjs', '/opt/dsh/toolkit/project-routing.mjs'),
    ('drawing-extraction.mjs', '/opt/dsh/toolkit/drawing-extraction.mjs'),
    ('cloud-brand-client.js', '/opt/dsh/node_modules/@deepseek-ai/dsh-client-ui-brand-official/lib/client.js'),
)
HOST_MEMORY_BYTES=1536*1024*1024
if MAX_HOSTS<0 or (IDLE!=0 and not 30<=IDLE<=3600):raise RuntimeError('invalid manager resource policy')
if CLOSED_SECONDS!=7200:raise RuntimeError('unreviewed closed workspace retention')
if MAX_HOSTS==0 and MIN_AVAILABLE_MIB<2048:raise RuntimeError('unlimited manager requires memory guard')
if (any(not OWNER.fullmatch(owner) for owner in RESERVED_OWNERS) or (MAX_HOSTS>0 and len(RESERVED_OWNERS)>=MAX_HOSTS)
    or not 0<=MIN_AVAILABLE_MIB<=65536):raise RuntimeError('invalid manager reservation policy')
OVERLAY_PATTERN=r'/data/megmeet-(?:cloud-access|web-image|web-files-idle|retention-network|dsh-upgrade|experience-skills|project-routing|dsh-cli)-[0-9]{8}-r[0-9]+/public'
if WORKER_OVERLAY_DIR and not re.fullmatch(OVERLAY_PATTERN,WORKER_OVERLAY_DIR):
    raise RuntimeError('invalid worker overlay directory')
if WORKER_RUNTIME not in ('0.1.7-alpha.2','0.1.7-rc.2',REVIEWED_RUNTIME,NATIVE021_RUNTIME):
    raise RuntimeError('unreviewed default worker runtime')
if CANARY_IMAGE or CANARY_OWNERS or CANARY_OVERLAY_DIR:
    if (not re.fullmatch(r'sha256:[a-f0-9]{64}',CANARY_IMAGE) or not CANARY_OWNERS
        or len(CANARY_OWNERS)>32 or any(not OWNER.fullmatch(owner) for owner in CANARY_OWNERS)
        or not re.fullmatch(OVERLAY_PATTERN,CANARY_OVERLAY_DIR)):
        raise RuntimeError('invalid canary worker configuration')
if HISTORY_OVERLAY_DIR and (HISTORY_OVERLAY_DIR!=HISTORY_OVERLAY_PATH or WORKER_RUNTIME!='0.1.7-rc.2' or CANARY_IMAGE or CANARY_OWNERS or CANARY_OVERLAY_DIR):
    raise RuntimeError('unreviewed history maintenance configuration')
if WORKFLOW_OVERLAY_DIR and (WORKFLOW_OVERLAY_DIR!=WORKFLOW_OVERLAY_PATH
    or WORKER_OVERLAY_DIR!=FOLLOWUP_OVERLAY or HISTORY_OVERLAY_DIR!=HISTORY_OVERLAY_PATH
    or WORKER_RUNTIME!='0.1.7-rc.2' or CANARY_IMAGE or CANARY_OWNERS or CANARY_OVERLAY_DIR):
    raise RuntimeError('unreviewed workflow overlay configuration')
if RUNTIME_FOLLOWUP_OVERLAY_DIR and (RUNTIME_FOLLOWUP_OVERLAY_DIR!=RUNTIME_FOLLOWUP_OVERLAY_PATH
    or WORKFLOW_OVERLAY_DIR!=WORKFLOW_OVERLAY_PATH or WORKER_RUNTIME!='0.1.7-rc.2'
    or WORKER_OVERLAY_DIR!=FOLLOWUP_OVERLAY or HISTORY_OVERLAY_DIR!=HISTORY_OVERLAY_PATH
    or CANARY_IMAGE or CANARY_OWNERS or CANARY_OVERLAY_DIR):
    raise RuntimeError('unreviewed runtime followup overlay configuration')
if WORKER_RUNTIME==NATIVE021_RUNTIME:
    if (any((WORKER_OVERLAY_DIR,HISTORY_OVERLAY_DIR,WORKFLOW_OVERLAY_DIR,RUNTIME_FOLLOWUP_OVERLAY_DIR,
             FAST_ENTRY_OVERLAY_DIR,CANARY_IMAGE,CANARY_OWNERS,CANARY_OVERLAY_DIR))
        or not re.fullmatch(r'sha256:[a-f0-9]{64}',IMAGE)
        or RUNTIME_ACTIVATION_FILE!=NATIVE021_ACTIVATION_PATH
        or NATIVE021_RECEIPTS_DIR!=NATIVE021_RECEIPTS_PATH
        or not re.fullmatch(r'[a-f0-9]{64}',RUNTIME_RELEASE_SHA)):
        raise RuntimeError('invalid native021 isolated runtime release configuration')
elif NATIVE021_RECEIPTS_DIR:
    raise RuntimeError('native021 owner admission requires native021 runtime')
elif WORKER_RUNTIME==REVIEWED_RUNTIME:
    if (any((WORKER_OVERLAY_DIR,HISTORY_OVERLAY_DIR,WORKFLOW_OVERLAY_DIR,RUNTIME_FOLLOWUP_OVERLAY_DIR,
             CANARY_IMAGE,CANARY_OWNERS,CANARY_OVERLAY_DIR))
        or not re.fullmatch(r'sha256:[a-f0-9]{64}',IMAGE)
        or RUNTIME_ACTIVATION_FILE!='/opt/personal-cloud/runtime-r41/activation.json'
        or not re.fullmatch(r'[a-f0-9]{64}',RUNTIME_RELEASE_SHA)):
        raise RuntimeError('invalid reviewed runtime release configuration')
elif RUNTIME_ACTIVATION_FILE or RUNTIME_RELEASE_SHA:
    raise RuntimeError('runtime release activation requires reviewed runtime')
if FAST_ENTRY_OVERLAY_DIR and (FAST_ENTRY_OVERLAY_DIR!=FAST_ENTRY_OVERLAY_PATH or WORKER_RUNTIME!=REVIEWED_RUNTIME):
    raise RuntimeError('invalid fast entry overlay configuration')

def runtime_activated():
    """Fail closed until health checks and the release journal commit succeed.

    This root-owned file is mounted read-only. It contains no authentication data.
    Runtime admission cannot be activated by an employee request.
    """
    if WORKER_RUNTIME not in (REVIEWED_RUNTIME,NATIVE021_RUNTIME):return True
    if image_id is None:return False
    path=Path(RUNTIME_ACTIVATION_FILE)
    try:
        if path.is_symlink() or path.parent.is_symlink():return False
        before=path.stat()
        if (not stat.S_ISREG(before.st_mode) or before.st_size>2048 or before.st_nlink!=1
            or before.st_uid!=0 or stat.S_IMODE(before.st_mode)&0o022):return False
        with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb') as stream:
            opened=os.fstat(stream.fileno());raw=stream.read(2049);after=os.fstat(stream.fileno())
        identity=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns)
        if identity(before)!=identity(opened) or identity(opened)!=identity(after):return False
        if WORKER_RUNTIME==NATIVE021_RUNTIME:
            return json.loads(raw)=={'schema':'megmeet-runtime-activation-r65-v1','runtime':NATIVE021_RUNTIME,
                'release_manifest_sha256':RUNTIME_RELEASE_SHA,'image_id':image_id,'status':'ACTIVE',
                'history_admission':'OWNER_SNAPSHOT_RECEIPTS_REQUIRED','legacy_overlays':False}
        return json.loads(raw)=={'schema':'megmeet-runtime-activation-r41-v1','runtime':REVIEWED_RUNTIME,
            'release_manifest_sha256':RUNTIME_RELEASE_SHA,'image_id':image_id,'status':'ACTIVE'}
    except (OSError,ValueError,TypeError):return False


def require_native021_history_receipt(owner,state_identity,files_identity):
    """Check a read-only operator receipt; never snapshot/migrate employee data.

    The offline release controller must create and verify both volume backups
    before issuing this receipt. Matching v4 schemas alone is not admission.
    Manager never accepts this object from employee JSON, nor writes it itself.
    The receipt is bound to volume creation identities and the exact release.
    """
    if WORKER_RUNTIME!=NATIVE021_RUNTIME:return
    owner_key(owner)
    file=Path(NATIVE021_RECEIPTS_DIR)/(owner+'.json')
    try:
        if (state_identity[0]!=image_id or state_identity[1]!=owner_key(owner)+'-state'
            or files_identity[0]!=owner_key(owner)+'-files'
            or not all(isinstance(value,str) and value for value in (state_identity[2],files_identity[1]))):
            raise ValueError()
        if any(parent.is_symlink() for parent in (file,*file.parents)):raise ValueError()
        before=file.stat()
        if (not stat.S_ISREG(before.st_mode) or before.st_size>4096 or before.st_nlink!=1
            or before.st_uid!=0 or stat.S_IMODE(before.st_mode)&0o022):raise ValueError()
        with os.fdopen(os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb') as stream:
            opened=os.fstat(stream.fileno());raw=stream.read(4097);after=os.fstat(stream.fileno())
        identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
        if identity(before)!=identity(opened) or identity(opened)!=identity(after):raise ValueError()
        data=json.loads(raw)
        expected={'schema':'megmeet-runtime-owner-snapshot-r65-v1','status':'SNAPSHOT_VERIFIED',
            'owner':owner,'manager_namespace':INSTANCE,'source_runtime':REVIEWED_RUNTIME,
            'target_runtime':NATIVE021_RUNTIME,'release_manifest_sha256':RUNTIME_RELEASE_SHA,
            'image_id':image_id,'state_volume':{'name':state_identity[1],'created_at':state_identity[2]},
            'files_volume':{'name':files_identity[0],'created_at':files_identity[1]},
            'backup':data.get('backup')}
        backup=data.get('backup')
        if (data!=expected or not isinstance(backup,dict)
            or set(backup)!={'state_sha256','files_sha256','verification_receipt_sha256'}
            or any(not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value) for value in backup.values())):
            raise ValueError()
    except (OSError,ValueError,TypeError,AttributeError,IndexError):
        raise HTTPException(503,'personal_cloud_runtime_snapshot_required') from None

def require_runtime_activation():
    if not runtime_activated():raise HTTPException(503,'personal_cloud_runtime_upgrade_preparing')

records={}
waiting={}
# Container creation and admission remain serialized. Only start/readiness wait
# is concurrent; each owner keeps a full resource reservation until READY or a
# confirmed owned-container removal. No later client can start a second copy.
starting={}
startup_cleanups={}
# Only this manager owns these transient jobs; receipts live in the owned state
# volume. Startup removes interrupted owned helpers before taking new opens.
migrations={}
migration_ready={}
migration_failures={}
migration_failure_codes={}
HISTORY_REFUSAL_CODES=frozenset({'ARTIFACT_LIMIT','HEADER_LIMIT','SESSION_LIMIT','PROJECTION_LIMIT','TRASH_LIMIT',
    'INCOMPLETE_DEPENDENCY_INVENTORY','MIGRATION_CORPUS_LIMIT','MIGRATION_EVENT_LIMIT','MIGRATION_INCOMPLETE_LOG',
    'MIGRATION_INCOMPLETE_ZSTD','MIGRATION_CORRUPT_ZSTD','MIGRATION_GENERATION_CONFLICT','MIGRATION_EMPTY_LOG',
    'MIGRATION_BACKUP_CONFLICT','MIGRATION_CORPUS_CHANGED','MIGRATION_RECEIPT_INVALID','MIGRATION_ALREADY_ROLLED_BACK',
    'MIGRATION_DELETION_PENDING','DELETION_BASELINE_LIMIT','INVALID_DELETE_MANIFEST','SNAPSHOT_CHANGED',
    'MAINTENANCE_LOCKED','NATIVE_NOT_QUIESCENT','UNSAFE_FILE','UNSAFE_DIRECTORY','UNSAFE_MAINTENANCE_LOCK',
    'UNRECOGNIZED_ARTIFACTS','UNRECOGNIZED_GENERATION','UNSUPPORTED_FORMAT','UNSUPPORTED_RUNTIME','MAINTENANCE_REFUSED',
    'LEGACY_MIGRATION_REQUIRED','MIGRATION_RECEIPT_PENDING','RUNTIME_SNAPSHOT_REQUIRED','DUAL_GENERATION_WITHOUT_RECEIPT','MIGRATION_SOURCE_CHANGED','MIGRATION_PRIOR_RUNTIME_PENDING','RUNTIME_SNAPSHOT_INVALID','RUNTIME_SNAPSHOT_CHANGED','RUNTIME_SNAPSHOT_PENDING','RUNTIME_SNAPSHOT_STORAGE_UNAVAILABLE'})

def history_refusal(content):
    """Never return exception text, paths, session ids, or provider responses."""
    try:
        value=json.loads(content)
        if isinstance(value,dict) and set(value)=={'error'} and value['error'] in HISTORY_REFUSAL_CODES:
            return value['error']
    except (ValueError,TypeError):pass
    return 'MAINTENANCE_REFUSED'
def allocated_owners():return set(records)|set(migrations)|set(starting)
def migration_queued():
    # An owned preparation task is already admitted; it is not a FIFO waiter.
    return JSONResponse({'status':'starting','phase':'preparing','retry_after':1},status_code=202,headers={'Retry-After':'1'})

QUEUE_LIMIT=128
QUEUE_TTL=30
lock=asyncio.Lock()
# Keep cold native startup pressure bounded. Waiting here never takes the
# admission lock; warm resume/heartbeat paths do not need a startup permit.
startup_slots=asyncio.Semaphore(2)
image_id=None
canary_image_id=None
CATALOG_CHUNK_BYTES=60000
CATALOG_MAX_BYTES=524288


async def docker(method,path,body=None,*,missing_ok=False):
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds='/var/run/docker.sock'),base_url='http://docker',timeout=65) as c:
        r=await c.request(method,'/v1.47'+path,json=body)
        if missing_ok and r.status_code==404:return None
        if r.status_code>=400:raise HTTPException(503,'personal_cloud_docker_unavailable')
        return r.json() if r.content else None


def auth(request):
    expected=Path(SECRET_FILE).read_text().strip()
    if len(expected)<32 or not hmac.compare_digest(request.headers.get('authorization','').encode(),('Bearer '+expected).encode()):
        raise HTTPException(401,'manager_authentication_required')


def owner_key(owner):
    if not OWNER.fullmatch(owner):raise HTTPException(400,'invalid_personal_owner')
    return INSTANCE+'-'+owner


def worker_selection(owner):
    """Only administrator configuration selects a runtime; never request JSON.

    History helpers use the same selection as the owner's next worker. A
    canary must never inherit alpha overlays over an rc.2 dependency tree.
    """
    owner_key(owner)
    require_runtime_activation()
    if owner in CANARY_OWNERS:
        if canary_image_id is None:raise HTTPException(503,'personal_cloud_canary_unavailable')
        return canary_image_id,CANARY_OVERLAY_DIR,CANARY_RUNTIME
    if image_id is None:raise HTTPException(503,'personal_cloud_worker_unavailable')
    return image_id,WORKER_OVERLAY_DIR,WORKER_RUNTIME


def container_config(owner,body,route_token):
    name=owner_key(owner)
    selected_image,overlay_dir,runtime=worker_selection(owner)
    if runtime==NATIVE021_RUNTIME:
        # No legacy bind may shadow the reviewed native image, even if globals
        # were changed after import by a future configuration reload.
        if any((overlay_dir,FAST_ENTRY_OVERLAY_DIR,HISTORY_OVERLAY_DIR,WORKFLOW_OVERLAY_DIR,RUNTIME_FOLLOWUP_OVERLAY_DIR)):
            raise HTTPException(503,'personal_cloud_native021_overlay_forbidden')
        admitted=migration_ready.get(owner)
        if not admitted or admitted[0]!=selected_image or admitted[1]!=name+'-state':
            raise HTTPException(503,'personal_cloud_runtime_snapshot_required')
    # Linux limits each environment entry to roughly 128KiB even when the
    # aggregate environment is valid. ASCII JSON makes chunk bytes explicit.
    catalog=json.dumps(body.catalog,separators=(',',':'),ensure_ascii=True)
    if len(catalog)>CATALOG_MAX_BYTES:raise HTTPException(400,'personal_cloud_catalog_too_large')
    chunks=[catalog[i:i+CATALOG_CHUNK_BYTES] for i in range(0,len(catalog),CATALOG_CHUNK_BYTES)]
    catalog_env=['CLOUD_MODEL_CATALOG_COUNT='+str(len(chunks))]
    catalog_env += ['CLOUD_MODEL_CATALOG_'+str(i)+'='+part for i,part in enumerate(chunks)]
    mounts=[{'Type':'volume','Source':name+'-state','Target':'/state'},
            {'Type':'volume','Source':name+'-files','Target':'/workspace'}]
    if overlay_dir:
        for filename in WORKER_OVERLAY_FILES:
            source=Path(overlay_dir)/filename
            if source.is_symlink() or not source.is_file():raise HTTPException(503,'personal_cloud_worker_overlay_unavailable')
            mounts.append({'Type':'bind','Source':str(source),'Target':'/opt/personal-cloud/'+filename,'ReadOnly':True})
        source=Path(overlay_dir)/BRAND_OVERLAY_FILE
        if source.is_symlink() or not source.is_file():raise HTTPException(503,'personal_cloud_brand_overlay_unavailable')
        mounts.append({'Type':'bind','Source':str(source),'Target':BRAND_OVERLAY_TARGET,'ReadOnly':True})
        if overlay_dir==FOLLOWUP_OVERLAY:
            if runtime!='0.1.7-rc.2':raise HTTPException(503,'personal_cloud_overlay_runtime_mismatch')
            source=Path(overlay_dir)/CHAT_OVERLAY_FILE
            if source.is_symlink() or not source.is_file():raise HTTPException(503,'personal_cloud_chat_overlay_unavailable')
            mounts.append({'Type':'bind','Source':str(source),'Target':CHAT_OVERLAY_TARGET,'ReadOnly':True})
            for filename in TOOLKIT_OVERLAY_FILES:
                source=Path(overlay_dir)/filename
                if source.is_symlink() or not source.is_file():raise HTTPException(503,'personal_cloud_toolkit_overlay_unavailable')
                mounts.append({'Type':'bind','Source':str(source),'Target':'/opt/dsh/toolkit/'+filename,'ReadOnly':True})
    if WORKFLOW_OVERLAY_DIR:
        # This fixed release layer only changes normal worker modules. The r29
        # maintenance helper and preserved history generations stay untouched.
        if runtime!='0.1.7-rc.2' or overlay_dir!=FOLLOWUP_OVERLAY:
            raise HTTPException(503,'personal_cloud_workflow_runtime_mismatch')
        replacements=[]
        root=Path(WORKFLOW_OVERLAY_DIR)
        if root.is_symlink() or not root.is_dir():
            raise HTTPException(503,'personal_cloud_workflow_overlay_unavailable')
        for filename,target in WORKFLOW_OVERLAY_MOUNTS:
            source=root/filename
            if source.is_symlink() or not source.is_file():
                raise HTTPException(503,'personal_cloud_workflow_overlay_unavailable')
            replacements.append({'Type':'bind','Source':str(source),'Target':target,'ReadOnly':True})
        targets={item['Target'] for item in replacements}
        mounts=[item for item in mounts if item['Target'] not in targets]+replacements
    if RUNTIME_FOLLOWUP_OVERLAY_DIR:
        root=Path(RUNTIME_FOLLOWUP_OVERLAY_DIR);source=root/BRAND_OVERLAY_FILE
        if (root.is_symlink() or not root.is_dir() or source.is_symlink() or not source.is_file()
            or source.stat().st_size>2*1024*1024
            or hashlib.sha256(source.read_bytes()).hexdigest()!=RUNTIME_FOLLOWUP_BRAND_SHA256):
            raise HTTPException(503,'personal_cloud_followup_brand_unavailable')
        mounts=[item for item in mounts if item['Target']!=BRAND_OVERLAY_TARGET]
        mounts.append({'Type':'bind','Source':str(source),'Target':BRAND_OVERLAY_TARGET,'ReadOnly':True})
    if FAST_ENTRY_OVERLAY_DIR:
        root=Path(FAST_ENTRY_OVERLAY_DIR)
        if root.is_symlink() or not root.is_dir():raise HTTPException(503,'personal_cloud_fast_entry_overlay_unavailable')
        verified=[]
        for filename,target,digest,size in FAST_ENTRY_NATIVE_FILES:
            source=root/filename
            if (source.is_symlink() or not source.is_file() or source.stat().st_size!=size
                or hashlib.sha256(source.read_bytes()).hexdigest()!=digest):
                raise HTTPException(503,'personal_cloud_fast_entry_overlay_unavailable')
            verified.append({'Type':'bind','Source':str(source),'Target':target,'ReadOnly':True})
        targets={item['Target'] for item in verified}
        mounts=[item for item in mounts if item['Target'] not in targets]+verified
    return {
      'Image':selected_image,'User':'1000:1000','WorkingDir':'/workspace',
      'Env':['CLOUD_ROUTE_TOKEN='+route_token,'CLOUD_MODEL_LEASE='+body.lease,
             'CLOUD_MODEL_BASE_URL='+MODEL_BASE]+catalog_env,
      'Labels':{LABEL:INSTANCE,'com.megmeet.personal-cloud.owner':owner,'com.megmeet.personal-cloud.runtime':runtime},
      'HostConfig':{'NetworkMode':NETWORK,'ReadonlyRootfs':True,'CapDrop':['ALL'],
        'SecurityOpt':['no-new-privileges:true'],'Memory':HOST_MEMORY_BYTES,'MemorySwap':HOST_MEMORY_BYTES,
        'NanoCpus':1000000000,'PidsLimit':512,'Init':True,'AutoRemove':False,
        'Tmpfs':{'/tmp':'rw,nosuid,noexec,size=134217728,uid=1000,gid=1000'},
        'LogConfig':{'Type':'json-file','Config':{'max-size':'5m','max-file':'2'}},
        'Mounts':mounts},
      'NetworkingConfig':{'EndpointsConfig':{NETWORK:{'Aliases':[name]}}}}


async def remove(record):
    info=await docker('GET','/containers/'+record['container_id']+'/json',missing_ok=True)
    if info is None:return
    if info['Config'].get('Labels',{}).get(LABEL)!=INSTANCE:raise HTTPException(503,'manager_ownership_mismatch')
    await docker('DELETE','/containers/'+record['container_id']+'?force=true&v=false')


async def worker_control(record,action):
    """Private fixed routes; no employee input becomes an upstream URL."""
    if action not in ('activity','idle-release','idle-resume'):raise ValueError('invalid worker control')
    async with httpx.AsyncClient(timeout=12,trust_env=False) as client:
        response=await client.request('GET' if action=='activity' else 'POST',record['origin']+'/__cloud/'+action,
            headers={'Authorization':'Bearer '+record['route_token']})
        if response.status_code!=200:raise HTTPException(503,'personal_cloud_worker_state_unavailable')
        value=response.json()
        if (not isinstance(value,dict) or set(value)!={'protocol','busy','draining'}
            or type(value.get('protocol')) is not int or value.get('protocol')!=1
            or type(value.get('busy')) is not bool or type(value.get('draining')) is not bool):
            raise HTTPException(503,'personal_cloud_worker_state_unavailable')
        return value


async def resume_host(record):
    if not record.get('release_pending'):return
    value=await worker_control(record,'idle-resume')
    if value['draining']:raise HTTPException(503,'personal_cloud_worker_resume_failed')
    record.pop('release_pending',None)


def observed_activity(record,busy,now=None):
    now=time.time() if now is None else now
    # Real work counts as activity throughout its run, and the first observed
    # completion starts a fresh idle window. Polling by itself never touches it.
    if busy or record.get('was_busy',False):record['work_touched']=now
    record['was_busy']=busy


def last_activity(record):
    return max(record['touched'],record.get('work_touched',0))


def prune_presence(record,now):
    clients=record.setdefault('clients',{})
    expired=[key for key,until in clients.items() if until<=now]
    last_expiry=max((clients[key] for key in expired),default=0)
    for key in expired:clients.pop(key,None)
    if expired and not clients:record['closed_at']=max(record.get('closed_at') or 0,last_expiry)
    return clients


def update_presence(record,client_id,present,now=None):
    now=time.time() if now is None else now
    clients=prune_presence(record,now)
    if present:
        clients[client_id]=now+PRESENCE_SECONDS
        record['closed_at']=None
    elif client_id in clients:
        clients.pop(client_id)
        if not clients:record['closed_at']=now


def retained(record,now=None):
    """Process retention is independent of the short, revocable route lease."""
    now=time.time() if now is None else now
    if prune_presence(record,now):return True
    if 'closed_at' not in record:record['closed_at']=record['touched']
    if record['closed_at'] is None:record['closed_at']=now
    closed_at=record.get('closed_at',record['touched'])
    return now-max(closed_at,record.get('work_touched',0),record['touched'])<CLOSED_SECONDS


def activity_ticket(record):
    # Called only under admission lock. A later sample invalidates a stale
    # response before it can clear a newer busy observation or remove a host.
    record['_activity_sample'] = record.get('_activity_sample', 0) + 1
    return record['_activity_sample']


def activity_liveness(record):
    # A stopped/missing Docker observation is only diagnostic. A successful
    # reopen/renew/touch while that response was in flight invalidates it,
    # even when the record and container ID themselves did not change.
    keys=('container_id','origin','route_token','lease','catalog_hash',
          'expires_at','touched','work_touched','was_busy','release_pending','closed_at')
    return tuple((key,record.get(key)) for key in keys)


async def reap_once():
    # A cancelled request or a Docker cleanup failure keeps its reservation.
    # Retry cleanup independently, so neither a slow removal nor a cold ready
    # wait holds the shared admission lock or delays another warm employee.
    for owner,record in list(starting.items()):
        if record.get('_cleanup_pending'):schedule_start_cleanup(owner,record)
    for owner in list(records):
        await asyncio.sleep(0)
        async with lock:
            record=records.get(owner)
            if record is None:continue
            ticket=activity_ticket(record)
            liveness=activity_liveness(record)
        # Read-only activity and failure diagnostics do not hold the global
        # admission lock. No decision below is valid for a replaced generation.
        observed=None;info=None;inspected=False
        try:
            observed=await worker_control(record,'activity')
        except Exception:
            try:
                info=await docker('GET','/containers/'+record['container_id']+'/json',missing_ok=True)
                inspected=True
            except Exception:pass
        async with lock:
            if records.get(owner) is not record or record.get('_activity_sample')!=ticket:continue
            now=time.time()
            if observed is None:
                if activity_liveness(record)!=liveness:continue
                if inspected and info is None:records.pop(owner,None)
                elif (inspected and isinstance(info,dict) and info.get('Config',{}).get('Labels',{}).get(LABEL)==INSTANCE
                      and info.get('State',{}).get('Running') is False):
                    try:await remove(record);records.pop(owner,None)
                    except Exception:pass
                continue
            observed_activity(record,observed['busy'],now)
            if observed['busy']:continue
            # Closing all clients starts a two-hour warm window; lease expiry
            # only prevents access, not premature destruction of a warm host.
            if retained(record,now):continue
            # Keep the existing exclusive drain/resume/delete fence. An early
            # idle sample is never sufficient authorization to remove a host.
            try:
                value=await worker_control(record,'idle-release')
                record['release_pending']=value['draining']
                if value['busy']:
                    observed_activity(record,True);continue
                if not value['draining']:continue
                if retained(record):
                    await resume_host(record);continue
                await remove(record)
                records.pop(owner,None)
            except Exception:
                record['release_pending']=True
                try:await resume_host(record)
                except Exception:pass


async def reap():
    while True:
        await asyncio.sleep(10)
        await reap_once()


@asynccontextmanager
async def lifespan(app):
    global image_id,canary_image_id
    info=await docker('GET','/images/'+IMAGE+'/json');image_id=info['Id']
    if WORKER_RUNTIME in (REVIEWED_RUNTIME,NATIVE021_RUNTIME) and (image_id!=IMAGE
        or (info.get('Config',{}).get('Labels') or {}).get('com.megmeet.dsh.runtime')!=WORKER_RUNTIME):
        raise RuntimeError('reviewed runtime image identity changed')
    if CANARY_IMAGE:
        info=await docker('GET','/images/'+CANARY_IMAGE+'/json')
        if info.get('Id')!=CANARY_IMAGE:raise RuntimeError('canary pinned image identity changed')
        canary_image_id=info['Id']
    network=await docker('GET','/networks/'+NETWORK)
    if not network.get('Internal'):raise RuntimeError('worker network must be internal')
    # Restart recovery: only owned workers, volumes deliberately retained.
    filters=json.dumps({'label':[LABEL+'='+INSTANCE]},separators=(',',':'))
    from urllib.parse import quote
    for item in await docker('GET','/containers/json?all=true&filters='+quote(filters)):
        await remove({'container_id':item['Id']})
    task=asyncio.create_task(reap())
    try:yield
    finally:
        task.cancel()
        jobs=list(migrations.values())+list(startup_cleanups.values())
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)


app=FastAPI(lifespan=lifespan,docs_url=None,openapi_url=None)


@app.middleware('http')
async def authenticated_bounded_body(request,call_next):
    # Authenticate before Pydantic/JSON parses worker-accessible request bodies.
    try:auth(request)
    except HTTPException as exc:return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>600*1024:return JSONResponse({'detail':'manager_request_too_large'},status_code=413)
    request._body=bytes(body)
    return await call_next(request)


class OpenBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    lease:str=Field(pattern=r'^mcl_[A-Za-z0-9_-]{43}$')
    catalog:dict
    expires_at:int


def queue_admission(owner,lease,now=None,*,memory_ok=True):
    """Bounded FIFO admission. Only active polling preserves a waiting place."""
    now=time.time() if now is None else now
    for key, value in list(waiting.items()):
        if value['until']<=now:waiting.pop(key,None)
    previous=waiting.get(owner)
    if previous and previous['lease']!=lease:waiting.pop(owner,None)
    general_waiting=[key for key in waiting if key not in RESERVED_OWNERS]
    general_active=sum(key not in RESERVED_OWNERS for key in allocated_owners())
    policy_room=(owner in RESERVED_OWNERS or
        ((MAX_HOSTS==0 or general_active<MAX_HOSTS-len(RESERVED_OWNERS))
         and (not general_waiting or general_waiting[0]==owner)))
    if memory_ok and (MAX_HOSTS==0 or len(allocated_owners())<MAX_HOSTS) and policy_room:
        waiting.pop(owner,None);return None
    if owner not in waiting and len(waiting)>=QUEUE_LIMIT:
        raise HTTPException(429,'personal_cloud_queue_full')
    waiting[owner]={'lease':lease,'until':now+QUEUE_TTL}
    return {'status':'queued','position':list(waiting).index(owner)+1,'retry_after':5}


def available_memory_bytes():
    memory=Path('/proc/meminfo').read_text()
    matches=re.findall(r'^MemAvailable:\s+([0-9]+) kB$',memory,re.M)
    if len(matches)!=1:raise ValueError('memory availability unknown')
    return int(matches[0])*1024


async def memory_admission(owner):
    """Reserve future worker growth, not just the current small RSS sample.

    MemAvailable already includes reclaimable cache. Only anonymous/RSS bytes
    are credited against each existing worker's fixed memory commitment.
    Unknown statistics fail closed, while a zero policy preserves old tests.
    Caller holds lock through container creation. Starting workers reserve a
    full commitment until publishing their ready record, so admission cannot
    oversell capacity while readiness waits run outside the shared lock.
    """
    if not MIN_AVAILABLE_MIB:return True
    try:
        available=available_memory_bytes()
        future=HOST_MEMORY_BYTES*(len(migrations)+len(starting))
        async with asyncio.timeout(8):
            for record in records.values():
                info=await docker('GET','/containers/'+record['container_id']+'/stats?stream=false&one-shot=true')
                stats=info['memory_stats']['stats']
                used=stats.get('anon',stats.get('rss'))
                if type(used) is not int or used<0 or used>HOST_MEMORY_BYTES:return False
                future+=HOST_MEMORY_BYTES-used
        available=min(available,available_memory_bytes())
        reserved_missing=sum(key not in allocated_owners() for key in RESERVED_OWNERS)
        reserved_extra=reserved_missing if owner not in RESERVED_OWNERS else max(0,reserved_missing-1)
        required=MIN_AVAILABLE_MIB*1024*1024+future+HOST_MEMORY_BYTES*(1+reserved_extra)
        return available>=required
    except Exception:return False


async def prepare_open(owner,body,request,attempt):
    auth(request);name=owner_key(owner)
    require_runtime_activation()
    if not time.time()<body.expires_at<=time.time()+905 or len(json.dumps(body.catalog))>524288:
        raise HTTPException(400,'invalid_host_lease')
    async with lock:
        if owner in starting:return migration_queued()
        if owner in migrations:return migration_queued()
        old=records.get(owner)
        catalog_hash=hashlib.sha256(json.dumps(body.catalog,sort_keys=True).encode()).hexdigest()
        # The employee owns the process and volumes. A second browser/desktop
        # login must neither evict it nor replace a task's authorizing client.
        if old:
            await resume_host(old)
            info=await docker('GET','/containers/'+old['container_id']+'/json',missing_ok=True)
            if info:
                labels=info.get('Config',{}).get('Labels') or {}
                if labels.get(LABEL)!=INSTANCE or labels.get('com.megmeet.personal-cloud.owner')!=owner:
                    raise HTTPException(503,'manager_ownership_mismatch')
            ready=False
            if info and info.get('State',{}).get('Running'):
                try:
                    async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
                        response=await client.get(old['origin']+'/__cloud/ready',headers={'Authorization':'Bearer '+old['route_token']})
                        ready=response.status_code==200
                except httpx.HTTPError:pass
            if ready:
                if old['catalog_hash']!=catalog_hash:
                    async with httpx.AsyncClient(timeout=35,trust_env=False) as client:
                        response=await client.post(old['origin']+'/__cloud/catalog',
                            headers={'Authorization':'Bearer '+old['route_token']},json={'catalog':body.catalog})
                        if response.status_code!=200 or response.json().get('status')!='updated':
                            raise HTTPException(503,'personal_cloud_catalog_update_failed')
                    old['catalog_hash']=catalog_hash
                old['expires_at']=max(old['expires_at'],body.expires_at);old['touched']=time.time();return public(old)
            if info is None:records.pop(owner,None);old=None
        if old:await remove(old);records.pop(owner,None)
        queued=queue_admission(owner,body.lease,memory_ok=await memory_admission(owner))
        if queued:return JSONResponse(queued,status_code=202,headers={'Retry-After':'5'})
        route_token=secrets.token_urlsafe(32)
        state_identity=None;files_identity=None
        for suffix in ('state','files'):
            volume=await docker('POST','/volumes/create',{'Name':name+'-'+suffix,'Labels':{LABEL:INSTANCE,'com.megmeet.personal-cloud.owner':owner}})
            # Docker's create response for an existing volume may omit labels.
            # Verify the persisted resource, never infer ownership from request.
            volume=await docker('GET','/volumes/'+name+'-'+suffix)
            labels=volume.get('Labels') or {}
            if labels.get(LABEL)!=INSTANCE or labels.get('com.megmeet.personal-cloud.owner')!=owner:
                raise HTTPException(503,'personal_cloud_volume_ownership_mismatch')
            if suffix=='state':
                created=volume.get('CreatedAt')
                if not isinstance(created,str) or not created:raise HTTPException(503,'personal_cloud_volume_identity_unavailable')
                state_identity=(worker_selection(owner)[0],volume.get('Name'),created)
            else:
                files_identity=(volume.get('Name'),volume.get('CreatedAt'))
        if WORKER_RUNTIME==NATIVE021_RUNTIME:
            if (files_identity[0]!=name+'-files' or not isinstance(files_identity[1],str) or not files_identity[1]):
                raise HTTPException(503,'personal_cloud_volume_identity_unavailable')
            require_native021_history_receipt(owner,state_identity,files_identity)
            migration_ready[owner]=state_identity
        elif migration_ready.get(owner)!=state_identity:
            if migration_failures.get(owner)==state_identity:
                reason=migration_failure_codes.get(owner,'MAINTENANCE_REFUSED')
                raise HTTPException(503,'personal_cloud_history_upgrade_refused'+(':'+reason if reason!='MAINTENANCE_REFUSED' else ''))
            migrations[owner]=asyncio.create_task(migrate_owner(owner,state_identity))
            return migration_queued()
        record={'container_id':name,'origin':'http://'+name+':3084','route_token':route_token,
                'lease':body.lease,'catalog_hash':catalog_hash,'expires_at':body.expires_at,'touched':time.time(),
                'clients':{},'closed_at':time.time(),'_start_attempt':attempt}
        # Reserve before awaiting create: cancellation after Docker accepted the
        # name still has a known, owner-labelled cleanup target and commitment.
        config=container_config(owner,body,route_token)
        starting[owner]=record
        record['_creation_task']=asyncio.create_task(create_start_container(name,config,record))
        # If the HTTP client disconnects, Docker may still accept its create.
        # Shield the one creation task; cleanup must await that result before
        # checking absence, rather than freeing a slot before a late create.
        await asyncio.shield(record['_creation_task'])
        return record


async def create_start_container(name,config,record):
    result=await docker('POST','/containers/create?name='+name,config)
    record['container_id']=result['Id']
    record['_creation_completed']=True


async def clean_failed_start(owner,record):
    try:
        try:await asyncio.shield(record['_creation_task'])
        except Exception:pass
        info=await docker('GET','/containers/'+record['container_id']+'/json',missing_ok=True)
        if info is not None:
            labels=info.get('Config',{}).get('Labels') or {}
            if labels.get(LABEL)!=INSTANCE or labels.get('com.megmeet.personal-cloud.owner')!=owner:
                raise HTTPException(503,'manager_ownership_mismatch')
            await remove(record)
        elif not record.get('_creation_completed'):
            # A transport failure leaves Docker's create result unknown. An
            # absent snapshot alone cannot prove a late accepted create will
            # never appear. Retain the reservation; future reaps can remove an
            # observed owned container, otherwise operator review is required.
            return
        async with lock:
            if starting.get(owner) is record:starting.pop(owner,None)
    except Exception:
        # Never free memory/slot accounting after an unconfirmed removal. The
        # reaper retries; same-owner opens remain preparing in the meantime.
        pass
    finally:
        if startup_cleanups.get(owner) is asyncio.current_task():startup_cleanups.pop(owner,None)


def schedule_start_cleanup(owner,record):
    if owner not in startup_cleanups:
        startup_cleanups[owner]=asyncio.create_task(clean_failed_start(owner,record))
    return startup_cleanups[owner]


def entry_timing(stage,started):
    # No owner, lease, route token, address, catalog, or model is logged.
    if stage not in ('prepare_open','startup_slot_wait','container_start','native_ready','worker_start_ready','open_total'):return
    try:
        print(json.dumps({'schema':'personal-cloud-entry-timing-r54-v1','stage':stage,
            'duration_ms':max(0,round((time.monotonic()-started)*1000))}),flush=True)
    except (OSError,ValueError):pass


async def wait_start_ready(record,*,clock=time.monotonic,sleep=asyncio.sleep):
    """Keep all 80 original retry windows; probe faster inside early windows.

    Each primary probe still receives its original one-second retry window.
    Supplemental probes in the first ten seconds improve successful entry
    detection without reducing that minimum startup allowance. Slow network
    probes may extend a window, never subtract from the remaining 80 windows.
    """
    started=clock()
    async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
        async def probe():
            try:
                response=await client.get(record['origin']+'/__cloud/ready',headers={'Authorization':'Bearer '+record['route_token']})
                if response.status_code==200:
                    record['native_workspace_id']=response.json()['native_workspace_id'];return True
            except httpx.HTTPError:pass
            return False
        for poll in range(80):
            if await probe():return
            retry_until=clock()+1
            while clock()<retry_until:
                remaining=retry_until-clock()
                if clock()-started>=10:
                    await sleep(remaining);break
                await sleep(min(0.2,remaining))
                if clock()<retry_until and clock()-started<10 and await probe():return
        raise HTTPException(503,'personal_cloud_start_timeout')


@app.post('/v1/hosts/{owner}/open')
async def open_host(owner:str,body:OpenBody,request:Request):
    started=time.monotonic();attempt=object();phase=started
    try:
        result=await prepare_open(owner,body,request,attempt)
        entry_timing('prepare_open',phase)
        if not isinstance(result,dict) or result.get('_start_attempt') is not attempt:return result
        record=result;phase=time.monotonic()
        # No shared admission lock while native DSH, plugins and workspace load.
        async with startup_slots:
            entry_timing('startup_slot_wait',phase)
            step=time.monotonic()
            try:await docker('POST','/containers/'+record['container_id']+'/start')
            finally:entry_timing('container_start',step)
            step=time.monotonic()
            try:await wait_start_ready(record)
            finally:entry_timing('native_ready',step)
        entry_timing('worker_start_ready',phase)
        async with lock:
            if starting.get(owner) is not record:raise HTTPException(503,'personal_cloud_start_generation_changed')
            records[owner]=record;starting.pop(owner,None)
            record.pop('_start_attempt',None)
            record.pop('_creation_task',None);record.pop('_creation_completed',None)
            return public(record)
    except BaseException:
        record=starting.get(owner)
        if record is not None and record.get('_start_attempt') is attempt:
            record['_cleanup_pending']=True
            cleanup=schedule_start_cleanup(owner,record)
            try:await asyncio.shield(cleanup)
            except BaseException:pass
        raise
    finally:entry_timing('open_total',started)


def public(record):
    # Only authenticated Gateway receives transport credentials, never desktop.
    return {key:record[key] for key in ('origin','route_token','native_workspace_id','expires_at')}


@app.get('/v1/status')
async def status(request:Request):
    auth(request)
    now=time.time()
    return {'status':'ready','active_hosts':len(allocated_owners()),'maximum_hosts':MAX_HOSTS,
        'waiting_hosts':sum(v['until']>now for v in waiting.values()),'queue_limit':QUEUE_LIMIT,
        'host_memory_limit_mib':1536,'idle_release_seconds':IDLE,
        'closed_retention_seconds':CLOSED_SECONDS,'presence_seconds':PRESENCE_SECONDS,
        'general_maximum_hosts':0 if MAX_HOSTS==0 else MAX_HOSTS-len(RESERVED_OWNERS),
        'host_slot_limit_enabled':MAX_HOSTS>0,'reserved_slots':len(RESERVED_OWNERS),
        'memory_guard_enabled':bool(MIN_AVAILABLE_MIB),'minimum_available_mib':MIN_AVAILABLE_MIB,
        'worker_runtime':WORKER_RUNTIME,'runtime_admission_enabled':runtime_activated()}


@app.post('/v1/hosts/{owner}/catalog')
async def update_catalog(owner:str,body:OpenBody,request:Request):
    """Refresh only an already-owned live generation; never launch or replace it."""
    auth(request);owner_key(owner)
    if not time.time()<body.expires_at<=time.time()+905 or len(json.dumps(body.catalog))>CATALOG_MAX_BYTES:
        raise HTTPException(400,'invalid_host_lease')
    async with lock:
        record=records.get(owner)
        if not record or record['expires_at']<=time.time():
            raise HTTPException(409,'personal_cloud_open_required')
        await resume_host(record)
        info=await docker('GET','/containers/'+record['container_id']+'/json',missing_ok=True)
        if not info or not info.get('State',{}).get('Running'):
            raise HTTPException(409,'personal_cloud_open_required')
        labels=info.get('Config',{}).get('Labels') or {}
        if labels.get(LABEL)!=INSTANCE or labels.get('com.megmeet.personal-cloud.owner')!=owner:
            raise HTTPException(503,'manager_ownership_mismatch')
        try:
            async with httpx.AsyncClient(timeout=35,trust_env=False) as client:
                response=await client.post(record['origin']+'/__cloud/catalog',
                    headers={'Authorization':'Bearer '+record['route_token']},json={'catalog':body.catalog})
                if response.status_code!=200 or response.json().get('status')!='updated':
                    raise HTTPException(503,'personal_cloud_catalog_update_failed')
        except (httpx.HTTPError,ValueError,AttributeError):
            raise HTTPException(503,'personal_cloud_catalog_update_failed') from None
        # A failed or partial native update must not advance this acknowledgement.
        # Retry sends the full authoritative catalog again, preserving revocations.
        record['catalog_hash']=hashlib.sha256(json.dumps(body.catalog,sort_keys=True).encode()).hexdigest()
        record['expires_at']=max(record['expires_at'],body.expires_at);record['touched']=time.time()
        return public(record)


@app.get('/v1/hosts/{owner}')
async def host(owner:str,request:Request,touch:bool=False):
    auth(request);owner_key(owner)
    async with lock:
        record=records.get(owner)
        if not record or record['expires_at']<=time.time():raise HTTPException(409,'personal_cloud_open_required')
        await resume_host(record)
        if touch:record['touched']=time.time()
        return public(record)


@app.get('/v1/hosts/{owner}/activity')
async def host_activity(owner:str,request:Request):
    auth(request);owner_key(owner)
    async with lock:
        record=records.get(owner)
        if not record or record['expires_at']<=time.time():raise HTTPException(409,'personal_cloud_open_required')
        ticket=activity_ticket(record)
    busy=None
    try:busy=(await worker_control(record,'activity'))['busy']
    except Exception:pass
    async with lock:
        if records.get(owner) is not record or record['expires_at']<=time.time():
            raise HTTPException(409,'personal_cloud_open_required')
        if record.get('_activity_sample')==ticket:
            if busy is not None:observed_activity(record,busy)
        else:
            # Overlapping browser/reaper polls must not apply out of order.
            busy=None
        return {'status':'ready','idle_seconds':IDLE,'idle_for_seconds':max(0,int(time.time()-last_activity(record))),'busy':busy}


class HeartbeatBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    lease:str=Field(pattern=r'^mcl_[A-Za-z0-9_-]{43}$')
    expires_at:int
    activity:bool=Field(default=True,strict=True)
    presence_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    present:bool=Field(default=True,strict=True)


async def ready_existing(owner,record):
    info=await docker('GET','/containers/'+record['container_id']+'/json',missing_ok=True)
    if info is None:raise HTTPException(409,'personal_cloud_open_required')
    labels=info.get('Config',{}).get('Labels') or {}
    if labels.get(LABEL)!=INSTANCE or labels.get('com.megmeet.personal-cloud.owner')!=owner:
        raise HTTPException(503,'manager_ownership_mismatch')
    if not info.get('State',{}).get('Running'):
        raise HTTPException(409,'personal_cloud_open_required')
    try:
        async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
            response=await client.get(record['origin']+'/__cloud/ready',headers={'Authorization':'Bearer '+record['route_token']})
            value=response.json() if response.status_code==200 else {}
            if value.get('status')!='ready' or value.get('native_workspace_id')!=record['native_workspace_id']:
                raise HTTPException(503,'personal_cloud_worker_unavailable')
    except (httpx.HTTPError,ValueError,AttributeError):
        raise HTTPException(503,'personal_cloud_worker_unavailable') from None


async def renew_existing(owner,body,request,*,verify_ready=False):
    auth(request);owner_key(owner)
    if not time.time()<body.expires_at<=time.time()+905:
        raise HTTPException(400,'invalid_host_lease')
    async with lock:
        record=records.get(owner)
        if not record:raise HTTPException(409,'personal_cloud_open_required')
        # A fresh gateway-authenticated client may renew an expired transport
        # lease. Never replace the original lease of already running tasks.
        if verify_ready or record['expires_at']<=time.time():await ready_existing(owner,record)
        await resume_host(record)
        record['expires_at']=max(record['expires_at'],body.expires_at)
        if body.activity:record['touched']=time.time()
        if body.presence_id:update_presence(record,body.presence_id,body.present)
        return public(record)


@app.post('/v1/hosts/{owner}/resume')
async def resume_existing(owner:str,body:HeartbeatBody,request:Request):
    require_runtime_activation()
    return await renew_existing(owner,body,request,verify_ready=True)


@app.post('/v1/hosts/{owner}/heartbeat')
async def heartbeat(owner:str,body:HeartbeatBody,request:Request):
    return await renew_existing(owner,body,request)


class StorageBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['inventory','trash','quarantine','restore','purge','wipe-workspace','delete-session','archived-sessions','migration-check','migration-apply','migration-rollback']
    sessionId:str|None=Field(default=None,max_length=200)
    digest:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    archivedDigest:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    trashId:str|None=Field(default=None,pattern=r'^[a-f0-9-]{36}$')


async def wipe_workspace(owner):
    """Caller holds lock and a durable Gateway deletion intent until completion.

    Preflight BOTH owned volumes before the first mutation. No force flag, no
    filesystem path, no helper with host mounts. Docker refuses referenced
    volumes, including a stopped container. Missing volumes make retries after
    a partial deletion idempotent; they must not cancel the Gateway intent.
    """
    name=owner_key(owner)
    if owner in allocated_owners():raise HTTPException(409,'personal_cloud_storage_busy')
    volumes=[]
    for suffix in ('state','files'):
        volume_name=name+'-'+suffix
        volume=await docker('GET','/volumes/'+volume_name,missing_ok=True)
        if volume is None:continue
        labels=volume.get('Labels') or {}
        if (volume.get('Name')!=volume_name or labels.get(LABEL)!=INSTANCE
                or labels.get('com.megmeet.personal-cloud.owner')!=owner):
            raise HTTPException(503,'personal_cloud_volume_ownership_mismatch')
        filters=quote(json.dumps({'volume':[volume_name]},separators=(',',':')))
        if await docker('GET','/containers/json?all=true&filters='+filters):
            raise HTTPException(409,'personal_cloud_storage_busy')
        volumes.append(volume_name)
    # No 409 is emitted after this point: a failure may follow a successful
    # first delete, so the caller must retain its committed deletion intent.
    for volume_name in volumes:
        await docker('DELETE','/volumes/'+volume_name,missing_ok=True)
    waiting.pop(owner,None)
    migration_ready.pop(owner,None);migration_failures.pop(owner,None);migration_failure_codes.pop(owner,None)
    return {'result':{'status':'deleted','scope':'workspace','stateDeleted':True,'filesDeleted':True}}


@app.post('/v1/storage/{owner}')
async def storage(owner:str,body:StorageBody,request:Request):
    """Exclusive offline maintenance; no caller-supplied image, path or volume."""
    auth(request);name=owner_key(owner);volume_name=name+'-state'
    require_runtime_activation()
    if body.action=='delete-session' and (not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',body.sessionId or '')
            or any(getattr(body,key) is not None for key in ('digest','archivedDigest','trashId'))):
        raise HTTPException(400,'personal_cloud_storage_arguments')
    if body.action.startswith('migration-') and any(value is not None for key,value in body.model_dump().items() if key!='action'):
        raise HTTPException(400,'personal_cloud_storage_arguments')
    async with lock:
        if body.action=='wipe-workspace':
            if any(value is not None for key,value in body.model_dump().items() if key!='action'):
                raise HTTPException(400,'personal_cloud_storage_arguments')
            return await wipe_workspace(owner)
        # Ordinary opens cannot race this fence. Never evict an active user.
        if owner in allocated_owners():raise HTTPException(409,'personal_cloud_storage_busy')
        volume=await docker('GET','/volumes/'+volume_name,missing_ok=True)
        if volume is None:
            if body.action=='delete-session':return {'result':{'status':'deleted','sessionId':body.sessionId,'alreadyAbsent':True}}
            if body.action=='archived-sessions':return {'result':{'sessionIds':[],'complete':True}}
            raise HTTPException(404,'personal_cloud_storage_absent')
        labels=volume.get('Labels') or {}
        if labels.get(LABEL)!=INSTANCE or labels.get('com.megmeet.personal-cloud.owner')!=owner:
            raise HTTPException(503,'personal_cloud_volume_ownership_mismatch')
        filters=quote(json.dumps({'volume':[volume_name]},separators=(',',':')))
        if await docker('GET','/containers/json?all=true&filters='+filters):
            raise HTTPException(409,'personal_cloud_storage_busy')
        payload=await run_history_helper(owner,body.model_dump(exclude_none=True))
        if body.action.startswith('migration-'):
            migration_ready.pop(owner,None);migration_failures.pop(owner,None);migration_failure_codes.pop(owner,None)
        elif body.action=='delete-session' and payload.get('result',{}).get('status')=='deleted':
            # A previously unfinished r19 delete may have blocked migration;
            # its completed offline retry permits a fresh admission attempt.
            migration_failures.pop(owner,None);migration_failure_codes.pop(owner,None)
        return payload


async def migrate_owner(owner,state_identity):
    """The migrations map reserves the owner and capacity until cleanup ends."""
    try:
        if WORKER_RUNTIME==NATIVE021_RUNTIME:
            raise HTTPException(503,'personal_cloud_runtime_snapshot_required')
        payload=await run_history_helper(owner,{'action':'migration-admit'})
        if payload.get('result')!={'status':'READY','runtime':worker_selection(owner)[2],'format':4}:
            raise HTTPException(503,'personal_cloud_history_upgrade_refused')
        async with lock:migration_ready[owner]=state_identity;migration_failures.pop(owner,None);migration_failure_codes.pop(owner,None)
    except asyncio.CancelledError:raise
    except Exception as error:
        code=error.detail.removeprefix('personal_cloud_storage_refused:') if isinstance(error,HTTPException) and isinstance(error.detail,str) else ''
        async with lock:
            migration_failures[owner]=state_identity
            migration_failure_codes[owner]=code if code in HISTORY_REFUSAL_CODES else 'MAINTENANCE_REFUSED'
    finally:
        async with lock:migrations.pop(owner,None)


async def run_history_helper(owner,body):
    # The old helper may transform/rollback generations and its Cmd does not
    # override the new worker image ENTRYPOINT. Require a separately reviewed
    # r65 maintenance path; never run the legacy helper against native021 data.
    if WORKER_RUNTIME==NATIVE021_RUNTIME:
        raise HTTPException(503,'personal_cloud_native021_maintenance_unreviewed')
    # Caller either holds the normal storage lock or owns migrations[owner].
    # Revalidate the exact volume and absence of even stopped references before
    # binding the owned state. Never bind a filesystem path or another volume.
    name=owner_key(owner);volume_name=name+'-state'
    volume=await docker('GET','/volumes/'+volume_name,missing_ok=True)
    labels=volume.get('Labels',{}) if volume else {}
    if (not volume or volume.get('Name')!=volume_name or labels.get(LABEL)!=INSTANCE
        or labels.get('com.megmeet.personal-cloud.owner')!=owner):raise HTTPException(503,'personal_cloud_volume_ownership_mismatch')
    filters=quote(json.dumps({'volume':[volume_name]},separators=(',',':')))
    if await docker('GET','/containers/json?all=true&filters='+filters):raise HTTPException(409,'personal_cloud_storage_busy')
    token=secrets.token_urlsafe(32);helper=name+'-maintenance-'+secrets.token_hex(4)
    # Container names allow more than DNS's 63 bytes per label.
    # Keep diagnostic ownership names, but route through a short alias.
    helper_alias='history-'+secrets.token_hex(8)
    mounts=[{'Type':'volume','Source':volume_name,'Target':'/state'}]
    selected_image,overlay_dir,runtime=worker_selection(owner)
    maintenance_dir=HISTORY_OVERLAY_DIR or overlay_dir
    maintenance_files=HISTORY_OVERLAY_FILES if HISTORY_OVERLAY_DIR else MAINTENANCE_OVERLAY_FILES
    if maintenance_dir:
        for filename in maintenance_files:
            source=Path(maintenance_dir)/filename
            if source.is_symlink() or not source.is_file():raise HTTPException(503,'personal_cloud_maintenance_overlay_unavailable')
            mounts.append({'Type':'bind','Source':str(source),'Target':'/opt/personal-cloud/'+filename,'ReadOnly':True})
    config={'Image':selected_image,'User':'1000:1000','WorkingDir':'/tmp',
      'Cmd':['node','/opt/personal-cloud/history-server.mjs'],
      'Env':['CLOUD_ROUTE_TOKEN='+token,'CLOUD_MAINTENANCE=exclusive-offline-v1','CLOUD_MIGRATION=session-v4-reviewed-v1'],
      'Labels':{LABEL:INSTANCE,'com.megmeet.personal-cloud.owner':owner},
      'HostConfig':{'NetworkMode':NETWORK,'ReadonlyRootfs':True,'CapDrop':['ALL'],
        'SecurityOpt':['no-new-privileges:true'],'Memory':512*1024*1024,'MemorySwap':512*1024*1024,
        'NanoCpus':500000000,'PidsLimit':32,
        'Tmpfs':{'/tmp':'rw,nosuid,noexec,size=16777216,uid=1000,gid=1000'},
        'LogConfig':{'Type':'json-file','Config':{'max-size':'1m','max-file':'1'}},
        'Mounts':mounts},
      'NetworkingConfig':{'EndpointsConfig':{NETWORK:{'Aliases':[helper_alias]}}}}
    result=await docker('POST','/containers/create?name='+helper,config)
    record={'container_id':result['Id']}
    try:
        await docker('POST','/containers/'+result['Id']+'/start')
        async with httpx.AsyncClient(timeout=65,trust_env=False) as client:
            headers={'Authorization':'Bearer '+token};origin='http://'+helper_alias+':3084'
            for _ in range(30):
                try:
                    ready=await client.get(origin+'/__cloud/ready',headers=headers,timeout=2)
                    if ready.status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.2)
            else:raise HTTPException(503,'personal_cloud_storage_start_failed')
            async with client.stream('POST',origin+'/__cloud/history',headers=headers,json=body) as response:
                content=bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>32*1024*1024:raise HTTPException(503,'personal_cloud_storage_limit')
                if response.status_code!=200:
                    raise HTTPException(409,'personal_cloud_storage_refused:'+history_refusal(content))
                payload=json.loads(content)
                if 'result' not in payload:raise HTTPException(503,'personal_cloud_storage_response')
                return payload
    except (httpx.HTTPError,ValueError):raise HTTPException(503,'personal_cloud_storage_unavailable') from None
    finally:await remove(record)
