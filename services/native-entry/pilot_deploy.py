#!/usr/bin/env python3
"""Install the fixed two-real-account pilot through the existing bastion.

No business-row dumps, environment dumps, raw service logs or credentials are
printed. State volumes are never deleted. Nginx changes happen only after the
private services and real model transport pass checks.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path('/data/megmeet-ai-platform-new/personal-entry-pilot')
PROJECT = 'megmeet-personal-pilot'
LABEL = 'megmeet.personal-pilot'
VERSION = 'real-accounts-r1'
HOST_IMAGE = 'sha256:92df7271a90079aa38ce0d3f98f97cf088d7472f2bcd5e331f802bb4af4f8236'
ENTRY_IMAGE = 'sha256:464685944b999493c5d01288ebb29b10c0d8c3c59edec980099cdef3dfbdc1f7'
ENTRY_NAME = 'megmeet-native-personal-entry'
GATEWAY = 'megmeet-ai-platform-new-gateway-1'
SHARED_HOSTS = ('megmeet-ai-platform-new-dsh-web-host-1', 'megmeet-ai-new-r10-dsh-web-host-1')
CONF = Path('/root/nginx/conf.d/default.conf')
SECRET = Path('/data/ai-platform/secrets/model-tokenkey-api-key.gateway')
FILES = ('entry_app.py', 'host_launcher.py', 'pilot_deploy.py', 'pilot_nginx.py')
PUBLIC = 'https://platform-ai.megmeet.com'


class Failure(Exception):
    pass


def require(value, code):
    if not value:
        raise Failure(code)


def passed(code):
    print(code + ': PASS', flush=True)


def execute(args, *, data=None, timeout=30, allowed=False):
    result = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if not allowed:
        require(result.returncode == 0, 'COMMAND_FAILED_' + re.sub('[^A-Z]', '_', Path(args[0]).name.upper()))
    return result


def docker(*args, **kwargs):
    return execute(['docker', *args], **kwargs)


def inspect(name, template):
    return docker('inspect', '--format', template, name).stdout.strip()


def contained(path: Path):
    require(path.is_absolute(), 'ABSOLUTE_PATH_REQUIRED')
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'SYMLINK_PATH_REFUSED')


def atomic(path: Path, data: bytes, *, uid=0, gid=0, mode=0o600):
    contained(path)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def employee(value):
    require(isinstance(value, str) and 1 <= len(value) <= 100 and value.strip() == value
            and all(ord(char) >= 33 and ord(char) != 127 for char in value), 'EMPLOYEE_FORMAT_INVALID')
    return value


def owner(value):
    require(isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0, 'USER_UUID_INVALID')
    return value


def volume_names(user_id):
    return {kind: 'megmeet-personal-' + owner(user_id) + '-' + kind for kind in ('state', 'bootstrap')}


def configuration(bindings):
    return {'public_origin': PUBLIC, 'sso_url': 'https://platformai-test.megmeet.com/v1/auth/query-user',
            'tokenkey_url': 'https://tokenkey.megmeet.com/v1',
            'tokenkey_secret_file': '/run/secrets/tokenkey-api-key',
            'bindings': [{'user_id': item['user_id'], 'user_no': item['user_no'],
                          'upstream': 'http://personal-' + party + ':3084',
                          'bootstrap': '/run/megmeet-bootstrap/' + party + '/ready.json',
                          'model_token_file': '/run/megmeet-entry/model-' + party}
                         for party, item in zip(('a', 'b'), bindings)]}


def mount(source, target, *, volume=False, writable=False):
    result = {'type': 'volume' if volume else 'bind', 'source': str(source), 'target': target, 'read_only': not writable}
    if volume:
        result['volume'] = {'nocopy': True}
    else:
        result['bind'] = {'create_host_path': False}
    return result


def compose_document(bindings, base=BASE):
    common = {'user': '10001:10001', 'read_only': True, 'init': True,
              'restart': 'unless-stopped', 'cap_drop': ['ALL'],
              'security_opt': ['no-new-privileges:true'], 'cpus': '1.0',
              'pids_limit': 256, 'stop_grace_period': '30s',
              'tmpfs': ['/tmp:rw,exec,nosuid,nodev,mode=1777,size=268435456'],
              'labels': {LABEL: VERSION},
              'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '2'}},
              'entrypoint': ['python', '-B']}
    services, volumes = {}, {}
    entry_mounts = [mount(base / 'app', '/opt/megmeet/native-entry'),
                    mount(base / 'private', '/run/megmeet-entry'),
                    mount(SECRET, '/run/secrets/tokenkey-api-key')]
    for party, item in zip(('a', 'b'), bindings):
        names = volume_names(item['user_id'])
        for kind, name in names.items():
            volumes[party + '-' + kind] = {'external': True, 'name': name}
        entry_mounts.append(mount(party + '-bootstrap', '/run/megmeet-bootstrap/' + party, volume=True))
        services['host-' + party] = {**common, 'image': HOST_IMAGE,
            'container_name': 'megmeet-personal-host-' + party,
            'mem_limit': '1280m', 'memswap_limit': '1280m',
            'command': ['/opt/megmeet/native-entry/host_launcher.py'],
            'environment': {'MEGMEET_OWNER_ID': item['user_id'], 'MEGMEET_PUBLIC_ORIGIN': PUBLIC,
                            'DSH_PROFILE': 'native-personal',
                            'MEGMEET_MODEL_TOKEN_FILE': '/run/megmeet-config/model-token',
                            'MEGMEET_MODEL_BASE_URL': 'http://native-entry:8080/internal/model/v1'},
            'networks': {party: {'aliases': ['personal-' + party]}},
            'volumes': [mount(base / 'app', '/opt/megmeet/native-entry'),
                        mount(party + '-state', '/state', volume=True, writable=True),
                        mount(party + '-bootstrap', '/run/megmeet-bootstrap', volume=True, writable=True),
                        mount(base / 'private' / ('model-' + party), '/run/megmeet-config/model-token')],
            'healthcheck': {'test': ['CMD', 'python', '-B', '-c',
                "import json,socket; p=json.load(open('/run/megmeet-bootstrap/ready.json')); assert p['ready'] is True; socket.create_connection(('127.0.0.1',3084),2).close()"],
                'interval': '5s', 'timeout': '4s', 'start_period': '110s', 'retries': 4}}
    services['entry'] = {**common, 'image': ENTRY_IMAGE, 'container_name': ENTRY_NAME,
        'mem_limit': '384m', 'memswap_limit': '384m',
        'environment': {'MEGMEET_ENTRY_CONFIG': '/run/megmeet-entry/config.json'},
        'command': ['-m', 'uvicorn', 'entry_app:app', '--app-dir', '/opt/megmeet/native-entry',
                    '--host', '0.0.0.0', '--port', '8080', '--no-access-log'],
        'networks': {'a': {'aliases': ['native-entry']}, 'b': {'aliases': ['native-entry']}, 'common': {}},
        'extra_hosts': ['platformai-test.megmeet.com:10.4.0.202', 'tokenkey.megmeet.com:10.4.1.112'],
        'volumes': entry_mounts,
        'healthcheck': {'test': ['CMD', 'python', '-B', '-c',
            "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2).status==200"],
            'interval': '5s', 'timeout': '4s', 'start_period': '15s', 'retries': 4}}
    return {'name': PROJECT, 'services': services, 'volumes': volumes,
            'networks': {'a': {'internal': True, 'name': PROJECT + '-a', 'labels': {LABEL: VERSION}},
                         'b': {'internal': True, 'name': PROJECT + '-b', 'labels': {LABEL: VERSION}},
                         'common': {'external': True, 'name': 'common-net'}}}


DB_INIT = '''
import json,sys
from app.history_store import _connect
users=json.load(sys.stdin)['users']
assert len(users)==2 and len(set(users))==2
assert all(isinstance(x,str) and 1<=len(x)<=100 and x.strip()==x and all(ord(c)>=33 and ord(c)!=127 for c in x) for x in users)
out=[]
with _connect() as conn, conn.cursor() as cur:
    for value in users:
        cur.execute('INSERT INTO platform.users (external_user_no) VALUES (%s) ON CONFLICT (external_user_no) DO NOTHING',(value,))
        cur.execute('SELECT id FROM platform.users WHERE external_user_no=%s',(value,))
        out.append(str(cur.fetchone()['id']))
print(json.dumps(out))
'''


def lock():
    import fcntl
    contained(BASE)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(BASE.stat().st_uid == 0 and BASE.stat().st_mode & 0o022 == 0, 'INSTALL_DIRECTORY_OWNER')
    fd = os.open(BASE / '.deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1, 'LOCK_INVALID')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Failure('ANOTHER_PILOT_DEPLOY_IS_RUNNING') from None
    return fd


def compose(*args, **kwargs):
    # Empty env-file prevents the caller's .env from becoming a hidden input.
    return docker('compose', '--env-file', '/dev/null', '-f', str(BASE / 'pilot.compose.json'), '-p', PROJECT, *args, **kwargs)


def read_setup():
    path = BASE / 'setup.json'
    contained(path)
    require(path.is_file() and path.stat().st_uid == 0 and path.stat().st_mode & 0o077 == 0, 'SETUP_FILE_PRIVATE')
    data = json.loads(path.read_text())
    require(data.get('version') == VERSION and len(data['bindings']) == 2, 'SETUP_VERSION_INVALID')
    for item in data['bindings']:
        employee(item['user_no'])
        owner(item['user_id'])
    require(len({item['user_id'] for item in data['bindings']}) == 2 and
            len({item['user_no'] for item in data['bindings']}) == 2, 'SETUP_USERS_NOT_DISTINCT')
    return data


def prepare():
    here = Path(__file__).resolve().parent
    contained(SECRET)
    require(SECRET.is_file() and 0 < SECRET.stat().st_size <= 4096 and SECRET.stat().st_mode & 0o007 == 0,
            'EXISTING_GATEWAY_TOKENKEY_FILE_INVALID')
    require(docker('image', 'inspect', '--format', '{{.Id}}', HOST_IMAGE).stdout.strip() == HOST_IMAGE, 'HOST_IMAGE_MISSING')
    require(docker('image', 'inspect', '--format', '{{.Id}}', ENTRY_IMAGE).stdout.strip() == ENTRY_IMAGE, 'ENTRY_IMAGE_MISSING')
    if (BASE / 'setup.json').exists():
        setup = read_setup()
        print('PERSONAL_ACCOUNTS: PRESERVED (unchanged, not displayed)', flush=True)
    else:
        print('请输入两个参与验证的真实员工工号，输入不回显；不需要密码或Token。', flush=True)
        users = [employee(getpass.getpass('测试账号A工号: ')), employee(getpass.getpass('测试账号B工号: '))]
        require(users[0] != users[1], 'TWO_DIFFERENT_EMPLOYEES_REQUIRED')
        result = docker('exec', '-i', GATEWAY, 'python', '-B', '-c', DB_INIT,
                        data=json.dumps({'users': users}), timeout=30)
        identifiers = json.loads(result.stdout)
        require(isinstance(identifiers, list) and len(identifiers) == 2, 'DATABASE_IDENTITY_RESULT')
        setup = {'version': VERSION, 'bindings': [{'user_no': number, 'user_id': owner(identifier)}
                                                for number, identifier in zip(users, identifiers)]}
        require(identifiers[0] != identifiers[1], 'DATABASE_IDENTITIES_NOT_DISTINCT')
        atomic(BASE / 'setup.json', json.dumps(setup).encode())
        passed('PLATFORM_USER_IDS_REUSED_OR_CREATED')
    appdir, private = BASE / 'app', BASE / 'private'
    for directory in (appdir, private):
        contained(directory)
        directory.mkdir(exist_ok=True, mode=0o700)
    os.chown(private, 10001, 10001)
    os.chmod(private, 0o700)
    os.chmod(appdir, 0o755)
    for filename in FILES:
        raw = (here / filename).read_bytes()
        target = appdir / filename
        if target.exists():
            require(target.read_bytes() == raw, 'EXISTING_PILOT_CODE_DIFFERS_STOP_FOR_UPGRADE')
        else:
            atomic(target, raw, mode=0o644)
    for party in ('a', 'b'):
        path = private / ('model-' + party)
        contained(path)
        if not path.exists():
            atomic(path, secrets.token_urlsafe(48).encode(), uid=10001, gid=10001)
        require(path.stat().st_uid == 10001 and path.stat().st_mode & 0o077 == 0
                and re.fullmatch(rb'[A-Za-z0-9_-]{40,256}', path.read_bytes()), 'SCOPED_TOKEN_FILE_INVALID')
    config = configuration(setup['bindings'])
    atomic(private / 'config.json', json.dumps(config).encode(), uid=10001, gid=10001)
    atomic(BASE / 'pilot.compose.json', json.dumps(compose_document(setup['bindings']), indent=2).encode())
    compose('config', '--quiet')
    passed('PERSONAL_PILOT_CONFIG_PREPARED')


def ensure_volumes(bindings):
    for binding in bindings:
        for kind, name in volume_names(binding['user_id']).items():
            check = docker('volume', 'inspect', '--format', '{{json .Labels}}', name, allowed=True)
            if check.returncode:
                docker('volume', 'create', '--label', LABEL + '=' + VERSION,
                       '--label', 'megmeet.owner=' + binding['user_id'], name)
            labels = json.loads(docker('volume', 'inspect', '--format', '{{json .Labels}}', name).stdout) or {}
            require(labels.get(LABEL) == VERSION and labels.get('megmeet.owner') == binding['user_id'],
                    'EXISTING_VOLUME_OWNERSHIP_MISMATCH')
            # A labelled empty root-owned volume can be left by an interrupted
            # first create. Complete only that initialization; never chown data.
            script = """import os
