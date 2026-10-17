"""Synthetic r9 source-package verification; no Docker, network or real archive."""
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

SCRIPT = Path(__file__).resolve().parents[1] / 'local-env/package-changsha.py'


class OfficeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='megmeet-office-release-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        relative = [
            'services/gateway/app/synthetic.py', 'services/personal-cloud/worker.mjs', 'db/migrations/001.sql',
            'services/gateway/requirements.txt', 'dev-local/Dockerfile.gateway', 'dev-local/install_dependencies.py',
            'dev-local/locks/requirements.installed.txt', 'dev-local/locks/runtime-manifest.json', 'dev-local/locks/images.env',
            'docs/IAM-DIRECT.md', 'docs/DESKTOP-AUTH-PERSONAL-MODEL.md', 'docs/PERSONAL-CLOUD-AND-PLUGINS-V3.md',
            'docs/DSH-UPGRADE-AND-EXTENSION-BOUNDARY.md', 'docs/STORAGE-LIFECYCLE.md',
            'dev-local/dsh/package.json', 'dev-local/dsh/package-lock.json', 'dev-local/dsh/Dockerfile',
            'dev-local/dsh/vendor-community/manifest.json', 'ops/changsha-desktop/compose.yaml',
            'ops/changsha-desktop/test_do_not_ship.py', 'ops/changsha-desktop/.env',
            # These tempting large/private artifacts must never enter the source allowlist.
            'office-engine/runtime-win/python/python.exe', 'office-engine/libreoffice/program/soffice.exe',
            'office-engine/ocr-models/synthetic.onnx', 'office-engine/build-windows.log',
        ]
        relative += ['office-engine/' + name for name in ('engine.py', 'requirements.in', 'requirements.lock.txt',
            'requirements-linux.lock.txt', 'ocr-config.json', 'ocr-models.json', 'prepare_ocr.py', 'THIRD-PARTY.md', 'README.md', 'Dockerfile', 'build_linux.sh')]
        relative += ['dev-local/dsh/office-adapter/' + name for name in ('index.mjs', 'adapter.mjs', 'transport.mjs', 'README.md')]
        for name in relative:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic public source\n')
        self.release = self.root / 'releases/changsha-personal-cloud-20260918-r9'
        self.release.mkdir(parents=True)
        (self.release / 'images.tar.gz').write_bytes(b'synthetic offline archive')
        self.native = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in relative
                       if name in ('services/personal-cloud/worker.mjs', 'office-engine/engine.py') or
                       name.startswith('dev-local/dsh/office-adapter/') and name.endswith('.mjs')}
        self.images = {'archive': {'sha256': hashlib.sha256(b'synthetic offline archive').hexdigest()},
            'application_source_sha256': {'synthetic.py': hashlib.sha256(b'synthetic public source\n').hexdigest()},
            'personal_native_source_sha256': self.native, 'office': {'capabilities': {'protocol': 'megmeet-office-v1'}}}

    def package(self):
        (self.release / 'images-manifest.json').write_text(json.dumps(self.images))
        tree = ast.parse(SCRIPT.read_text(encoding='utf-8'), filename=str(SCRIPT))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ROOT' for t in node.targets):
                node.value = ast.Call(func=ast.Name(id='Path', ctx=ast.Load()), args=[ast.Constant(str(self.root))], keywords=[])
        ast.fix_missing_locations(tree)
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(tree, str(SCRIPT), 'exec'), {'__file__': str(SCRIPT), '__name__': '__main__'})
        return self.root / 'releases/MEGMEET-Changsha-Personal-Cloud-20260918-r9.zip'

    def test_office_sources_present_and_windows_runtime_secrets_tests_not_shipped(self):
        target = self.package()
        with ZipFile(target) as archive:
            names = set(archive.namelist())
            for name in ('engine.py', 'requirements-linux.lock.txt', 'ocr-config.json', 'ocr-models.json', 'prepare_ocr.py', 'THIRD-PARTY.md', 'README.md'):
                self.assertIn('new-system/office-engine/' + name, names)
            for name in ('index.mjs', 'adapter.mjs', 'transport.mjs'):
                self.assertIn('new-system/dev-local/dsh/office-adapter/' + name, names)
            self.assertFalse(any('/runtime-win/' in n or '/libreoffice/' in n or n.endswith('.onnx') or n.endswith('/.env') or 'test_do_not_ship' in n for n in names))
            self.assertIn('0.7.0', archive.read('INSTALL.md').decode())

    def test_source_changed_after_image_export_refused(self):
        (self.root / 'office-engine/engine.py').write_text('changed since export')
        with self.assertRaisesRegex(SystemExit, 'source changed'):
            self.package()

    def test_missing_office_image_evidence_refused(self):
        del self.images['office']
        with self.assertRaisesRegex(SystemExit, 'verification evidence'):
            self.package()

    def test_existing_r9_archive_never_overwritten(self):
        target = self.package()
        before = target.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'refusing to replace'):
            self.package()
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
