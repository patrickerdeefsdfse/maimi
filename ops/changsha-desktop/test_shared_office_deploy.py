"""Synthetic configuration/Compose checks only; never read live company files."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('office_configuration_test', HERE / 'configure-office.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    if os.name != 'posix' or os.geteuid() != 0:
        pytest.skip('real ownership checks run as local WSL root')
    ops, private = tmp_path / 'ops', tmp_path / 'private'
    ops.mkdir()
    (ops / '.env').write_text('IAM_CLIENT_ID=synthetic-existing\nPOSTGRES_PASSWORD=synthetic-preserved\n')
    calls = []
    monkeypatch.setattr(module, 'quiet_compose', lambda directory, config, **kwargs: calls.append((directory, config, kwargs)))
    return ops, private, calls


def test_python310_and_shell_files_are_lf():
    ast.parse((HERE / 'configure-office.py').read_bytes(), feature_version=(3, 10))
    for name in ('configure-office.sh', 'start-server.sh'):
        body = (HERE / name).read_bytes()
        assert b'\r' not in body and body.startswith(b'#!/usr/bin/env bash\n')


def test_independent_key_generated_once_without_reading_iam(configuration, monkeypatch):
    ops, private, calls = configuration
    before = (ops / '.env').read_bytes()
    read_text = Path.read_text
    def guarded(path, *args, **kwargs):
        assert path.name != '.env', 'Office configuration must not collect IAM/database values'
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', guarded)
    assert module.configure(ops, private) == 'INDEPENDENT_KEY_CREATED_OR_PRESERVED'
    secret = private / 'office-service-secret'
    value, inode = secret.read_bytes(), secret.stat().st_ino
    assert len(value.strip()) >= 32
    assert (secret.stat().st_uid, secret.stat().st_gid, stat.S_IMODE(secret.stat().st_mode)) == (0, 10001, 0o440)
    assert stat.S_IMODE((ops / '.office.env').stat().st_mode) == 0o600
    assert (ops / '.env').read_bytes() == before
    monkeypatch.setattr(module.secrets, 'token_urlsafe', lambda _: pytest.fail('Existing key rotated'))
    assert module.configure(ops, private) == 'EXISTING_KEY_AND_SETTINGS_PRESERVED'
    assert (secret.read_bytes(), secret.stat().st_ino) == (value, inode)
    assert len(calls) == 2


def test_invalid_config_and_missing_existing_secret_fail_closed(configuration):
    ops, private, _ = configuration
    module.configure(ops, private)
    config = ops / '.office.env'
    config.write_text(config.read_text() + 'IAM_CLIENT_ID=forbidden-override\n')
    with pytest.raises(ValueError, match='UNKNOWN_OR_DUPLICATE'):
        module.check(ops)
    config.write_text(config.read_text().split('IAM_CLIENT_ID=')[0])
    (private / 'office-service-secret').unlink()
    with pytest.raises(ValueError, match='REQUIRED_FILE_MISSING'):
        module.configure(ops, private)
    assert not (private / 'office-service-secret').exists()


def test_failed_compose_preserves_old_config_and_removes_only_new_key(configuration, monkeypatch):
    ops, private, _ = configuration
    before = (ops / '.env').read_bytes()
    monkeypatch.setattr(module, 'quiet_compose', lambda *a, **k: (_ for _ in ()).throw(ValueError('OFFICE_COMPOSE_CHECK_FAILED_NO_SERVICES_STARTED')))
    with pytest.raises(ValueError, match='COMPOSE_CHECK_FAILED'):
        module.configure(ops, private)
    assert (ops / '.env').read_bytes() == before
    assert not (ops / '.office.env').exists() and not (private / 'office-service-secret').exists()
    assert not (ops / '.office-configuration-in-progress').exists()
    assert not list(ops.glob('.office-check-*'))


def test_private_secret_symlink_wrong_permissions_and_incomplete_marker(configuration):
    ops, private, _ = configuration
    module.configure(ops, private)
    secret = private / 'office-service-secret'
    secret.chmod(0o444)
    with pytest.raises(ValueError, match='METADATA_INVALID'):
        module.check(ops)
    secret.chmod(0o440)
    link = private / 'linked-office-secret'
    link.symlink_to(secret)
    with pytest.raises(ValueError, match='PATH_UNSAFE'):
        module.secret_check(link)
    (ops / '.office-configuration-in-progress').write_text('synthetic interrupted operation')
    with pytest.raises(ValueError, match='CONFIGURATION_INCOMPLETE'):
        module.configure(ops, private)


def test_resource_configuration_rejects_invalid_or_unbounded_values(configuration):
    ops, private, _ = configuration
    module.configure(ops, private)
    values = module.read_config(ops / '.office.env')
    for changes in ({'OFFICE_CONCURRENCY': '0'}, {'OFFICE_QUEUE_LIMIT': '999'},
                    {'OFFICE_MEMORY_LIMIT': '999g'}, {'OFFICE_CPU_LIMIT': '0'},
                    {'OFFICE_SPOOL_BYTES': str(8 * 1024**3), 'OFFICE_MEMORY_LIMIT': '8g'}):
        with pytest.raises(ValueError):
            module.validate({**values, **changes})
    assert values['OFFICE_CONCURRENCY'] == '2' and values['OFFICE_QUEUE_LIMIT'] == '16'


def test_secret_length_matches_gateway_and_worker_contract(configuration):
    ops, private, _ = configuration
    module.configure(ops, private)
    secret = private / 'office-service-secret'
    secret.write_text('s' * 256 + '\n')
    module.secret_check(secret)
    secret.write_text('s' * 257 + '\n')
    with pytest.raises(ValueError, match='METADATA_INVALID'):
        module.secret_check(secret)


@pytest.mark.skipif(os.name == 'nt', reason='Bash CLI test uses WSL')
@pytest.mark.parametrize('enabled,failed,connectors', [(False, False, False), (True, False, False), (True, True, False), (True, False, True)])
def test_startup_office_opt_in_clear_exports_and_validate_before_up(tmp_path, enabled, failed, connectors):
    shutil.copy2(HERE / 'start-server.sh', tmp_path / 'start-server.sh')
    binaries, log = tmp_path / 'bin', tmp_path / 'calls.jsonl'
    binaries.mkdir()
    stub = '''#!PYTHON
import json,os,sys
from pathlib import Path
with open(os.environ['OFFICE_CALL_LOG'],'a') as stream:
 stream.write(json.dumps({'name':Path(sys.argv[0]).name,'args':sys.argv[1:],'exported':os.getenv('OFFICE_SERVICE_SECRET_SOURCE')})+'\\n')
if 'configure-office.py' in sys.argv and os.getenv('OFFICE_FORCE_FAILURE')=='1':sys.exit(1)
'''.replace('#!PYTHON', '#!' + sys.executable)
    for name in ('docker', 'python3'):
        path = binaries / name
        path.write_text(stub)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'], 'OFFICE_CALL_LOG': str(log),
           'OFFICE_FORCE_FAILURE': '1' if failed else '0', 'OFFICE_SERVICE_SECRET_SOURCE': 'injected-invalid-secret'}
    args = ['bash', str(tmp_path / 'start-server.sh'), '--nginx-network']
    if enabled:
        args += ['--shared-office']
    if connectors:
        args += ['--connectors']
    result = subprocess.run(args, env=env, capture_output=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(call['exported'] is None for call in calls)
    up = [call for call in calls if 'up' in call['args']]
    office = [call for call in calls if 'configure-office.py' in call['args']]
    assert bool(office) == enabled
    if failed:
        assert result.returncode != 0 and not up
    else:
        assert result.returncode == 0 and len(up) == 1
        assert ('office.compose.yaml' in up[0]['args']) == enabled
        assert ('.office.env' in up[0]['args']) == enabled
    if office:
        assert '--check' in office[0]['args'] and '--nginx-network' in office[0]['args']
        assert ('--connectors' in office[0]['args']) == connectors
        assert calls.index(office[0]) < next((i for i, call in enumerate(calls) if 'up' in call['args']), len(calls))


@pytest.mark.skipif(os.name == 'nt' or not shutil.which('docker'), reason='local Compose parser only')
@pytest.mark.parametrize('nginx,connectors', [(False, False), (True, False), (True, True)])
def test_real_compose_merge_keeps_cloud_network_and_office_private(tmp_path, nginx, connectors):
    values = {'POSTGRES_PASSWORD': 'synthetic-password-not-real', 'IAM_CLIENT_ID': 'synthetic-iam-app'}
    for key in ('IAM_CLIENT_SECRET_SOURCE', 'IAM_TOKEN_ENCRYPTION_KEY_SOURCE', 'PERSONAL_KEY_SERVICE_SECRET_SOURCE', 'PERSONAL_CLOUD_MANAGER_SECRET_SOURCE'):
        values[key] = str(tmp_path / key.lower())
    base = tmp_path / '.env'
    base.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    office = tmp_path / '.office.env'
    office.write_text(f'OFFICE_SERVICE_SECRET_SOURCE={tmp_path}/office-service-secret\n')
    args = ['docker', 'compose', '--env-file', str(base)]
    if connectors:
        connector = tmp_path / '.connectors.env'
        connector.write_text(f'CONNECTOR_CONFIG_SOURCE={tmp_path}/connector-files\n')
        args += ['--env-file', str(connector)]
    args += ['--env-file', str(office)]
    names = ['compose.yaml', 'personal-cloud.compose.yaml']
    if nginx:
        names.append('compose.nginx-network.yaml')
    if connectors:
        names.append('connectors.compose.yaml')
    names.append('office.compose.yaml')
    for name in names:
        args += ['-f', str(HERE / name)]
    env = {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'DOCKER_HOST': 'unix:///var/run/docker.sock'}
    result = subprocess.run(args + ['config', '--format', 'json'], env=env, capture_output=True, timeout=25)
    assert result.returncode == 0, 'Synthetic full Compose merge failed'
    merged = json.loads(result.stdout)
    gateway, worker = merged['services']['gateway'], merged['services']['office-service']
    assert set(gateway['networks']) == {'data', 'outbound', 'cloud-workers', 'office'} | ({'common-net'} if nginx else set())
    assert set(worker['networks']) == {'office'} and merged['networks']['office']['internal'] is True
    assert not worker.get('ports') and worker['user'] == '10001:10001' and worker['read_only'] is True
    assert int(worker['mem_limit']) == 8 * 1024**3 and float(worker['cpus']) == 2 and int(worker['pids_limit']) == 256
    assert gateway['environment']['OFFICE_SERVICE_URL'] == 'http://office-service:8093'
    assert gateway['environment']['IAM_CLIENT_ID'] == 'synthetic-iam-app'
    assert merged['services']['personal-cloud-manager']['environment']['PERSONAL_CLOUD_MAX_ACTIVE_HOSTS'] == '2'
    assert any('/var/lib/megmeet-office/jobs:' in row and 'uid=10001,gid=10001,mode=0700' in row for row in worker['tmpfs'])
    assert len(worker['volumes']) == 1
    for service in (gateway, worker):
        mount = next(row for row in service['volumes'] if row['target'] == '/run/secrets/office-service-secret')
        assert mount['read_only'] is True and mount['bind'].get('create_host_path', False) is False
    assert not any('IAM_' in key or 'TOKENKEY' in key for key in worker['environment'])


def test_quiet_compose_never_exposes_settings_or_uses_exported_secrets(tmp_path, monkeypatch):
    for name in ('.env', '.office.env', 'compose.yaml', 'personal-cloud.compose.yaml', 'compose.nginx-network.yaml', 'connectors.compose.yaml', 'office.compose.yaml'):
        (tmp_path / name).write_text('synthetic fixture')
    captured = []
    def run(args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, b'SYNTHETIC_SECRET_STDOUT', b'SYNTHETIC_SECRET_STDERR')
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setenv('OFFICE_SERVICE_SECRET_SOURCE', 'SYNTHETIC_EXPORTED_SECRET')
    with pytest.raises(ValueError, match='^OFFICE_COMPOSE_CHECK_FAILED_NO_SERVICES_STARTED$'):
        module.quiet_compose(tmp_path, tmp_path / '.office.env', nginx_network=True, connectors=True)
    args, options = captured[0]
    assert args[-2:] == ['config', '--quiet'] and 'up' not in args
    assert set(options['env']) == {'PATH', 'LANG', 'DOCKER_HOST'} and options['capture_output'] is True


@pytest.mark.skipif(os.name != 'posix' or not shutil.which('docker'), reason='real local Bash/Compose only')
def test_actual_configuration_shim_create_recheck_preserves_existing_files(tmp_path):
    if os.geteuid() != 0:
        pytest.skip('real root:10001 file ownership')
    directory, private = tmp_path / 'ops', tmp_path / 'private'
    directory.mkdir()
    for name in ('compose.yaml', 'personal-cloud.compose.yaml', 'compose.nginx-network.yaml', 'office.compose.yaml'):
        shutil.copy2(HERE / name, directory / name)
    values = {'POSTGRES_PASSWORD': 'synthetic-password-not-real', 'IAM_CLIENT_ID': 'synthetic-iam-app'}
    for key in ('IAM_CLIENT_SECRET_SOURCE', 'IAM_TOKEN_ENCRYPTION_KEY_SOURCE', 'PERSONAL_KEY_SERVICE_SECRET_SOURCE', 'PERSONAL_CLOUD_MANAGER_SECRET_SOURCE'):
        values[key] = str(tmp_path / key.lower())
    base = directory / '.env'
    base.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    base.chmod(0o600)
    original = base.read_bytes()
    args = ['bash', str(HERE / 'configure-office.sh'), '--directory', str(directory),
            '--secrets-directory', str(private), '--nginx-network']
    first = subprocess.run(args, capture_output=True, timeout=30)
    assert first.returncode == 0 and b'SERVICES_STARTED: NO' in first.stdout
    secret = private / 'office-service-secret'
    saved = secret.read_bytes()
    second = subprocess.run(args + ['--check'], capture_output=True, timeout=30)
    assert second.returncode == 0 and b'CHECKED' in second.stdout
    assert secret.read_bytes() == saved and base.read_bytes() == original
    assert saved.strip() not in first.stdout + first.stderr + second.stdout + second.stderr


@pytest.mark.skipif(os.name != 'posix' or not shutil.which('docker'), reason='real local Compose only')
def test_increment_uses_old_base_without_copying_env_and_checks_extra_overlay(tmp_path):
    if os.geteuid() != 0:
        pytest.skip('real root:10001 file ownership')
    old, new, private = tmp_path / 'old', tmp_path / 'new', tmp_path / 'private'
    old.mkdir()
    new.mkdir()
    for name in ('compose.yaml', 'personal-cloud.compose.yaml', 'compose.nginx-network.yaml'):
        shutil.copy2(HERE / name, old / name)
    shutil.copy2(HERE / 'office.compose.yaml', new / 'office.compose.yaml')
    values = {'POSTGRES_PASSWORD': 'synthetic-password-not-real', 'IAM_CLIENT_ID': 'synthetic-iam-app'}
    for key in ('IAM_CLIENT_SECRET_SOURCE', 'IAM_TOKEN_ENCRYPTION_KEY_SOURCE', 'PERSONAL_KEY_SERVICE_SECRET_SOURCE', 'PERSONAL_CLOUD_MANAGER_SECRET_SOURCE'):
        values[key] = str(tmp_path / key.lower())
    base = old / '.env'
    base.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    base.chmod(0o600)
    original = base.read_bytes()
    overlay = tmp_path / 'images.compose.yaml'
    overlay.write_text('services:\n  gateway:\n    image: megmeet-ai-gateway:shared-20260921-r12\n  office-service:\n    image: megmeet-office-service:shared-20260921-r12\n')
    args = ['bash', str(HERE / 'configure-office.sh'), '--directory', str(new), '--base-directory', str(old),
            '--secrets-directory', str(private), '--nginx-network', '--compose-file', str(overlay)]
    first = subprocess.run(args, capture_output=True, timeout=30)
    assert first.returncode == 0, 'increment office configuration failed'
    saved = (private / 'office-service-secret').read_bytes()
    second = subprocess.run(args + ['--check'], capture_output=True, timeout=30)
    assert second.returncode == 0 and not (new / '.env').exists()
    assert base.read_bytes() == original and (private / 'office-service-secret').read_bytes() == saved
    assert saved.strip() not in first.stdout + first.stderr + second.stdout + second.stderr
    overlay.write_text('services:\n  gateway:\n    invalid_compose_field: refused\n')
    rejected = subprocess.run(args + ['--check'], capture_output=True, timeout=30)
    assert rejected.returncode != 0 and b'COMPOSE_CHECK_FAILED' in rejected.stderr
    assert base.read_bytes() == original
