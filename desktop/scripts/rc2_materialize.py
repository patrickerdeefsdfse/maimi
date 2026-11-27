"""Stage the pinned official Windows runtime and the previous company's redistributable plugins."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from asar_extract import extract

repo=Path(__file__).resolve().parents[1]
pins=json.loads((repo/'deps/pins.json').read_text(encoding='utf-8'))
work=Path(os.environ['MAIMI_WORK']).resolve()
seven=shutil.which('7z') or str(Path(os.environ['ProgramFiles'])/'7-Zip/7z.exe')

def load(p):return json.loads(p.read_text(encoding='utf-8'))
def write(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def installer(key,pin,alg,expected):
    path=Path(os.environ[key]).resolve(strict=True)
    with path.open('rb') as stream:hash=hashlib.file_digest(stream,alg)
    digest=base64.b64encode(hash.digest()).decode() if alg=='sha512' else hash.hexdigest()
    if digest!=expected:raise ValueError(f'{key} does not match its signed release pin')
    root=work/'source-assets'/pin
    root.mkdir(parents=True,exist_ok=False)
    subprocess.run([seven,'e',path,r'$PLUGINSDIR\app-64.7z','-o'+str(root),'-bso0','-bsp0'],check=True)
    subprocess.run([seven,'x',root/'app-64.7z','-o'+str(root/'files'),'-bso0','-bsp0'],check=True)
    return root/'files'

def stage():
    packed=work/'windows/win-unpacked';resources=packed/'resources';app=resources/'app'
    if packed.exists():raise RuntimeError('Use a fresh MAIMI_WORK directory')
    upstream=installer('MAIMI_UPSTREAM_SETUP','upstream','sha512',pins['upstreamWindows']['sha512'])
    previous=installer('MAIMI_PREVIOUS_SETUP','previous','sha256',pins['baselineWindows']['sha256'])
    mime=Path(os.environ['MAIMI_MIME_ARCHIVE']).resolve(strict=True)
    with mime.open('rb') as f:
        assert base64.b64encode(hashlib.file_digest(f,'sha512').digest()).decode()==pins['mime']['sha512']
    with tarfile.open(mime) as tar:tar.extractall(work/'source-assets/mime',filter='data')
    old=previous/'resources/app'
    assert load(old/'package.json')['version']==pins['baselineWindows']['version']
    resources.mkdir(parents=True)
    for item in previous.iterdir():
        if item.name=='resources':continue
        if item.is_dir():shutil.copytree(item,packed/item.name)
        else:shutil.copyfile(item,packed/item.name)
    prepared=work/'prepared/windows/electron'
    prepared.mkdir(parents=True)
    shutil.copyfile(previous/'Maimi.exe',prepared/'electron.exe')
    extract(upstream/'resources/app.asar',app,pins['dsh']['release'])
    shutil.copytree(upstream/'resources/runtime',resources/'runtime')
    for name in ['icon.png','tray.ico','elevate.exe']:shutil.copyfile(previous/'resources'/name,resources/name)
    shutil.copytree(old/'maimi',app/'maimi')
    shutil.copytree(repo/'shell/maimi',app/'maimi',dirs_exist_ok=True)
    configured=os.environ.get('MAIMI_ENTERPRISE_USERS')
    if configured:shutil.copyfile(Path(configured).resolve(strict=True),app/'maimi/enterprise-users.json')
    source=repo/'shell/renderer'
    for name in ['company-login.html','company-login.css','company-login.js','maimi-icon.png']:
        shutil.copyfile(source/name if (source/name).is_file() else old/'renderer'/name,app/'renderer'/name)
    for name in ['update-dialog.html','update-dialog.js','update-close.svg','maimi-light.css','welcome.css','policy-login-loading.html']:
        shutil.copyfile(source/name,app/'renderer'/name)
    shutil.copyfile(source/'update-dialog.rc2.css',app/'renderer/update-dialog.css')
    dsh=app/'dsh';previous_dsh=old/'dsh'
    shutil.copytree(previous_dsh/'plugins',dsh/'plugins',dirs_exist_ok=True)
    shutil.copytree(repo/'runtime/plugins',dsh/'plugins',dirs_exist_ok=True)
    for group in (previous_dsh/'node_modules').iterdir():
        if group.name.startswith('.'):continue
        for pkg in group.iterdir() if group.name.startswith('@') else [group]:
            if not (pkg/'package.json').is_file() or (pkg.parent.name=='@deepseek-ai' and pkg.name.startswith('dsh')):continue
            dest=dsh/'node_modules'/pkg.relative_to(previous_dsh/'node_modules')
            if not dest.exists():shutil.copytree(pkg,dest)
    installed=load(dsh/'package.json');prev=load(previous_dsh/'package.json')
    for name in prev.get('dependencies',{}):
        manifest=dsh/'node_modules'/name/'package.json'
        if not name.startswith('@deepseek-ai/') and name not in installed['dependencies'] and manifest.is_file():
            installed['dependencies'][name]=load(manifest)['version']
    write(dsh/'package.json',installed)
    shell=load(repo/'shell/package.json')
    for pkg,version in shell['dependencies'].items():
        assert load(app/'node_modules'/pkg/'package.json')['version']==version,(pkg,version)
    write(app/'package.json',shell)
    (resources/'app-update.yml').write_text('provider: generic\nurl: https://desktop-gateway.example.invalid/maimi/updates/windows/\nchannel: stable\nuseMultipleRangeRequest: false\nupdaterCacheDirName: maimi-desktop-updater\n',encoding='utf-8')

if __name__=='__main__':stage()
