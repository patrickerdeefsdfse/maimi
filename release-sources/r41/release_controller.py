"""r41 runtime and business release; journals precede mutations, activation follows health."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

RELEASE = '20261008-r41-dsh020'
NEW = 'r41-dsh020'
OLD = 'r40-remembered-login'
FOLDER = re.compile(r'\.dsh020-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')
HEX = re.compile(r'[a-f0-9]{64}')
SERVICES = frozenset(('gateway', 'personal-cloud-manager', 'retention-worker', 'downloads',
                      'postgres', 'redis', 'office-service', 'db-migrate'))
CHANGED = ('gateway', 'personal-cloud-manager')
PRESERVED = SERVICES - set(CHANGED)

class Stop(ValueError):
    pass

def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

class Controller:
    def __init__(self, root, manifest_sha, product):
        self.root, self.manifest_sha, self.p = Path(root), manifest_sha, product

    def read(self, path):
        return self.p.read(path)

    def write(self, path, value):
        self.p.write_new(path, encoded(value))

    def progress(self, phase, service=None):
        print(json.dumps({'status':'R41_RELEASE_PROGRESS','phase':phase,'service':service,
                          'changed_services':list(CHANGED),'recursive_ancestor_validation':False}),flush=True)

    def prior(self):
        path = self.root / 'transition.json'
        return self.read(path) if path.exists() else None

    def folder(self, value):
        folder = Path(value.get('backup_directory', ''))
        if folder.parent != self.root or not FOLDER.fullmatch(folder.name):
            raise Stop('R41_TRANSACTION_PATH_INVALID')
        return folder

    def journal(self, folder, **changes):
        path = folder / 'progress.json'
        value = json.loads(self.read(path)) if path.exists() else {}
        value.update(changes)
        if path.exists():
            self.p.replace(path, encoded(value))
        else:
            self.write(path, value)
        if 'phase' in changes:
            self.progress(changes['phase'], changes.get('creating'))
        return value

    def validate_receipt_files(self, value, folder):
        stage=json.loads(self.read(folder/'progress.json'));accepted=[None]
        for filename,mode,status in (('applied.json',NEW,'APPLIED'),('restored-r40.json',OLD,'RESTORED_R40')):
            path=folder/filename
            if not path.exists():continue
            raw=self.read(path);receipt=json.loads(raw);ids=receipt.get('current_service_ids',{})
            if (receipt.get('status')!=status or receipt.get('effective_mode')!=mode
                or any(receipt.get(k)!=v for k,v in value.items() if k!='status')
                or set(ids)!=SERVICES or any(not isinstance(v,str) or not HEX.fullmatch(v) for v in ids.values())
                or any(ids[s]!=value['original_service_ids'][s] for s in PRESERVED)
                or stage.get('created',{}).get(mode)!={s:ids[s] for s in CHANGED}):
                raise Stop('R41_TERMINAL_PAIR_CHANGED')
            accepted.append(raw)
        if self.prior() not in accepted:raise Stop('R41_TRANSITION_NOT_OWNED')

    def validate(self, value, folder):
        if (self.folder(value) != folder or value.get('release') != RELEASE
                or value.get('manifest_sha256') != self.manifest_sha or value.get('status') != 'PREPARED'
                or value.get('previous_mode') != OLD):
            raise Stop('R41_PREPARED_CONTRACT_CHANGED')
        ids = value.get('original_service_ids', {})
        if set(ids) != SERVICES or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in ids.values()):
            raise Stop('R41_BASELINE_IDS_INVALID')
        if sha(self.read(folder / 'baseline.json')) != value.get('baseline_sha256'):
            raise Stop('R41_BASELINE_PAIR_CHANGED')
        self.p.preserved(value, folder)

    def rollback_intent(self, value, folder):
        path = folder / 'rollback-intent.json'
        if not path.exists():
            return False
        applied = self.read(folder / 'applied.json')
        receipt = json.loads(applied)
        if (receipt.get('status') != 'APPLIED' or receipt.get('effective_mode') != NEW
                or any(receipt.get(k) != v for k, v in value.items() if k != 'status')):
            raise Stop('R41_ROLLBACK_APPLIED_PAIR_CHANGED')
        expected = {'schema': 'dsh020-rollback-intent-v1', 'target': OLD,
                    'prepared_sha256': sha(self.read(folder / 'prepared.json')),
                    'applied_sha256': sha(applied)}
        if self.read(path) != encoded(expected):
            raise Stop('R41_ROLLBACK_INTENT_CHANGED')
        return True

    def recognized(self, value, folder):
        self.validate(value, folder)
        stage = json.loads(self.read(folder / 'progress.json'))
        creating = stage.get('creating')
        if stage.get('target') not in (OLD, NEW) or (creating is not None and creating not in CHANGED):
            raise Stop('R41_JOURNAL_INVALID')
        current = self.p.snapshot()
        if set(current) != SERVICES:
            raise Stop('R41_SERVICE_SET_CHANGED')
        for service in PRESERVED:
            item = current[service]
            if item is None or item['id'] != value['original_service_ids'][service]:
                raise Stop('R41_OTHER_SERVICE_CHANGED')
            self.p.unchanged(service, item, value, folder)
        modes = {}
        for service in CHANGED:
            item = current[service]
            if item is None:
                if creating != service:
                    raise Stop('R41_UNKNOWN_MISSING_SERVICE')
                modes[service] = None
                continue
            if item['id'] == value['original_service_ids'][service]:
                self.p.static(service, item, OLD, value, folder, original=True)
                modes[service] = OLD
                continue
            tag = item.get('runtime41', {})
            mode = tag.get('mode')
            if mode not in (OLD, NEW) or tag != {'release': RELEASE, 'transaction': folder.name, 'mode': mode}:
                raise Stop('R41_UNKNOWN_SERVICE')
            if (stage.get('created', {}).get(mode, {}).get(service) != item['id']
                    and not (creating == service and stage['target'] == mode)):
                raise Stop('R41_UNRECORDED_SERVICE')
            self.p.static(service, item, mode, value, folder)
            modes[service] = mode
        return current, modes

    def check(self, finishing=None):
        self.progress('CHECKING_CURRENT_RELEASE')
        self.p.verify_bundle()
        marker = self.root / '.update-incomplete'
        if marker.exists():
            if finishing is None:
                raise Stop('R41_MAINTENANCE_INCOMPLETE_USE_RECOVER')
            if self.read(marker) != self.read(finishing / 'prepared.json'):
                raise Stop('R41_FINISHING_MARKER_CHANGED')
        raw = self.prior()
        if raw is None:
            result = self.p.baseline()
            return {'status': 'VERIFIED', 'current_mode': OLD, 'current_service_ids': result['ids'],
                    'live_changes': 'NONE', 'baseline': 'APPLIED_R40_AND_DESKTOP_0120'}
        receipt = json.loads(raw)
        folder = self.folder(receipt)
        value = json.loads(self.read(folder / 'prepared.json'))
        self.validate(value, folder)
        self.validate_receipt_files(value, folder)
        rollback_pending = self.rollback_intent(value, folder)
        mode = NEW if receipt.get('status') == 'APPLIED' else OLD if receipt.get('status') == 'RESTORED_R40' else None
        if (mode is None or self.read(folder / ('applied.json' if mode == NEW else 'restored-r40.json')) != raw
                or any(receipt.get(k) != v for k, v in value.items() if k != 'status')):
            raise Stop('R41_TERMINAL_PAIR_CHANGED')
        stage = json.loads(self.read(folder / 'progress.json'))
        if stage.get('phase') != 'FINISHED' or stage.get('target') != mode or stage.get('creating') is not None:
            raise Stop('R41_TERMINAL_JOURNAL_CHANGED')
        current, actual = self.recognized(value, folder)
        ids = {s: item['id'] if item else None for s, item in current.items()}
        if set(actual.values()) != {mode} or ids != receipt.get('current_service_ids'):
            raise Stop('R41_TERMINAL_IDENTITIES_CHANGED')
        health = self.p.health(mode, ids, value, folder)
        if mode==NEW and health.get('runtime_admission_enabled') is not True:raise Stop('R41_COMPLETED_ACTIVATION_NOT_READY_USE_RECOVER')
        if self.prior() != raw or self.p.snapshot() != current:
            raise Stop('R41_CHANGED_DURING_CHECK')
        return {'status': 'VERIFIED', 'current_mode': mode, 'current_service_ids': ids,
                'health': health, 'live_changes': 'NONE', 'rollback_intent_recorded': rollback_pending}

    def prepare(self):
        result = self.check()
        if result['current_mode'] == NEW:
            return None
        # A completed rollback remains checkable but needs a fresh successor
        # release for another apply; do not grow implicit transaction chains.
        if self.prior() is not None:
            raise Stop('R41_ALREADY_ROLLED_BACK_NEW_RELEASE_REQUIRED')
        self.p.prepare_runtime()
        current = self.p.snapshot()
        if {s: item['id'] if item else None for s, item in current.items()} != result['current_service_ids']:
            raise Stop('R41_BASELINE_CHANGED')
        folder = self.root / ('.dsh020-switch-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
        self.p.mkdir(folder)
        baseline = self.p.capture(current, folder)
        self.write(folder / 'baseline.json', baseline)
        value = {'release': RELEASE, 'manifest_sha256': self.manifest_sha, 'status': 'PREPARED',
                 'backup_directory': str(folder), 'previous_mode': OLD,
                 'original_service_ids': result['current_service_ids'], 'baseline_sha256': sha(encoded(baseline)),
                 'changed_services': list(CHANGED), 'manager_restarted': True, 'workers_disconnected': True,
                 'history_format_changed': False, 'history_maintenance': 'OWNER_SCOPED_PRE_UPGRADE_SNAPSHOT', 'images_changed': True,
                 'previous_worker_runtime':'0.1.7-rc.2','worker_runtime':'0.2.0-rc.2'}
        self.write(folder / 'prepared.json', value)
        self.journal(folder, phase='PREPARED', target=NEW, creating=None, created={})
        self.p.prepare_compose(value, folder)
        self.validate(value, folder)
        self.rollback_intent(value, folder)
        self.p.nginx_preflight()
        if self.prior() is not None or self.p.snapshot() != current:
            raise Stop('R41_CHANGED_BEFORE_MAINTENANCE')
        self.p.preapply_rollback_guard(value, folder)
        self.p.write_new(self.root / '.update-incomplete', self.read(folder / 'prepared.json'))
        return value, folder

    def recover(self):
        marker = self.root / '.update-incomplete'
        if not marker.exists():
            raw=self.prior()
            if raw is None:raise Stop('R41_NO_INCOMPLETE_TRANSACTION')
            receipt=json.loads(raw)
            if receipt.get('status')!='APPLIED':raise Stop('R41_NO_APPLIED_RUNTIME_TO_RECOVER')
            folder=self.folder(receipt);value=json.loads(self.read(folder/'prepared.json'))
            self.validate(value,folder);self.validate_receipt_files(value,folder)
            if self.rollback_intent(value,folder):raise Stop('R41_FINISH_EXISTING_ROLLBACK')
            stage=json.loads(self.read(folder/'progress.json'))
            if stage.get('phase')!='FINISHED' or stage.get('target')!=NEW or stage.get('creating') is not None:raise Stop('R41_RECOVERY_COMPLETED_JOURNAL_INVALID')
            current,modes=self.recognized(value,folder)
            if set(modes.values())!={NEW} or {s:v['id'] if v else None for s,v in current.items()}!=receipt.get('current_service_ids'):raise Stop('R41_RECOVERY_CURRENT_IDENTITIES_CHANGED')
            self.p.recover_activation_preflight(value,folder)
            self.p.write_new(marker,self.read(folder/'prepared.json'))
        raw = self.read(marker)
        value = json.loads(raw)
        folder = self.folder(value)
        if self.read(folder / 'prepared.json') != raw:
            raise Stop('R41_MARKER_PAIR_CHANGED')
        self.validate(value, folder)
        accepted = [None] + [self.read(folder / n) for n in ('applied.json', 'restored-r40.json') if (folder / n).exists()]
        if self.prior() not in accepted:
            raise Stop('R41_TRANSITION_NOT_OWNED')
        self.recognized(value, folder)
        return value, folder

    def begin_rollback(self):
        self.check()
        raw = self.prior()
        if raw is None or json.loads(raw).get('status') != 'APPLIED':
            raise Stop('R41_APPLIED_REQUIRED_FOR_ROLLBACK')
        folder = self.folder(json.loads(raw))
        value = json.loads(self.read(folder / 'prepared.json'))
        self.p.rollback_preflight(value, folder)
        self.p.nginx_preflight()
        if not self.rollback_intent(value, folder):
            intent = {'schema': 'dsh020-rollback-intent-v1', 'target': OLD,
                      'prepared_sha256': sha(self.read(folder / 'prepared.json')), 'applied_sha256': sha(raw)}
            self.write(folder / 'rollback-intent.json', intent)
        self.p.write_new(self.root / '.update-incomplete', self.read(folder / 'prepared.json'))
        self.journal(folder, target=OLD, phase='ROLLBACK_PREPARED')
        return value, folder

    def apply(self, value, folder, mode):
        if mode not in (OLD, NEW):
            raise Stop('R41_TARGET_INVALID')
        self.validate(value, folder)
        self.validate_receipt_files(value, folder)
        if mode == OLD:
            self.p.rollback_preflight(value, folder)
        if self.rollback_intent(value, folder) and mode != OLD:
            raise Stop('R41_ROLLBACK_INTENT_REQUIRES_OLD')
        stage = json.loads(self.read(folder / 'progress.json'))
        if stage.get('target') == OLD and mode != OLD:
            raise Stop('R41_FINISH_EXISTING_ROLLBACK')
        self.journal(folder, target=mode)
        # Stop only the two changing front-door services. Existing workers are
        # retained until the manager's established startup cleanup; no -v.
        own = {'release': RELEASE, 'transaction': folder.name, 'mode': mode}
        for service in CHANGED:
            current, _ = self.recognized(value, folder)
            item = current[service]
            if item and item.get('runtime41') != own and item['running']:
                self.progress('STOPPING_SERVICE',service)
                self.p.stop(service, item['id'])
                self.recognized(value, folder)
        pending = json.loads(self.read(folder / 'progress.json')).get('creating')
        order = ([pending] if pending in CHANGED else []) + [s for s in CHANGED if s != pending]
        for service in order:
            current, actual = self.recognized(value, folder)
            item = current[service]
            if item is None or item.get('runtime41') != own:
                self.journal(folder, phase='CREATING', creating=service)
                self.p.create(service, mode, value, folder)
                current, actual = self.recognized(value, folder)
                item = current[service]
                if item is None or item['running'] or actual[service] != mode:
                    raise Stop('R41_CREATED_SERVICE_INVALID')
            stage = json.loads(self.read(folder / 'progress.json'))
            created = stage.get('created', {})
            created.setdefault(mode, {})[service] = item['id']
            self.journal(folder, phase='CREATED', creating=None, created=created)
        # Gateway health requests require the manager DNS/name to exist. No
        # nginx test/reload occurs while either old service is absent.
        for service in ('personal-cloud-manager', 'gateway'):
            current, _ = self.recognized(value, folder)
            if not current[service]['running']:
                self.progress('STARTING_SERVICE',service)
                self.p.start(service, current[service]['id'], mode, value, folder)
            self.recognized(value, folder)
        self.p.post_start(value, folder)
        self.progress('RELOADING_NGINX')
        self.p.reload_nginx(value, folder)
        current, actual = self.recognized(value, folder)
        ids = {s: item['id'] if item else None for s, item in current.items()}
        self.progress('CHECKING_HEALTH')
        health = self.p.health(mode, ids, value, folder)
        if set(actual.values()) != {mode} or self.p.snapshot() != current:
            raise Stop('R41_CHANGED_DURING_HEALTH')
        status = 'APPLIED' if mode == NEW else 'RESTORED_R40'
        receipt = {**value, 'status': status, 'effective_mode': mode, 'current_service_ids': ids,
                   'health': health, 'nginx_configuration_changed': False, 'employee_acceptance_pending': True}
        path = folder / ('applied.json' if mode == NEW else 'restored-r40.json')
        if path.exists():
            saved = json.loads(self.read(path))
            if any(saved.get(k) != v for k, v in receipt.items() if k != 'health'):
                raise Stop('R41_TERMINAL_PAIR_CHANGED')
            receipt = saved
        else:
            self.write(path, receipt)
        accepted = [None] + [self.read(folder / n) for n in ('applied.json', 'restored-r40.json') if (folder / n).exists()]
        if self.prior() not in accepted:
            raise Stop('R41_TRANSITION_CHANGED')
        self.p.replace(self.root / 'transition.json', encoded(receipt))
        self.journal(folder, phase='FINISHED', target=mode, creating=None)
        if mode==NEW:self.p.activate(value,folder)
        self.check(finishing=folder)
        marker = self.root / '.update-incomplete'
        if self.read(marker) != self.read(folder / 'prepared.json'):
            raise Stop('R41_MARKER_CHANGED')
        self.p.unlink_marker(marker)
        self.progress('COMPLETED')
        return {**receipt, 'runtime_activation_verified_at_completion':mode==NEW}

    def execute(self, action):
        if action == 'check':
            return self.check()
        if action == 'apply-maintenance-window':
            prepared = self.prepare()
            return {'status': 'ALREADY_APPLIED_AND_VERIFIED', 'live_changes': 'NONE'} if prepared is None else self.apply(*prepared, NEW)
        if action == 'recover-maintenance-window':
            value, folder = self.recover()
            target = OLD if self.rollback_intent(value, folder) else json.loads(self.read(folder / 'progress.json'))['target']
            return self.apply(value, folder, target)
        if action == 'rollback-maintenance-window':
            value, folder = self.recover() if (self.root / '.update-incomplete').exists() else self.begin_rollback()
            return self.apply(value, folder, OLD)
        raise Stop('R41_ACTION_INVALID')
