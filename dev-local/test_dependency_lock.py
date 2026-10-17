from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from install_dependencies import REQUIREMENTS_KEY, select_requirements, target


class DependencyLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'requirements.txt'
        self.source.write_bytes(b'fastapi==0.115.6\nuvicorn[standard]==0.34.0\n')
        self.locks = self.directory / 'locks'
        self.locks.mkdir()

    def write_lock(self):
        lock = self.locks / 'requirements.installed.txt'
        lock.write_bytes(b'fastapi==0.115.6\nuvicorn==0.34.0\nstarlette==0.41.3\n')
        manifest = {
            'source_sha256': {REQUIREMENTS_KEY: hashlib.sha256(self.source.read_bytes()).hexdigest()},
            'dependency_target': target(),
            'dependency_lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
        }
        (self.locks / 'runtime-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return lock

    def test_first_build_uses_source(self):
        self.assertEqual(select_requirements(self.source, self.locks), (self.source, False))

    def test_next_build_selects_exact_lock(self):
        lock = self.write_lock()
        self.assertEqual(select_requirements(self.source, self.locks), (lock, True))

    def test_source_drift_stops_instead_of_silent_fallback(self):
        self.write_lock()
        self.source.write_bytes(b'fastapi==0.116.0\nuvicorn[standard]==0.34.0\n')
        with self.assertRaisesRegex(ValueError, 'source requirements changed'):
            select_requirements(self.source, self.locks)

    def test_partial_and_tampered_locks_stop(self):
        lock = self.write_lock()
        lock.write_bytes(lock.read_bytes() + b'httpx==0.28.1\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            select_requirements(self.source, self.locks)
        lock.unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete dependency lock'):
            select_requirements(self.source, self.locks)


if __name__ == '__main__':
    unittest.main()
