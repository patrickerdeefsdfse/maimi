"""Synthetic root-filesystem and real local Docker checks; no company access."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from private_store import PrivateStore, StoreError, key_bytes, key_of

@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os,'geteuid') and os.geteuid()==0,
                     'Linux root required')
class LinuxChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='maimi-r53-synthetic-',dir='/root')
        self.base=Path(self.temp.name)
        self.root=self.base/'rotation';self.root.mkdir(mode=0o700)
        self.parent=self.base/'credentials';self.parent.mkdir(mode=0o700)
        self.credential=self.parent/'kuaidi100-mcp.json'
        self.old=key_bytes('SyntheticOld');self.new=key_bytes('SyntheticNew')
        self.credential.write_bytes(self.old);self.credential.chmod(0o600);os.chown(self.credential,10001,10001)
        self.store=PrivateStore(self.root,self.credential,shared_locks=())
    def tearDown(self):self.temp.cleanup()
    def test_same_inode_private_backup_write_and_restore(self):
        with self.store.locked():
            old,ident=self.store.read_credential()
            self.store.write('previous-key.json',old);self.store.write('pending-key.json',self.new)
            self.store.write_json('journal.json',{'phase':'WRITING'})
            self.store.write_credential_in_place(self.new,ident)
            self.assertEqual(self.store.read_credential(ident),(self.new,ident))
            backup=(self.root/'previous-key.json').stat()
            self.assertEqual((backup.st_uid,backup.st_gid,backup.st_mode&0o777),(0,0,0o600))
            self.store.write_credential_in_place(old,ident)
            self.assertEqual(self.store.read_credential(ident)[0],old)
    def test_empty_and_partial_write_restore(self):
        old,ident=self.store.read_credential()
        for partial in (b'',b'{"key":"Synthetic'):
            self.credential.write_bytes(partial)
            self.store.write_credential_in_place(old,ident)
            self.assertEqual(self.store.read_credential(ident)[0],old)
    def test_operation_lock_and_shared_release_lock(self):
        import fcntl
        with self.store.locked():
            with self.assertRaisesRegex(StoreError,'ROTATION_BUSY'):
                with self.store.locked():pass
        shared=self.root/'release.lock';fd=os.open(shared,os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);self.store.shared_locks=(shared,)
            with self.assertRaisesRegex(StoreError,'RELEASE_MAINTENANCE_BUSY'):
                with self.store.locked():pass
        finally:os.close(fd)
    def test_wrong_permissions_owner_and_hardlink_rejected(self):
        for mode in (0o644,0o666,0o400):
            self.credential.chmod(mode)
            with self.assertRaises(StoreError):self.store.read_credential()
        self.credential.chmod(0o600);os.chown(self.credential,0,0)
        with self.assertRaises(StoreError):self.store.read_credential()
        os.chown(self.credential,10001,10001);os.link(self.credential,self.parent/'alias')
        with self.assertRaises(StoreError):self.store.read_credential()
    def test_symlink_and_changed_inode_rejected(self):
        old,ident=self.store.read_credential()
        real=self.parent/'real';self.credential.rename(real);self.credential.symlink_to(real)
        with self.assertRaises((StoreError,OSError)):self.store.read_credential()
        self.credential.unlink();self.credential.write_bytes(old)
        self.credential.chmod(0o600);os.chown(self.credential,10001,10001)
        with self.assertRaises(StoreError):self.store.write_credential_in_place(self.new,ident)
        self.assertEqual(self.credential.read_bytes(),old)
    def test_unsafe_parent_private_target_and_bad_json_rejected(self):
        self.parent.chmod(0o777)
        with self.assertRaises(StoreError):self.store.read_credential()
        self.parent.chmod(0o700)
        target=self.root/'victim';target.write_bytes(b'preserve');target.chmod(0o600)
        (self.root/'pending-key.json').symlink_to(target)
        with self.assertRaises(StoreError):self.store.write('pending-key.json',self.new)
        self.assertEqual(target.read_bytes(),b'preserve')
        self.credential.write_bytes(b'{"key":"a","key":"b"}')
        with self.assertRaises(StoreError):self.store.read_credential()
        with self.assertRaises(StoreError):self.store.write('../escape.json',b'{}')
    def test_actual_docker_readonly_bind_and_process_reload(self):
        name='maimi-r53-synthetic-'+uuid.uuid4().hex[:12]
        def docker(*args,data=None):
            p=subprocess.run(['docker','--host','unix:///var/run/docker.sock',*args],input=data,
                             capture_output=True,timeout=25,check=False)
            self.assertEqual(p.returncode,0,'LOCAL_SYNTHETIC_DOCKER_COMMAND_FAILED')
            return p.stdout
        program="import json,time;key=json.load(open('/credential.json'))['key'];open('/tmp/cache','w').write(key);time.sleep(180)"
        check="import sys,json,hmac; print(json.dumps({'matches':hmac.compare_digest(open('/tmp/cache').read(),sys.stdin.read())}))"
        created=False
        try:
            cid=docker('run','-d','--name',name,'--network','none','--read-only','--user','10001:10001',
                       '--cap-drop','ALL','--security-opt','no-new-privileges',
                       '--tmpfs','/tmp:rw,noexec,nosuid,size=1m',
                       '--mount','type=bind,source='+str(self.credential)+',target=/credential.json,readonly',
                       'python:3.12-slim','python','-B','-c',program).decode().strip();created=True
            def verify(key):
                deadline=time.monotonic()+10
                while True:
                    p=subprocess.run(['docker','--host','unix:///var/run/docker.sock','exec','-i',cid,
                                      'python','-B','-c',check],input=key.encode(),capture_output=True,timeout=5)
                    if p.returncode==0:
                        self.assertEqual(json.loads(p.stdout),{'matches':True});return
                    if time.monotonic()>deadline:self.fail('LOCAL_SYNTHETIC_PROCESS_NOT_READY')
                    time.sleep(.2)
            verify(key_of(self.old))
            old,ident=self.store.read_credential()
            docker('stop','--time','1',cid)
            self.store.write_credential_in_place(self.new,ident)
            docker('start',cid);verify(key_of(self.new))
            self.assertEqual(docker('inspect','--format','{{.Id}}',name).decode().strip(),cid)
            self.assertEqual(self.store.read_credential(ident)[0],self.new)
            docker('stop','--time','1',cid)
            self.store.write_credential_in_place(old,ident)
            docker('start',cid);verify(key_of(old))
            self.assertEqual(docker('inspect','--format','{{.Id}}',name).decode().strip(),cid)
        finally:
            if created:docker('rm','-f',name)

if __name__=='__main__':unittest.main()
