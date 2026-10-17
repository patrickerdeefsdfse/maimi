import copy
import json
import pytest
import controller as c
from private_store import key_bytes, key_of, StoreError

OLD = key_bytes('SyntheticOldKey')
NEW = key_bytes('SyntheticNewKey')
IDENT = {'dev': 1, 'ino': 2, 'uid': 10001, 'gid': 10001, 'mode': 384, 'nlink': 1}
BASE = {'gateway_id': 'a'*64, 'credential_identity': IDENT, 'other_services': {'manager': 'unchanged'}}

class Store:
    def __init__(self):
        self.files = {}
        self.credential = OLD
        self.log = []
        self.fail_write = False
    def read_json(self, name, optional=False):
        data = self.read(name, optional=optional)
        return json.loads(data) if data is not None else None
    def read(self, name, optional=False):
        if name not in self.files and not optional:
            raise StoreError('PRIVATE_FILE_MISSING')
        return self.files.get(name)
    def write(self, name, raw):
        self.files[name] = raw
        self.log.append(('write', name))
    def write_json(self, name, data):
        self.write(name, json.dumps(data).encode())
    def read_credential(self, expected=None):
        if expected is not None:
            assert expected == IDENT
        key_of(self.credential)
        return self.credential, IDENT
    def write_credential_in_place(self, raw, expected):
        assert expected == IDENT
        assert self.read_json('journal.json')['phase'] in {'WRITING', 'RESTORING'}
        self.log.append(('credential', key_of(raw)))
        if self.fail_write:
            self.credential = b''
            raise StoreError('CREDENTIAL_WRITE_FAILED')
        self.credential = raw

class Env:
    def __init__(self):
        self.store = Store()
        self.is_running = True
        self.stamp = '2026-10-14T00:00:00.000000000Z'
        self.calls = []
        self.fail_provider_at = 0
        self.provider_count = 0
        self.fail_run = None
        self.drift = False
        self.fail_phase = None
        self.health_fail = False
        self.clock_value = 0
        self.ctl = c.Controller(self.store, self.run, lambda: key_of(NEW), self.provider,
                                self.inspect, self.verify, self.health, self.sleep, self.clock, self.progress)
    def clock(self): return self.clock_value
    def sleep(self, n): self.clock_value += 30
    def inspect(self, run):
        assert self.is_running
        return copy.deepcopy(BASE)
    def verify(self, run, baseline, gateway_running=True):
        if self.drift:
            raise c.preflight.PreflightError('SOURCE_CHANGED')
        assert baseline == BASE
        assert self.is_running is gateway_running
        self.calls.append(('verified', gateway_running))
    def progress(self, phase):
        self.calls.append(('phase', phase))
        if phase == self.fail_phase:
            raise KeyboardInterrupt()
    def provider(self, gid, key):
        assert gid == BASE['gateway_id']
        assert key == key_of(NEW)
        self.provider_count += 1
        self.calls.append(('provider', self.provider_count))
        if self.provider_count == self.fail_provider_at:
            raise c.provider_check.ProviderCheckError('MCP_AUTH_REJECTED')
        return {'status': 'VERIFIED', 'control_requests': 3, 'actual_tracking_acceptance': 'NOT_TESTED'}
    def health(self, run, gid, key):
        assert self.is_running and gid == BASE['gateway_id'] and key_of(self.store.credential) == key
        if self.health_fail:
            raise c.RotationError('GATEWAY_NOT_READY')
        return {'status': 'READY'}
    def run(self, argv, timeout=15, input_bytes=None):
        self.calls.append(('command', argv[1]))
        if argv[1] == self.fail_run:
            raise c.RotationError('LOCAL_COMMAND_TIMEOUT')
        if argv[1] == 'stop':
            assert self.store.read_json('journal.json')['phase'] in {'STOPPING', 'RESTORING'}
            assert argv[-1] == BASE['gateway_id']
            self.is_running = False
            return b'ok'
        if argv[1] == 'start':
            assert self.store.read_json('journal.json')['phase'] in {'STARTING', 'RESTORING'}
            self.is_running = True
            self.stamp = '2026-10-14T01:00:00.000000000Z'
            return b'ok'
        if argv[1] == 'inspect':
            return json.dumps(self.is_running if 'Running' in argv[-2] else self.stamp).encode()
        raise AssertionError('unexpected command')

def test_stage_saves_old_and_pending_without_mutating_active_or_stopping():
    e = Env()
    assert e.ctl.stage()['status'] == 'STAGED'
    assert e.store.credential == OLD and e.is_running
    assert e.store.files['previous-key.json'] == OLD
    assert e.store.files['pending-key.json'] == NEW
    assert ('command', 'stop') not in e.calls
    assert e.provider_count == 1
    assert e.store.read_json('journal.json')['phase'] == 'STAGED'

def test_same_key_changes_nothing():
    e = Env(); e.ctl.key_reader = lambda: key_of(OLD)
    assert e.ctl.stage()['status'] == 'UNCHANGED'
    assert not e.store.files and e.provider_count == 0

def test_invalid_new_key_no_calls_or_stage():
    e = Env(); e.ctl.key_reader = lambda: 'invalid key'
    with pytest.raises(StoreError): e.ctl.stage()
    assert not e.store.files and e.store.credential == OLD

def test_failed_new_account_preserves_old_and_no_stage():
    e = Env(); e.fail_provider_at = 1
    with pytest.raises(c.provider_check.ProviderCheckError): e.ctl.stage()
    assert not e.store.files and e.store.credential == OLD and e.is_running

def test_stage_does_not_overwrite_existing_rotation():
    e = Env(); e.ctl.stage()
    with pytest.raises(c.RotationError, match='EXISTING_ROTATION'): e.ctl.stage()

