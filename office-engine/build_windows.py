"""Build an owned, portable runtime, never install into the employee's Python."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
DEST = ROOT / 'runtime-win'
CACHE = ROOT / 'downloads'
CACHE.mkdir(exist_ok=True)
DEST.mkdir(exist_ok=True)
url = 'https://www.python.org/ftp/python/3.13.15/windows-3.13.15.json'
with urllib.request.urlopen(url, timeout=60) as r:
    manifest = json.load(r)
item = next(x for x in manifest['versions'] if x['id'] == 'pythoncore-3.13-64')
expected = '6479223746cdfb79d25865110d6f524ac98de081324e119af1dc3ae36bddc7a5'
assert item['hash']['sha256'] == expected
archive = CACHE / 'python-3.13.15-amd64.zip'
if not archive.exists():
    with urllib.request.urlopen(item['url'], timeout=120) as r, archive.open('xb') as f:
        shutil.copyfileobj(r, f)
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
python = DEST / 'python/python.exe'
if not python.exists():
    with ZipFile(archive) as z:
        for n in z.namelist():
            assert not n.startswith('/') and '..' not in Path(n).parts
        z.extractall(DEST / 'python')
subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--disable-pip-version-check',
                '--prefer-binary', '-r', str(ROOT / 'requirements.in'),
                '--report', str(ROOT / 'windows-install-report.json')], check=True)
freeze = subprocess.check_output([str(python), '-I', '-m', 'pip', 'freeze'], text=True)
(ROOT / 'requirements.lock.txt').write_text(freeze, encoding='utf-8')
(ROOT / 'python-source.json').write_text(json.dumps({'manifest': url, **item}, indent=2), encoding='utf-8')
print('OFFICE_WINDOWS_DEPENDENCIES: PASS', flush=True)
