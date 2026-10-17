"""Two-step r52 account rotation. Stage first; a maintenance command activates it."""
from __future__ import annotations
import getpass
import hashlib
import json
import os
import subprocess
import sys
import time
import warnings

import preflight
import provider_check
from private_store import PrivateStore, StoreError, KEY_RE, key_of, key_bytes, same_key, strict_json

SCHEMA = 'kuaidi100-key-rotation-r53-v1'
PHASES = {'STAGED', 'STOPPING', 'STOPPED', 'WRITING', 'KEY_WRITTEN', 'STARTING',
          'VERIFYING', 'APPLIED', 'RESTORING', 'RESTORED'}
COMMANDS = {'--stage-key', '--check', '--apply-maintenance-window', '--restore-previous-key'}

class RotationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def require(value, code):
    if not value:
        raise RotationError(code)

def emit(phase):
    print(json.dumps({'schema': SCHEMA, 'status': 'PROGRESS', 'phase': phase}, separators=(',', ':')), flush=True)

def run(argv, timeout=15, input_bytes=None):
    require(isinstance(argv, list) and argv and argv[0] == 'docker', 'LOCAL_COMMAND_INVALID')
    if argv[1:3] != ['--host', 'unix:///var/run/docker.sock']:
        argv = ['docker', '--host', 'unix:///var/run/docker.sock'] + argv[1:]
    try:
        result = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout,
                                check=False, env=dict(provider_check.RUNNER_ENV))
        require(result.returncode == 0 and len(result.stdout) <= 256*1024, 'LOCAL_COMMAND_FAILED')
        return result.stdout
    except subprocess.TimeoutExpired:
        raise RotationError('LOCAL_COMMAND_TIMEOUT') from None
    except RotationError:
        raise
    except Exception:
        raise RotationError('LOCAL_COMMAND_FAILED') from None

def read_key():
    require(sys.stdin.isatty() and sys.stderr.isatty(), 'INTERACTIVE_TERMINAL_REQUIRED')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            first = getpass.getpass('New Kuaidi100 MCP key (hidden input): ')
            second = getpass.getpass('Repeat new key (hidden input): ')
    except getpass.GetPassWarning:
        raise RotationError('INTERACTIVE_TERMINAL_REQUIRED') from None
    except (EOFError, KeyboardInterrupt):
        raise RotationError('INPUT_CANCELLED') from None
    require(KEY_RE.fullmatch(first) is not None, 'KEY_FORMAT_INVALID')
    import hmac
    require(hmac.compare_digest(first, second), 'KEY_CONFIRMATION_MISMATCH')
    return first

HEALTH_CHILD = r'''
import sys,os,json,logging,hmac,hashlib,urllib.request
logging.disable(logging.CRITICAL)
try:
 raw=sys.stdin.buffer.read(2049);value=json.loads(raw)
 if set(value)!={'key'} or not isinstance(value['key'],str):raise ValueError()
 if (os.getuid(),os.getgid())!=(10001,10001):raise ValueError()
 if hashlib.sha256(open('/app/app/logistics_query.py','rb').read()).hexdigest()!='f556b4e349109aa8fd87304b14e4f2bc942ed89664b5c8a21f023dfa143fb2de':raise ValueError()
 sys.path.insert(0,'/app')
 from app.logistics_query import LogisticsConfig
 config=LogisticsConfig.from_env()
 if not config.enabled or config.kuaidi100 is not None or config.kuaidi100_mcp is None:raise ValueError()
 if not hmac.compare_digest(config.kuaidi100_mcp.key,value['key']):raise ValueError()
 if config.factory_phone_policy is None or config.mes_user_code!='M00001402':raise ValueError()
 class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,*args,**kwargs):return None
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 with op.open('http://127.0.0.1:8080/ready',timeout=4) as response:
  if response.status!=200 or json.loads(response.read(8193))!={'status':'ready','database':'ready','redis':'ready'}:raise ValueError()
 print(json.dumps({'status':'READY','mounted_key_matches':True,'configuration_load_verified':True,'business_requests':0}))
except BaseException:
 print(json.dumps({'status':'NOT_READY'}))
'''

