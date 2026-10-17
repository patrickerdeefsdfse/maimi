"""Execute the exact ephemeral helper against synthetic Linux files, never SSH."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from remote_program import PROGRAM

@unittest.skipUnless(sys.platform.startswith('linux'),'openat/dirfd helper requires Linux target')
class RemoteFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='service-remote-helper-synthetic-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'workspace';self.root.mkdir()
    def invoke(self,action,**args):
        payload=json.dumps({'action':action,'root':str(self.root),'arguments':args}).encode()
        result=subprocess.run([sys.executable,'-I','-S','-c',PROGRAM],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        self.assertEqual(result.returncode,0);self.assertEqual(result.stderr,b'')
        return json.loads(result.stdout)
    def test_create_read_modify_exact_sha_and_mode(self):
        created=self.invoke('write_file',relative_path='hello.py',content='print("合成")\n',expected_sha256='MISSING')
        self.assertTrue(created['ok']);self.assertTrue(created['result']['created'])
        read=self.invoke('read_file',relative_path='hello.py',max_bytes=32768);self.assertTrue(read['ok'])
        self.assertEqual(read['result']['sha256'],hashlib.sha256((self.root/'hello.py').read_bytes()).hexdigest())
        os.chmod(self.root/'hello.py',0o750)
        changed=self.invoke('write_file',relative_path='hello.py',content='changed\n',expected_sha256=read['result']['sha256'])
        self.assertTrue(changed['ok']);self.assertFalse(changed['result']['created']);self.assertEqual((self.root/'hello.py').stat().st_mode&0o777,0o750)
        self.assertEqual((self.root/'hello.py').read_text(),'changed\n');self.assertFalse(list(self.root.glob('.maimi-write-*')))
    def test_exclusive_create_and_stale_sha_never_overwrite(self):
        target=self.root/'a.py';target.write_bytes(b'original')
        for expected in ('MISSING','0'*64):
            result=self.invoke('write_file',relative_path='a.py',content='replacement',expected_sha256=expected)
            self.assertFalse(result['ok']);self.assertEqual(result['error_code'],'FILE_CHANGED');self.assertEqual(target.read_bytes(),b'original')
    def test_parent_and_final_symlink_cannot_escape(self):
        outside=self.base/'outside';outside.mkdir();(outside/'secret.py').write_text('outside synthetic')
        (self.root/'parent').symlink_to(outside,target_is_directory=True);(self.root/'leaf.py').symlink_to(outside/'secret.py')
        for name in ('parent/secret.py','leaf.py'):
            for action in ('read_file','write_file'):
                args={'relative_path':name,'max_bytes':32768} if action=='read_file' else {'relative_path':name,'content':'overwrite','expected_sha256':'MISSING'}
                result=self.invoke(action,**args);self.assertFalse(result['ok'])
        self.assertEqual((outside/'secret.py').read_text(),'outside synthetic')
    def test_root_symlink_rejected(self):
        actual=self.root;self.root=self.base/'link';self.root.symlink_to(actual,target_is_directory=True)
        self.assertFalse(self.invoke('write_file',relative_path='a.py',content='x',expected_sha256='MISSING')['ok'])
    def test_hardlinked_file_rejected(self):
        target=self.root/'a.py';target.write_text('synthetic');os.link(target,self.base/'second')
        result=self.invoke('read_file',relative_path='a.py',max_bytes=32768);self.assertFalse(result['ok']);self.assertEqual(result['error_code'],'PATH_NOT_ALLOWED')
    def test_size_binary_control_and_missing_file_rejected(self):
        for data,code in ((b'x'*32769,'FILE_TOO_LARGE'),(b'a\x00b','PATH_NOT_ALLOWED'),(b'\xff','OPERATION_FAILED')):
            (self.root/'a.py').write_bytes(data);result=self.invoke('read_file',relative_path='a.py',max_bytes=32768)
            self.assertFalse(result['ok']);self.assertEqual(result['error_code'],code)
        result=self.invoke('read_file',relative_path='missing.py',max_bytes=32768);self.assertEqual(result['error_code'],'FILE_NOT_FOUND')
    def test_cwd_command_runs_only_from_registered_directory(self):
        payload=json.dumps({'action':'run_command','root':str(self.root),'arguments':{'relative_cwd':'.','command':'pwd; printf synthetic'}}).encode()
        result=subprocess.run([sys.executable,'-I','-S','-c',PROGRAM],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,str(self.root).encode()+b'\nsynthetic')
    def test_traversal_denied_without_creating_file(self):
        for path in ('../escape.py','/escape.py','a//b.py','.env','x.key'):
            result=self.invoke('write_file',relative_path=path,content='x',expected_sha256='MISSING');self.assertFalse(result['ok'])
        self.assertFalse((self.base/'escape.py').exists())

if __name__=='__main__':unittest.main()
