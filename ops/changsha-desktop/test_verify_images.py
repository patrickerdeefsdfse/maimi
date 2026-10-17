"""Offline manifest checks with synthetic image metadata only."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('verify_images', Path(__file__).with_name('verify-images.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OfflineImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='megmeet-image-check-')
        self.path = Path(self.temporary.name) / 'images-manifest.json'
        self.identifier = 'sha256:' + '1' * 64
        self.rows = [{'reference': reference, 'platform_image_id': self.identifier, 'source_digest': digest}
                     for reference, digest in module.SOURCES.items()]
        self.path.write_text(json.dumps({'images': self.rows}))

    def tearDown(self):
        self.temporary.cleanup()

    def test_matching_ids_and_platform_accepted(self):
        with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=self.identifier + ' linux/amd64\n')) as run:
            module.verify(self.path)
            self.assertEqual(run.call_count, len(module.SOURCES))

    def test_changed_tag_or_wrong_platform_rejected(self):
        for stdout in ('sha256:' + '2' * 64 + ' linux/amd64\n', self.identifier + ' linux/arm64\n'):
            with self.subTest(stdout=stdout), patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout)), self.assertRaises(ValueError):
                module.verify(self.path)

    def test_unknown_image_reference_rejected_before_docker(self):
        self.rows[0]['reference'] = 'unreviewed:latest'
        self.path.write_text(json.dumps({'images': self.rows}))
        with patch.object(module.subprocess, 'run') as run, self.assertRaises(ValueError):
            module.verify(self.path)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
