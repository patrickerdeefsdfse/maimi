#!/usr/bin/env python3
"""Bounded login-only upgrade of the already verified two-account pilot.

Only entry_app.py and entry_login.py are installed. The entry container alone
is recreated. Nginx configuration is not edited; its static upstream needs a
test/reload after Docker assigns the recreated entry an address. No Cookie,
SSO request, model request, user record or service log is collected.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import uuid

RELEASE = 'megmeet-native-entry-login-r1-20260916'
MARKER = 'MEGMEET_ENTRY_LOGIN_ONLY_V1'
PACKAGE_FILES = {'entry_app.py', 'entry_login.py', 'login_deploy.py', 'start-login.sh', 'LOGIN-README.md'}
BASE = Path('/data/megmeet-ai-platform-new/personal-entry-pilot')
APP = BASE / 'app'
JOURNAL = BASE / 'login-upgrade-state.json'
CONF = Path('/root/nginx/conf.d/default.conf')
ENTRY = 'megmeet-native-personal-entry'
PROJECT = 'megmeet-personal-pilot'
IMAGE = 'sha256:464685944b999493c5d01288ebb29b10c0d8c3c59edec980099cdef3dfbdc1f7'
PROTECTED = ('megmeet-personal-host-a', 'megmeet-personal-host-b',
             'megmeet-ai-platform-new-gateway-1', 'nginx-https')
OLD_FILES = {
    'entry_app.py': '8a9c88b7fffdc98278b40425f76b397429ae413ae57984cd5d40c7fb3b23d680',
    'host_launcher.py': '41a908f01408ad45dcd6284339818681f9e8a6ed844e626be017eac676119c6e',
    'pilot_deploy.py': 'f1e26ce081bd7531e730e84d7fdedd1e14860424139086ada748e85be429234c',
    'pilot_nginx.py': '0aad0f689c3d332d4e107364932dd0465f7220adf97813fc7f906f661c0dc928',
}
PUBLIC = 'https://platform-ai.megmeet.com'
HTML_MARKERS = ('name="megmeet-entry-scope" content="ENTRY_LOGIN_ONLY"',
                'name="megmeet-entry-release" content="' + MARKER + '"',
                '/megmeet-ai-native/__megmeet/login.js')
CSP_REQUIRED = {"default-src 'none'", "script-src 'self'", "connect-src 'self'",
                "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'", "object-src 'none'"}


class Stop(Exception):
    """Only fixed diagnostic codes may cross the console boundary."""


def need(value, code):
    if not value:
        raise Stop(code)


def passed(code):
    print(code + ': PASS', flush=True)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(path):
    need(path.is_absolute(), 'ABSOLUTE_PATH_REQUIRED')
    for ancestor in (path, *path.parents):
        need(not ancestor.is_symlink(), 'SYMLINK_PATH_REFUSED')


def read_file(path, *, maximum=2 * 1024 * 1024, uid=0, private=False):
    safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == uid,
             'FILE_TYPE_OR_OWNER_INVALID')
        need(not info.st_mode & (0o077 if private else 0o022), 'FILE_PERMISSIONS_INVALID')
        need(info.st_size <= maximum, 'FILE_TOO_LARGE')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(maximum + 1)
        need(len(raw) <= maximum, 'FILE_TOO_LARGE')
        return raw
    finally:
        os.close(fd)


def private_directory(path, *, create=False):
    safe_path(path)
    if create:
        path.mkdir(mode=0o700)
    info = path.stat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
         'PRIVATE_DIRECTORY_REQUIRED')


def atomic(path, raw, *, mode=0o600):
    safe_path(path)
    fd, temporary = tempfile.mkstemp(prefix='.login-write-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args, *, timeout=30, allowed=False):
    result = subprocess.run(args, text=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if not allowed:
        need(result.returncode == 0, 'COMMAND_FAILED_' + Path(args[0]).name.upper())
    return result


def docker(*args, **kwargs):
    return run(['docker', *args], **kwargs)


def inspect(name, template):
    return docker('inspect', '--format', template, name).stdout.strip()


def compose_recreate():
    docker('compose', '--env-file', '/dev/null', '-f', str(BASE / 'pilot.compose.json'),
           '-p', PROJECT, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
           '--force-recreate', 'entry', timeout=90)


def package(root):
    raw = read_file(root / 'manifest.json', maximum=16384)
    manifest = json.loads(raw)
    need(set(manifest) == {'release', 'files'} and manifest['release'] == RELEASE
         and set(manifest['files']) == PACKAGE_FILES, 'PACKAGE_MANIFEST_INVALID')
    files = {}
    for name in sorted(PACKAGE_FILES):
        expected = manifest['files'][name]
        need(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'PACKAGE_HASH_INVALID')
        files[name] = read_file(root / name)
        need(digest(files[name]) == expected, 'PACKAGE_HASH_MISMATCH')
        if name.endswith('.py'):
            ast.parse(files[name], filename=name)
    need(MARKER.encode() in files['entry_login.py'], 'LOGIN_RELEASE_MARKER_MISSING')
    return files


def load_verified_module(name, raw, filename):
    module = types.ModuleType(name)
    module.__file__ = str(filename)
    sys.modules[name] = module
    exec(compile(raw, str(filename), 'exec'), module.__dict__)
    return module


def source_state(files):
    """Only a known original or the complete exact new pair is accepted."""
    entry = read_file(APP / 'entry_app.py')
    helper = APP / 'entry_login.py'
    if digest(entry) == OLD_FILES['entry_app.py'] and not helper.exists():
        return 'original'
    if entry == files['entry_app.py'] and helper.exists() and read_file(helper) == files['entry_login.py']:
        return 'applied'
    raise Stop('ENTRY_SOURCE_DRIFT')


def verify_mounts(actual, specification):
    expected = {}
    for item in specification['services']['entry']['volumes']:
        source = item['source']
        if item['type'] == 'volume':
            source = specification['volumes'][source]['name']
        expected[item['target']] = (item['type'], source, not item['read_only'])
    need(len(actual) == len(expected), 'ENTRY_MOUNT_COUNT_CHANGED')
    for item in actual:
        source = item.get('Name') if item['Type'] == 'volume' else item['Source']
        need(expected.get(item['Destination']) == (item['Type'], source, item['RW']), 'ENTRY_MOUNT_CHANGED')


def verify_entry(specification, *, healthy=True, allow_missing=False):
    if allow_missing:
        found = docker('inspect', '--format', '{{.Id}}', ENTRY, allowed=True)
        if found.returncode != 0:
            # An interrupted recreate can leave no entry. A successful exact
            # name query distinguishes that from a failed Docker connection.
            absent = docker('ps', '-a', '--filter', 'name=^/' + ENTRY + '$', '--format', '{{.Names}}')
            need(not absent.stdout.strip(), 'ENTRY_NAME_IN_USE_DURING_RECOVERY')
            return
    entry = specification['services']['entry']
    need(inspect(ENTRY, '{{.Image}}') == IMAGE == entry['image'], 'ENTRY_IMAGE_CHANGED')
    need(inspect(ENTRY, '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}}') == '10001:10001 true',
         'ENTRY_RUNTIME_USER_OR_ROOTFS')
    need(json.loads(inspect(ENTRY, '{{json .Config.Entrypoint}}')) == entry['entrypoint'] and
         json.loads(inspect(ENTRY, '{{json .Config.Cmd}}')) == entry['command'], 'ENTRY_COMMAND_CHANGED')
    need(inspect(ENTRY, '{{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}}')
         == PROJECT + ' entry', 'ENTRY_COMPOSE_OWNER_CHANGED')
    need(inspect(ENTRY, '{{json .HostConfig.PortBindings}}') in ('null', '{}'), 'ENTRY_PORTS_CHANGED')
    need(json.loads(inspect(ENTRY, '{{json .HostConfig.CapDrop}}')) == ['ALL'] and
         'no-new-privileges:true' in json.loads(inspect(ENTRY, '{{json .HostConfig.SecurityOpt}}')),
         'ENTRY_CAPABILITIES_CHANGED')
    need(set(json.loads(inspect(ENTRY, '{{json .NetworkSettings.Networks}}'))) ==
         {PROJECT + '-a', PROJECT + '-b', 'common-net'}, 'ENTRY_NETWORKS_CHANGED')
    verify_mounts(json.loads(inspect(ENTRY, '{{json .Mounts}}')), specification)
    if healthy:
        need(inspect(ENTRY, '{{.State.Status}} {{.State.Health.Status}}') == 'running healthy', 'ENTRY_NOT_HEALTHY')


def fingerprint(name):
    return inspect(name, '{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Running}}')


def verify_runtime_files(files, *, upgraded):
    expected = {'entry_app.py': digest(files['entry_app.py']) if upgraded else OLD_FILES['entry_app.py'],
                'entry_login.py': digest(files['entry_login.py']) if upgraded else None}
    script = """import hashlib,json,os
