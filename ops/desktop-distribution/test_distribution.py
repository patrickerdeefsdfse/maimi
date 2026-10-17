"""Synthetic fixtures only: no company endpoint, credentials or installer."""
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('distribution_app', HERE / 'app.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = {}
        self.scripts = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.elements[attrs['id']] = attrs
        if tag == 'script':
            self.scripts.append(attrs)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='megmeet-distribution-test-')
        self.root = Path(self.temporary.name)
        module.SITE = self.root
        (self.root / 'releases').mkdir()
        (self.root / 'updates').mkdir()
        for name in ('index.html', 'app.css', 'app.js', 'app-icon.png'):
            (self.root / name).write_bytes(b'synthetic static content')
        (self.root / 'index.html').write_bytes((HERE / 'site/index.html').read_bytes())
        (self.root / 'release.json').write_text('{"schema_version":1,"status":"unpublished"}')
        self.client = TestClient(module.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.temporary.cleanup)

    def publish(self):
        filename = 'MEGMEET-AI-Setup-0.5.0-win-x64.exe'
        payload = b'MZ' + bytes(range(256)) * 8
        (self.root / 'releases' / filename).write_bytes(payload)
        metadata = {'schema_version': 1, 'status': 'published', 'version': '0.5.0',
                    'filename': filename, 'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        (self.root / 'release.json').write_text(json.dumps(metadata))
        return '/maimi/releases/' + filename, payload, metadata

    def test_unpublished_page_is_truthful_and_no_feed_exists(self):
        response = self.client.get('/maimi/', follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        page = Page(response.text)
        self.assertNotIn('href', page.elements['download'])
        self.assertEqual(page.elements['download']['aria-disabled'], 'true')
        self.assertIn('安装包正在准备中', response.text)
        self.assertNotIn('正在读取版本信息', response.text)
        self.assertEqual(page.scripts, [])
        metadata = self.client.get('/maimi/release.json')
        self.assertEqual(metadata.json(), {'schema_version': 1, 'status': 'unpublished'})
        self.assertEqual(metadata.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/maimi/updates/company-win-x64.json').status_code, 404)
        self.assertEqual(self.client.get('/maimi/health').json()['release_status'], 'unpublished')

    def test_canonical_redirect_and_security_headers(self):
        response = self.client.get('/maimi', follow_redirects=False)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers['location'], '/maimi/')
        response = self.client.get('/maimi/')
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertNotIn('set-cookie', response.headers)

    def test_published_page_works_without_javascript_or_metadata_fetch(self):
        url, _, metadata = self.publish()
        response = self.client.get('/maimi/')
        self.assertEqual(response.status_code, 200)
        page = Page(response.text)
        self.assertEqual(page.elements['download']['href'], url)
        self.assertEqual(page.elements['download']['download'], metadata['filename'])
        self.assertNotIn('aria-disabled', page.elements['download'])
        self.assertNotIn('hidden', page.elements['checksum-details'])
        self.assertIn('版本 ' + metadata['version'], response.text)
        self.assertIn(metadata['sha256'], response.text)
        self.assertIn('href="/maimi/web/"', response.text)
        self.assertEqual(page.scripts, [])
        self.assertNotIn('请启用浏览器 JavaScript', response.text)
        self.assertNotIn('正在读取版本信息', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        head = self.client.head('/maimi/')
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head.headers['content-length']), len(response.content))

    def test_invalid_release_never_enables_download_or_leaks_raw_values(self):
        for invalid in ('extra_field', 'wrong_size', 'missing_installer', 'injected_filename'):
            with self.subTest(invalid=invalid):
                _, _, metadata = self.publish()
                if invalid == 'extra_field':
                    metadata['client_secret'] = 'SYNTHETIC_PRIVATE_SENTINEL'
                elif invalid == 'wrong_size':
                    metadata['size_bytes'] += 1
                elif invalid == 'missing_installer':
                    (self.root / 'releases' / metadata['filename']).unlink()
                else:
                    metadata['filename'] = '\" onclick=\"SYNTHETIC_PRIVATE_SENTINEL'
                (self.root / 'release.json').write_text(json.dumps(metadata))
                response = self.client.get('/maimi/')
                self.assertEqual(response.status_code, 503)
                page = Page(response.text)
                self.assertNotIn('href', page.elements['download'])
                self.assertEqual(page.elements['download']['aria-disabled'], 'true')
                self.assertIn('暂时无法读取下载信息', response.text)
                self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', response.text)
                self.assertEqual(page.scripts, [])

    def test_unrecognized_template_fails_closed(self):
        self.publish()
        (self.root / 'index.html').write_text('<html>unreviewed template</html>')
        response = self.client.get('/maimi/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': 'page_unavailable'})

    def test_full_download_head_and_resumable_byte_ranges(self):
        url, payload, expected = self.publish()
        self.assertEqual(self.client.get('/maimi/release.json').json(), expected)
        full = self.client.get(url)
        self.assertEqual(full.content, payload)
        self.assertIn('attachment;', full.headers['content-disposition'])
        self.assertEqual(full.headers['accept-ranges'], 'bytes')
        head = self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head.headers['content-length']), len(payload))
        for value, wanted in [('bytes=0-9', payload[:10]), ('bytes=10-', payload[10:]), ('bytes=-9', payload[-9:])]:
            part = self.client.get(url, headers={'Range': value})
            self.assertEqual(part.status_code, 206)
            self.assertEqual(part.content, wanted)
            self.assertTrue(part.headers['content-range'].startswith('bytes '))
        stale = self.client.get(url, headers={'Range': 'bytes=0-9', 'If-Range': '"stale"'})
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.content, payload)

    def test_unsatisfiable_and_multi_ranges_are_bounded(self):
        url, _, _ = self.publish()
        for value in ('bytes=9999999-', 'bytes=10-5', 'bytes=0-1,2-3', 'bytes=-', 'bytes=' + '1' * 100 + '-'):
            self.assertIn(self.client.get(url, headers={'Range': value}).status_code, (400, 416))

    def test_fixed_feed_is_byte_exact_and_not_cached(self):
        signed = b'{ "payload": "synthetic", "signature": "synthetic-only" }\n'
        (self.root / 'updates/company-win-x64.json').write_bytes(signed)
        response = self.client.get('/maimi/updates/company-win-x64.json')
        self.assertEqual(response.content, signed)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        (self.root / 'updates/private.key').write_text('synthetic private sentinel')
        self.assertEqual(self.client.get('/maimi/updates/private.key').status_code, 404)

    def test_no_directory_listing_secret_routes_or_other_api(self):
        (self.root / '.env').write_text('SYNTHETIC=not-public')
        for url in ('/', '/docs', '/openapi.json', '/v1/iam/callback', '/maimi/releases/', '/maimi/updates/',
                    '/maimi/.env', '/maimi/assets/.env', '/maimi/releases/%2e%2e%2f.env',
                    '/maimi/releases/name..exe', '/maimi/releases/private.pem', '/maimi/releases/test.exe%00'):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.post('/maimi/release.json', json={}).status_code, 405)

    def test_metadata_never_leaks_unrecognized_fields(self):
        _, _, metadata = self.publish()
        metadata['client_secret'] = 'synthetic sentinel'
        (self.root / 'release.json').write_text(json.dumps(metadata))
        response = self.client.get('/maimi/release.json')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('sentinel', response.text)

    def test_wrong_installer_size_disables_metadata(self):
        _, _, metadata = self.publish()
        metadata['size_bytes'] += 1
        (self.root / 'release.json').write_text(json.dumps(metadata))
        self.assertEqual(self.client.get('/maimi/release.json').status_code, 503)
        self.assertEqual(self.client.get('/maimi/health').status_code, 503)

    def test_symlink_release_and_feed_are_not_served(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'outside synthetic content')
        try:
            (self.root / 'releases/leak.exe').symlink_to(outside)
            (self.root / 'updates/company-win-x64.json').symlink_to(outside)
        except OSError:
            self.skipTest('symlink support unavailable on this test host')
        self.assertEqual(self.client.get('/maimi/releases/leak.exe').status_code, 404)
        self.assertEqual(self.client.get('/maimi/updates/company-win-x64.json').status_code, 404)

    def test_real_uvicorn_http_download_head_and_range(self):
        url, payload, metadata = self.publish()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'app:app', '--app-dir', str(HERE),
            '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
            env={**os.environ, 'DIST_SITE_ROOT': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        origin = 'http://127.0.0.1:' + str(port)
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(origin + '/maimi/health', timeout=.2) as response:
                        self.assertEqual(response.status, 200)
                    break
                except OSError:
                    if process.poll() is not None:
                        self.fail('synthetic Uvicorn exited before ready')
                    time.sleep(.1)
            else:
                self.fail('synthetic Uvicorn ready timeout')
            with urllib.request.urlopen(origin + '/maimi/', timeout=2) as response:
                html = response.read().decode('utf-8')
                page = Page(html)
                self.assertEqual(page.elements['download']['href'], url)
                self.assertEqual(page.scripts, [])
                self.assertIn(metadata['sha256'], html)
                self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
            with urllib.request.urlopen(urllib.request.Request(origin + url, method='HEAD'), timeout=2) as response:
                self.assertEqual(int(response.headers['Content-Length']), len(payload))
                self.assertEqual(response.read(), b'')
            with urllib.request.urlopen(urllib.request.Request(origin + url, headers={'Range': 'bytes=4-13'}), timeout=2) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), payload[4:14])
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
