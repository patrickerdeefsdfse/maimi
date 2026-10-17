"""Activation failure tests with synthetic config and mocked Docker only."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
import pilot_deploy as deploy
import pilot_nginx


@pytest.fixture
def system(monkeypatch, tmp_path):
    base = tmp_path / 'pilot'
    base.mkdir()
    conf = tmp_path / 'default.conf'
    conf.write_text('original-config')
    monkeypatch.setattr(deploy, 'BASE', base)
    monkeypatch.setattr(deploy, 'CONF', conf)
    monkeypatch.setattr(pilot_nginx, 'patch_nginx', lambda text: ('personal-config', {'changed': True}))
    monkeypatch.setattr(deploy, 'read_setup', lambda: {'bindings': []})
    monkeypatch.setattr(deploy, 'ensure_volumes', lambda bindings: None)
    monkeypatch.setattr(deploy, 'owned_containers', lambda: None)
    monkeypatch.setattr(deploy, 'private_networks', lambda **kwargs: None)
    monkeypatch.setattr(deploy, 'nginx_entry_dns', lambda: None)
    monkeypatch.setattr(deploy, 'compose', lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy, 'wait_ready', lambda *args: None)
    monkeypatch.setattr(deploy, 'runtime_checks', lambda: None)
    monkeypatch.setattr(deploy.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(deploy, 'atomic', lambda path, data, **kwargs: path.write_bytes(data))
    calls = []
    def inspect(name, template):
        if name == 'nginx-https':
            return 'true' if template == '{{.State.Running}}' else '{"common-net":{}}'
        return 'gateway-unchanged'
    monkeypatch.setattr(deploy, 'inspect', inspect)
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'inspect':
            if args[-1] in deploy.SHARED_HOSTS:
                return SimpleNamespace(returncode=0, stdout='a'*64 + ' true dsh-web-host')
            return SimpleNamespace(returncode=0, stdout='true')
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(deploy, 'docker', docker)
    monkeypatch.setattr(deploy, 'public_status', lambda *args, **kwargs: None)
    return base, conf, calls, docker


def test_failed_public_check_restores_config_and_does_not_retire_hosts(system, monkeypatch):
    base, conf, calls, _ = system
    def fail(*args, **kwargs):
        raise deploy.Failure('PUBLIC_ROUTE_UNEXPECTED_STATUS')
    monkeypatch.setattr(deploy, 'public_status', fail)
    with pytest.raises(deploy.Failure, match='PUBLIC_ROUTE_UNEXPECTED_STATUS'):
        deploy.activate()
    assert conf.read_text() == 'original-config'
    assert not (base / 'active.json').exists()
    assert not any(call[0] == 'stop' for call in calls)
    assert sum(call == ('exec', 'nginx-https', 'nginx', '-s', 'reload') for call in calls) == 2


def test_activation_record_precedes_shared_host_retirement(system, monkeypatch):
    base, conf, calls, original_docker = system
    def docker(*args, **kwargs):
        if args[0] == 'stop':
            assert (base / 'active.json').exists()
            assert conf.read_text() == 'personal-config'
            return SimpleNamespace(returncode=1, stdout='')
        return original_docker(*args, **kwargs)
    monkeypatch.setattr(deploy, 'docker', docker)
    with pytest.raises(deploy.Failure, match='ROUTES_ACTIVE_SHARED_HOST_STOP_FAILED'):
        deploy.activate()
    assert conf.read_text() == 'personal-config'
    assert '"shared_host_retirement": "pending"' in (base / 'active.json').read_text()


def test_wrong_shared_role_fails_before_proxy_mutation(system, monkeypatch):
    base, conf, calls, original_docker = system
    def docker(*args, **kwargs):
        if args[0] == 'inspect' and args[-1] in deploy.SHARED_HOSTS:
            return SimpleNamespace(returncode=0, stdout='a'*64 + ' true unrelated-company-app')
        return original_docker(*args, **kwargs)
    monkeypatch.setattr(deploy, 'docker', docker)
    with pytest.raises(deploy.Failure, match='SHARED_HOST_ROLE_CHANGED'):
        deploy.activate()
    assert conf.read_text() == 'original-config'
    assert not (base / 'active.json').exists()


def test_loopback_probe_does_not_carry_auth_or_change_transport_checks(monkeypatch):
    seen = []
    def execute(args, **kwargs):
        seen.extend(args)
        return SimpleNamespace(returncode=0, stdout='{}\n403')
    monkeypatch.setattr(deploy, 'execute', execute)
    deploy.public_status('/megmeet-ai-native/__megmeet/identity', expected=403)
    assert '--resolve' in seen and 'platform-ai.megmeet.com:443:127.0.0.1' in seen
    assert '--noproxy' in seen and seen[seen.index('--noproxy') + 1] == '*'
    assert not any('Cookie' in value or 'Bearer' in value for value in seen)
    assert 'verify=True' in deploy.SSO_TRANSPORT


def test_foreign_container_or_unowned_private_network_rejected(monkeypatch):
    record = {'labels': {deploy.LABEL: deploy.VERSION, 'com.docker.compose.project': deploy.PROJECT},
              'internal': True, 'members': {'f'*64: {'Name': 'foreign-listener'}}}
    monkeypatch.setattr(deploy, 'docker', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(record)))
    with pytest.raises(deploy.Failure, match='FOREIGN_CONTAINER_ON_PRIVATE_NETWORK'):
        deploy.private_networks()
    record['members'] = {}
    record['labels'] = {}
    with pytest.raises(deploy.Failure, match='PRIVATE_NETWORK_OWNERSHIP_MISMATCH'):
        deploy.private_networks()


def test_volume_create_race_cannot_adopt_foreign_volume(monkeypatch):
    calls = []
    count = 0
    def docker(*args, **kwargs):
        nonlocal count
        calls.append(args)
        if args[:2] == ('volume', 'inspect'):
            count += 1
            return SimpleNamespace(returncode=1 if count == 1 else 0, stdout='{}')
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(deploy, 'docker', docker)
    with pytest.raises(deploy.Failure, match='EXISTING_VOLUME_OWNERSHIP_MISMATCH'):
        deploy.ensure_volumes([{'user_id': '11111111-1111-1111-1111-111111111111'}])
    assert not any(call[0] == 'run' for call in calls)