from pathlib import Path
assert os.getenv('MEGMEET_ENTRY_CONFIG')=='/run/megmeet-entry/config.json'
root=Path('/opt/megmeet/native-entry')
out={}
for name in ('entry_app.py','entry_login.py'):
    p=root/name
    assert not p.is_symlink()
    out[name]=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
print(json.dumps(out,sort_keys=True))
"""
    result = docker('exec', ENTRY, 'python', '-B', '-c', script)
    need(json.loads(result.stdout) == expected, 'RUNTIME_SOURCE_OR_CONFIG_MISMATCH')


def fingerprints():
    found = {name: fingerprint(name) for name in PROTECTED}
    need(all(value.endswith(' true') for value in found.values()), 'PROTECTED_SERVICE_NOT_RUNNING')
    return found


def unchanged(before, file_hashes):
    need(fingerprints() == before, 'HOST_GATEWAY_OR_NGINX_CONTAINER_CHANGED')
    for path, expected in file_hashes.items():
        uid = 10001 if path == str(BASE / 'private/config.json') else 0
        need(digest(read_file(Path(path), uid=uid, private=path != str(CONF))) == expected,
             'PROTECTED_CONFIG_CHANGED')
    for name in ('host_launcher.py', 'pilot_deploy.py', 'pilot_nginx.py'):
        need(digest(read_file(APP / name)) == OLD_FILES[name], 'PILOT_SOURCE_CHANGED')


def preflight():
    private_directory(BASE)
    safe_path(APP)
    info = APP.stat()
    need(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022, 'APP_DIRECTORY_INVALID')
    baseline = {}
    for name in ('host_launcher.py', 'pilot_deploy.py', 'pilot_nginx.py'):
        baseline[name] = read_file(APP / name)
        need(digest(baseline[name]) == OLD_FILES[name], 'PILOT_BASELINE_SOURCE_DRIFT')
    pilot = load_verified_module('_login_pilot_original', baseline['pilot_deploy.py'], APP / 'pilot_deploy.py')
    setup_raw = read_file(BASE / 'setup.json', private=True)
    setup = pilot.read_setup()
    config_raw = read_file(BASE / 'private/config.json', uid=10001, private=True)
    compose_raw = read_file(BASE / 'pilot.compose.json', private=True)
    active_raw = read_file(BASE / 'active.json', private=True)
    specification = json.loads(compose_raw)
    need(specification == pilot.compose_document(setup['bindings']), 'PILOT_COMPOSE_DRIFT')
    need(json.loads(config_raw) == pilot.configuration(setup['bindings']), 'PILOT_IDENTITY_CONFIG_DRIFT')
    active = json.loads(active_raw)
    need(active.get('version') == 'real-accounts-r1' and active.get('shared_host_retirement') == 'completed', 'PILOT_NOT_ACTIVE')
    conf_raw = read_file(CONF)
    nginx = load_verified_module('_login_pilot_nginx', baseline['pilot_nginx.py'], APP / 'pilot_nginx.py')
    same, metadata = nginx.patch_nginx(conf_raw.decode())
    need(same.encode() == conf_raw and metadata.get('changed') is False, 'NGINX_ROUTE_DRIFT')
    need(active['route_metadata']['after_sha256'] == digest(conf_raw), 'ACTIVE_NGINX_FINGERPRINT_CHANGED')
    tracked = {str(BASE / 'setup.json'): digest(setup_raw), str(BASE / 'private/config.json'): digest(config_raw),
               str(BASE / 'pilot.compose.json'): digest(compose_raw), str(BASE / 'active.json'): digest(active_raw),
               str(CONF): digest(conf_raw)}
    need(os.access(APP, os.W_OK), 'APP_DIRECTORY_NOT_WRITABLE')
    return specification, tracked


def wait_ready():
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        state = inspect(ENTRY, '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}')
        if state == 'running healthy':
            return
        if state.startswith(('dead', 'exited')):
            break
        time.sleep(2)
    raise Stop('ENTRY_START_TIMEOUT')


def reload_nginx():
    # R1 has a literal proxy_pass, not a variable resolver. Recreating entry
    # requires re-resolution by a config reload; the config bytes never change.
    network = json.loads(inspect(ENTRY, '{{json .NetworkSettings.Networks}}'))
    address = network['common-net']['IPAddress']
    need(bool(re.fullmatch(r'(?:[0-9]{1,3}\.){3}[0-9]{1,3}', address)), 'ENTRY_ADDRESS_INVALID')
    for attempt in range(15):
        answer = docker('exec', 'nginx-https', 'getent', 'hosts', ENTRY, allowed=True)
        if answer.returncode == 0 and {line.split()[0] for line in answer.stdout.splitlines() if line.split()} == {address}:
            break
        need(attempt < 14, 'NGINX_ENTRY_DNS_STALE')
        time.sleep(1)
    docker('exec', 'nginx-https', 'nginx', '-t')
    docker('exec', 'nginx-https', 'nginx', '-s', 'reload')
    passed('NGINX_STATIC_UPSTREAM_RELOADED_WITHOUT_CONFIG_EDIT')


def anonymous_request(path, accept):
    # Existing certificate is localhost-only; this no-credential request is
    # pinned to 127.0.0.1 and bypasses proxies. It checks routing, not TLS trust.
    need(path in ('/megmeet-ai-native/', '/megmeet-ai-native/__megmeet/login.js',
                  '/megmeet-ai-native/__megmeet/identity', '/megmeet-ai-native/health'), 'PROBE_PATH_INVALID')
    result = run(['curl', '--disable', '--silent', '--show-error', '--insecure', '--noproxy', '*',
                  '--connect-timeout', '3', '--max-time', '8', '--max-filesize', '262144',
                  '--resolve', 'platform-ai.megmeet.com:443:127.0.0.1', '--include',
                  '--header', 'Accept: ' + accept, PUBLIC + path])
    output = result.stdout.replace('\r\n', '\n')
    header, separator, body = output.partition('\n\n')
    need(separator and re.match(r'^HTTP/\S+ [0-9]{3}(?: |$)', header), 'PUBLIC_RESPONSE_INVALID')
    code = int(header.splitlines()[0].split()[1])
    headers = {}
    for line in header.splitlines()[1:]:
        name, colon, value = line.partition(':')
        need(colon, 'PUBLIC_HEADERS_INVALID')
        name, value = name.lower(), value.strip()
        if name in headers:
            if name in {'vary', 'cache-control', 'set-cookie'}:
                headers[name] += ', ' + value
                continue
            need(headers[name] == value and name != 'content-security-policy', 'PUBLIC_HEADERS_INVALID')
        headers[name] = value
    return code, headers, body


def verify_login_response(code, headers, body):
    need(code == 403 and 'text/html' in headers.get('content-type', ''), 'LOGIN_HTML_NOT_SERVED')
    need(all(body.count(marker) == 1 for marker in HTML_MARKERS), 'LOGIN_HTML_MARKER_MISMATCH')
    directives = {item.strip() for item in headers.get('content-security-policy', '').split(';') if item.strip()}
    need(CSP_REQUIRED <= directives and not any('unsafe-inline' in item or 'unsafe-eval' in item for item in directives),
         'LOGIN_CSP_INVALID')
    need('no-store' in headers.get('cache-control', '') and 'private' in headers.get('cache-control', ''), 'LOGIN_CACHE_POLICY_INVALID')
    need('set-cookie' not in headers, 'ANONYMOUS_PROBE_MUST_NOT_SET_COOKIE')


def public_checks(*, upgraded):
    for attempt in range(12):
        try:
            code, _, body = anonymous_request('/megmeet-ai-native/health', 'application/json')
            need(code == 200 and json.loads(body).get('service') == 'native-entry', 'PUBLIC_HEALTH_FAILED')
            if upgraded:
                verify_login_response(*anonymous_request('/megmeet-ai-native/', 'text/html'))
            break
        except (Stop, ValueError):
            if attempt == 11:
                raise
            time.sleep(1)
    code, headers, body = anonymous_request('/megmeet-ai-native/__megmeet/identity', 'application/json')
    need(code == 403 and json.loads(body).get('detail') == 'authentication required', 'ANONYMOUS_IDENTITY_BOUNDARY')
    if upgraded:
        code, headers, body = anonymous_request('/megmeet-ai-native/__megmeet/login.js', 'application/javascript')
        need(code == 200 and body.lstrip().startswith('// ' + MARKER), 'LOGIN_SCRIPT_MARKER_MISMATCH')
        need('javascript' in headers.get('content-type', '') and 'no-store' in headers.get('cache-control', ''), 'LOGIN_SCRIPT_HEADERS')
        need('set-cookie' not in headers, 'ANONYMOUS_PROBE_MUST_NOT_SET_COOKIE')


def write_journal(record):
    atomic(JOURNAL, json.dumps(record, sort_keys=True).encode())


def restore(record, files, specification):
    need(record['release'] == RELEASE and re.fullmatch(r'login-backup-[0-9a-f]{32}', record['backup']), 'RESTORE_RECORD_INVALID')
    backup = BASE / record['backup']
    private_directory(backup)
    old = read_file(backup / 'entry_app.py', private=True)
    need(digest(old) == OLD_FILES['entry_app.py'], 'RESTORE_BACKUP_HASH_INVALID')
    entry_now = read_file(APP / 'entry_app.py')
    need(entry_now in (old, files['entry_app.py']), 'RESTORE_ENTRY_DRIFT')
    helper = APP / 'entry_login.py'
    if helper.exists():
        need(read_file(helper) == files['entry_login.py'], 'RESTORE_HELPER_DRIFT')
    unchanged(record['protected'], record['file_hashes'])
    atomic(APP / 'entry_app.py', old, mode=0o644)
    if helper.exists():
        helper.unlink()
    compose_recreate()
    wait_ready()
    verify_entry(specification)
    verify_runtime_files(files, upgraded=False)
    reload_nginx()
    public_checks(upgraded=False)
    unchanged(record['protected'], record['file_hashes'])
    record['state'] = 'restored'
    write_journal(record)
    passed('LOGIN_ENTRY_AUTOMATIC_RESTORE')


def upgrade(files):
    specification, tracked = preflight()
    pending = json.loads(read_file(JOURNAL, private=True)) if JOURNAL.exists() else None
    if pending and pending.get('state') == 'applying':
        verify_entry(specification, healthy=False, allow_missing=True)
        restore(pending, files, specification)
    state = source_state(files)
    verify_entry(specification)
    verify_runtime_files(files, upgraded=state == 'applied')
    protected = fingerprints()
    if state == 'applied':
        need(pending and pending.get('state') == 'complete' and pending.get('release') == RELEASE, 'APPLIED_WITHOUT_RELEASE_RECORD')
        public_checks(upgraded=True)
        unchanged(protected, tracked)
        passed('LOGIN_ENTRY_ALREADY_APPLIED')
        return
    backup = BASE / ('login-backup-' + uuid.uuid4().hex)
    private_directory(backup, create=True)
    atomic(backup / 'entry_app.py', read_file(APP / 'entry_app.py'))
    record = {'release': RELEASE, 'state': 'applying', 'backup': backup.name,
              'protected': protected, 'file_hashes': tracked,
              'new_hashes': {name: digest(files[name]) for name in ('entry_app.py', 'entry_login.py')}}
    write_journal(record)
    print('LOGIN_ENTRY_BACKUP: ' + str(backup), flush=True)
    try:
        atomic(APP / 'entry_login.py', files['entry_login.py'], mode=0o644)
        atomic(APP / 'entry_app.py', files['entry_app.py'], mode=0o644)
        compose_recreate()
        wait_ready()
        verify_entry(specification)
        need(source_state(files) == 'applied', 'INSTALLED_SOURCE_HASH_MISMATCH')
        verify_runtime_files(files, upgraded=True)
        reload_nginx()
        public_checks(upgraded=True)
        unchanged(protected, tracked)
        record['state'] = 'complete'
        write_journal(record)
    except BaseException:
        try:
            restore(record, files, specification)
        except BaseException:
            print('LOGIN_ENTRY_AUTOMATIC_RESTORE: CHECK_REQUIRED', flush=True)
        raise
    passed('LOGIN_ENTRY_UPGRADE')
    passed('HOSTS_GATEWAY_AND_NGINX_CONFIG_UNCHANGED')
    print('BROWSER_COLD_LOGIN_RETURN: PENDING', flush=True)
    print('ACTIVE_CHAT_REFRESH_AND_LOGOUT: NOT_IMPLEMENTED_IN_THIS_RELEASE', flush=True)


def main():
    descriptor = None
    try:
        need(os.name == 'posix' and os.geteuid() == 0, 'ROOT_LINUX_REQUIRED')
        os.umask(0o077)
        need(len(sys.argv) == 1, 'UNEXPECTED_ARGUMENTS')
        files = package(Path(__file__).resolve().parent)
        private_directory(BASE)
        import fcntl
        descriptor = os.open(BASE / '.deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(descriptor)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1, 'DEPLOY_LOCK_INVALID')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(Stop('UPGRADE_INTERRUPTED')))
        upgrade(files)
        return 0
    except Stop as exc:
        print('LOGIN_ENTRY_UPGRADE_STOP: ' + str(exc), flush=True)
        return 1
    except BaseException as exc:
        print('LOGIN_ENTRY_UPGRADE_STOP_CLASS: ' + type(exc).__name__, flush=True)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == '__main__':
    sys.exit(main())
