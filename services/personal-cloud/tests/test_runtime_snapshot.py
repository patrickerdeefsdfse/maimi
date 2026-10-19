"""Offline, synthetic-only snapshot checks; optional real local Docker exercise."""
from pathlib import Path
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
import uuid

SOURCE=Path(__file__).with_name('runtime_snapshot.py')
if not SOURCE.is_file():SOURCE=Path(__file__).resolve().parents[1]/'runtime_snapshot.py'
sys.path.insert(0,str(SOURCE.parent))
from runtime_snapshot import DockerCLI, SnapshotRefused, prepare_owner_snapshot, LABEL

OWNER='a'*32
MANAGER='b'*64
IMAGE='sha256:'+'c'*64
RELEASE='d'*64
NAMESPACE='maimi-synthetic'


class FakeDocker:
    def __init__(self):
        self.calls=[];self.running=False;self.active=[];self.refs=[];self.owner=OWNER
        self.changed=False;self.verify_mismatch=False;self.source_runtime='0.2.0-rc.2'
        self.manager_reads=0;self.volume_reads=0

    def manager(self,cid):
        self.manager_reads+=1
        return dict(id=cid,running=self.running,state='exited',project='megmeet-desktop-server',
                    service='personal-cloud-manager',source_runtime=self.source_runtime)

    def active_managers(self):return self.active

    def volume(self,name):
        self.volume_reads+=1
        return dict(name=name,created_at='CHANGED' if self.changed and self.volume_reads>2 else '2026-10-18T00:00:00Z',
                    labels={LABEL:NAMESPACE,'com.megmeet.personal-cloud.owner':self.owner})

    def references(self,name):return self.refs

    def helper(self,image,volumes,destination,mode):
        self.calls.append(mode)
        result=dict(schema='native021-volume-snapshot-verification-v1',status='VERIFIED',volumes={})
        for kind in ('state','files'):
            archive=destination/(kind+'.tar.gz')
            if mode=='create':archive.write_bytes(('synthetic-'+kind).encode())
            result['volumes'][kind]=dict(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),members=1,
                                        uncompressed_bytes=10,archive_bytes=archive.stat().st_size)
        if mode=='verify' and self.verify_mismatch:result['volumes']['state']['members']=2
        return result


@unittest.skipUnless(os.geteuid()==0,'root-owned admission files are a real constraint')
class SnapshotUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.backup=self.root/'backup';self.backup.mkdir(mode=0o700)
        self.receipts=self.root/'receipts';self.receipts.mkdir(mode=0o700)
        self.docker=FakeDocker()

    def tearDown(self):self.temp.cleanup()

    def run_snapshot(self,**kwargs):
        args=dict(docker=self.docker,owner=OWNER,namespace=NAMESPACE,manager_id=MANAGER,image_id=IMAGE,
                  release_sha=RELEASE,helper_image=IMAGE,backup_directory=self.backup,receipts_directory=self.receipts)
        args.update(kwargs);return prepare_owner_snapshot(**args)

    def assert_refused(self,reason,**kwargs):
        with self.assertRaisesRegex(SnapshotRefused,'^'+reason+'$'):self.run_snapshot(**kwargs)
        self.assertFalse((self.receipts/(OWNER+'.json')).exists())

    def test_success_requires_two_independent_passes_and_binds_both_volumes(self):
        result=self.run_snapshot()
        self.assertEqual(result['status'],'PREPARED_NOT_ACTIVATED')
        self.assertFalse(result['activation_written']);self.assertFalse(result['source_volumes_modified'])
        self.assertEqual(self.docker.calls,['create','verify']);self.assertEqual(self.docker.manager_reads,2)
        receipt=json.loads((self.receipts/(OWNER+'.json')).read_text())
        self.assertEqual(receipt['state_volume']['name'],NAMESPACE+'-'+OWNER+'-state')
        self.assertEqual(receipt['files_volume']['name'],NAMESPACE+'-'+OWNER+'-files')
        self.assertEqual(receipt['image_id'],IMAGE);self.assertEqual(receipt['release_manifest_sha256'],RELEASE)
        self.assertEqual(receipt['backup']['verification_receipt_sha256'],hashlib.sha256((self.backup/'verification.json').read_bytes()).hexdigest())
        self.assertEqual((self.receipts/(OWNER+'.json')).stat().st_mode & 0o777,0o600)

    def test_running_manager_rejected_before_helpers(self):
        self.docker.running=True;self.assert_refused('MANAGER_MUST_ALREADY_BE_STOPPED');self.assertEqual(self.docker.calls,[])

    def test_another_running_manager_rejected(self):
        self.docker.active=['other'];self.assert_refused('MANAGER_MUST_ALREADY_BE_STOPPED')

    def test_snapshot_of_new_runtime_cannot_claim_old_source(self):
        self.docker.source_runtime='0.2.1-rc.2';self.assert_refused('MANAGER_MUST_ALREADY_BE_STOPPED')

    def test_stopped_worker_reference_also_rejected(self):
        self.docker.refs=['stopped-worker'];self.assert_refused('VOLUME_STILL_REFERENCED');self.assertEqual(self.docker.calls,[])

    def test_owner_mismatch_rejected(self):
        self.docker.owner='f'*32;self.assert_refused('VOLUME_OWNERSHIP_MISMATCH')

    def test_volume_recreation_during_snapshot_never_admitted(self):
        self.docker.changed=True;self.assert_refused('SNAPSHOT_VOLUME_CHANGED')

    def test_independent_verifier_mismatch_never_admitted(self):
        self.docker.verify_mismatch=True;self.assert_refused('SNAPSHOT_VERIFICATION_MISMATCH')

    def test_nonempty_or_world_writable_destination_rejected(self):
        (self.backup/'existing').write_bytes(b'x');self.assert_refused('EMPTY_DISTINCT_BACKUP_DIRECTORY_REQUIRED')
        (self.backup/'existing').unlink();self.backup.chmod(0o777);self.assert_refused('UNSAFE_DESTINATION')

    def test_symlink_destination_and_mutable_image_rejected(self):
        alias=self.root/'alias';alias.symlink_to(self.backup,target_is_directory=True)
        self.assert_refused('UNSAFE_DESTINATION',backup_directory=alias)
        self.assert_refused('SNAPSHOT_IDENTITY_INVALID',image_id='worker:latest')


