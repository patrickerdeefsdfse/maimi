"""Bounded, allowlisted r38 Docker metadata projection for r40 review.

No predecessor Product constructors and no full Docker inspect/environment.
collect(run) uses two project-scoped ps calls, one container inspect batch and
one linux/amd64 image metadata batch. It returns the legacy snapshot/policy
shapes; callers must refresh it at phase boundaries, never cache across changes.
run(label, argv, timeout) must return bytes and redact command failures.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

class Stop(ValueError):
    pass

HEX = re.compile(r'[a-f0-9]{64}')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}')
SERVICES = frozenset(('gateway', 'personal-cloud-manager', 'retention-worker',
    'downloads', 'postgres', 'redis', 'office-service', 'db-migrate'))
PROJECTS = {'megmeet-desktop-server': SERVICES - {'downloads'},
            'megmeet-desktop-distribution': frozenset({'downloads'})}
PUBLIC_ENV = (
    'MAIMI_LOGISTICS_ENABLED','MAIMI_LOGISTICS_KUAIDI100_ENABLED',
    'MAIMI_LOGISTICS_MES_USER_CODE','MAIMI_LOGISTICS_KUAIDI100_MCP_ENABLED',
    'MAIMI_LOGISTICS_KUAIDI100_SINGLE_PROCESS','WEB_CONCURRENCY','UVICORN_WORKERS',
    'TMPDIR',
    'MAIMI_SESSION_TTL_SECONDS', 'PERSONAL_CLOUD_RETENTION_POLICY', 'MIGRATIONS_REQUIRED_VERSION',
    'WEB_SEARCH_PROVIDER', 'PLATFORM_FILE_ROOT', 'PERSONAL_CLOUD_WORKER_IMAGE', 'PERSONAL_CLOUD_MAX_ACTIVE_HOSTS',
    'PERSONAL_CLOUD_IDLE_SECONDS', 'PERSONAL_CLOUD_RESERVED_OWNERS', 'PERSONAL_CLOUD_MIN_AVAILABLE_MIB',
    'PERSONAL_CLOUD_WORKER_OVERLAY_DIR', 'PERSONAL_CLOUD_WORKER_MEMORY_MIB', 'PERSONAL_CLOUD_QUEUE_LIMIT',
    'MAIMI_PROJECTS_ENABLED', 'MAIMI_PROJECT_CARD_ASSETS_DIR', 'MAIMI_PROJECT_MEETING_ASSETS_DIR',
    'MAIMI_PROJECT_MEETING_MANIFEST_FILE', 'MAIMI_PROJECT_MONITOR_ORIGIN',
    'PERSONAL_CLOUD_WORKER_RUNTIME', 'PERSONAL_CLOUD_CANARY_WORKER_IMAGE',
    'PERSONAL_CLOUD_CANARY_OWNERS', 'PERSONAL_CLOUD_CANARY_OVERLAY_DIR',
    'DINGTALK_CLI_ENABLED', 'DINGTALK_CLI_BINARY', 'DINGTALK_CLI_STATE_DIR',
    'DINGTALK_CLI_CATALOG', 'DINGTALK_CLI_MAX_PROCESSES',
    'PERSONAL_CLOUD_HISTORY_OVERLAY_DIR', 'PERSONAL_CLOUD_WORKFLOW_OVERLAY_DIR',
    'PERSONAL_CLOUD_RUNTIME_FOLLOWUP_OVERLAY_DIR',
    'PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA',
    'PERSONAL_LOGIN_ACTIVATION_FILE','PERSONAL_LOGIN_RELEASE_SHA',
)
# Historical r24 returns equality booleans only for these fields, never an
# unexpected configured value. Keep that exact rule even in a batched query.
PHOTO_ENV = {'MAIMI_PROJECT_PHOTO_URL': 'http://10.3.1.26:8098/api/person/get',
             'MAIMI_PROJECT_PHOTO_TOKEN_FILE': '/run/secrets/megmeet-card-photo-token'}
LABELS = {
    'runtime42': ('com.megmeet.runtime42.', {'gateway','personal-cloud-manager'}),
    'runtime41': ('com.megmeet.runtime41.', {'gateway','personal-cloud-manager'}),
    'labels': ('com.megmeet.experience.', SERVICES),
    'r25': ('com.megmeet.project-runtime.', {'gateway'}),
    'r26': ('com.megmeet.dsh-cli.', {'gateway', 'personal-cloud-manager', 'retention-worker'}),
    'r27': ('com.megmeet.skill-results.', {'gateway', 'personal-cloud-manager'}),
    'hotfix': ('com.megmeet.open-hotfix.', {'gateway'}),
    'history': ('com.megmeet.history-admission.', {'personal-cloud-manager'}),
    'workflow': ('com.megmeet.workflow.', {'gateway', 'personal-cloud-manager'}),
    'search': ('com.megmeet.search.', {'gateway'}),
    'webvmware': ('com.megmeet.webvmware.', {'gateway'}),
    'runtimefollowup': ('com.megmeet.runtimefollowup.', {'gateway', 'personal-cloud-manager'}),
    'dashboard': ('com.megmeet.dashboard.', {'gateway'}),
    'remembered': ('com.megmeet.remembered-login.', {'gateway', 'personal-cloud-manager'}),
}
POLICY_FIELDS = {
    'image': '.Image', 'readonly': '.HostConfig.ReadonlyRootfs', 'privileged': '.HostConfig.Privileged',
    'cap_add': '.HostConfig.CapAdd', 'cap_drop': '.HostConfig.CapDrop', 'security_opt': '.HostConfig.SecurityOpt',
    'pid_mode': '.HostConfig.PidMode', 'ipc_mode': '.HostConfig.IpcMode', 'userns_mode': '.HostConfig.UsernsMode',
    'network_mode': '.HostConfig.NetworkMode', 'memory': '.HostConfig.Memory',
    'memory_swap': '.HostConfig.MemorySwap', 'nano_cpus': '.HostConfig.NanoCpus',
    'pids_limit': '.HostConfig.PidsLimit', 'shm_size': '.HostConfig.ShmSize',
    'restart_policy': '.HostConfig.RestartPolicy', 'user': '.Config.User', 'workdir': '.Config.WorkingDir',
    'cmd': '.Config.Cmd', 'entrypoint': '.Config.Entrypoint', 'port_bindings': '.HostConfig.PortBindings',
    'tmpfs': '.HostConfig.Tmpfs', 'devices': '.HostConfig.Devices', 'device_requests': '.HostConfig.DeviceRequests',
    'sysctls': '.HostConfig.Sysctls', 'readonly_paths': '.HostConfig.ReadonlyPaths',
    'masked_paths': '.HostConfig.MaskedPaths', 'ulimits': '.HostConfig.Ulimits',
    'cgroupns_mode': '.HostConfig.CgroupnsMode',
}

def _field(value):
    if value.startswith('.HostConfig.'):
        value = '(index .HostConfig ' + json.dumps(value.removeprefix('.HostConfig.')) + ')'
    return '{{json ' + value + '}}'

def _label(name):
    return _field('(index .Config.Labels ' + json.dumps(name) + ')')

def template():
    fields = {'id': _field('.Id'), 'image': _field('.Image'), 'running': _field('.State.Running'),
        'project': _label('com.docker.compose.project'), 'service': _label('com.docker.compose.service'),
        'oneoff': _label('com.docker.compose.oneoff')}
    fields['labels'] = '{' + ','.join(json.dumps(group) + ':{' + ','.join(
        json.dumps(key) + ':' + _label(prefix + key) for key in ('release', 'transaction', 'mode')) + '}'
        for group, (prefix, _) in LABELS.items()) + '}'
    policy = {key: _field(value) for key, value in POLICY_FIELDS.items()}
    policy['mounts'] = '[{{range $i,$m := .Mounts}}{{if $i}},{{end}}{"type":{{json $m.Type}},"source":{{json $m.Source}},"destination":{{json $m.Destination}},"rw":{{json $m.RW}}}{{end}}]'
    policy['network_names'] = '[{{range $name,$v := .NetworkSettings.Networks}}{{json $name}},{{end}}null]'
    conditions = ' '.join('(eq $k ' + json.dumps(key) + ')' for key in PUBLIC_ENV)
    policy['environment_rows'] = '[{{range .Config.Env}}{{$k := index (split . "=") 0}}{{if or ' + conditions + '}}{{json .}},{{end}}{{end}}null]'
    clauses = ''.join('{{if eq $k ' + json.dumps(key) + '}}{"name":' + json.dumps(key) + ',"matches":{{eq . ' + json.dumps(key+'='+value) + '}}},{{end}}' for key, value in PHOTO_ENV.items())
    policy['photo_environment'] = '[{{range .Config.Env}}{{$k := index (split . "=") 0}}' + clauses + '{{end}}null]'
    fields['policy'] = '{' + ','.join(json.dumps(key)+':'+value for key,value in policy.items()) + '}'
    return '{' + ','.join(json.dumps(key)+':'+value for key,value in fields.items()) + '}'

def _lines(raw, maximum=1048576):
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise Stop('FAST_METADATA_OUTPUT_INVALID')
    try:
        return [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise Stop('FAST_METADATA_OUTPUT_INVALID') from None

def discover(run, *, allow_missing_gateway=False, allow_missing_services=()):
    missing = set(allow_missing_services)
    if allow_missing_gateway:
        missing.add('gateway')
    if not missing <= {'gateway', 'personal-cloud-manager'}:
        raise Stop('FAST_METADATA_MISSING_SCOPE_INVALID')
    ids = {service: None for service in SERVICES}
    fmt = '{"id":{{json .ID}},"service":{{json (.Label "com.docker.compose.service")}},"oneoff":{{json (.Label "com.docker.compose.oneoff")}}}'
    for project, allowed in PROJECTS.items():
        rows = _lines(run('R40_SERVICE_IDS', ['docker', 'ps', '-a', '--no-trunc',
            '--filter', 'label=com.docker.compose.project='+project, '--format', fmt], 15), 131072)
        if len(rows) > 64:
            raise Stop('FAST_METADATA_SERVICE_LIMIT')
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'id','service','oneoff'}:
                raise Stop('FAST_METADATA_SERVICE_INVALID')
            service = row['service']
            if service not in allowed:
                continue  # Ancestor queries ignored non-target service labels.
            if not isinstance(row['id'],str) or not HEX.fullmatch(row['id']) or not isinstance(row['oneoff'],str) or row['oneoff'].lower() not in {'true','false'}:
                raise Stop('FAST_METADATA_SERVICE_INVALID')
            if row['oneoff'].lower() == 'true':
                continue
            if ids[service] is not None:
                raise Stop('FAST_METADATA_SERVICE_NOT_UNIQUE')
            ids[service] = row['id']
    if any(value is None and key not in missing for key,value in ids.items()):
        raise Stop('FAST_METADATA_SERVICE_MISSING')
    if len({value for value in ids.values() if value}) != sum(value is not None for value in ids.values()):
        raise Stop('FAST_METADATA_DUPLICATE_ID')
    return ids

def canonical_policy(value):
    required = set(POLICY_FIELDS) | {'mounts', 'network_names', 'environment_rows', 'photo_environment'}
    if not isinstance(value,dict) or set(value) != required:
        raise Stop('FAST_METADATA_POLICY_INVALID')
    result = deepcopy(value)
    result['environment'] = {}
    env = result.pop('environment_rows')
    if not isinstance(env,list) or len(env) > len(PUBLIC_ENV)+1:
        raise Stop('FAST_METADATA_ENV_INVALID')
    for row in env:
        if row is None:
            continue
        if not isinstance(row,str):
            raise Stop('FAST_METADATA_ENV_INVALID')
        key, sep, content = row.partition('=')
        if not sep or key not in PUBLIC_ENV or key in result['environment']:
            raise Stop('FAST_METADATA_ENV_INVALID')
        result['environment'][key] = content
    photo = result.pop('photo_environment')
    if not isinstance(photo,list) or len(photo) > len(PHOTO_ENV)+1:
        raise Stop('FAST_METADATA_PHOTO_INVALID')
    for row in photo:
        if row is None:
            continue
        if not isinstance(row,dict) or set(row) != {'name','matches'} or row['name'] not in PHOTO_ENV or row['matches'] is not True or row['name'] in result['environment']:
            raise Stop('FAST_METADATA_PHOTO_INVALID')
        result['environment'][row['name']] = PHOTO_ENV[row['name']]
    mounts = result['mounts']
    if not isinstance(mounts,list) or any(not isinstance(row,dict) or set(row) != {'type','source','destination','rw'} for row in mounts):
        raise Stop('FAST_METADATA_MOUNT_INVALID')
    if any(not isinstance(row['destination'],str) for row in mounts) or len({row['destination'] for row in mounts}) != len(mounts):
        raise Stop('FAST_METADATA_DUPLICATE_MOUNT')
    result['mounts'] = sorted(mounts,key=lambda row:row['destination'])
    if not isinstance(result['network_names'],list) or any(not isinstance(name,str) and name is not None for name in result['network_names']):
        raise Stop('FAST_METADATA_NETWORK_INVALID')
    result['network_names'] = sorted(name for name in result['network_names'] if name is not None)
    return result

def collect(run, *, allow_missing_gateway=False, allow_missing_services=()):
    ids = discover(run,allow_missing_gateway=allow_missing_gateway,
                   allow_missing_services=allow_missing_services)
    identifiers = sorted(value for value in ids.values() if value is not None)
    rows = _lines(run('R40_BATCH_PUBLIC_METADATA', ['docker','inspect','--type','container','--format',template(),*identifiers],30))
    if len(rows) != len(identifiers):
        raise Stop('FAST_METADATA_CONTAINER_COUNT')
    records = {}
    for row in rows:
        if not isinstance(row,dict) or set(row) != {'id','image','running','project','service','oneoff','labels','policy'}:
            raise Stop('FAST_METADATA_CONTAINER_INVALID')
        service = row['service']
        if (service not in SERVICES or row['id'] != ids[service] or service in records
                or row['project'] != ('megmeet-desktop-distribution' if service == 'downloads' else 'megmeet-desktop-server')
                or not isinstance(row['oneoff'],str) or row['oneoff'].lower() != 'false'
                or not isinstance(row['image'],str) or not IMAGE.fullmatch(row['image']) or type(row['running']) is not bool):
            raise Stop('FAST_METADATA_CONTAINER_IDENTITY_CHANGED')
        labels = row['labels']
        if not isinstance(labels,dict) or set(labels) != set(LABELS) or any(not isinstance(item,dict) or set(item) != {'release','transaction','mode'} or any(value is not None and not isinstance(value,str) for value in item.values()) for item in labels.values()):
            raise Stop('FAST_METADATA_LABEL_INVALID')
        policy = canonical_policy(row['policy'])
        if policy['image'] != row['image']:
            raise Stop('FAST_METADATA_IMAGE_CHANGED')
        records[service] = (row,policy)
    references = sorted({row['image'] for row,policy in records.values()})
    fmt = '{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}}}'
    images = _lines(run('R40_BATCH_PLATFORM_METADATA', ['docker','image','inspect','--platform','linux/amd64','--format',fmt,*references],30),131072)
    if len(images) != len(references) or any(not isinstance(row,dict) or set(row) != {'id','os','arch'} or row['os'] != 'linux' or row['arch'] != 'amd64' or not isinstance(row['id'],str) or not IMAGE.fullmatch(row['id']) for row in images):
        raise Stop('FAST_METADATA_IMAGE_PLATFORM_INVALID')
    # Docker image inspect renders each argument in the supplied order. Keep
    # raw container .Image for snapshot, normalize ONLY policy.image as r23 did.
    platforms = dict(zip(references,(row['id'] for row in images)))
    snapshot, policies = {}, {}
    for service in sorted(SERVICES):
        if service not in records:
            snapshot[service] = None
            continue
        row,policy = records[service]
        item = {'id':row['id'],'image':row['image'],'running':row['running']}
        for group,(_,applies) in LABELS.items():
            if service in applies:
                item[group] = row['labels'][group]
        snapshot[service] = item
        policy['image'] = platforms[row['image']]
        policies[row['id']] = policy
    return snapshot,policies

NGINX_ID = 'b5eb7a37866ad2453e577948086e357dfbb8a3b7a92f5022f0fd1b7e82526a8b'
NGINX_MAIN = '/etc/nginx/nginx.conf'
NGINX_SITE = '/etc/nginx/conf.d/default.conf'
NGINX_MIME = '/etc/nginx/mime.types'
NGINX_CONF = re.compile(r'/etc/nginx/conf\.d/[A-Za-z0-9_.-]+[.]conf')
NGINX_BASE_PINS = {NGINX_MAIN:'4dee6b0c7c8278f6a9b827261c25d3215c9ea8955e41ff7fe3eabdd5297edce7',
    NGINX_MIME:'6f95d1d7d75e3c072907d845622a69d23110d1266c16ff122b3109b8b21f3ae9'}

def nginx_identity(run):
    fmt = '{"id":{{json .Id}},"running":{{json .State.Running}},"networks":[{{range $name,$v := .NetworkSettings.Networks}}{{json $name}},{{end}}null],"mounts":[{{range $i,$m := .Mounts}}{{if $i}},{{end}}{"type":{{json $m.Type}},"source":{{json $m.Source}},"destination":{{json $m.Destination}}}{{end}}]}'
    rows = _lines(run('R40_NGINX_PUBLIC_METADATA',['docker','inspect','--type','container','--format',fmt,'nginx-https'],15),131072)
    if len(rows) != 1:
        raise Stop('FAST_METADATA_NGINX_IDENTITY_INVALID')
    row = rows[0]
    if not isinstance(row,dict) or set(row) != {'id','running','networks','mounts'} or row['id'] != NGINX_ID or row['running'] is not True or not isinstance(row['networks'],list) or 'common-net' not in row['networks'] or not isinstance(row['mounts'],list):
        raise Stop('FAST_METADATA_NGINX_IDENTITY_CHANGED')
    if any(not isinstance(m,dict) or set(m) != {'type','source','destination'} or not isinstance(m['destination'],str) for m in row['mounts']):
        raise Stop('FAST_METADATA_NGINX_MOUNTS_INVALID')
    required = [m for m in row['mounts'] if m['destination'] == '/etc/nginx/conf.d']
    if len(required) != 1 or required[0]['type'] != 'bind' or required[0]['source'] != '/root/nginx/conf.d' or any(m['destination'].startswith('/etc/nginx/conf.d/') for m in row['mounts']):
        raise Stop('FAST_METADATA_NGINX_BIND_CHANGED')
    return row['id']

def nginx_metadata(run, read_public):
    """Legacy metadata shape, but config leaves the container only as hashes.

    Requires existing sha256sum inside nginx (a missing utility stops safely).
    read_public must enforce regular/no-symlink/bounded host public-file reads.
    This function does not parse or alter any nginx route.
    """
    identifier = nginx_identity(run)
    command = 'set -eu; for f in /etc/nginx/conf.d/*.conf; do [ -f "$f" ] || exit 41; sha256sum "$f"; done; sha256sum /etc/nginx/nginx.conf /etc/nginx/mime.types'
    raw = run('R40_NGINX_PUBLIC_HASHES',['docker','exec',identifier,'sh','-c',command],15)
    if not isinstance(raw,bytes) or len(raw)>32768:
        raise Stop('FAST_METADATA_NGINX_HASH_INVALID')
    sources,names = {},[]
    try:
        for line in raw.decode('ascii').splitlines():
            match = re.fullmatch(r'([a-f0-9]{64})  (/[^\r\n]+)',line)
            if match is None:
                raise ValueError()
            digest,name = match.groups()
            if name in sources or not (NGINX_CONF.fullmatch(name) or name in NGINX_BASE_PINS):
                raise ValueError()
            sources[name] = digest
            if NGINX_CONF.fullmatch(name):
                names.append(name)
        if not 1 <= len(names) <= 64 or NGINX_SITE not in sources or any(sources.get(k)!=v for k,v in NGINX_BASE_PINS.items()):
            raise ValueError()
    except (ValueError,UnicodeError):
        raise Stop('FAST_METADATA_NGINX_CONFIG_INVALID') from None
    site = read_public(Path('/root/nginx/conf.d/default.conf'))
    if hashlib.sha256(site).hexdigest()!=sources[NGINX_SITE]:
        raise Stop('FAST_METADATA_NGINX_SITE_BIND_CHANGED')
    return {'identity':identifier,'names':names,'site_sha256':sources[NGINX_SITE], 'sources_sha256':sources}

def nginx_action(run, action):
    if action not in {'test','reload'}:
        raise Stop('FAST_METADATA_NGINX_ACTION_INVALID')
    identifier = nginx_identity(run)
    args = ['-t'] if action=='test' else ['-s','reload']
    run('R40_NGINX_'+action.upper(),['docker','exec','--user','0:0',identifier,'nginx',*args],30)
    if nginx_identity(run) != identifier:
        raise Stop('FAST_METADATA_NGINX_CHANGED_DURING_ACTION')

def nginx(run, *args):
    if args == ('-t',):
        return nginx_action(run, 'test')
    if args == ('-s', 'reload'):
        return nginx_action(run, 'reload')
    raise Stop('FAST_METADATA_NGINX_ACTION_INVALID')

# Exact inherited flock ordering through r38. These are process locks, not
# incomplete-publication markers. The caller must hold all file descriptors
# through the whole transaction, append its own r40 .apply.lock last, and use
# O_NOFOLLOW + owner/root regular/nlink/mode checks + LOCK_EX|LOCK_NB. Do not
# execute or instantiate any predecessor controller to acquire these locks.
LOCK_PATHS = tuple(Path(value) for value in (
    '/data/megmeet-shared-office-20260921-r12/.apply.lock',
    '/data/maimi-upload-20260921-r11/.r11-login-diagnostics-download-page-20260921.lock',
    '/data/megmeet-maimi-session-20260922-r13/.apply.lock',
    '/data/megmeet-cloud-access-20260922-r15/.apply.lock',
    '/data/megmeet-upload-gateway-20260923-r16/.apply.lock',
    '/data/megmeet-web-image-20260923-r17/.apply.lock',
    '/data/megmeet-web-files-idle-20260923-r18/.apply.lock',
    '/data/megmeet-retention-network-20260923-r19/.apply.lock',
    '/data/megmeet-dsh-upgrade-20260923-r20/.apply.lock',
    '/data/megmeet-web-entry-20260923-r21/.apply.lock',
    '/data/megmeet-desktop-distribution-0.9.0-r21-20260923/.apply.lock',
    '/data/megmeet-web-upload-20260924-r22/.apply.lock',
    '/data/megmeet-experience-skills-20260924-r23/.apply.lock',
    '/data/megmeet-project-routing-20260924-r24/.apply.lock',
    '/data/megmeet-project-runtime-20260924-r25/.apply.lock',
    '/data/megmeet-dsh-cli-20260928-r26/.apply.lock',
    '/data/megmeet-dsh-cli-20260928-r27/.apply.lock',
    '/data/maimi-upload-20260921-r11/.desktop-download-0.10.1-r27-20260928.lock',
    '/data/megmeet-open-hotfix-20260928-r28/.apply.lock',
    '/data/megmeet-dsh-history-20260928-r29/.apply.lock',
    '/data/megmeet-workflow-20260928-r30/.apply.lock',
    '/data/maimi-upload-20260921-r11/.desktop-download-0.11.0-r30-20260928.lock',
    '/data/maimi-upload-20260921-r11/.desktop-download-0.11.1-r32-20260929.lock',
    '/data/megmeet-search-20260929-r32/.apply.lock',
    '/data/megmeet-web-vmware-20260929-r34/.apply.lock',
    '/data/megmeet-runtime-followup-20260929-r35/.apply.lock',
    '/data/megmeet-vmware-dashboard-20260929-r38/.apply.lock',
))
