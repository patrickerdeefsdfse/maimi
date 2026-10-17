"""Verify the exported source allowlist, never read a runtime secret."""
import hashlib
import json
from pathlib import Path

root=Path(__file__).resolve().parents[3]
manifest=json.loads((root/'release-manifest.json').read_text())
for name,record in manifest['files'].items():
    path=root/name
    if path.is_symlink() or root not in path.resolve().parents:raise SystemExit('Unsafe release path')
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        # Host-side verification must also work on the supported Python 3.10.
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    actual=digest.hexdigest()
    if actual!=record['sha256']:raise SystemExit('Release source verification failed: '+name)
print('RELEASE_SOURCE_CHECKSUMS: PASS')
