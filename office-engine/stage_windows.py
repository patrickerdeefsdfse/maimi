"""Stage the independent office component in the owned development runtime."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import urllib.request
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
DEST = ROOT.parent / 'desktop-local/runtime/office'
SOURCE = ROOT / 'runtime-win/python/Lib/site-packages'
DEST.mkdir(exist_ok=True)
with urllib.request.urlopen('https://www.python.org/ftp/python/3.13.15/windows-3.13.15.json', timeout=60) as response:
    manifest = json.load(response)
item = next(v for v in manifest['versions'] if v['id'] == 'pythonembed-3.13-64')
archive = ROOT / 'downloads/python-3.13.15-embed-amd64.zip'
if not archive.exists():
    with urllib.request.urlopen(item['url'], timeout=120) as response, archive.open('xb') as stream:
        shutil.copyfileobj(response, stream)
assert hashlib.sha256(archive.read_bytes()).hexdigest() == item['hash']['sha256']
if not (DEST/'py/python.exe').exists():
    with ZipFile(archive) as z:
        assert all(not n.startswith('/') and '..' not in Path(n).parts for n in z.namelist())
        z.extractall(DEST/'py')
(DEST/'py/python313._pth').write_text('python313.zip\n.\nlib\nimport site\n', encoding='utf-8')

def copy_tree(src, dst, skip):
    for file in src.rglob('*'):
        relative = file.relative_to(src)
        parts = relative.parts
        if src == ROOT/'libreoffice':
            if parts[:2] == ('share','extensions') and len(parts)>2 and parts[2].startswith('dict-') and parts[2] != 'dict-en':
                continue
            if parts[:2] == ('program','resource') and len(parts)>2 and parts[2] not in ('en-US','zh-CN','zh-TW'):
                continue
        if not file.is_file() or any(x.lower() in skip for x in relative.parts) or file.suffix in {'.pyc','.pyo'}:
            continue
        assert not file.is_symlink()
        target = dst/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != file.stat().st_size:
            shutil.copyfile(file, target)

# Runtime resources and all licenses stay; test suites, build headers and pip
# are not employee runtime features and produce unnecessarily long paths.
copy_tree(SOURCE, DEST/'py/lib', {'__pycache__','tests','test','testing','pip','include'})
copy_tree(ROOT/'libreoffice', DEST/'libreoffice', {'libreoffice_26.2.6_win_x86-64.msi'})
copy_tree(ROOT/'ocr-models', DEST/'ocr-models', set())
for name in ['engine.py','ocr-config.json','ocr-models.json','requirements.lock.txt','python-source.json','libreoffice-source.json']:
    shutil.copyfile(ROOT/name, DEST/name)
adapter = ROOT.parent/'dev-local/dsh/office-adapter'
for name in ['index.mjs','adapter.mjs','transport.mjs']:
    (DEST/'adapter').mkdir(exist_ok=True)
    shutil.copyfile(adapter/name, DEST/'adapter'/name)
shutil.copyfile(ROOT/'THIRD-PARTY.md', DEST/'THIRD-PARTY.md')
entries = [p for p in sorted(DEST.rglob('*')) if p.is_file() and p.name != 'office-manifest.json' and '__pycache__' not in p.parts]
print('OFFICE_FILES_TO_HASH:',len(entries),flush=True)
def digest(file):
    return file.relative_to(DEST).as_posix(), {'bytes':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
with ThreadPoolExecutor(max_workers=12) as pool:
    files = dict(pool.map(digest, entries))
record = {'protocol':'megmeet-office-v1','version':'1.0.0','python':'3.13.15',
          'libreoffice':'26.2.6','dsh_adapter':'0.1.6-alpha.1','files':files,
          'scope':'offline processing; no corporate credentials or user documents'}
(DEST/'office-manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('OFFICE_COMPONENT_STAGED:',len(files),sum(r['bytes'] for r in files.values()),flush=True)
print('OFFICE_MANIFEST_SHA256:',hashlib.sha256((DEST/'office-manifest.json').read_bytes()).hexdigest(),flush=True)
policy={'version':'1.0.0','manifestSha256':hashlib.sha256((DEST/'office-manifest.json').read_bytes()).hexdigest()}
(ROOT.parent/'desktop-local/office-policy.json').write_text(json.dumps(policy,indent=2)+'\n',encoding='utf-8')