def gateway_health(runner, gateway_id, key):
    try:
        raw = runner(['docker', 'exec', '-i', gateway_id, 'python', '-I', '-B', '-c', HEALTH_CHILD],
                     timeout=8, input_bytes=key_bytes(key))
        value = strict_json(raw)
        require(value == {'status': 'READY', 'mounted_key_matches': True,
                          'configuration_load_verified': True, 'business_requests': 0}, 'GATEWAY_NOT_READY')
        return value
    except RotationError:
        raise
    except Exception:
        raise RotationError('GATEWAY_NOT_READY') from None

def running(runner, gateway_id):
    try:
        value = strict_json(runner(['docker', 'inspect', '--type', 'container', '--format',
                                    '{{json .State.Running}}', gateway_id]))
        require(type(value) is bool, 'GATEWAY_STATE_INVALID')
        return value
    except RotationError:
        raise
    except Exception:
        raise RotationError('GATEWAY_STATE_INVALID') from None

def started_at(runner, gateway_id):
    try:
        value = strict_json(runner(['docker', 'inspect', '--type', 'container', '--format',
                                    '{{json .State.StartedAt}}', gateway_id]))
        require(isinstance(value, str) and 20 <= len(value) <= 64, 'GATEWAY_STATE_INVALID')
        return value
    except RotationError:
        raise
    except Exception:
        raise RotationError('GATEWAY_STATE_INVALID') from None

