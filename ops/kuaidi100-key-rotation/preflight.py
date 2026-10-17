"""Read-only, fixed-R52 preflight for a same-container MCP key rotation.

No credential contents, environment, full inspect, arbitrary paths or network
requests are read. The caller owns locking and all credential mutations.
run(argv, timeout=15, input_bytes=None) returns bytes and must redact failures.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

ROOT = Path('/data/megmeet-logistics-eta-r52-20261014')
TRANSACTION = ROOT / '.logistics-eta-switch-20261014T052913Z-54642e05'
RELEASE = '20261014-r52-logistics-eta'
MODE = 'r52-logistics-eta'
MANIFEST_SHA = 'd64054b2e1a3d61e756ac15aee8336d54f9969f3d640e10af875cdbea0f7becd'
PAYLOAD_SHA = 'f0699689cdc8b1b06f290334fa2a6cbd3bd3049f3c21eb56abfd41880088cd30'
BASELINE_SHA = '9915cdffb4f1111f84770e131b8aebc1c58236553b63794bbf99415ac93fd543'
CREDENTIAL_PATH = Path('/etc/megmeet/logistics/kuaidi100-mcp.json')
CREDENTIAL_TARGET = '/run/secrets/maimi-logistics-kuaidi100-mcp.json'
CURRENT_IDS = {
    'db-migrate': '44291dfffc58682e8b791ba45f3285ca92f56c30623afcf824b4537a801b89d4',
    'downloads': 'ca7c24c2520a79e74d82c5ca52f12a980c66946d9de539ddd34ecccc8ee1ab87',
    'gateway': '5426a885a079904ef65f94b107a46199728302e4787405f15afb9ec4a4aaf0c7',
    'office-service': 'e7444311557bcb1e34c5a623fb2f286453ea8841989c59f917a41f4a69681300',
    'personal-cloud-manager': '742b132ed74e243b1584908aa5c63ee98dbde3236abce4284dd9ef5be8606628',
    'postgres': '03663601f0917f3d85a8ac246810c33f7acf82343800da3a83b61ed663ca5199',
    'redis': 'a6b499d6f8f7413bac09cd1bd7d31d718d782f0abe3eec5c9b647ba1b704b97f',
    'retention-worker': 'd0899a25f26f83f02fce45fffff3e8d99247d40d29ff3dfc880ea6f501f92ba7',
}
ORIGINAL_IDS = {**CURRENT_IDS,
    'gateway': 'fd08bc1b17c8bf5ac866a0499dd2be51c96e2fda7fe84024c4154bce51f39fe6',
    'personal-cloud-manager': 'df115a7d14d4e609965a61c8e7d2a86f72ce484be540f6687dca2718684b0408'}
TARGETS = {
    'gateway': {
        '/app/app/personal_cloud_plugin_static.py': 'public/app/personal_cloud_plugin_static.py',
        '/app/app/logistics_query.py': 'public/app/logistics_query.py',
        '/app/app/kuaidi100_mcp.py': 'public/app/kuaidi100_mcp.py'},
    'personal-cloud-manager': {'/opt/personal-cloud/manager.py': 'public/manager/manager.py'}}
KNOWN_RELEASE_ROOTS = tuple(Path('/data') / name for name in (
    'megmeet-remembered-login-20260930-r40', 'megmeet-dsh020-r41-20261008',
    'megmeet-skills-cards-r42-20261008', 'megmeet-card-storage-r43-20261008',
    'megmeet-card-photos-r44-20261009', 'megmeet-logistics-r45-20261009',
    'megmeet-logistics-phone-r46-20261009', 'megmeet-fast-entry-r47-20261009',
    'megmeet-skills-logistics-r48-20261012', 'megmeet-skills-picker-r49-20261012',
    'megmeet-logistics-layout-r50-20261012', 'megmeet-logistics-eta-r52-20261014'))
HEX = re.compile(r'[a-f0-9]{64}')
MAX_PUBLIC_BYTES = 8 * 1024 * 1024


class PreflightError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise PreflightError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def _json(raw, limit=MAX_PUBLIC_BYTES):
    _require(isinstance(raw, bytes) and len(raw) <= limit, 'R53_METADATA_SIZE_INVALID')
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise PreflightError('R53_METADATA_JSON_INVALID') from None


def _safe_parents(path):
    _require(path.is_absolute() and '..' not in path.parts, 'R53_UNSAFE_PUBLIC_PATH')
    for parent in path.parents:
        info = parent.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                 and not stat.S_IMODE(info.st_mode) & 0o022, 'R53_UNSAFE_PARENT')


def _read_public(path):
    path = Path(path)
    _safe_parents(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
             and before.st_uid == 0 and not stat.S_IMODE(before.st_mode) & 0o022
             and before.st_size <= MAX_PUBLIC_BYTES, 'R53_PUBLIC_FILE_INVALID')
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read(MAX_PUBLIC_BYTES + 1)
        after = os.fstat(handle.fileno())
    _require(identity(before) == identity(opened) == identity(after) == identity(path.lstat())
             and len(raw) == before.st_size, 'R53_PUBLIC_FILE_CHANGED')
    return raw


def _credential_identity(*, strict_size=False):
    # lstat only: key bytes and their digest are deliberately never read here.
    _safe_parents(CREDENTIAL_PATH)
    info = CREDENTIAL_PATH.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
             and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (10001, 10001, 0o600),
             'R53_CREDENTIAL_METADATA_INVALID')
    if strict_size:
        _require(0 < info.st_size <= 8192, 'R53_CREDENTIAL_SIZE_INVALID')
    return {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode), 'nlink': info.st_nlink}


def _check_incomplete():
    for root in KNOWN_RELEASE_ROOTS:
        _require(not os.path.lexists(root / '.update-incomplete'), 'R53_RELEASE_INCOMPLETE')


def _expected_receipt():
    return {
        'schema': 'logistics-eta-r52-receipt-v1', 'release': RELEASE, 'status': 'APPLIED',
        'payload_sha256': PAYLOAD_SHA, 'backup_directory': TRANSACTION.as_posix(),
        'previous_mode': 'r48-skills-logistics', 'baseline_sha256': BASELINE_SHA,
        'original_service_ids': ORIGINAL_IDS, 'effective_mode': MODE,
        'current_service_ids': CURRENT_IDS, 'changed_services': ['gateway', 'personal-cloud-manager'],
        'manager_restarted': True, 'workers_disconnected': True, 'tonggong_restarted': False,
        'images_changed': False, 'database_changed': False, 'closed_retention_seconds': 7200,
        'persistent_login': True, 'tokenkey_cache_preserved': True, 'maximum_hosts': 0,
        'worker_runtime': '0.2.0-rc.2', 'desktop_publication_pending': True,
        'desktop_version': '0.13.0', 'employee_acceptance': 'PENDING'}


def _release_proof():
    _check_incomplete()
    sampled = {}
    def take(path):
        path = Path(path)
        raw = _read_public(path)
        sampled[path] = raw
        return raw
    raw = take(ROOT / 'release-manifest.json')
    _require(_sha(raw) == MANIFEST_SHA, 'R53_R52_MANIFEST_CHANGED')
    manifest = _json(raw)
    _require(manifest.get('release') == RELEASE and manifest.get('publication_status') == 'READY'
             and manifest.get('status') == 'FROZEN', 'R53_R52_MANIFEST_INVALID')
    receipt_raw = take(ROOT / 'transition.json')
    expected = _expected_receipt()
    _require(_json(receipt_raw) == expected and receipt_raw == _encoded(expected)
             and take(TRANSACTION / 'applied.json') == receipt_raw,
             'R53_R52_RECEIPT_CHANGED')
    raw = take(TRANSACTION / 'baseline.json')
    _require(_sha(raw) == BASELINE_SHA, 'R53_R52_BASELINE_CHANGED')
    original = _json(raw)
    _require({s: item['id'] for s, item in original['items'].items()} == ORIGINAL_IDS,
             'R53_R52_BASELINE_IDS_CHANGED')
    prepared = {key: expected[key] for key in (
        'release', 'payload_sha256', 'backup_directory', 'previous_mode',
        'baseline_sha256', 'original_service_ids')}
    prepared.update(schema='logistics-eta-r52-prepared-v1', status='PREPARED')
    _require(_json(take(TRANSACTION / 'prepared.json')) == prepared, 'R53_R52_PREPARED_CHANGED')
    _require(_json(take(TRANSACTION / 'progress.json')) == {
        'phase': 'FINISHED', 'created': {s: CURRENT_IDS[s] for s in TARGETS}, 'creating': None},
        'R53_R52_PROGRESS_CHANGED')
    files = manifest['files']
    _require(isinstance(files, dict) and len(files) == 16, 'R53_R52_FILE_SET_CHANGED')
    for name, pin in files.items():
        path = PurePosixPath(name)
        _require(not path.is_absolute() and '..' not in path.parts and len(path.parts) <= 4,
                 'R53_R52_FILE_PATH_INVALID')
        content = take(ROOT / name)
        _require(pin == {'sha256': _sha(content), 'size_bytes': len(content)},
                 'R53_R52_FILE_CHANGED')
    sources = deepcopy(original['source_pins'])
    _require(isinstance(sources, dict) and set(sources) <= set(CURRENT_IDS)
             and set(TARGETS) <= set(sources), 'R53_R52_SOURCE_SET_INVALID')
    for service, targets in TARGETS.items():
        for target, name in targets.items():
            sources[service][target] = files[name]['sha256']
    for name, pin in files.items():
        if name.startswith('public/native/'):
            sources['personal-cloud-manager'][(ROOT / name).as_posix()] = pin['sha256']
    for values in sources.values():
        _require(isinstance(values, dict) and len(values) <= 256, 'R53_SOURCE_PIN_LIMIT')
        for path, digest in values.items():
            parsed = PurePosixPath(path)
            _require(parsed.is_absolute() and '..' not in parsed.parts
                     and not path.startswith('/run/secrets/') and not parsed.name.startswith('.')
                     and parsed.suffix in {'.py', '.json', '.js', '.mjs', '.md'}
                     and isinstance(digest, str) and HEX.fullmatch(digest), 'R53_SOURCE_PIN_INVALID')
    for path, before in sampled.items():
        _require(_read_public(path) == before, 'R53_RELEASE_CHANGED_DURING_CHECK')
    _check_incomplete()
    return {'fingerprint': _sha(_encoded({str(p): _sha(raw) for p, raw in sampled.items()})),
            'source_pins': sources}


def _run(run, argv, timeout=15):
    try:
        raw = run(argv, timeout=timeout)
        _require(isinstance(raw, bytes) and len(raw) <= 256 * 1024, 'R53_DOCKER_OUTPUT_INVALID')
        return raw
    except PreflightError:
        raise
    except Exception:
        raise PreflightError('R53_DOCKER_CHECK_FAILED') from None


def _field(value):
    return '{{json ' + value + '}}'


def _inspect_template():
    fields = {
        'id': '.Id', 'image': '.Image', 'running': '.State.Running', 'state': '.State.Status',
        'oom': '.State.OOMKilled', 'exit_code': '.State.ExitCode', 'restarts': '.RestartCount',
        'started': '.State.StartedAt', 'finished': '.State.FinishedAt', 'user': '.Config.User',
        'readonly': '.HostConfig.ReadonlyRootfs', 'privileged': '.HostConfig.Privileged',
        'project': '(index .Config.Labels "com.docker.compose.project")',
        'service': '(index .Config.Labels "com.docker.compose.service")',
        'release': '(index .Config.Labels "com.megmeet.logistics-eta52.release")',
        'transaction': '(index .Config.Labels "com.megmeet.logistics-eta52.transaction")',
        'mode': '(index .Config.Labels "com.megmeet.logistics-eta52.mode")'}
    paths = [CREDENTIAL_TARGET, *(p for v in TARGETS.values() for p in v), (ROOT / 'public/native').as_posix()]
    match = ' '.join('(eq $m.Destination ' + json.dumps(path) + ')' for path in paths)
    mounts = '[{{range $m := .Mounts}}{{if or ' + match + '}}' + (
        '{"type":{{json $m.Type}},"source":{{json $m.Source}},'
        '"destination":{{json $m.Destination}},"rw":{{json $m.RW}}},') + '{{end}}{{end}}null]'
    return '{' + ','.join(json.dumps(k) + ':' + _field(v) for k, v in fields.items()) + ',"mounts":' + mounts + '}'


def _snapshot(run, gateway_running):
    # Match R52 discovery: unrelated service labels and Compose one-off helpers
    # do not belong to this rotation. A duplicate normal target still fails.
    discovery_format = ('{"id":{{json .ID}},"service":{{json (.Label "com.docker.compose.service")}},'
                        '"oneoff":{{json (.Label "com.docker.compose.oneoff")}}}')
    for project, services in (
            ('megmeet-desktop-server', set(CURRENT_IDS) - {'downloads'}),
            ('megmeet-desktop-distribution', {'downloads'})):
        raw = _run(run, ['docker', 'ps', '-a', '--no-trunc', '--filter',
                         'label=com.docker.compose.project=' + project, '--format', discovery_format])
        lines = raw.splitlines()
        _require(len(lines) <= 64, 'R53_SERVICE_DISCOVERY_LIMIT')
        found = {}
        for line in lines:
            row = _json(line, 2048)
            _require(isinstance(row, dict) and set(row) == {'id', 'service', 'oneoff'},
                     'R53_SERVICE_DISCOVERY_INVALID')
            service = row['service']
            _require(isinstance(service, str), 'R53_SERVICE_DISCOVERY_INVALID')
            if service not in services:
                continue
            _require(isinstance(row['id'], str) and HEX.fullmatch(row['id'])
                     and isinstance(row['oneoff'], str) and row['oneoff'].lower() in {'true', 'false'},
                     'R53_SERVICE_DISCOVERY_INVALID')
            if row['oneoff'].lower() == 'true':
                continue
            _require(service not in found, 'R53_SERVICE_SET_CHANGED')
            found[service] = row['id']
        _require(found == {s: CURRENT_IDS[s] for s in services}, 'R53_SERVICE_SET_CHANGED')
    raw = _run(run, ['docker', 'inspect', '--type', 'container', '--format',
                     _inspect_template(), *[CURRENT_IDS[s] for s in sorted(CURRENT_IDS)]])
    lines = raw.splitlines()
    _require(len(lines) == len(CURRENT_IDS), 'R53_INSPECT_COUNT_INVALID')
    result = {}
    for line in lines:
        row = _json(line, 32768)
        _require(isinstance(row, dict), 'R53_SERVICE_METADATA_INVALID')
        service = row.get('service')
        _require(service in CURRENT_IDS and service not in result and row.get('id') == CURRENT_IDS[service],
                 'R53_SERVICE_ID_CHANGED')
        _require(row.get('project') == ('megmeet-desktop-distribution' if service == 'downloads' else 'megmeet-desktop-server'),
                 'R53_SERVICE_PROJECT_CHANGED')
        expected_running = gateway_running if service == 'gateway' else service != 'db-migrate'
        _require(row.get('running') is expected_running and row.get('oom') is False
                 and row.get('state') == ('running' if expected_running else 'exited'),
                 'R53_SERVICE_STATE_CHANGED')
        if service == 'db-migrate':
            _require(row.get('exit_code') == 0, 'R53_MIGRATION_NOT_COMPLETE')
        mounts = row.get('mounts')
        _require(isinstance(mounts, list) and mounts and mounts[-1] is None
                 and all(isinstance(m, dict) for m in mounts[:-1]), 'R53_MOUNT_METADATA_INVALID')
        row['mounts'] = sorted(mounts[:-1], key=lambda m: m.get('destination', ''))
        if service in TARGETS:
            _require((row.get('release'), row.get('transaction'), row.get('mode')) ==
                     (RELEASE, TRANSACTION.name, MODE), 'R53_RELEASE_LABEL_CHANGED')
            expected_mounts = [{'type': 'bind', 'source': (ROOT / name).as_posix(),
                                'destination': target, 'rw': False}
                               for target, name in TARGETS[service].items()]
            if service == 'gateway':
                expected_mounts.append({'type': 'bind', 'source': CREDENTIAL_PATH.as_posix(),
                                        'destination': CREDENTIAL_TARGET, 'rw': False})
                _require(row.get('user') == '10001:10001' and row.get('readonly') is True
                         and row.get('privileged') is False, 'R53_GATEWAY_POLICY_CHANGED')
            else:
                expected_mounts.append({'type': 'bind', 'source': (ROOT / 'public/native').as_posix(),
                                        'destination': (ROOT / 'public/native').as_posix(), 'rw': False})
            _require(row['mounts'] == sorted(expected_mounts, key=lambda m: m['destination']),
                     'R53_EXPECTED_BIND_CHANGED')
        result[service] = row
    _require(set(result) == set(CURRENT_IDS), 'R53_SERVICE_SET_CHANGED')
    return result


def _live_sources(run, sources, gateway_running):
    for service, pins in sorted(sources.items()):
        if service == 'gateway' and not gateway_running:
            continue
        if service == 'db-migrate':
            raise PreflightError('R53_STOPPED_SOURCE_SERVICE_UNSUPPORTED')
        code = (
            'import hashlib,json,os,stat\n'
            'pins=' + repr(pins) + '\n'
            'def check(path,expected):\n'
            ' try:\n'
            '  before=os.lstat(path)\n'
            '  if not stat.S_ISREG(before.st_mode) or before.st_size>8388608:return False\n'
            '  with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),"rb") as f:\n'
            '   opened=os.fstat(f.fileno());raw=f.read(8388609);after=os.fstat(f.fileno())\n'
            '  identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)\n'
            '  return identity(before)==identity(opened)==identity(after)==identity(os.lstat(path)) and hashlib.sha256(raw).hexdigest()==expected\n'
            ' except Exception:return False\n'
            'print(json.dumps({"source_pins_match":all(check(p,h) for p,h in pins.items())}))\n')
        value = _json(_run(run, ['docker', 'exec', CURRENT_IDS[service], 'python', '-I', '-B', '-c', code], 25), 1024)
        _require(value == {'source_pins_match': True}, 'R53_LIVE_SOURCE_CHANGED')


def _mounted_identity(run, *, strict_size=False):
    code = (
        'import json,os,stat\n'
        'try:\n'
        ' s=os.lstat(' + repr(CREDENTIAL_TARGET) + ')\n'
        ' v={"uid":os.geteuid(),"gid":os.getegid(),"regular":stat.S_ISREG(s.st_mode),'
        '"size_valid":0<s.st_size<=8192,"credential":{"dev":s.st_dev,"ino":s.st_ino,'
        '"uid":s.st_uid,"gid":s.st_gid,"mode":stat.S_IMODE(s.st_mode),"nlink":s.st_nlink}}\n'
        ' print(json.dumps(v))\n'
        'except Exception:print(json.dumps({"status":"FAILED"}))\n')
    value = _json(_run(run, ['docker', 'exec', CURRENT_IDS['gateway'], 'python', '-I', '-B', '-c', code]), 2048)
    _require(isinstance(value, dict) and value.get('uid') == 10001 and value.get('gid') == 10001
             and value.get('regular') is True and (not strict_size or value.get('size_valid') is True),
             'R53_EFFECTIVE_GATEWAY_USER_OR_FILE_INVALID')
    return value.get('credential')


def inspect_baseline(run):
    """Return a JSON-safe baseline; never reads a key or changes the server."""
    try:
        proof = _release_proof()
        identity = _credential_identity(strict_size=True)
        services = _snapshot(run, True)
        _live_sources(run, proof['source_pins'], True)
        _require(_mounted_identity(run, strict_size=True) == identity, 'R53_CREDENTIAL_BIND_IDENTITY_CHANGED')
        _require(_credential_identity(strict_size=True) == identity and _snapshot(run, True) == services
                 and _release_proof() == proof, 'R53_BASELINE_CHANGED_DURING_CHECK')
        return {'schema': 'kuaidi100-r53-preflight-baseline-v1', 'gateway_id': CURRENT_IDS['gateway'],
                'release_fingerprint': proof['fingerprint'], 'source_pins': proof['source_pins'],
                'credential_identity': identity, 'services': services}
    except PreflightError:
        raise
    except Exception:
        raise PreflightError('R53_PREFLIGHT_FAILED') from None


def verify_unchanged(run, baseline, *, gateway_running=True):
    """Check the original container and seven peers across stop/write/start.

    Credential size may change within bounds; mtime/content are not compared.
    When stopped, no command is executed in the gateway.
    """
    try:
        _require(type(gateway_running) is bool and isinstance(baseline, dict)
                 and baseline.get('schema') == 'kuaidi100-r53-preflight-baseline-v1'
                 and baseline.get('gateway_id') == CURRENT_IDS['gateway'], 'R53_BASELINE_INVALID')
        proof = _release_proof()
        _require(proof == {'fingerprint': baseline['release_fingerprint'],
                           'source_pins': baseline['source_pins']}, 'R53_RELEASE_CHANGED')
        _require(_credential_identity() == baseline['credential_identity'], 'R53_CREDENTIAL_IDENTITY_CHANGED')
        current = _snapshot(run, gateway_running)
        for service in CURRENT_IDS:
            if service != 'gateway':
                _require(current[service] == baseline['services'][service], 'R53_PRESERVED_SERVICE_CHANGED')
            else:
                mutable = {'running', 'state', 'exit_code', 'restarts', 'started', 'finished'}
                project = lambda row: {k: v for k, v in row.items() if k not in mutable}
                _require(project(current[service]) == project(baseline['services'][service]),
                         'R53_GATEWAY_STATIC_CHANGED')
        _live_sources(run, proof['source_pins'], gateway_running)
        if gateway_running:
            _require(_mounted_identity(run) == baseline['credential_identity'], 'R53_CREDENTIAL_BIND_IDENTITY_CHANGED')
        _require(_credential_identity() == baseline['credential_identity']
                 and _snapshot(run, gateway_running) == current
                 and _release_proof() == proof, 'R53_STATE_CHANGED_DURING_CHECK')
        return {'status': 'VERIFIED', 'gateway_id_unchanged': True,
                'gateway_running': gateway_running, 'non_gateway_services_unchanged': 7,
                'credential_identity_unchanged': True, 'credential_values_read': False}
    except PreflightError:
        raise
    except Exception:
        raise PreflightError('R53_RECHECK_FAILED') from None
