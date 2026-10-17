#!/usr/bin/env python3
"""Run the real new personal launcher and entry only in owned offline resources."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
HOST_IMAGE = 'sha256:92df7271a90079aa38ce0d3f98f97cf088d7472f2bcd5e331f802bb4af4f8236'
ENTRY_IMAGE = 'sha256:464685944b999493c5d01288ebb29b10c0d8c3c59edec980099cdef3dfbdc1f7'
LIVE = ('megmeet-ai-platform-new-dsh-web-host-1', 'megmeet-ai-platform-new-gateway-1', 'nginx-https')
LABEL = 'megmeet.native-personal-probe.run'


class Failure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Failure(code)


def docker(*args, payload=None, timeout=25, failure=False):
    result = subprocess.run(['docker', *args], input=payload, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode and not failure:
        raise Failure('DOCKER_COMMAND_FAILED')
    return result


def inspect(cid, template):
    return docker('inspect', '--format', template, cid).stdout.strip()


def fingerprint(cid):
    return inspect(cid, '{{.Id}} {{.Image}} {{.State.StartedAt}}')


def passed(code):
    print(code + ': PASS', flush=True)


def bounded_markers(text):
    for line in text.splitlines()[-80:]:
        numeric = re.fullmatch(r'PROBE_(?:NATIVE|SSO|UPSTREAM|DIRECT_PUBLIC|DIRECT_PUBLIC_FORWARDED_PROTO|DIRECT_LOOPBACK|WS_BROWSER|WS_UPSTREAM|WS_DIRECT)_HTTP_STATUS: [1-5][0-9]{2}', line)
        if numeric or re.fullmatch(r'[A-Z][A-Z0-9_]{1,110}: (?:PASS|[A-Z][A-Z0-9_]{1,100}|[A-Za-z]{1,60})', line):
            print(line, flush=True)


class Run:
    def __init__(self):
        self.uid = uuid.uuid4().hex
        self.prefix = 'megmeet-personal-probe-' + self.uid
        self.cids = []
        self.volumes = []
        self.networks = []
        self.before = {}
        self.temp = None

    def create(self, role, image, extra, command):
        result = docker('create', '--name', self.prefix + '-' + role,
                        '--label', LABEL + '=' + self.uid, '--pull', 'never',
                        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                        *extra, '--entrypoint', 'python', image, *command)
        cid = result.stdout.strip()
        require(re.fullmatch(r'[0-9a-f]{64}', cid), 'CREATED_CONTAINER_ID')
        self.cids.append(cid)
        return cid

    def remove(self, cid):
        require(cid in self.cids and inspect(cid, '{{index .Config.Labels "' + LABEL + '"}}') == self.uid,
                'CONTAINER_OWNERSHIP')
        docker('rm', '-f', cid)
        self.cids.remove(cid)

    def volume(self, role):
        name = self.prefix + '-' + role
        require(docker('volume', 'inspect', '--format', '{{.Name}}', name, failure=True).returncode != 0,
                'VOLUME_ALREADY_EXISTS')
        require(docker('volume', 'create', '--label', LABEL + '=' + self.uid, name).stdout.strip() == name,
                'VOLUME_NAME')
        require(docker('volume', 'inspect', '--format', '{{index .Labels "' + LABEL + '"}}', name).stdout.strip() == self.uid,
                'VOLUME_OWNER')
        self.volumes.append(name)
        cid = self.create('init-' + role, HOST_IMAGE, [
            '--network', 'none', '--user', '0:0', '--cap-add', 'CHOWN', '--memory', '128m', '--pids-limit', '32',
            '--mount', 'type=volume,source=' + name + ',target=/state,volume-nocopy'],
            ['-B', '-c', "import os; assert not os.listdir('/state'); os.chmod('/state',0o700); os.chown('/state',10001,10001)"])
        docker('start', '--attach', cid)
        require(inspect(cid, '{{.State.Status}} {{.State.ExitCode}}') == 'exited 0', 'VOLUME_INIT')
        self.remove(cid)
        return name

    def network(self, party):
        name = self.prefix + '-net-' + party
        require(docker('network', 'inspect', '--format', '{{.Name}}', name, failure=True).returncode != 0,
                'NETWORK_ALREADY_EXISTS')
        docker('network', 'create', '--internal', '--driver', 'bridge', '--label', LABEL + '=' + self.uid, name)
        require(docker('network', 'inspect', '--format', '{{.Internal}} {{index .Labels "' + LABEL + '"}}', name).stdout.strip()
                == 'true ' + self.uid, 'NETWORK_ISOLATION')
        self.networks.append(name)
        return name

    def runtime_flags(self, memory):
        return ['--init', '--user', '10001:10001', '--cpus', '1', '--memory', memory,
                '--memory-swap', memory, '--pids-limit', '256',
                '--tmpfs', '/tmp:rw,exec,nosuid,nodev,mode=1777,size=256m',
                '--mount', 'type=bind,source=' + str(HERE) + ',target=/probe,readonly']

    def mounts(self, cid, expected, nets, image):
        actual = json.loads(inspect(cid, '{{json .Mounts}}'))
        persistent = [m for m in actual if m['Type'] != 'tmpfs']
        require(len(persistent) == len(expected), 'EXTRA_CONTAINER_MOUNT')
        for mount in persistent:
            require(mount['Destination'] in expected, 'MOUNT_DESTINATION')
            kind, source, writable = expected[mount['Destination']]
            require(mount['Type'] == kind and mount['RW'] is writable, 'MOUNT_MODE')
            require((mount.get('Name') if kind == 'volume' else mount['Source']) == source, 'MOUNT_SOURCE')
        require(all(m['Destination'] == '/tmp' for m in actual if m['Type'] == 'tmpfs'), 'TMPFS_DESTINATION')
        require(set(json.loads(inspect(cid, '{{json .HostConfig.Tmpfs}}'))) == {'/tmp'}, 'TMPFS_ALLOWLIST')
        require(inspect(cid, '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}}') == '10001:10001 true', 'UID_READONLY')
        require(inspect(cid, '{{.Image}}') == image, 'IMAGE_UNCHANGED')
        require(inspect(cid, '{{json .HostConfig.PortBindings}}') in ('{}', 'null'), 'PUBLISHED_PORT')
        require(set(json.loads(inspect(cid, '{{json .NetworkSettings.Networks}}'))) == set(nets), 'NETWORK_ALLOWLIST')

    def host(self, party, phase, owner, state, bootstrap, net, configdir):
        model = str(configdir / ('model-' + party))
        cid = self.create('host-' + party + '-' + phase, HOST_IMAGE, self.runtime_flags('1280m') + [
            '--network', net, '--network-alias', 'host-' + party,
            '--mount', 'type=volume,source=' + state + ',target=/state,volume-nocopy',
            '--mount', 'type=volume,source=' + bootstrap + ',target=/run/megmeet-bootstrap,volume-nocopy',
            '--mount', 'type=bind,source=' + model + ',target=/run/megmeet-config/model-token,readonly',
            '-e', 'MEGMEET_OWNER_ID=' + owner,
            '-e', 'MEGMEET_MODEL_TOKEN_FILE=/run/megmeet-config/model-token',
            '-e', 'MEGMEET_MODEL_BASE_URL=http://native-entry:8080/internal/model/v1',
            '-e', 'MEGMEET_PUBLIC_ORIGIN=https://platform-ai.megmeet.com'],
            ['-B', '/probe/host_launcher.py'])
        self.mounts(cid, {'/probe': ('bind', str(HERE), False), '/state': ('volume', state, True),
                         '/run/megmeet-bootstrap': ('volume', bootstrap, True),
                         '/run/megmeet-config/model-token': ('bind', model, False)}, [net], HOST_IMAGE)
        docker('start', cid)
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            result = docker('exec', cid, 'python', '-B', '-c',
                            "import json; from pathlib import Path; p=Path('/run/megmeet-bootstrap/ready.json'); raise SystemExit(0 if p.is_file() and json.loads(p.read_text()).get('ready') is True else 2)",
                            timeout=8, failure=True)
            if result.returncode == 0:
                passed('LAUNCHER_' + party.upper() + '_' + phase.upper() + '_READY')
                return cid
            if inspect(cid, '{{.State.Status}}') != 'running':
                break
            time.sleep(1)
        bounded_markers(docker('logs', '--tail', '30', cid, failure=True).stdout)
        raise Failure('NEW_HOST_LAUNCHER_NOT_READY')

    def entry(self, nets, bootstraps, configdir):
        cid = self.create('entry', ENTRY_IMAGE, self.runtime_flags('384m') + [
            '--network', nets[0], '--network-alias', 'native-entry',
            '--mount', 'type=bind,source=' + str(configdir) + ',target=/run/megmeet-entry,readonly',
            '--mount', 'type=volume,source=' + bootstraps[0] + ',target=/run/megmeet-bootstrap/a,readonly,volume-nocopy',
            '--mount', 'type=volume,source=' + bootstraps[1] + ',target=/run/megmeet-bootstrap/b,readonly,volume-nocopy'],
            ['-B', '/probe/probe_client.py', 'serve'])
        docker('network', 'connect', '--alias', 'native-entry', nets[1], cid)
        self.mounts(cid, {'/probe': ('bind', str(HERE), False),
                         '/run/megmeet-entry': ('bind', str(configdir), False),
                         '/run/megmeet-bootstrap/a': ('volume', bootstraps[0], False),
                         '/run/megmeet-bootstrap/b': ('volume', bootstraps[1], False)}, nets, ENTRY_IMAGE)
        docker('start', cid)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            result = docker('exec', cid, 'python', '-B', '-c',
                            "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2); assert r.status==200", timeout=5, failure=True)
            if result.returncode == 0:
                passed('OFFLINE_CANDIDATE_ENTRY_READY')
                return cid
            if inspect(cid, '{{.State.Status}}') != 'running':
                break
            time.sleep(0.5)
        bounded_markers(docker('logs', '--tail', '25', cid, failure=True).stdout)
        raise Failure('CANDIDATE_ENTRY_NOT_READY')

    def check(self, entry, phase):
        result = docker('exec', entry, 'python', '-B', '/probe/probe_client.py', 'check', phase,
                        timeout=90, failure=True)
        bounded_markers(result.stdout)
        require(result.returncode == 0, 'ENTRY_INTEGRATION_CHECK_FAILED')

    def linux_guards(self, entry):
        result = docker('exec', entry, 'python', '-B', '/probe/probe_client.py', 'linux-guards',
                        timeout=30, failure=True)
        bounded_markers(result.stdout)
        require(result.returncode == 0, 'LINUX_GUARDS_FAILED')

    def dependency_preflight(self):
        cid = self.create('dependency-preflight', ENTRY_IMAGE, self.runtime_flags('192m') + [
            '--network', 'none'], ['-B', '-c',
            "import sys; sys.path.insert(0,'/probe'); import entry_app, probe_client, uvicorn; print('CANDIDATE_IMPORTS: PASS')"])
        result = docker('start', '--attach', cid, timeout=20, failure=True)
        bounded_markers(result.stdout)
        require(result.returncode == 0 and inspect(cid, '{{.State.Status}} {{.State.ExitCode}}') == 'exited 0',
                'CANDIDATE_DEPENDENCY_IMPORTS_FAILED')
        self.remove(cid)

    def reconcile_owned_resources(self):
        """Recover daemon-created resources whose create response was lost.

        Label lookup only identifies candidates. Verify each object's immutable
        ID or exact name, name prefix, and label before adopting for cleanup.
        """
        label = 'label=' + LABEL + '=' + self.uid
        candidates = docker('ps', '-aq', '--filter', label).stdout.splitlines()
        for candidate in candidates:
            require(re.fullmatch(r'[0-9a-f]{12,64}', candidate), 'RECONCILE_CONTAINER_ID')
            detail = inspect(candidate, '{{.Id}} {{.Name}} {{index .Config.Labels "' + LABEL + '"}}').split()
            require(len(detail) == 3 and re.fullmatch(r'[0-9a-f]{64}', detail[0])
                    and detail[1].startswith('/' + self.prefix + '-') and detail[2] == self.uid,
                    'RECONCILE_CONTAINER_OWNER')
            if detail[0] not in self.cids:
                self.cids.append(detail[0])
        for kind, owned in [('volume', self.volumes), ('network', self.networks)]:
            for candidate in docker(kind, 'ls', '-q', '--filter', label).stdout.splitlines():
                detail = docker(kind, 'inspect', '--format', '{{.Name}} {{index .Labels "' + LABEL + '"}}', candidate).stdout.split()
                require(len(detail) == 2 and detail[0].startswith(self.prefix + '-') and detail[1] == self.uid,
                        'RECONCILE_RESOURCE_OWNER')
                if detail[0] not in owned:
                    owned.append(detail[0])

    def cleanup(self):
        good = True
        try:
            self.reconcile_owned_resources()
        except Exception:
            good = False
        for cid in list(reversed(self.cids)):
            try:
                self.remove(cid)
            except Exception:
                good = False
        for kind, names in [('volume', self.volumes), ('network', self.networks)]:
            for name in list(reversed(names)):
                try:
                    owner = docker(kind, 'inspect', '--format', '{{index .Labels "' + LABEL + '"}}', name).stdout.strip()
                    require(owner == self.uid, 'RESOURCE_CLEANUP_OWNER')
                    docker(kind, 'rm', name)
                    names.remove(name)
                except Exception:
                    good = False
        if self.temp is not None:
            try:
                self.temp.cleanup()
            except Exception:
                good = False
        print('PERSONAL_ENTRY_PROBE_CLEANUP: ' + ('PASS' if good else 'CHECK_REQUIRED'), flush=True)
        if not good:
            print('PROBE_RESOURCE_PREFIX: ' + self.prefix, flush=True)
        return good


def main():
    run, rc, step, lock = Run(), 1, 'preflight', None
    try:
        require(os.name == 'posix' and os.geteuid() == 0 and shutil.which('docker'), 'ROOT_LINUX_DOCKER_REQUIRED')
        require(not any(x in str(HERE) for x in (',', '\n', '\r')), 'PACKAGE_PATH')
        require(HERE.stat().st_mode & 0o005 == 0o005, 'PACKAGE_DIRECTORY_NOT_WORLD_READABLE')
        for file in ('entry_app.py', 'host_launcher.py', 'probe_client.py', 'probe_page.py', 'test_host_launcher.py'):
            require((HERE / file).is_file() and (HERE / file).stat().st_mode & 0o004, 'PACKAGE_FILE_NOT_READABLE')
        lock_fd = os.open('/tmp/megmeet-native-personal-entry-probe.lock',
                          os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        lock = os.fdopen(lock_fd, 'a')
        lock_info = os.fstat(lock_fd)
        require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == 0 and lock_info.st_nlink == 1,
                'LOCK_FILE_INVALID')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        available = re.search(r'(?m)^MemAvailable:\s+(\d+) kB$', Path('/proc/meminfo').read_text())
        require(available and int(available[1]) >= 3584 * 1024, 'AVAILABLE_MEMORY_BELOW_3584_MIB')
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Failure('TOTAL_TIMEOUT')))
        signal.signal(signal.SIGINT, lambda *_: (_ for _ in ()).throw(Failure('INTERRUPTED')))
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(Failure('INTERRUPTED')))
        signal.alarm(540)
        run.before = {name: fingerprint(name) for name in LIVE}
        for image in (HOST_IMAGE, ENTRY_IMAGE):
            require(docker('image', 'inspect', '--format', '{{.Id}}', image).stdout.strip() == image, 'REQUIRED_IMAGE_MISSING')
        step = 'candidate-imports'
        run.dependency_preflight()
        print('SCOPE: real candidate launcher and entry; synthetic identities; offline owned resources', flush=True)
        print('PERSONAL_ENTRY_PROBE_REVISION: R4_WEBSOCKET_HANDSHAKE', flush=True)
        step = 'private-config'
        run.temp = tempfile.TemporaryDirectory(prefix=run.prefix + '-', dir='/tmp')
        configdir = Path(run.temp.name)
        configdir.chmod(0o700)
        os.chown(configdir, 10001, 10001)
        owners = [str(uuid.uuid4()), str(uuid.uuid4())]
        identities = [{'user_no': 'synthetic-' + party, 'cookie': secrets.token_urlsafe(32)} for party in ('a', 'b')]
        config = {'public_origin': 'https://platform-ai.megmeet.com', 'sso_url': 'https://sso.probe.invalid/query-user',
                  'tokenkey_url': 'https://model.probe.invalid/v1', 'tokenkey_secret_file': '/run/secrets/never-supplied',
                  'bindings': [{'user_id': owners[i], 'user_no': identities[i]['user_no'],
                                'upstream': 'http://host-' + party + ':3084',
                                'bootstrap': '/run/megmeet-bootstrap/' + party + '/ready.json',
                                'model_token_file': '/run/megmeet-entry/model-' + party}
                               for i, party in enumerate(('a', 'b'))]}
        files = {'config.json': json.dumps(config), 'probe-identities.json': json.dumps(identities),
                 'model-a': secrets.token_urlsafe(48), 'model-b': secrets.token_urlsafe(48)}
        for name, value in files.items():
            path = configdir / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(value + '\n')
            os.chown(path, 10001, 10001)
        step = 'isolated-resources'
        networks = [run.network(party) for party in ('a', 'b')]
        states = [run.volume('state-' + party) for party in ('a', 'b')]
        bootstraps = [run.volume('bootstrap-' + party) for party in ('a', 'b')]
        # The scoped model catalog endpoint must exist before DSH initializes.
        # Empty bootstrap volumes are valid here; requests fail closed until
        # each launcher publishes ready.json after workspace registration.
        step = 'new-entry'
        entry = run.entry(networks, bootstraps, configdir)
        step = 'linux-contract-guards'
        run.linux_guards(entry)
        step = 'new-launchers'
        a = run.host('a', 'initial', owners[0], states[0], bootstraps[0], networks[0], configdir)
        b = run.host('b', 'initial', owners[1], states[1], bootstraps[1], networks[1], configdir)
        b_before = fingerprint(b)
        step = 'initial-integration'
        run.check(entry, 'initial')
        step = 'recreate-a'
        docker('stop', '--time', '12', a, timeout=20)
        require(inspect(a, '{{.State.Status}} {{.State.ExitCode}}') == 'exited 0', 'A_GRACEFUL_STOP')
        run.remove(a)
        a2 = run.host('a', 'restored', owners[0], states[0], bootstraps[0], networks[0], configdir)
        require(a2 != a and fingerprint(b) == b_before, 'RECREATE_OR_B_UNCHANGED')
        step = 'restored-integration'
        run.check(entry, 'restored')
        require(fingerprint(b) == b_before, 'B_CHANGED_DURING_A_RECOVERY')
        passed('CANDIDATE_PERSONAL_ENTRY_INTEGRATION')
        rc = 0
    except Exception as exc:
        print('FAILED_STEP: ' + step, flush=True)
        print('ERROR_CLASS: ' + type(exc).__name__, flush=True)
        if isinstance(exc, Failure):
            print('CHECK: ' + str(exc), flush=True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if not run.cleanup():
            rc = 1
        for name, before in run.before.items():
            try:
                unchanged = fingerprint(name) == before
            except Exception:
                unchanged = False
            label = 'LIVE_NGINX' if name == 'nginx-https' else ('LIVE_GATEWAY' if '-gateway-' in name else 'LIVE_HOST')
            print(label + '_UNCHANGED: ' + ('PASS' if unchanged else 'NOT_CONFIRMED'), flush=True)
            if not unchanged:
                rc = 1
        for item in ('REAL_SSO', 'BROWSER_AUTOSELECT', 'CHAT_HISTORY', 'MODEL_CALL', 'LOAD_CAPACITY', 'PRODUCTION_DEPLOYMENT'):
            print(item + ': NOT_TESTED', flush=True)
        print('PERSONAL_ENTRY_INTEGRATION_EXIT:' + str(rc), flush=True)
        if lock:
            lock.close()
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