class Controller:
    def __init__(self, store=None, runner=run, key_reader=read_key, provider=provider_check.check_new_key,
                 inspector=preflight.inspect_baseline, verifier=preflight.verify_unchanged,
                 health=gateway_health, sleeper=time.sleep, clock=time.monotonic, progress=emit):
        self.store = store or PrivateStore()
        self.runner, self.key_reader, self.provider = runner, key_reader, provider
        self.inspector, self.verifier, self.health = inspector, verifier, health
        self.sleeper, self.clock, self.progress = sleeper, clock, progress

    def journal(self):
        value = self.store.read_json('journal.json', optional=True)
        if value is not None:
            require(isinstance(value, dict) and value.get('schema') == SCHEMA and value.get('phase') in PHASES
                    and isinstance(value.get('baseline'), dict) and isinstance(value.get('credential_identity'), dict)
                    and isinstance(value.get('gateway_started_at'), str), 'JOURNAL_INVALID')
        return value

    def phase(self, journal, phase):
        journal['phase'] = phase
        self.store.write_json('journal.json', journal)
        self.progress(phase)

    def stage(self):
        require(self.journal() is None, 'EXISTING_ROTATION_PRESERVED')
        self.progress('CHECKING_CURRENT_R52')
        baseline = self.inspector(self.runner)
        old, identity = self.store.read_credential()
        require(identity == baseline['credential_identity'], 'CREDENTIAL_IDENTITY_CHANGED')
        stamp = started_at(self.runner, baseline['gateway_id'])
        new = key_bytes(self.key_reader())
        if same_key(old, new):
            return {'status': 'UNCHANGED', 'services_restarted': 0, 'active_configuration_changed': False}
        self.progress('CHECKING_NEW_ACCOUNT_TOOLS')
        provider = self.provider(baseline['gateway_id'], key_of(new))
        self.verifier(self.runner, baseline, gateway_running=True)
        now, _ = self.store.read_credential(identity)
        require(same_key(old, now), 'ACTIVE_CREDENTIAL_CHANGED')
        require(started_at(self.runner, baseline['gateway_id']) == stamp, 'GATEWAY_RESTARTED_DURING_STAGE')
        self.store.write('previous-key.json', old)
        self.store.write('pending-key.json', new)
        journal = {'schema': SCHEMA, 'phase': 'STAGED', 'baseline': baseline,
                   'credential_identity': identity, 'gateway_started_at': stamp,
                   'provider_discovery': provider}
        self.phase(journal, 'STAGED')
        return {'status': 'STAGED', 'services_restarted': 0, 'active_configuration_changed': False,
                'old_credential_backup_saved': True, 'new_key_saved_privately': True,
                'new_account': provider, 'next': 'APPLY_DURING_MAINTENANCE_WINDOW'}

    def wait_health(self, baseline, key):
        deadline = self.clock() + 75
        while True:
            self.verifier(self.runner, baseline, gateway_running=True)
            try:
                return self.health(self.runner, baseline['gateway_id'], key)
            except RotationError:
                if self.clock() >= deadline:
                    raise RotationError('GATEWAY_READY_TIMEOUT') from None
                self.progress('WAITING_GATEWAY_READY')
                self.sleeper(3)

    def apply(self):
        j = self.journal()
        require(j is not None, 'NEW_KEY_NOT_STAGED')
        if j['phase'] == 'APPLIED':
            return self.check()
        require(j['phase'] == 'STAGED', 'INCOMPLETE_ROTATION_USE_RESTORE')
        baseline, ident = j['baseline'], j['credential_identity']
        self.verifier(self.runner, baseline, gateway_running=True)
        old, new = self.store.read('previous-key.json'), self.store.read('pending-key.json')
        key_of(old); key_of(new)
        current, _ = self.store.read_credential(ident)
        require(same_key(old, current) and not same_key(old, new), 'ACTIVE_CREDENTIAL_CHANGED')
        self.progress('RECHECKING_NEW_ACCOUNT_TOOLS')
        self.provider(baseline['gateway_id'], key_of(new))
        self.verifier(self.runner, baseline, gateway_running=True)
        require(same_key(self.store.read_credential(ident)[0], old), 'ACTIVE_CREDENTIAL_CHANGED')
        j['gateway_started_at'] = started_at(self.runner, baseline['gateway_id'])
        self.phase(j, 'STOPPING')
        self.runner(['docker', 'stop', '--time', '25', baseline['gateway_id']], timeout=35)
        self.verifier(self.runner, baseline, gateway_running=False)
        self.phase(j, 'STOPPED')
        require(same_key(self.store.read_credential(ident)[0], old), 'ACTIVE_CREDENTIAL_CHANGED')
        self.phase(j, 'WRITING')
        self.verifier(self.runner, baseline, gateway_running=False)
        self.store.write_credential_in_place(new, ident)
        self.phase(j, 'KEY_WRITTEN')
        self.verifier(self.runner, baseline, gateway_running=False)
        self.phase(j, 'STARTING')
        self.runner(['docker', 'start', baseline['gateway_id']], timeout=35)
        self.phase(j, 'VERIFYING')
        health = self.wait_health(baseline, key_of(new))
        require(started_at(self.runner, baseline['gateway_id']) != j['gateway_started_at'], 'GATEWAY_RELOAD_NOT_VERIFIED')
        discovery = self.provider(baseline['gateway_id'], key_of(new))
        self.verifier(self.runner, baseline, gateway_running=True)
        require(same_key(self.store.read_credential(ident)[0], new), 'ACTIVE_CREDENTIAL_CHANGED')
        receipt = {'schema': SCHEMA, 'status': 'APPLIED', 'effective_mode': 'r52-logistics-eta',
                   'services_restarted': 1, 'changed_services': ['gateway'], 'container_ids_changed': False,
                   'manager_restarted': False, 'workers_restarted': False, 'database_changed': False,
                   'images_changed': False, 'active_configuration_changed': True,
                   'credential_inode_preserved': True, 'new_gateway_process_started': True,
                   'old_credential_backup_saved': True, 'health': health, 'new_account': discovery,
                   'maximum_mcp_control_requests': 10, 'mes_requests': 0, 'waybill_requests': 0, 'eta_requests': 0,
                   'employee_business_acceptance': 'PENDING', 'credential_values_or_hash_output': False}
        self.store.write_json('receipt.json', receipt)
        self.phase(j, 'APPLIED')
        return receipt

    def restore(self):
        j = self.journal()
        require(j is not None, 'NO_ROTATION_TO_RESTORE')
        require(j['phase'] in PHASES - {'STAGED', 'RESTORED'}, 'RESTORE_NOT_REQUIRED')
        baseline, ident = j['baseline'], j['credential_identity']
        old = self.store.read('previous-key.json')
        new = self.store.read('pending-key.json')
        key_of(old); key_of(new)
        # A root operator's unrelated later key change is never overwritten.
        # Partial bytes are recoverable only at the two journaled write phases.
        try:
            active, _ = self.store.read_credential(ident)
        except StoreError as error:
            require(j['phase'] in {'WRITING', 'RESTORING'} and
                    error.code in {'KEY_FORMAT_INVALID', 'PRIVATE_FILE_UNSAFE'}, 'ACTIVE_CREDENTIAL_CHANGED')
        else:
            require(same_key(active, old) or same_key(active, new), 'ACTIVE_CREDENTIAL_CHANGED')
        is_running = running(self.runner, baseline['gateway_id'])
        self.verifier(self.runner, baseline, gateway_running=is_running)
        self.phase(j, 'RESTORING')
        if is_running:
            self.runner(['docker', 'stop', '--time', '25', baseline['gateway_id']], timeout=35)
        self.verifier(self.runner, baseline, gateway_running=False)
        self.store.write_credential_in_place(old, ident)
        self.verifier(self.runner, baseline, gateway_running=False)
        self.runner(['docker', 'start', baseline['gateway_id']], timeout=35)
        health = self.wait_health(baseline, key_of(old))
        require(same_key(self.store.read_credential(ident)[0], old), 'RESTORE_VERIFY_FAILED')
        receipt = {'schema': SCHEMA, 'status': 'RESTORED_PREVIOUS_KEY', 'effective_mode': 'r52-logistics-eta',
                   'active_configuration': 'PREVIOUS_KEY', 'container_ids_changed': False,
                   'changed_services': ['gateway'], 'manager_restarted': False, 'workers_restarted': False,
                   'database_changed': False, 'health': health, 'network_provider_requests': 0,
                   'credential_values_or_hash_output': False}
        self.store.write_json('receipt.json', receipt)
        self.phase(j, 'RESTORED')
        return receipt

    def check(self):
        j = self.journal()
        if j is None:
            self.inspector(self.runner)
            return {'status': 'VERIFIED', 'rotation': 'NOT_STAGED', 'services_restarted': 0}
        require(j['phase'] in {'STAGED', 'APPLIED', 'RESTORED'}, 'INCOMPLETE_ROTATION_USE_RESTORE')
        self.verifier(self.runner, j['baseline'], gateway_running=True)
        target = self.store.read('pending-key.json' if j['phase'] == 'APPLIED' else 'previous-key.json')
        require(same_key(self.store.read_credential(j['credential_identity'])[0], target), 'ACTIVE_CREDENTIAL_CHANGED')
        return {'status': 'VERIFIED', 'rotation': j['phase'], 'services_restarted': 0,
                'active_key_matches_expected': True, 'business_requests': 0}

