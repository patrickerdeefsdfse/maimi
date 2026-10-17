"""Host-side source checks on the Python 3.10 hashlib surface, synthetic files only."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
RUN_WITHOUT_FILE_DIGEST = (
    'import hashlib,runpy,sys\n'
    'if hasattr(hashlib,"file_digest"): delattr(hashlib,"file_digest")\n'
    'runpy.run_path(sys.argv[1],run_name="__main__")\n'
)


class PackageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='megmeet-host-package-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / 'new-system/ops/changsha-desktop/verify-package.py'
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(HERE / 'verify-package.py', self.script)
        # Span multiple read chunks so the compatibility fix cannot silently
        # verify only a prefix, while keeping the regression under 3 MiB.
        self.payload = self.root / 'new-system/synthetic-payload.bin'
        self.contents = b'synthetic-public-package\x00' * 100000
        self.payload.write_bytes(self.contents)
        records = {}
        for item in (self.script, self.payload):
            records[item.relative_to(self.root).as_posix()] = {
                'sha256': hashlib.sha256(item.read_bytes()).hexdigest(),
            }
        (self.root / 'release-manifest.json').write_text(json.dumps({'files': records}))

    def run_verifier(self):
        return subprocess.run(
            [sys.executable, '-B', '-c', RUN_WITHOUT_FILE_DIGEST, str(self.script)],
            capture_output=True, text=True,
        )

    def test_valid_source_package_works_without_file_digest(self):
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RELEASE_SOURCE_CHECKSUMS: PASS', result.stdout)

    def test_same_size_change_after_first_chunk_is_rejected_without_file_digest(self):
        changed = bytearray(self.contents)
        changed[1024 * 1024 + 123] ^= 1
        self.payload.write_bytes(changed)
        result = self.run_verifier()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Release source verification failed:', result.stderr)
        self.assertNotIn('AttributeError', result.stderr)


if __name__ == '__main__':
    unittest.main()
