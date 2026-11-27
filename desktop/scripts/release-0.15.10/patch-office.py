"""Apply a publisher-verified Python wheel to the bundled local Windows office interpreter."""
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from prepare import HERE, OLD_STAGE, STAGE, digest, read, safe, save

from prepare import SHELL, TOOLS, vendor

version='13.0.0'
runtime=STAGE/'resources/runtime/primary-runtime'
python=runtime/'dependencies/python/python.exe'
packages=runtime/'dependencies/python/Lib/site-packages'
old='pillow-12.3.0.dist-info'
new=f'pillow-{version}.dist-info'
record,=[e for e in read(SHELL/'deps/windows-vendors-0.15.10.json') if e['name']=='pillow-win-amd64']
wheel={'url':record['url']}
archive=vendor('pillow-win-amd64',version)
with zipfile.ZipFile(archive) as bundle:
    entries={i.filename:i for i in bundle.infolist() if not i.is_dir()}
    indexes={name:(sha,size) for name,sha,size in csv.reader(io.StringIO(bundle.read(new+'/RECORD').decode('utf8')))}
    assert set(entries)==set(indexes)
    assert all(safe(n).split('/')[0] in {'PIL',new,'pillow.libs'} for n in entries)
    old_files=set()
    for prefix in ['PIL',old,'pillow.libs']:
        orig=OLD_STAGE/packages.relative_to(STAGE)/prefix
        old_files.update(p.relative_to(OLD_STAGE/packages.relative_to(STAGE)).as_posix() for p in orig.rglob('*') if p.is_file())
    for name in old_files-set(entries):
        target=packages/name
        assert target.resolve()==target and target.is_relative_to(STAGE)
        target.unlink(missing_ok=True)
    for name,item in entries.items():
        content=bundle.read(item)
        sha,size=indexes[name]
        if sha:
            assert sha=='sha256='+base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=') and len(content)==int(size)
        else:assert name==new+'/RECORD'
        target=packages/name
        assert target.resolve()==target and target.is_relative_to(STAGE)
        if not target.is_file() or target.read_bytes()!=content:
            target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.with_name(target.name+'.next')
            with temp.open('xb') as f:f.write(content)
            os.replace(temp,target)
    pending=sorted((packages/n).relative_to(STAGE).as_posix() for n in entries)
    removed=sorted((packages/n).relative_to(STAGE).as_posix() for n in old_files-set(entries))
old_meta=packages/old
if old_meta.is_dir():
    assert old_meta.resolve()==old_meta and old_meta.is_relative_to(STAGE)
    for directory in sorted(old_meta.rglob('*'),key=lambda p:len(p.parts),reverse=True):
        assert directory.is_dir() and directory.resolve()==directory
        directory.rmdir()
    old_meta.rmdir()
manifest=runtime/'runtime.json'
data=read(manifest)
assert data['pythonPackages']['Pillow'] in ['12.3.0',version]
data['pythonPackages']['Pillow']=version
temp=manifest.with_suffix('.company-next')
assert not temp.exists()
save(temp,data)
os.replace(temp,manifest)
for cmd in [[python,'-I','-B','-m','pip','check'],[python,'-I','-B',TOOLS/'office-smoke.py']]:
    subprocess.run(cmd,timeout=140,check=True)
save(HERE/'desktop-office.json',dict(previous='12.3.0',pillow=version,url=wheel['url'],wheel_sha256=digest(archive),paths=pending,
                                      removed=removed,removed_directories=[old_meta.relative_to(STAGE).as_posix()],
                                      scope='Bundled Windows CPython 3.12',pip_check=True,officeFormats=['PNG','JPEG','DOCX','XLSX','PPTX']))