@unittest.skipUnless(os.getenv('MAIMI_SNAPSHOT_LOCAL_DOCKER_TEST')=='1' and os.geteuid()==0,
                     'explicit local synthetic Docker run only')
class SnapshotDockerTests(unittest.TestCase):
    def test_real_isolated_archives_reopen_and_match_synthetic_source_bytes(self):
        docker=DockerCLI();owner=uuid.uuid4().hex;namespace='r65-snapshot-test'
        helper=os.environ['MAIMI_SNAPSHOT_HELPER_IMAGE']
        volumes=[];manager=None
        with tempfile.TemporaryDirectory(prefix='maimi-r65-synthetic-snapshot-') as temp:
            backup=Path(temp)/'backup';receipts=Path(temp)/'receipts'
            backup.mkdir(mode=0o700);receipts.mkdir(mode=0o700)
            try:
                for kind in ('state','files'):
                    name=namespace+'-'+owner+'-'+kind;volumes.append(name)
                    docker.command('volume','create','--label',LABEL+'='+namespace,
                        '--label','com.megmeet.personal-cloud.owner='+owner,name)
                    payload='synthetic-'+kind+'\n'
                    docker.command('run','--rm','--network','none','--user','0:0','--mount','type=volume,src='+name+',dst=/sample',
                        '--entrypoint','python',helper,'-c',
                        'from pathlib import Path; Path("/sample/example.txt").write_text('+repr(payload)+')')
                manager=docker.command('create','--label','com.docker.compose.project=megmeet-desktop-server',
                    '--label','com.docker.compose.service=personal-cloud-manager',
                    '--env','PERSONAL_CLOUD_WORKER_RUNTIME=0.2.0-rc.2','--entrypoint','python',helper,'-c','pass')
                result=prepare_owner_snapshot(docker=docker,owner=owner,namespace=namespace,manager_id=manager,
                    image_id=IMAGE,release_sha=RELEASE,helper_image=helper,backup_directory=backup,receipts_directory=receipts)
                self.assertEqual(result['status'],'PREPARED_NOT_ACTIVATED')
                for kind,name in zip(('state','files'),volumes):
                    expected=('synthetic-'+kind+'\n').encode()
                    with tarfile.open(backup/(kind+'.tar.gz'),'r:gz') as archive:
                        self.assertEqual(archive.getnames(),['example.txt'])
                        self.assertEqual(archive.extractfile('example.txt').read(),expected)
                    observed=docker.command('run','--rm','--network','none','--read-only',
                        '--mount','type=volume,src='+name+',dst=/sample,readonly','--entrypoint','python',helper,'-c',
                        'from pathlib import Path; import hashlib; print(hashlib.sha256(Path("/sample/example.txt").read_bytes()).hexdigest())')
                    self.assertEqual(observed,hashlib.sha256(expected).hexdigest())
                receipt=json.loads((receipts/(owner+'.json')).read_text())
                self.assertEqual(receipt['owner'],owner);self.assertEqual(receipt['target_runtime'],'0.2.1-rc.2')
                # Exercise the actual manager's admission check against the real receipt.
                test_dir=Path('/candidate')/Path(__file__).parent.resolve().relative_to(SOURCE.parent.resolve())
                program=('import sys; sys.path.insert(0,'+repr(str(test_dir))+'); from test_manager_native021 import load; '
                    'm=load(PERSONAL_CLOUD_WORKER_IMAGE='+repr(IMAGE)+',PERSONAL_CLOUD_RUNTIME_RELEASE_SHA='+repr(RELEASE)+
                    ',PERSONAL_CLOUD_MANAGER_NAMESPACE='+repr(namespace)+'); '
                    'm.image_id='+repr(IMAGE)+'; m.NATIVE021_RECEIPTS_DIR="/receipts"; '
                    'm.require_native021_history_receipt('+repr(owner)+','+
                    repr((IMAGE,receipt['state_volume']['name'],receipt['state_volume']['created_at']))+','+
                    repr((receipt['files_volume']['name'],receipt['files_volume']['created_at']))+'); print("ADMITTED")')
                admitted=docker.command('run','--rm','--network','none','--read-only','--user','0:0',
                    '--mount','type=bind,src='+str(SOURCE.parent.resolve())+',dst=/candidate,readonly',
                    '--mount','type=bind,src='+str(receipts)+',dst=/receipts,readonly',
                    '--entrypoint','python',helper,'-B','-c',program)
                self.assertEqual(admitted,'ADMITTED')
                self.assertEqual(docker.manager(manager)['state'],'created')
                self.assertEqual(docker.active_managers(),[])
            finally:
                if manager:docker.command('rm',manager)
                for name in reversed(volumes):docker.command('volume','rm',name)


if __name__=='__main__':unittest.main()