def main(argv=None):
    result = {'schema': SCHEMA, 'credential_values_or_hash_output': False,
              'raw_response_or_error_output': False, 'mes_requests': 0, 'waybill_requests': 0, 'eta_requests': 0}
    try:
        args = sys.argv[1:] if argv is None else argv
        require(len(args) == 1 and args[0] in COMMANDS, 'INVALID_ARGUMENTS')
        require(sys.platform.startswith('linux') and os.geteuid() == 0, 'LINUX_ROOT_REQUIRED')
        os.umask(0o077)
        ctl = Controller()
        with ctl.store.locked():
            method = {'--stage-key': ctl.stage, '--check': ctl.check,
                      '--apply-maintenance-window': ctl.apply, '--restore-previous-key': ctl.restore}[args[0]]
            result.update(method())
        code = 0
    except (RotationError, StoreError, preflight.PreflightError, provider_check.ProviderCheckError) as error:
        result.update(status='STOP', reason=error.code, automatic_rollback='NONE',
                      action='PRESERVE_OUTPUT_AND_JOURNAL')
        code = 1
    except (KeyboardInterrupt, EOFError):
        result.update(status='STOP', reason='INPUT_OR_OPERATION_INTERRUPTED', automatic_rollback='NONE',
                      action='PRESERVE_OUTPUT_AND_JOURNAL')
        code = 1
    except BaseException:
        result.update(status='STOP', reason='ROTATION_CHECK_FAILED', automatic_rollback='NONE',
                      action='PRESERVE_OUTPUT_AND_JOURNAL')
        code = 1
    print(json.dumps(result, ensure_ascii=True, separators=(',', ':')), flush=True)
    return code

if __name__ == '__main__':
    raise SystemExit(main())