def test_apply_backups_and_journal_before_stop_and_write():
    e = Env(); e.ctl.stage()
    result = e.ctl.apply()
    assert result['status'] == 'APPLIED' and e.store.credential == NEW and e.is_running
    assert result['container_ids_changed'] is False
    assert e.provider_count == 3
    assert e.store.read_json('journal.json')['phase'] == 'APPLIED'
    assert e.store.files['previous-key.json'] == OLD
    serial = json.dumps(result)
    assert key_of(NEW) not in serial and key_of(OLD) not in serial

def test_repeated_apply_does_not_restart_or_requery():
    e = Env(); e.ctl.stage(); e.ctl.apply()
    before = sum(call == ('command', 'stop') for call in e.calls)
    assert e.ctl.apply()['rotation'] == 'APPLIED'
    assert e.provider_count == 3 and sum(call == ('command', 'stop') for call in e.calls) == before

def test_preapply_provider_failure_keeps_staged_and_running():
    e = Env(); e.ctl.stage(); e.fail_provider_at = 2
    with pytest.raises(c.provider_check.ProviderCheckError): e.ctl.apply()
    assert e.is_running and e.store.credential == OLD
    assert e.store.read_json('journal.json')['phase'] == 'STAGED'

@pytest.mark.parametrize('phase', ['STOPPING', 'STOPPED', 'WRITING', 'KEY_WRITTEN', 'STARTING', 'VERIFYING'])
def test_every_interruption_can_restore_old_configuration(phase):
    e = Env(); e.ctl.stage(); e.fail_phase = phase
    with pytest.raises(KeyboardInterrupt): e.ctl.apply()
    e.fail_phase = None
    result = e.ctl.restore()
    assert result['status'] == 'RESTORED_PREVIOUS_KEY' and e.is_running and e.store.credential == OLD
    assert e.store.read_json('journal.json')['phase'] == 'RESTORED'
    assert e.provider_count == 2

def test_mid_write_empty_file_restored_using_same_identity():
    e = Env(); e.ctl.stage(); e.store.fail_write = True
    with pytest.raises(StoreError): e.ctl.apply()
    assert e.store.credential == b'' and not e.is_running
    e.store.fail_write = False
    assert e.ctl.restore()['status'] == 'RESTORED_PREVIOUS_KEY'
    assert e.is_running and e.store.credential == OLD

def test_stop_timeout_is_recoverable_and_new_key_never_written():
    e = Env(); e.ctl.stage(); e.fail_run = 'stop'
    with pytest.raises(c.RotationError): e.ctl.apply()
    assert e.store.credential == OLD
    e.fail_run = None
    assert e.ctl.restore()['status'] == 'RESTORED_PREVIOUS_KEY'

def test_post_reload_provider_failure_can_restore_without_external_requests():
    e = Env(); e.ctl.stage(); e.fail_provider_at = 3
    with pytest.raises(c.provider_check.ProviderCheckError): e.ctl.apply()
    assert e.store.credential == NEW and e.is_running
    e.ctl.restore()
    assert e.provider_count == 3 and e.store.credential == OLD

def test_health_timeout_can_restore():
    e = Env(); e.ctl.stage(); e.health_fail = True
    with pytest.raises(c.RotationError, match='READY_TIMEOUT'): e.ctl.apply()
    e.health_fail = False
    assert e.ctl.restore()['status'] == 'RESTORED_PREVIOUS_KEY'

def test_service_drift_prevents_stop():
    e = Env(); e.ctl.stage(); e.drift = True
    with pytest.raises(c.preflight.PreflightError): e.ctl.apply()
    assert e.is_running and e.store.credential == OLD and ('command', 'stop') not in e.calls

def test_changed_active_key_preserved_without_stop():
    e = Env(); e.ctl.stage(); e.store.credential = key_bytes('UnrelatedChange')
    with pytest.raises(c.RotationError, match='ACTIVE_CREDENTIAL_CHANGED'): e.ctl.apply()
    assert e.is_running and ('command', 'stop') not in e.calls

@pytest.mark.parametrize('phase', ['STAGED', 'RESTORED'])
def test_restore_requires_incomplete_transaction(phase):
    e = Env(); e.ctl.stage()
    j=e.store.read_json('journal.json'); j['phase']=phase; e.store.write_json('journal.json', j)
    with pytest.raises(c.RotationError, match='RESTORE_NOT_REQUIRED'): e.ctl.restore()

def test_explicit_restore_after_success_for_business_acceptance_failure():
    e=Env();e.ctl.stage();e.ctl.apply()
    assert e.ctl.restore()['status']=='RESTORED_PREVIOUS_KEY'
    assert e.store.credential==OLD and e.is_running and e.provider_count==3

def test_explicit_restore_preserves_unrelated_later_key():
    e=Env();e.ctl.stage();e.ctl.apply();e.store.credential=key_bytes('LaterUnrelatedKey')
    with pytest.raises(c.RotationError,match='ACTIVE_CREDENTIAL_CHANGED'):e.ctl.restore()
    assert e.store.credential==key_bytes('LaterUnrelatedKey')

@pytest.mark.parametrize('args', [[], ['--stage-key', 'visible-key'], ['--key=visible-key'], ['--help'], ['--path', '/tmp/x']])
def test_cli_rejects_unsupported_or_secret_arguments_without_echo(args, capsys):
    assert c.main(args) == 1
    output = capsys.readouterr().out
    assert json.loads(output)['reason'] == 'INVALID_ARGUMENTS'
    assert 'visible-key' not in output and '/tmp/x' not in output
