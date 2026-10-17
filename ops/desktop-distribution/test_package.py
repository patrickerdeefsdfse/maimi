"""Public-only bundle checks with synthetic EXE and feed, no signing key."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('distribution_verify', HERE / 'verify-distribution.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='megmeet-distribution-package-')
        self.root = Path(self.temporary.name) / 'source'
        self.root.mkdir()
        for name in (*verify.CODE, *verify.ASSETS, 'package.py'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / name, target)
        (self.root / 'site/releases').mkdir()
        (self.root / 'site/updates').mkdir()
        (self.root / 'site/release.json').write_text('{"schema_version":1,"status":"unpublished"}')
        self.addCleanup(self.temporary.cleanup)

    def publish(self):
        payload = b'MZsynthetic-executable-not-a-real-installer'
        filename = 'MEGMEET-AI-Setup-0.5.0-win-x64.exe'
        (self.root / 'site/releases' / filename).write_bytes(payload)
        value = {'schema_version': 1, 'status': 'published', 'version': '0.5.0', 'filename': filename,
                 'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        (self.root / 'site/release.json').write_text(json.dumps(value))
        (self.root / 'site/updates/company-win-x64.json').write_text('{"synthetic_signature":"not-real"}')
        return value

    def test_unpublished_is_allowed_only_explicitly(self):
        self.assertEqual(verify.inspect_site(self.root)['status'], 'unpublished')
        with self.assertRaises(ValueError):
            verify.inspect_site(self.root, require_published=True)

    def test_installer_digest_and_executable_header_are_verified(self):
        value = self.publish()
        self.assertEqual(verify.inspect_site(self.root, True), value)
        installer = self.root / 'site/releases' / value['filename']
        installer.write_bytes(b'MZtampered')
        with self.assertRaises(ValueError):
            verify.inspect_site(self.root, True)

    def test_unexpected_site_files_and_symlinks_are_refused(self):
        (self.root / 'site/secret.key').write_text('synthetic sentinel')
        with self.assertRaises(ValueError):
            verify.inspect_site(self.root)
        (self.root / 'site/secret.key').unlink()
        outside = self.root / 'synthetic-only'; outside.write_text('sentinel')
        try:
            (self.root / 'site/releases/leak.exe').symlink_to(outside)
        except OSError:
            self.skipTest('symlink support unavailable')
        with self.assertRaises(ValueError):
            verify.inspect_site(self.root)

    def test_actual_packaging_roundtrip_detects_changed_code_and_excludes_builder(self):
        self.publish()
        old_release = self.root / 'site/releases/MEGMEET-AI-Setup-0.4.0-x64.exe'
        old_release.write_bytes(b'MZprior-release-preserved')
        package = self.root.parent / 'synthetic-distribution.zip'
        subprocess.run([sys.executable, '-B', str(self.root / 'package.py'), '--output', str(package)],
                       check=True, capture_output=True, text=True)
        extracted = self.root.parent / 'extracted'
        with ZipFile(package) as archive:
            self.assertFalse(any('test_' in name or 'package.py' in name for name in archive.namelist()))
            self.assertFalse(any('0.4.0' in name for name in archive.namelist()))
            archive.extractall(extracted)
        self.assertEqual(old_release.read_bytes(), b'MZprior-release-preserved')
        root = extracted / 'desktop-distribution'
        verify.verify_bundle(root)
        self.assertEqual(verify.inspect_site(root, True)['version'], '0.5.0')
        (root / 'app.py').write_text('tampered')
        with self.assertRaises(ValueError):
            verify.verify_bundle(root)

    def test_python310_api_surface_verifies_bundle_and_rejects_same_size_tampering(self):
        self.publish()
        package = self.root.parent / 'python310-distribution.zip'
        subprocess.run([sys.executable, '-B', str(self.root / 'package.py'), '--output', str(package)],
                       check=True, capture_output=True, text=True)
        extracted = self.root.parent / 'python310-extracted'
        with ZipFile(package) as archive:
            archive.extractall(extracted)
        root = extracted / 'desktop-distribution'
        runner = (
            'import hashlib,runpy,sys\n'
            'if hasattr(hashlib,"file_digest"): delattr(hashlib,"file_digest")\n'
            'sys.argv=[sys.argv[1],"--require-published"]\n'
            'runpy.run_path(sys.argv[0],run_name="__main__")\n'
        )
        command = [sys.executable, '-B', '-c', runner, str(root / 'verify-distribution.py')]
        valid = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn('DISTRIBUTION_VERIFY: PASS', valid.stdout)
        target = root / 'app.py'
        original = target.read_bytes()
        target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        tampered = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn('DISTRIBUTION_VERIFY: FAIL', tampered.stderr)
        self.assertNotIn('AttributeError', tampered.stderr)

    def test_crlf_shell_source_exports_lf_with_exact_manifest_and_runs_in_bash(self):
        self.publish()
        script = self.root / 'start-distribution.sh'
        unix = script.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        windows = unix.replace(b'\n', b'\r\n')
        script.write_bytes(windows)
        package = self.root.parent / 'crlf-distribution.zip'
        subprocess.run([sys.executable, '-B', str(self.root / 'package.py'), '--output', str(package)],
                       check=True, capture_output=True, text=True)
        extracted = self.root.parent / 'crlf-extracted'
        with ZipFile(package) as archive:
            body = archive.read('desktop-distribution/start-distribution.sh')
            manifest = json.loads(archive.read('desktop-distribution/distribution-manifest.json'))
            self.assertEqual(body, unix)
            self.assertNotIn(b'\r', body)
            self.assertEqual(manifest['files']['start-distribution.sh'],
                             {'size_bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
            for name, record in manifest['files'].items():
                payload = archive.read('desktop-distribution/' + name)
                self.assertEqual(record, {'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
            archive.extractall(extracted)
        self.assertEqual(script.read_bytes(), windows)  # Export does not rewrite source.
        bundle = extracted / 'desktop-distribution'
        verify.verify_bundle(bundle)
        if sys.platform == 'linux':
            # Execute the real exported shell through its option gate. Block Docker
            # even if a future regression accidentally reaches a service command.
            bash = shutil.which('bash')
            self.assertIsNotNone(bash)
            result = subprocess.run([bash, '-c',
                'docker() { return 97; }; export -f docker; exec bash "$1" --invalid-option',
                '_', str(bundle / 'start-distribution.sh')], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Unsupported option', result.stderr)
            self.assertIn('DISTRIBUTION_VERIFY: PASS', result.stdout)
            self.assertNotIn('pipefail', result.stderr)


if __name__ == '__main__':
    unittest.main()
