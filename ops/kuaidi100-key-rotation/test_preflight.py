"""Offline contract tests; no Docker, provider or private credential access."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import preflight as p


IDENTITY = {'dev': 8, 'ino': 4242, 'uid': 10001, 'gid': 10001, 'mode': 0o600, 'nlink': 1}


class Docker:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.source_match = True
        self.mounted = {**IDENTITY}
        self.uid = self.gid = 10001
        self.size_valid = True
        self.extra = False
        self.discovery_extras = []
        self.discovery_transform = None
        self.failure = False
        for service, identifier in p.CURRENT_IDS.items():
            self.rows[service] = {
                'id': identifier, 'image': 'sha256:' + '1' * 64,
                'running': service != 'db-migrate',
                'state': 'exited' if service == 'db-migrate' else 'running',
                'oom': False, 'exit_code': 0, 'restarts': 0,
                'started': '2026-10-14T00:00:00Z', 'finished': '0001-01-01T00:00:00Z',
                'user': '10001:10001', 'readonly': True, 'privileged': False,
                'project': 'megmeet-desktop-distribution' if service == 'downloads' else 'megmeet-desktop-server',
                'service': service, 'release': None, 'transaction': None, 'mode': None,
                'mounts': [None],
            }
            if service in p.TARGETS:
                row = self.rows[service]
                row.update(release=p.RELEASE, transaction=p.TRANSACTION.name, mode=p.MODE)
                mounts = [{'type': 'bind', 'source': (p.ROOT / name).as_posix(), 'destination': target, 'rw': False}
                          for target, name in p.TARGETS[service].items()]
                mounts.append({'type': 'bind', 'source': p.CREDENTIAL_PATH.as_posix() if service == 'gateway'
                               else (p.ROOT / 'public/native').as_posix(),
                               'destination': p.CREDENTIAL_TARGET if service == 'gateway'
                               else (p.ROOT / 'public/native').as_posix(), 'rw': False})
                row['mounts'] = mounts + [None]

    def __call__(self, argv, timeout=15, input_bytes=None):
        self.calls.append((deepcopy(argv), timeout, input_bytes))
        assert input_bytes is None
        if self.failure:
            raise RuntimeError('SENSITIVE_RAW_FAILURE')
        assert argv[0] == 'docker'
        if argv[1] == 'ps':
            project = argv[argv.index('--filter') + 1].rsplit('=', 1)[1]
            values = [{'id': v['id'], 'service': v['service'], 'oneoff': 'False'}
                      for v in self.rows.values() if v['project'] == project]
            if self.extra:
                values.append({'id': 'e' * 64, 'service': values[0]['service'], 'oneoff': 'False'})
            values.extend(row for expected_project, row in self.discovery_extras if expected_project == project)
            if self.discovery_transform is not None:
                values = self.discovery_transform(values)
            return b'\n'.join(p._encoded(row).rstrip(b'\n') for row in values) + b'\n'
        if argv[1] == 'inspect':
            assert '{{json .}}' not in argv[5]
            assert '.Config.Env' not in argv[5]
            assert '{{json .Config}}' not in argv[5]
            return b'\n'.join(json.dumps(row).encode() for row in self.rows.values()) + b'\n'
        assert argv[1] == 'exec' and argv[3:7] == ['python', '-I', '-B', '-c']
        compile(argv[7], '<read-only-probe>', 'exec')
        assert self.rows['gateway']['running'] or argv[2] != p.CURRENT_IDS['gateway']
        if 'source_pins_match' in argv[7]:
            assert p.CREDENTIAL_TARGET not in argv[7]
            return p._encoded({'source_pins_match': self.source_match})
        assert 'os.lstat' in argv[7] and 'open(' not in argv[7] and 'read' not in argv[7]
        return p._encoded({'uid': self.uid, 'gid': self.gid, 'regular': True,
                           'size_valid': self.size_valid, 'credential': self.mounted})


@pytest.fixture
def setup(monkeypatch):
    candidate = Path(__file__).parent.parent / 'logistics-eta-compat-r52-20261014/cloud/candidate'
    manifest_raw = (candidate / 'release-manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    assert p._sha(manifest_raw) == p.MANIFEST_SHA
    files = {p.ROOT / 'release-manifest.json': manifest_raw}
    for name in manifest['files']:
        files[p.ROOT / name] = (candidate / name).read_bytes()
    original = {'items': {s: {'id': identifier} for s, identifier in p.ORIGINAL_IDS.items()},
                'source_pins': {s: {next(iter(targets)): 'a' * 64} for s, targets in p.TARGETS.items()}}
    raw = p._encoded(original)
    monkeypatch.setattr(p, 'BASELINE_SHA', p._sha(raw))
    files[p.TRANSACTION / 'baseline.json'] = raw
    receipt = p._expected_receipt()
    files[p.ROOT / 'transition.json'] = p._encoded(receipt)
    files[p.TRANSACTION / 'applied.json'] = files[p.ROOT / 'transition.json']
    prepared = {k: receipt[k] for k in ('release', 'payload_sha256', 'backup_directory',
                                      'previous_mode', 'baseline_sha256', 'original_service_ids')}
    prepared.update(schema='logistics-eta-r52-prepared-v1', status='PREPARED')
    files[p.TRANSACTION / 'prepared.json'] = p._encoded(prepared)
    files[p.TRANSACTION / 'progress.json'] = p._encoded({
        'phase': 'FINISHED', 'created': {s: p.CURRENT_IDS[s] for s in p.TARGETS}, 'creating': None})
    monkeypatch.setattr(p, '_read_public', lambda path: files[Path(path)])
    monkeypatch.setattr(p.os.path, 'lexists', lambda path: False)
    monkeypatch.setattr(p, '_credential_identity', lambda **kwargs: dict(IDENTITY))
    return Docker(), files


def assert_error(code, fn):
    with pytest.raises(p.PreflightError) as error:
        fn()
    assert error.value.code == code
    assert str(error.value) == code
    assert 'SENSITIVE' not in str(error.value)


def test_complete_fixed_release_readonly(setup):
    docker, _ = setup
    value = p.inspect_baseline(docker)
    assert value['gateway_id'] == p.CURRENT_IDS['gateway']
    assert value['credential_identity'] == IDENTITY
    assert set(value['services']) == set(p.CURRENT_IDS)
    assert p.verify_unchanged(docker, value)['non_gateway_services_unchanged'] == 7
    assert {args[1] for args, _, _ in docker.calls} <= {'ps', 'inspect', 'exec'}
    encoded = json.dumps(value)
    assert 'api_key' not in encoded and 'key_hash' not in encoded


@pytest.mark.parametrize('service', [s for s in p.CURRENT_IDS if s != 'gateway'])
def test_seven_peers_cannot_restart_or_change(setup, service):
    docker, _ = setup
    baseline = p.inspect_baseline(docker)
    docker.rows[service]['started'] = '2026-10-14T01:00:00Z'
    assert_error('R53_PRESERVED_SERVICE_CHANGED', lambda: p.verify_unchanged(docker, baseline))


def test_stopped_gateway_recheck_does_not_exec_it(setup):
    docker, _ = setup
    baseline = p.inspect_baseline(docker)
    docker.rows['gateway'].update(running=False, state='exited', exit_code=137,
                                  finished='2026-10-14T01:00:00Z')
    docker.calls.clear()
    result = p.verify_unchanged(docker, baseline, gateway_running=False)
    assert result['gateway_running'] is False
    assert not any(a[1] == 'exec' and a[2] == p.CURRENT_IDS['gateway'] for a, _, _ in docker.calls)


def test_restart_allows_only_gateway_lifecycle_changes_and_size(setup):
    docker, _ = setup
    baseline = p.inspect_baseline(docker)
    docker.rows['gateway'].update(started='2026-10-14T02:00:00Z', restarts=1,
                                  finished='2026-10-14T01:00:00Z')
    docker.size_valid = False  # recovery can observe a truncated same inode
    assert p.verify_unchanged(docker, baseline)['status'] == 'VERIFIED'
    assert_error('R53_EFFECTIVE_GATEWAY_USER_OR_FILE_INVALID', lambda: p.inspect_baseline(docker))


@pytest.mark.parametrize('field,value', [('uid', 0), ('gid', 0)])
def test_effective_user_must_match_configured_user(setup, field, value):
    docker, _ = setup
    setattr(docker, field, value)
    assert_error('R53_EFFECTIVE_GATEWAY_USER_OR_FILE_INVALID', lambda: p.inspect_baseline(docker))


@pytest.mark.parametrize('field,value', [('user', '0:0'), ('readonly', False), ('privileged', True)])
def test_configured_gateway_policy_is_checked(setup, field, value):
    docker, _ = setup
    docker.rows['gateway'][field] = value
    assert_error('R53_GATEWAY_POLICY_CHANGED', lambda: p.inspect_baseline(docker))


@pytest.mark.parametrize('change', ['rw', 'source', 'duplicate', 'type'])
def test_fixed_file_readonly_bind_only(setup, change):
    docker, _ = setup
    mounts = docker.rows['gateway']['mounts']
    row = next(m for m in mounts if isinstance(m, dict) and m['destination'] == p.CREDENTIAL_TARGET)
    if change == 'duplicate':
        mounts.insert(0, deepcopy(row))
    else:
        row[change] = {'rw': True, 'source': '/etc/other.json', 'type': 'volume'}[change]
    assert_error('R53_EXPECTED_BIND_CHANGED', lambda: p.inspect_baseline(docker))


def test_host_and_container_same_inode(setup):
    docker, _ = setup
    docker.mounted['ino'] += 1
    assert_error('R53_CREDENTIAL_BIND_IDENTITY_CHANGED', lambda: p.inspect_baseline(docker))


def test_replaced_credential_inode_rejected(setup, monkeypatch):
    docker, _ = setup
    baseline = p.inspect_baseline(docker)
    monkeypatch.setattr(p, '_credential_identity', lambda **kwargs: {**IDENTITY, 'ino': 9000})
    assert_error('R53_CREDENTIAL_IDENTITY_CHANGED', lambda: p.verify_unchanged(docker, baseline))


def test_duplicate_normal_target_container_rejected(setup):
    docker, _ = setup
    docker.extra = True
    assert_error('R53_SERVICE_SET_CHANGED', lambda: p.inspect_baseline(docker))


def test_historical_helpers_and_oneoffs_ignored_like_r52(setup):
    docker, _ = setup
    docker.discovery_extras = [
        ('megmeet-desktop-server', {'id': 'e' * 64, 'service': 'historical-helper', 'oneoff': ''}),
        ('megmeet-desktop-server', {'id': 'd' * 64, 'service': 'gateway', 'oneoff': 'True'}),
        ('megmeet-desktop-server', {'id': 'c' * 64, 'service': 'db-migrate', 'oneoff': 'true'}),
        ('megmeet-desktop-distribution', {'id': 'b' * 64, 'service': 'downloads', 'oneoff': 'TRUE'}),
    ]
    baseline = p.inspect_baseline(docker)
    assert p.verify_unchanged(docker, baseline)['status'] == 'VERIFIED'


def test_oneoff_cannot_replace_normal_target(setup):
    docker, _ = setup
    def oneoff(values):
        return [{**row, 'oneoff': 'True'} if row['service'] == 'gateway' else row for row in values]
    docker.discovery_transform = oneoff
    assert_error('R53_SERVICE_SET_CHANGED', lambda: p.inspect_baseline(docker))


def test_fixed_id_change_in_discovery_rejected(setup):
    docker, _ = setup
    def changed(values):
        return [{**row, 'id': 'e' * 64} if row['service'] == 'gateway' else row for row in values]
    docker.discovery_transform = changed
    assert_error('R53_SERVICE_SET_CHANGED', lambda: p.inspect_baseline(docker))


@pytest.mark.parametrize('oneoff', ['', 'unknown', True, None])
def test_target_oneoff_metadata_must_be_explicit_bool_string(setup, oneoff):
    docker, _ = setup
    def changed(values):
        return [{**row, 'oneoff': oneoff} if row['service'] == 'gateway' else row for row in values]
    docker.discovery_transform = changed
    assert_error('R53_SERVICE_DISCOVERY_INVALID', lambda: p.inspect_baseline(docker))


def test_historical_helpers_discovery_is_bounded(setup):
    docker, _ = setup
    docker.discovery_extras = [('megmeet-desktop-server',
                               {'id': 'e' * 64, 'service': 'helper', 'oneoff': 'False'})] * 65
    assert_error('R53_SERVICE_DISCOVERY_LIMIT', lambda: p.inspect_baseline(docker))


@pytest.mark.parametrize('service', ['gateway', 'personal-cloud-manager', 'postgres'])
def test_missing_running_service_rejected(setup, service):
    docker, _ = setup
    docker.rows[service].update(running=False, state='exited')
    assert_error('R53_SERVICE_STATE_CHANGED', lambda: p.inspect_baseline(docker))


def test_db_migrate_must_be_successfully_completed(setup):
    docker, _ = setup
    docker.rows['db-migrate']['exit_code'] = 1
    assert_error('R53_MIGRATION_NOT_COMPLETE', lambda: p.inspect_baseline(docker))


def test_source_code_pin_changed(setup):
    docker, _ = setup
    docker.source_match = False
    assert_error('R53_LIVE_SOURCE_CHANGED', lambda: p.inspect_baseline(docker))


def test_incomplete_release_stops_before_docker(setup, monkeypatch):
    docker, _ = setup
    monkeypatch.setattr(p.os.path, 'lexists', lambda path: Path(path) == p.ROOT / '.update-incomplete')
    assert_error('R53_RELEASE_INCOMPLETE', lambda: p.inspect_baseline(docker))
    assert not docker.calls


@pytest.mark.parametrize('path,code', [
    ('release-manifest.json', 'R53_R52_MANIFEST_CHANGED'),
    ('public/app/logistics_query.py', 'R53_R52_FILE_CHANGED')])
def test_frozen_package_content_is_pinned(setup, path, code):
    docker, files = setup
    files[p.ROOT / path] += b'\n'
    assert_error(code, lambda: p.inspect_baseline(docker))
    assert not docker.calls


@pytest.mark.parametrize('filename,code', [
    ('baseline.json', 'R53_R52_BASELINE_CHANGED'),
    ('applied.json', 'R53_R52_RECEIPT_CHANGED'),
    ('progress.json', 'R53_R52_PROGRESS_CHANGED'),
    ('prepared.json', 'R53_R52_PREPARED_CHANGED')])
def test_transaction_proofs_cannot_change(setup, filename, code):
    docker, files = setup
    files[p.TRANSACTION / filename] = b'{}\n'
    assert_error(code, lambda: p.inspect_baseline(docker))


def test_release_file_changed_during_double_read(setup, monkeypatch):
    docker, files = setup
    count = 0
    def read(path):
        nonlocal count
        path = Path(path)
        if path == p.ROOT / 'transition.json':
            count += 1
            if count > 1:
                return b'{}'
        return files[path]
    monkeypatch.setattr(p, '_read_public', read)
    assert_error('R53_RELEASE_CHANGED_DURING_CHECK', lambda: p.inspect_baseline(docker))


def test_docker_failure_is_redacted(setup):
    docker, _ = setup
    docker.failure = True
    assert_error('R53_DOCKER_CHECK_FAILED', lambda: p.inspect_baseline(docker))


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'not json', b'\xff'])
def test_json_rejects_ambiguous_or_malformed_metadata(raw):
    assert_error('R53_METADATA_JSON_INVALID', lambda: p._json(raw))


def test_baseline_invalid_is_redacted(setup):
    docker, _ = setup
    assert_error('R53_BASELINE_INVALID', lambda: p.verify_unchanged(docker, {}))


def test_host_credential_metadata_only_no_open(monkeypatch):
    import stat
    monkeypatch.setattr(p, '_safe_parents', lambda path: None)
    file = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=10001,
                           st_gid=10001, st_size=0, st_dev=8, st_ino=4242)
    monkeypatch.setattr(type(p.CREDENTIAL_PATH), 'lstat', lambda self: file)
    monkeypatch.setattr(p.os, 'open', lambda *a, **k: pytest.fail('credential content must not be opened'))
    assert p._credential_identity() == IDENTITY
    assert_error('R53_CREDENTIAL_SIZE_INVALID', lambda: p._credential_identity(strict_size=True))
    file.st_size = 100
    assert p._credential_identity(strict_size=True) == IDENTITY
    file.st_nlink = 2
    assert_error('R53_CREDENTIAL_METADATA_INVALID', p._credential_identity)
