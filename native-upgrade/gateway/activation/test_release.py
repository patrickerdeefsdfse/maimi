import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import runtime_release as release

from runtime_release import *

OWNER='a'*32;MANAGER='b'*64;GATEWAY='c'*64;TARGET='d'*64
IMAGE='sha256:'+'e'*64;OLDIMAGE='sha256:'+'9'*64;NS='maimi-release-test'


class Docker:
    def __init__(self):
        self.running=False;self.target=False;self.active=False;self.fail_probe=False;self.refs=[];self.calls=[]
        self.volumes={NS+'-'+OWNER+'-'+k:dict(name=NS+'-'+OWNER+'-'+k,created_at='old-'+k,
            labels={LABEL:NS,OWNER_LABEL:OWNER}) for k in ('state','files')}
    def service(self,cid):
        return dict(id=cid,running=self.running if cid!=TARGET else self.target,
            state='running' if (self.running if cid!=TARGET else self.target) else 'created',
            project='megmeet-desktop-server',service='gateway' if cid==GATEWAY else 'personal-cloud-manager')
    def helper_python_version(self,image):return {'version':[3,12],'data_filter':True}
    def active_service(self,service):return [TARGET] if service=='personal-cloud-manager' and self.target else []
    def image(self,image):return dict(id=image,os='linux',arch='amd64',runtime=OLD if image==OLDIMAGE else NEW)
    def source_profile(self,cid):return dict(worker_image=OLDIMAGE,release='1'*64,runtime=OLD,namespace=NS)
    def owned_volumes(self,namespace):return sorted(self.volumes)
    def volume(self,name):return self.volumes[name]
    def references(self,name):return self.refs
    def manager(self,cid):return {**self.service(cid),'source_runtime':OLD}
    def active_managers(self):return self.active_service('personal-cloud-manager')
    def helper(self,image,volumes,destination,mode):
        self.calls.append(mode);result={}
        for k in ('state','files'):
            path=destination/(k+'.tar.gz')
            if mode=='create':path.write_bytes(('synthetic-'+k).encode())
            result[k]=dict(sha256=digest(path),members=1,uncompressed_bytes=1,archive_bytes=path.stat().st_size)
        return dict(schema='native021-volume-snapshot-verification-v1',status='VERIFIED',volumes=result)
    def manager_profile(self,cid):
        return dict(worker_image=IMAGE,release='f'*64,runtime=NEW,
            activation='/opt/personal-cloud/runtime-r65/activation.json',snapshots='/opt/personal-cloud/runtime-r65/owners',
            new_owners='/opt/personal-cloud/runtime-r65/new-owners',namespace=NS)
    def manager_probe(self,cid):
        enabled=(self.root/'activation.json').exists()
        return dict(status='ready',worker_runtime=NEW,runtime_admission_enabled=enabled and not self.fail_probe,active_hosts=0)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'release';self.root.mkdir(mode=0o700)
        self.docker=Docker();self.docker.root=self.root
        self.plan=dict(schema='r65-offline-release-plan-v1',namespace=NS,gateway_id=GATEWAY,source_manager_id=MANAGER,
            source_image_id=OLDIMAGE,source_release_sha='1'*64,target_image_id=IMAGE,target_release_sha='f'*64,helper_image_id=IMAGE)
    def tearDown(self):self.tmp.cleanup()
    def prepare(self):return prepare_release(docker=self.docker,plan=self.plan,root=self.root)
    def test_prepare_requires_verified_both_archives_then_records_all_owners(self):
        r=self.prepare();self.assertEqual(r['owners'],1);self.assertEqual(self.docker.calls,['create','verify'])
        self.assertFalse((self.root/'activation.json').exists());read_prepared(self.root)
    def test_running_ingress_never_snapshots(self):
        self.docker.running=True
        with self.assertRaisesRegex(ReleaseRefused,'QUIESCENT'):self.prepare()
        self.assertEqual(self.docker.calls,[])
    def test_incomplete_owner_pair_never_snapshots(self):
        del self.docker.volumes[NS+'-'+OWNER+'-files']
        with self.assertRaisesRegex(ReleaseRefused,'INCOMPLETE_OWNER'):self.prepare()
    def test_stopped_worker_reference_rejected(self):
        self.docker.refs=['stopped']
        with self.assertRaisesRegex(ReleaseRefused,'VOLUME_NOT_QUIESCENT'):self.prepare()
    def test_image_mismatch_rejected(self):
        self.docker.image=lambda image:dict(id='different' if image==IMAGE else image,os='linux',arch='amd64',runtime=NEW if image==IMAGE else OLD)
        with self.assertRaisesRegex(ReleaseRefused,'TARGET_IMAGE'):self.prepare()
    def test_source_backup_digest_change_blocks_activation(self):
        self.prepare();(self.root/'backups'/OWNER/'state.tar.gz').write_bytes(b'changed');self.docker.target=True
        with self.assertRaisesRegex(ReleaseRefused,'BACKUP_DIGEST'):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertFalse((self.root/'activation.json').exists())
    def test_owner_set_change_after_snapshot_blocks_activation(self):
        self.prepare();self.docker.target=True
        for k in ('state','files'):
            name=NS+'-'+'2'*32+'-'+k
            self.docker.volumes[name]=dict(name=name,created_at='later',labels={LABEL:NS,OWNER_LABEL:'2'*32})
        with self.assertRaisesRegex(ReleaseRefused,'OWNER_SET_CHANGED'):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
    def test_activation_waits_real_manager_gate_and_keeps_ingress_stopped(self):
        self.prepare();self.docker.target=True
        r=activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertEqual(r['status'],'ACTIVE_INGRESS_STILL_STOPPED');self.assertFalse(r['services_started'])
        self.assertTrue((self.root/'activated.json').exists())
    def test_failed_gate_probe_revokes_activation(self):
        self.prepare();self.docker.target=True;self.docker.fail_probe=True
        with self.assertRaisesRegex(ReleaseRefused,'ACTIVATION_NOT_OBSERVED'):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertFalse((self.root/'activation.json').exists());self.assertEqual(len(list(self.root.glob('activation-failed-*'))),1)
    def test_changed_manager_config_blocks_activation(self):
        self.prepare();self.docker.target=True;self.docker.manager_profile=lambda _:{}
        with self.assertRaisesRegex(ReleaseRefused,'CONFIG_MISMATCH'):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
    def test_preparation_not_restarted_over_existing_state(self):
        self.prepare()
        with self.assertRaisesRegex(ReleaseRefused,'EMPTY_RELEASE_DIRECTORY'):self.prepare()
    def test_mutable_image_and_unsafe_plan_refused(self):
        self.plan['target_image_id']='worker:latest'
        with self.assertRaisesRegex(ReleaseRefused,'PLAN_INVALID'):self.prepare()
    def test_bad_helper_version_is_rejected_before_any_durable_write(self):
        def fail(_):raise SnapshotRefused('HELPER_PYTHON_312_REQUIRED')
        self.docker.helper_python_version=fail
        with self.assertRaisesRegex(SnapshotRefused,'312_REQUIRED'):self.prepare()
        self.assertEqual(list(self.root.iterdir()),[])
    def test_malformed_preparation_never_activates(self):
        self.prepare();(self.root/'prepared.json').write_text('{"wrong":true}');self.docker.target=True
        with self.assertRaisesRegex(ReleaseRefused,'PREPARATION_CHANGED'):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertFalse((self.root/'activation.json').exists())
    def test_existing_changed_activation_is_never_overwritten(self):
        self.prepare();self.docker.target=True;(self.root/'activation.json').write_text('{"wrong":true}')
        with self.assertRaises(ReleaseRefused):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertEqual((self.root/'activation.json').read_text(),'{"wrong":true}')
    def test_activation_file_fsync_failure_revokes_only_own_inode(self):
        self.prepare();self.docker.target=True;real=release.os.fsync;gate=self.root/'activation.json'
        def failed(fd):
            if gate.exists() and release.os.fstat(fd).st_ino==gate.stat().st_ino:raise OSError('synthetic fsync failure')
            return real(fd)
        with patch.object(release.os,'fsync',side_effect=failed):
            with self.assertRaises(OSError):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertFalse(gate.exists());self.assertTrue((self.root/'activation-intent.json').exists())
    def test_activation_directory_fsync_failure_revokes_own_gate(self):
        self.prepare();self.docker.target=True;real=release.sync_directory;triggered=[]
        def failed(path):
            if (self.root/'activation.json').exists() and not triggered:
                triggered.append(True);raise OSError('synthetic directory failure')
            return real(path)
        with patch.object(release,'sync_directory',side_effect=failed):
            with self.assertRaises(OSError):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertFalse((self.root/'activation.json').exists())
    def test_completion_receipt_write_or_fsync_failure_revokes_gate(self):
        self.prepare();self.docker.target=True;real=release.write_receipt
        def failed(path,data):
            if path.name=='activated.json':
                path.write_bytes(b'{');raise OSError('synthetic ENOSPC')
            return real(path,data)
        with patch.object(release,'write_receipt',side_effect=failed):
            with self.assertRaises(OSError):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertFalse((self.root/'activation.json').exists())
    def test_racing_exclusive_gate_is_preserved(self):
        self.prepare();self.docker.target=True;real=release.write_receipt;gate=self.root/'activation.json'
        foreign=b'{"other-operation":true}'
        def raced(path,data):
            real(path,data)
            if path.name=='activation-intent.json':gate.write_bytes(foreign)
        with patch.object(release,'write_receipt',side_effect=raced):
            with self.assertRaises(FileExistsError):activate_release(docker=self.docker,root=self.root,target_manager_id=TARGET)
        self.assertEqual(gate.read_bytes(),foreign)
    def test_snapshot_verification_receipt_directory_sync_failure_blocks_owner_receipt(self):
        real=release.os.fsync
        def failed(fd):
            import stat
            if stat.S_ISDIR(release.os.fstat(fd).st_mode) and (self.root/'backups'/OWNER/'verification.json').exists():
                raise OSError('synthetic directory fsync failure')
            return real(fd)
        with patch.object(release.os,'fsync',side_effect=failed):
            with self.assertRaises(OSError):self.prepare()
        self.assertFalse((self.root/'owners'/(OWNER+'.json')).exists())
        self.assertFalse((self.root/'prepared.json').exists())

if __name__=='__main__':unittest.main()