p='/state'
s=os.stat(p)
if s.st_uid==0 and s.st_gid==0:
    assert not os.listdir(p)
    os.chmod(p,0o700)
    os.chown(p,10001,10001)
else:
    assert s.st_uid==10001 and s.st_gid==10001 and s.st_mode&0o777==0o700
"""
            docker('run', '--rm', '--pull', 'never', '--network', 'none', '--read-only', '--user', '0:0',
                   '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--security-opt', 'no-new-privileges',
                   '--memory', '128m', '--pids-limit', '32', '--entrypoint', 'python',
                   '--mount', 'type=volume,source=' + name + ',target=/state,volume-nocopy',
                   HOST_IMAGE, '-B', '-c', script)
    passed('PRIVATE_PERSISTENT_VOLUMES')


def owned_containers():
    for name in (ENTRY_NAME, 'megmeet-personal-host-a', 'megmeet-personal-host-b'):
        result = docker('inspect', '--format', '{{index .Config.Labels "' + LABEL + '"}}', name, allowed=True)
        require(result.returncode != 0 or result.stdout.strip() == VERSION, 'CONTAINER_NAME_IN_USE')


def private_networks(*, complete=False):
    for party in ('a', 'b'):
        result = docker('network', 'inspect', '--format',
                        '{"labels":{{json .Labels}},"members":{{json .Containers}},"internal":{{.Internal}}}',
                        PROJECT + '-' + party, allowed=True)
        if result.returncode:
            require(not complete, 'PRIVATE_NETWORK_MISSING')
            continue
        data = json.loads(result.stdout)
        labels = data['labels'] or {}
        require(data['internal'] is True and labels.get(LABEL) == VERSION
                and labels.get('com.docker.compose.project') == PROJECT, 'PRIVATE_NETWORK_OWNERSHIP_MISMATCH')
        expected = {ENTRY_NAME, 'megmeet-personal-host-' + party}
        actual = set()
        for cid, member in (data['members'] or {}).items():
            name = member['Name']
            require(name in expected, 'FOREIGN_CONTAINER_ON_PRIVATE_NETWORK')
            require(inspect(name, '{{.Id}}') == cid and inspect(name, '{{index .Config.Labels "' + LABEL + '"}}') == VERSION,
                    'PRIVATE_NETWORK_MEMBER_OWNER')
            actual.add(name)
        require(not complete or actual == expected, 'PRIVATE_NETWORK_MEMBER_SET')


def nginx_entry_dns():
    expected = json.loads(inspect(ENTRY_NAME, '{{json .NetworkSettings.Networks}}'))['common-net']['IPAddress']
    result = docker('exec', 'nginx-https', 'getent', 'hosts', ENTRY_NAME)
    addresses = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    require(addresses == {expected}, 'NGINX_ENTRY_DNS_AMBIGUOUS')


def wait_ready(name, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = inspect(name, '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}')
        if value == 'running healthy':
            return
        if value.startswith(('exited', 'dead')):
            break
        time.sleep(2)
    raise Failure('SERVICE_NOT_READY_' + ('ENTRY' if name == ENTRY_NAME else name[-1].upper()))


def runtime_checks():
    for name in (ENTRY_NAME, 'megmeet-personal-host-a', 'megmeet-personal-host-b'):
        require(inspect(name, '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}}') == '10001:10001 true', 'RUNTIME_USER_OR_ROOTFS')
        require(inspect(name, '{{json .HostConfig.PortBindings}}') in ('null', '{}'), 'PILOT_PORTS_MUST_NOT_BE_PUBLISHED')
        mounts = json.loads(inspect(name, '{{json .Mounts}}'))
        require(not any('docker.sock' in mount.get('Source', '') for mount in mounts), 'DOCKER_SOCKET_FORBIDDEN')
        if name != ENTRY_NAME:
            party = name[-1]
            require(set(json.loads(inspect(name, '{{json .NetworkSettings.Networks}}'))) == {PROJECT + '-' + party}, 'HOST_PRIVATE_NETWORK')
            require(not any(mount['Destination'] in {'/run/megmeet-entry', '/run/secrets/tokenkey-api-key'} for mount in mounts), 'HOST_COMPANY_SECRET_FORBIDDEN')
        else:
            require(set(json.loads(inspect(name, '{{json .NetworkSettings.Networks}}'))) == {PROJECT + '-a', PROJECT + '-b', 'common-net'}, 'ENTRY_NETWORK_SET')
    for party in ('a', 'b'):
        require(docker('network', 'inspect', '--format', '{{.Internal}}', PROJECT + '-' + party).stdout.strip() == 'true', 'HOST_NETWORK_NOT_INTERNAL')
    passed('RUNTIME_MOUNT_NETWORK_BOUNDARIES')


SSO_TRANSPORT = '''
import httpx
from pathlib import Path
assert 0<len(Path('/run/secrets/tokenkey-api-key').read_bytes())<=4096
try:
    with httpx.Client(verify=True,trust_env=False,timeout=8,follow_redirects=False) as c:
        r=c.post('https://platformai-test.megmeet.com/v1/auth/query-user')
    assert r.status_code in (200,400,401,403,500)
    print('SSO_TLS_TRANSPORT: PASS')
    print('SSO_NO_COOKIE_HTTP: '+str(r.status_code))
except Exception:
    print('SSO_TLS_TRANSPORT: FAIL')
    raise SystemExit(1)
'''
MODEL_CHECK = '''
import json,urllib.request
from pathlib import Path
token=Path('/run/megmeet-config/model-token').read_text().strip()
body=json.dumps({'model':'deepseek-flash-v4.1','messages':[{'role':'user','content':'Reply exactly PERSONAL_MODEL_OK'}],'stream':False}).encode()
try:
    request=urllib.request.Request('http://native-entry:8080/internal/model/v1/chat/completions',data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=100) as r:
        data=json.loads(r.read(1024*1024)); assert r.status==200
    text=data['choices'][0]['message']['content']
    assert isinstance(text,str) and text.strip()=='PERSONAL_MODEL_OK'
except Exception:
    print('PERSONAL_MODEL_CHAIN: FAIL'); raise SystemExit(1)
print('PERSONAL_MODEL_CHAIN: PASS')
'''


def public_status(path, *, json_service=None, expected=200):
    # Existing local certificate is localhost-only. This fixed loopback probe
    # carries no identity/secret and verifies routes, not public TLS validity.
    result = execute(['curl', '--silent', '--show-error', '--max-time', '8',
                      '--insecure', '--noproxy', '*',
                      '--resolve', 'platform-ai.megmeet.com:443:127.0.0.1',
                      '--write-out', '\n%{http_code}', PUBLIC + path])
    body, code = result.stdout.rsplit('\n', 1)
    require(code == str(expected), 'PUBLIC_ROUTE_UNEXPECTED_STATUS')
    if json_service:
        require(json.loads(body).get('service') == json_service, 'PUBLIC_ROUTE_WRONG_SERVICE')


def activate():
    from pilot_nginx import patch_nginx
    setup = read_setup()
    print('PERSONAL_PILOT_STAGE: PRECHECK', flush=True)
    contained(CONF)
    original = CONF.read_bytes()
    patched, metadata = patch_nginx(original.decode('utf-8'))
    require(inspect('nginx-https', '{{.State.Running}}') == 'true', 'NGINX_NOT_RUNNING')
    require('common-net' in json.loads(inspect('nginx-https', '{{json .NetworkSettings.Networks}}')), 'NGINX_COMMON_NET_MISSING')
    gateway_before = inspect(GATEWAY, '{{.Id}} {{.Image}} {{.State.StartedAt}}')
    shared = []
    for name in SHARED_HOSTS:
        result = docker('inspect', '--format', '{{.Id}} {{.State.Running}} {{index .Config.Labels "com.docker.compose.service"}}', name, allowed=True)
        if result.returncode:
            continue
        values = result.stdout.split()
        require(len(values) == 3 and values[2] == 'dsh-web-host', 'SHARED_HOST_ROLE_CHANGED')
        if values[1] == 'true':
            shared.append(values[0])
    owned_containers()
    private_networks()
    running = [docker('inspect', '--format', '{{.State.Running}}', name, allowed=True)
               for name in (ENTRY_NAME, 'megmeet-personal-host-a', 'megmeet-personal-host-b')]
    missing = [budget for result, budget in zip(running, (384, 1280, 1280))
               if result.returncode != 0 or result.stdout.strip() != 'true']
    if missing:
        available = re.search(r'(?m)^MemAvailable:\s+(\d+) kB$', Path('/proc/meminfo').read_text())
        require(available and int(available[1]) >= (sum(missing) + 640) * 1024, 'MEMORY_HEADROOM_BELOW_REQUIRED')
    ensure_volumes(setup['bindings'])
    compose('config', '--quiet')
    print('PERSONAL_PILOT_STAGE: START_ENTRY', flush=True)
    compose('up', '-d', '--no-deps', '--pull', 'never', 'entry', timeout=60)
    wait_ready(ENTRY_NAME, 30)
    result = docker('exec', ENTRY_NAME, 'python', '-B', '-c', SSO_TRANSPORT, timeout=15, allowed=True)
    for line in result.stdout.splitlines():
        if re.fullmatch(r'SSO_(?:TLS_TRANSPORT: (?:PASS|FAIL)|NO_COOKIE_HTTP: [1-5][0-9]{2})', line):
            print(line, flush=True)
    require(result.returncode == 0, 'ENTRY_SSO_TLS_OR_SECRET_ACCESS_FAILED')
    for party in ('a', 'b'):
        print('PERSONAL_PILOT_STAGE: START_HOST_' + party.upper(), flush=True)
        compose('up', '-d', '--no-deps', '--pull', 'never', 'host-' + party, timeout=60)
        wait_ready('megmeet-personal-host-' + party, 120)
        passed('PERSONAL_HOST_' + party.upper() + '_READY')
    runtime_checks()
    private_networks(complete=True)
    nginx_entry_dns()
    for party in ('a', 'b'):
        result = docker('exec', 'megmeet-personal-host-' + party, 'python', '-B', '-c', MODEL_CHECK, timeout=110, allowed=True)
        require(result.returncode == 0, 'MODEL_CHAIN_FAILED_' + party.upper())
        passed('PERSONAL_MODEL_CHAIN_' + party.upper())
    print('PERSONAL_PILOT_STAGE: ACTIVATE_ENTRY_ROUTES', flush=True)
    require(CONF.read_bytes() == original, 'NGINX_CHANGED_DURING_PREPARATION')
    backup = BASE / ('nginx-before-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.conf')
    atomic(backup, original)
    print('NGINX_BACKUP: ' + str(backup), flush=True)
    info = CONF.stat()
    replaced = False
    try:
        if patched.encode() != original:
            atomic(CONF, patched.encode(), uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode))
            replaced = True
        docker('exec', 'nginx-https', 'nginx', '-t')
        docker('exec', 'nginx-https', 'nginx', '-s', 'reload')
        # Wait for new workers; a reload acknowledgement is not routing evidence.
        for attempt in range(10):
            try:
                public_status('/megmeet-ai-native/health', json_service='native-entry')
                break
            except (Failure, ValueError):
                if attempt == 9:
                    raise
                time.sleep(1)
        public_status('/megmeet-ai-native/__megmeet/identity', expected=403)
        public_status('/megmeet-ai-native/internal/model/v1/models', expected=404)
        passed('PUBLIC_ENTRY_HEALTH_ANONYMOUS_AND_INTERNAL_BOUNDARIES')
        require(gateway_before == inspect(GATEWAY, '{{.Id}} {{.Image}} {{.State.StartedAt}}'), 'LIVE_GATEWAY_CHANGED')
    except Exception:
        if replaced:
            atomic(CONF, original, uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode))
            restored = docker('exec', 'nginx-https', 'nginx', '-t', allowed=True)
            if restored.returncode == 0:
                restored = docker('exec', 'nginx-https', 'nginx', '-s', 'reload', allowed=True)
            print('NGINX_RESTORE: ' + ('PASS' if restored.returncode == 0 else 'CHECK_REQUIRED'), flush=True)
        raise
    # Record successful activation before retirement, so a retirement failure
    # cannot be misreported as "nothing deployed" on the next run.
    active = {'version': VERSION, 'nginx_backup': str(backup), 'route_metadata': metadata,
              'activated_at': time.time(), 'shared_host_retirement': 'pending'}
    atomic(BASE / 'active.json', json.dumps(active).encode())
    passed('PERSONAL_ENTRY_ROUTES_ACTIVE')
    # Only the named shared DSH test Hosts are retired. Volumes, Gateways,
    # portal, SSO and all other company applications stay outside this action.
    for cid in shared:
        stopped = docker('stop', '--time', '30', cid, timeout=40, allowed=True)
        if stopped.returncode:
            print('PERSONAL_ENTRY_ROUTES: ACTIVE; SHARED_HOST_RETIREMENT: CHECK_REQUIRED', flush=True)
            raise Failure('ROUTES_ACTIVE_SHARED_HOST_STOP_FAILED')
    active['shared_host_retirement'] = 'completed'
    atomic(BASE / 'active.json', json.dumps(active).encode())
    passed('PERSONAL_ENTRY_PILOT_DEPLOY')
    print('ENTRY_URL: ' + PUBLIC + '/megmeet-ai-native/', flush=True)
    print('IDENTITY_URL: ' + PUBLIC + '/megmeet-ai-native/__megmeet/identity', flush=True)
    print('REAL_ACCOUNT_BROWSER_CHAT_HISTORY_ISOLATION: PENDING', flush=True)
    print('AUTOSELECT_LOGIN_REFRESH_LOGOUT_CAPACITY: PENDING', flush=True)


def main():
    descriptor = None
    try:
        require(os.name == 'posix' and os.geteuid() == 0, 'ROOT_LINUX_REQUIRED')
        os.umask(0o077)
        require(len(sys.argv) == 2 and sys.argv[1] in {'prepare', 'activate'}, 'MODE_REQUIRED')
        descriptor = lock()
        (prepare if sys.argv[1] == 'prepare' else activate)()
        return 0
    except Failure as exc:
        print('PERSONAL_PILOT_STOP: ' + str(exc), flush=True)
        return 1
    except Exception as exc:
        if type(exc).__name__ == 'NginxPatchError' and re.fullmatch(r'[A-Z_]{1,100}', str(exc)):
            print('PERSONAL_PILOT_STOP: ' + str(exc), flush=True)
        else:
            print('PERSONAL_PILOT_STOP_CLASS: ' + type(exc).__name__, flush=True)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == '__main__':
    sys.exit(main())
