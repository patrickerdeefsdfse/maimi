"""Acquire official LibreOffice MSI plus SHA256 for owned administrative extraction."""
import hashlib
from pathlib import Path
import re
import urllib.request
import shutil
import json

root = Path(__file__).resolve().parent
url = 'https://download.documentfoundation.org/libreoffice/stable/26.2.6/win/x86_64/LibreOffice_26.2.6_Win_x86-64.msi'
with urllib.request.urlopen(url+'.sha256', timeout=60) as r:
    checksum = r.read().decode().split()[0]
assert re.fullmatch('[0-9a-f]{64}', checksum)
file = root / 'downloads' / url.rsplit('/',1)[-1]
file.parent.mkdir(exist_ok=True)
if not file.exists():
    with urllib.request.urlopen(url, timeout=180) as r, file.open('xb') as stream:
        shutil.copyfileobj(r, stream, 1024*1024)
with file.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
assert digest == checksum
(root / 'libreoffice-source.json').write_text(json.dumps({'url':url, 'sha256':checksum, 'version':'26.2.6'}, indent=2))
print('LIBREOFFICE_DOWNLOAD: PASS', flush=True)
