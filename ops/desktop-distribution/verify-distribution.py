"""Verify a static distribution bundle without Docker, secrets or networking."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
CODE = ('app.py', 'compose.yaml', 'compose.nginx-network.yaml', 'nginx-maimi.locations.conf',
        'image-lock.json', 'start-distribution.sh', 'verify-distribution.py', 'README.md')
ASSETS = ('site/index.html', 'site/app.css', 'site/app.js', 'site/app-icon.png', 'site/release.json')
EXE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.exe$')
SHA = re.compile(r'^[a-f0-9]{64}$')


def allowed(name):
    if name in (*CODE, *ASSETS, 'site/updates/company-win-x64.json'):
        return True
    parts = name.split('/')
    return len(parts) == 3 and parts[:2] == ['site', 'releases'] and EXE.fullmatch(parts[2]) is not None and '..' not in parts[2]


def regular(root, name):
    if not allowed(name):
        raise ValueError('distribution path not allowed')
    item = root
    for part in name.split('/'):
        item = item / part
        if item.is_symlink():
            raise ValueError('distribution symlink denied')
    if not item.is_file() or item.stat().st_nlink != 1:
        raise ValueError('distribution file missing or invalid')
    return item


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        # The company host runs Python 3.10; file_digest starts at 3.11.
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_site(root, require_published=False):
    for name in (*CODE, *ASSETS):
        regular(root, name)
    site = root / 'site'
    # Refuse an accidental .env/key/source drop into the public site, even
    # though the HTTP server itself has no route for these files.
    for item in site.rglob('*'):
        if item.is_symlink():
            raise ValueError('site symlink denied')
        if item.is_file() and not allowed(item.relative_to(root).as_posix()):
            raise ValueError('unexpected public site file')
    value = json.loads((site / 'release.json').read_text(encoding='utf-8'))
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('release schema invalid')
    if value.get('status') == 'unpublished':
        if set(value) != {'schema_version', 'status'} or require_published:
            raise ValueError('release not published')
    else:
        if (set(value) != {'schema_version', 'status', 'version', 'filename', 'size_bytes', 'sha256'}
                or value.get('status') != 'published' or not isinstance(value['version'], str)
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?', value['version'])
                or not isinstance(value['filename'], str) or not EXE.fullmatch(value['filename']) or '..' in value['filename']
                or type(value['size_bytes']) is not int or not 1 <= value['size_bytes'] <= 4 * 1024**3
                or not isinstance(value['sha256'], str) or not SHA.fullmatch(value['sha256'])):
            raise ValueError('published release invalid')
        installer = regular(root, 'site/releases/' + value['filename'])
        if installer.stat().st_size != value['size_bytes'] or sha(installer) != value['sha256']:
            raise ValueError('installer integrity mismatch')
        with installer.open('rb') as stream:
            if stream.read(2) != b'MZ':
                raise ValueError('installer is not Windows executable')
        # The actual cryptographic feed verifier is owned by desktop updater.
        # This checks its presence/shape without altering or interpreting the
        # signature's canonical bytes.
        feed = regular(root, 'site/updates/company-win-x64.json')
        if not 1 <= feed.stat().st_size <= 512 * 1024 or not isinstance(json.loads(feed.read_bytes()), dict):
            raise ValueError('update feed missing or invalid')
    return value


def verify_bundle(root):
    manifest_file = root / 'distribution-manifest.json'
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ValueError('bundle manifest missing')
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('bundle manifest invalid')
    entries = manifest['files']
    if not set((*CODE, *ASSETS)).issubset(entries):
        raise ValueError('bundle manifest incomplete')
    for name, record in entries.items():
        item = regular(root, name)
        if (not isinstance(record, dict) or set(record) != {'size_bytes', 'sha256'}
                or item.stat().st_size != record['size_bytes'] or sha(item) != record['sha256']):
            raise ValueError('bundle integrity mismatch')
    actual = {p.relative_to(root).as_posix() for p in (root / 'site').rglob('*') if p.is_file()}
    if actual != {name for name in entries if name.startswith('site/')}:
        raise ValueError('site differs from verified manifest')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='store_true', help='builder-only: validate source without exported manifest')
    parser.add_argument('--require-published', action='store_true')
    args = parser.parse_args()
    try:
        if not args.source:
            verify_bundle(ROOT)
        value = inspect_site(ROOT, args.require_published)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SystemExit('DISTRIBUTION_VERIFY: FAIL (' + type(exc).__name__ + ')') from None
    print('DISTRIBUTION_VERIFY: PASS')
    print('RELEASE_STATUS:', value['status'])


if __name__ == '__main__':
    main()
