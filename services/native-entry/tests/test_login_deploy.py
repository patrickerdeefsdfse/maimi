"""Login-only deployment transaction tests; Docker and HTTP are never used."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
import login_deploy as deploy  # noqa: E402


OLD = b'# original entry code\n'
FILES = {'entry_app.py': b'# exact upgraded entry code\n',
         'entry_login.py': b'# exact upgraded login helper\n'}


@contextmanager
def transaction(tmp_path):
    base = tmp_path / 'pilot'
    app = base / 'app'
    app.mkdir(parents=True)
    (app / 'entry_app.py').write_bytes(OLD)
    before = {'a': 'synthetic-unchanged-fingerprint'}
    specification, tracked = {'synthetic': 'specification'}, {'synthetic': 'hash'}
    actions = []

    def atomic(path, raw, **kwargs):
        actions.append(('write', path.name))
        path.write_bytes(raw)

    def directory(path, create=False):
        if create:
            path.mkdir()
        assert path.is_dir()

    mocks = {}
    with patch.multiple(deploy, BASE=base, APP=app, JOURNAL=base / 'login-upgrade-state.json'), \
            patch.dict(deploy.OLD_FILES, {'entry_app.py': deploy.digest(OLD)}), \
            patch.object(deploy, 'read_file', side_effect=lambda path, **kwargs: path.read_bytes()), \
            patch.object(deploy, 'atomic', side_effect=atomic), \
            patch.object(deploy, 'private_directory', side_effect=directory), \
            patch.object(deploy, 'preflight', return_value=(specification, tracked)), \
            patch.object(deploy, 'fingerprints', return_value=before):
        with patch.object(deploy, 'compose_recreate') as recreate, \
                patch.object(deploy, 'wait_ready') as ready, \
                patch.object(deploy, 'verify_entry') as verify, \
                patch.object(deploy, 'verify_runtime_files') as runtime_files, \
                patch.object(deploy, 'reload_nginx') as reload, \
                patch.object(deploy, 'public_checks') as public, \
                patch.object(deploy, 'unchanged') as unchanged:
            mocks.update(recreate=recreate, ready=ready, verify=verify, reload=reload,
                         public=public, unchanged=unchanged, base=base, app=app,
                         actions=actions, before=before, tracked=tracked, spec=specification)
            yield mocks


def test_upgrade_writes_only_entry_pair_and_retains_private_original(tmp_path):
    with transaction(tmp_path) as state:
        deploy.upgrade(FILES)
        assert (state['app'] / 'entry_app.py').read_bytes() == FILES['entry_app.py']
        assert (state['app'] / 'entry_login.py').read_bytes() == FILES['entry_login.py']
        journal = json.loads(deploy.JOURNAL.read_text())
        assert journal['state'] == 'complete'
        assert (state['base'] / journal['backup'] / 'entry_app.py').read_bytes() == OLD
        state['recreate'].assert_called_once_with()
        state['reload'].assert_called_once_with()
        state['public'].assert_called_once_with(upgraded=True)
        state['unchanged'].assert_called_with(state['before'], state['tracked'])
        assert {name for action, name in state['actions']} <= {'entry_app.py', 'entry_login.py', 'login-upgrade-state.json'}


def test_exact_applied_rerun_checks_without_recreating_or_writing(tmp_path):
    with transaction(tmp_path) as state:
        deploy.upgrade(FILES)
        state['recreate'].reset_mock()
        state['reload'].reset_mock()
        state['public'].reset_mock()
        state['actions'].clear()
        deploy.upgrade(FILES)
        state['recreate'].assert_not_called()
        state['reload'].assert_not_called()
        state['public'].assert_called_once_with(upgraded=True)
        assert not state['actions']


def test_unknown_entry_drift_refuses_before_backup_or_runtime_mutation(tmp_path):
    with transaction(tmp_path) as state:
        (state['app'] / 'entry_app.py').write_bytes(b'unknown deployment')
        with pytest.raises(deploy.Stop, match='ENTRY_SOURCE_DRIFT'):
            deploy.upgrade(FILES)
        state['recreate'].assert_not_called()
        assert not state['actions']


def test_public_validation_failure_restores_exact_original_and_recreates_only_entry(tmp_path):
    with transaction(tmp_path) as state:
        state['public'].side_effect = [deploy.Stop('SYNTHETIC_PUBLIC_FAILURE'), None]
        with pytest.raises(deploy.Stop, match='SYNTHETIC_PUBLIC_FAILURE'):
            deploy.upgrade(FILES)
        assert (state['app'] / 'entry_app.py').read_bytes() == OLD
        assert not (state['app'] / 'entry_login.py').exists()
        assert json.loads(deploy.JOURNAL.read_text())['state'] == 'restored'
        assert state['recreate'].call_count == 2
        assert state['reload'].call_count == 2
        assert state['public'].call_args_list[-1].kwargs == {'upgraded': False}


def test_recreate_failure_restores_source_even_when_candidate_never_started(tmp_path):
    with transaction(tmp_path) as state:
        state['recreate'].side_effect = [deploy.Stop('SYNTHETIC_START_FAILED'), None]
        with pytest.raises(deploy.Stop, match='SYNTHETIC_START_FAILED'):
            deploy.upgrade(FILES)
        assert (state['app'] / 'entry_app.py').read_bytes() == OLD
        assert not (state['app'] / 'entry_login.py').exists()
        assert json.loads(deploy.JOURNAL.read_text())['state'] == 'restored'


def test_pending_known_transaction_is_restored_before_fresh_upgrade(tmp_path):
    with transaction(tmp_path) as state:
        backup = state['base'] / ('login-backup-' + 'a' * 32)
        backup.mkdir()
        (backup / 'entry_app.py').write_bytes(OLD)
        (state['app'] / 'entry_app.py').write_bytes(FILES['entry_app.py'])
        (state['app'] / 'entry_login.py').write_bytes(FILES['entry_login.py'])
        deploy.JOURNAL.write_text(json.dumps({'release': deploy.RELEASE, 'state': 'applying',
            'backup': backup.name, 'protected': state['before'], 'file_hashes': state['tracked']}))
        deploy.upgrade(FILES)
        assert state['recreate'].call_count == 2
        assert state['verify'].call_args_list[0].kwargs == {'healthy': False, 'allow_missing': True}
        assert state['public'].call_args_list[0].kwargs == {'upgraded': False}
        assert json.loads(deploy.JOURNAL.read_text())['state'] == 'complete'


def test_restore_never_overwrites_an_unknown_helper_created_during_failure(tmp_path):
    with transaction(tmp_path) as state:
        def unknown_helper(**kwargs):
            (state['app'] / 'entry_login.py').write_bytes(b'foreign helper')
            raise deploy.Stop('SYNTHETIC_FAILURE')
        state['public'].side_effect = unknown_helper
        with pytest.raises(deploy.Stop, match='SYNTHETIC_FAILURE'):
            deploy.upgrade(FILES)
        assert (state['app'] / 'entry_login.py').read_bytes() == b'foreign helper'
        assert json.loads(deploy.JOURNAL.read_text())['state'] == 'applying'
        assert state['recreate'].call_count == 1


def test_compose_command_cannot_recreate_host_gateway_or_build_images():
    with patch.object(deploy, 'docker') as docker:
        deploy.compose_recreate()
    args = docker.call_args.args
    assert args == ('compose', '--env-file', '/dev/null', '-f', str(deploy.BASE / 'pilot.compose.json'),
                    '-p', deploy.PROJECT, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'entry')
    assert docker.call_args.kwargs['timeout'] >= 30


def test_recovery_accepts_absence_only_after_successful_exact_name_query():
    replies = [subprocess.CompletedProcess([], 1, '', 'not shown'),
               subprocess.CompletedProcess([], 0, '', '')]
    with patch.object(deploy, 'docker', side_effect=replies) as docker:
        deploy.verify_entry({}, healthy=False, allow_missing=True)
    assert docker.call_args_list[1].args == ('ps', '-a', '--filter', 'name=^/' + deploy.ENTRY + '$', '--format', '{{.Names}}')
    replies[1] = subprocess.CompletedProcess([], 0, deploy.ENTRY, '')
    with patch.object(deploy, 'docker', side_effect=replies), pytest.raises(deploy.Stop, match='ENTRY_NAME_IN_USE'):
        deploy.verify_entry({}, healthy=False, allow_missing=True)


def login_fixture():
    headers = {'content-type': 'text/html; charset=utf-8',
               'content-security-policy': '; '.join(sorted(deploy.CSP_REQUIRED)),
               'cache-control': 'private, no-store'}
    body = '\n'.join(deploy.HTML_MARKERS)
    return 403, headers, body


def test_exact_login_html_markers_and_csp_pass():
    deploy.verify_login_response(*login_fixture())


def test_deployer_contract_matches_real_login_module_without_calling_sso():
    from entry_login import LOGIN_HEADERS, login_page, login_script
    from starlette.responses import HTMLResponse
    response = HTMLResponse(login_page(deploy.PUBLIC), status_code=403, headers=LOGIN_HEADERS)
    deploy.verify_login_response(response.status_code, dict(response.headers), response.body.decode())
    assert login_script(deploy.PUBLIC).lstrip().startswith('// ' + deploy.MARKER)


def test_runtime_code_check_uses_only_fixed_files_and_known_config_environment():
    expected = {name: deploy.digest(raw) for name, raw in FILES.items()}
    with patch.object(deploy, 'docker', return_value=subprocess.CompletedProcess([], 0, json.dumps(expected), '')) as docker:
        deploy.verify_runtime_files(FILES, upgraded=True)
    command = docker.call_args.args
    assert command[:5] == ('exec', deploy.ENTRY, 'python', '-B', '-c')
    assert 'MEGMEET_ENTRY_CONFIG' in command[-1]
    assert 'os.environ' not in command[-1] and '/run/secrets/' not in command[-1]
    with patch.object(deploy, 'docker', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
        with pytest.raises(deploy.Stop, match='RUNTIME_SOURCE_OR_CONFIG_MISMATCH'):
            deploy.verify_runtime_files(FILES, upgraded=True)


@pytest.mark.parametrize('mutation', ['missing-marker', 'duplicate-marker', 'inline', 'no-csp', 'cookie', 'cache', 'status'])
def test_broken_or_weakened_login_response_is_rejected(mutation):
    code, headers, body = login_fixture()
    if mutation == 'missing-marker':
        body = ''
    elif mutation == 'duplicate-marker':
        body += deploy.HTML_MARKERS[0]
    elif mutation == 'inline':
        headers['content-security-policy'] += "; script-src 'unsafe-inline'"
    elif mutation == 'no-csp':
        headers.pop('content-security-policy')
    elif mutation == 'cookie':
        headers['set-cookie'] = 'synthetic=never-send'
    elif mutation == 'cache':
        headers['cache-control'] = 'public, max-age=60'
    else:
        code = 200
    with pytest.raises(deploy.Stop):
        deploy.verify_login_response(code, headers, body)


def test_anonymous_curl_disables_user_config_proxies_and_sends_no_credentials():
    output = 'HTTP/1.1 403 Forbidden\r\nContent-Type: text/html\r\nVary: Cookie\r\nVary: Accept-Encoding\r\nCache-Control: private\r\nCache-Control: no-store\r\n\r\nbody'
    with patch.object(deploy, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')) as run:
        status, headers, body = deploy.anonymous_request('/megmeet-ai-native/', 'text/html')
    command = run.call_args.args[0]
    assert command[:2] == ['curl', '--disable']
    assert command[command.index('--noproxy') + 1] == '*'
    assert command[command.index('--resolve') + 1] == 'platform-ai.megmeet.com:443:127.0.0.1'
    assert [command[index + 1] for index, item in enumerate(command) if item == '--header'] == ['Accept: text/html']
    assert '--location' not in command and '--cookie' not in command
    assert status == 403 and body == 'body'
    assert headers['vary'] == 'Cookie, Accept-Encoding'
    assert headers['cache-control'] == 'private, no-store'


def test_anonymous_probe_rejects_arbitrary_ss_o_or_model_paths():
    with patch.object(deploy, 'run') as run, pytest.raises(deploy.Stop, match='PROBE_PATH_INVALID'):
        deploy.anonymous_request('/v1/auth/query-user', 'application/json')
    run.assert_not_called()


def test_nginx_reload_waits_for_unique_current_address_without_mutating_config():
    network = json.dumps({'common-net': {'IPAddress': '172.18.0.77'}})
    results = [subprocess.CompletedProcess([], 0, '172.18.0.66 stale\n172.18.0.77 current\n', ''),
               subprocess.CompletedProcess([], 0, '172.18.0.77 current\n', ''),
               subprocess.CompletedProcess([], 0, '', ''), subprocess.CompletedProcess([], 0, '', '')]
    with patch.object(deploy, 'inspect', return_value=network), patch.object(deploy, 'docker', side_effect=results) as docker, \
            patch.object(deploy.time, 'sleep') as sleep:
        deploy.reload_nginx()
    assert sleep.call_count == 1
    assert [call.args for call in docker.call_args_list] == [
        ('exec', 'nginx-https', 'getent', 'hosts', deploy.ENTRY),
        ('exec', 'nginx-https', 'getent', 'hosts', deploy.ENTRY),
        ('exec', 'nginx-https', 'nginx', '-t'),
        ('exec', 'nginx-https', 'nginx', '-s', 'reload')]


def test_mount_verification_rejects_writable_bootstrap_or_unexpected_volume():
    specification = {'services': {'entry': {'volumes': [
        {'type': 'bind', 'source': '/trusted/app', 'target': '/opt/megmeet/native-entry', 'read_only': True},
        {'type': 'volume', 'source': 'a-bootstrap', 'target': '/run/megmeet-bootstrap/a', 'read_only': True}]}},
        'volumes': {'a-bootstrap': {'name': 'owner-a-bootstrap'}}}
    mounts = [{'Type': 'bind', 'Source': '/trusted/app', 'Destination': '/opt/megmeet/native-entry', 'RW': False},
              {'Type': 'volume', 'Name': 'owner-a-bootstrap', 'Destination': '/run/megmeet-bootstrap/a', 'RW': False}]
    deploy.verify_mounts(mounts, specification)
    for field, value in [('RW', True), ('Name', 'owner-b-bootstrap')]:
        changed = copy.deepcopy(mounts)
        changed[1][field] = value
        with pytest.raises(deploy.Stop, match='ENTRY_MOUNT_CHANGED'):
            deploy.verify_mounts(changed, specification)
