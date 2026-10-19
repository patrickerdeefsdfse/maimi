import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import runtime_snapshot as snapshot
import runtime_release as release


class SnapshotGuards(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.output=self.root/'snapshot';self.output.mkdir()
        for kind in ('state','files'):(self.root/('source-'+kind)).mkdir()
    def tearDown(self):self.tmp.cleanup()
    def run_helper(self,mode):
        text=snapshot.HELPER.replace("Path('/snapshot')",'Path('+repr(str(self.output))+')').replace(
            "Path('/source-'+kind)",'Path('+repr(str(self.root/'source-'))+'+kind)')
        out=io.StringIO()
        with patch.object(sys,'argv',['helper',mode]),contextlib.redirect_stdout(out):exec(compile(text,'helper','exec'),{})
        return json.loads(out.getvalue())
    def test_sticky_directory_refused_before_snapshot_receipt(self):
        child=self.root/'source-state'/'tmp';child.mkdir();child.chmod(0o1777)
        with self.assertRaisesRegex(ValueError,'UNSUPPORTED_OWNERSHIP'):self.run_helper('create')
        self.assertFalse((self.output/'state.tar.gz').exists())
    def test_unsupported_owner_archive_rejected_on_verify(self):
        for kind in ('state','files'):
            with tarfile.open(self.output/(kind+'.tar.gz'),'w:gz') as tar:
                member=tarfile.TarInfo('unsupported');member.uid=1234;tar.addfile(member)
        with self.assertRaisesRegex(ValueError,'UNSUPPORTED_OWNERSHIP'):self.run_helper('verify')
    def test_archive_fsync_failure_never_reports_verified(self):
        with patch.object(snapshot.os,'fsync',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.run_helper('create')
    def test_common_archive_policy_accepts_root_and_employee_and_rejects_duplicates(self):
        scope={};exec(snapshot.ARCHIVE_POLICY,scope);validate=scope['accepted_member'];seen=set()
        for uid,name in ((0,'root'),(1000,'employee')):
            member=tarfile.TarInfo(name);member.uid=uid;member.gid=uid;member.mode=0o600
            validate(member,str(self.root),seen)
        with self.assertRaisesRegex(ValueError,'MEMBER_INVALID'):validate(member,str(self.root),seen)
    def test_every_snapshot_and_restore_volume_mount_disables_copy_up(self):
        for cls,method in ((snapshot.DockerCLI,'helper'),(release.ReleaseDocker,'restore_helper')):
            client=cls();calls=[]
            def command(*args,**kwargs):
                calls.append(args);return '{}'
            client.command=command
            for mode in (('create','verify') if method=='helper' else ('restore','verify')):
                getattr(client,method)('sha256:'+'a'*64,{'state':{'name':'synthetic-state'},'files':{'name':'synthetic-files'}},self.output,mode)
            mounts=[a for call in calls for a in call if a.startswith('type=volume,')]
            self.assertEqual(len(mounts),4)
            self.assertTrue(all('volume-nocopy' in a for a in mounts))
    def test_supported_snapshot_create_and_second_pass_match(self):
        (self.root/'source-state'/'file').write_bytes(b'synthetic')
        self.assertEqual(self.run_helper('create'),self.run_helper('verify'))

if __name__=='__main__':unittest.main()
