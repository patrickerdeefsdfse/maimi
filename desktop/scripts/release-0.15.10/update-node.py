"""Install the verified official x64 Node LTS distribution in the optional local tool runtime."""
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from prepare import DESCRIPTOR, HERE, OLD_STAGE, STAGE, digest, read, save

from prepare import SHELL, vendor

version='24.22.3'
base=f'https://nodejs.org/dist/v{version}/'
package=f'node-v{version}-win-x64'
archive=vendor('node-windows-x64',version)
checks={'win-x64/node.exe':read(SHELL/'deps/pins.json')['windowsSecurityComponents']['nodeWindowsX64']['exeSha256']}
runtime=STAGE/'resources/runtime/primary-runtime'
node=runtime/'dependencies/node'
paths=[]
with zipfile.ZipFile(archive) as bundle:
    for item,rel in [('node.exe','bin/node.exe'),('LICENSE','LICENSE')]:
        file=node/rel
        assert file.resolve()==file and file.resolve().is_relative_to(STAGE)
        with bundle.open(package+'/'+item) as src:
            content=src.read()
        if file.read_bytes()!=content:
            temp=file.with_name(file.name+'.candidate')
            with temp.open('xb') as output:output.write(content)
            os.replace(temp,file)
        paths.append(file.relative_to(STAGE).as_posix())
exe=node/'bin/node.exe'
assert digest(exe)==checks['win-x64/node.exe']
runtime_versions=json.loads(subprocess.check_output([exe,'-p','JSON.stringify(process.versions)']))
assert runtime_versions['node']==version and runtime_versions['undici']=='7.30.2'
pnpm=subprocess.check_output([exe,runtime/'dependencies/pnpm/bin/pnpm.mjs','--version'],text=True).strip()
assert pnpm=='11.28.5'
ids=read(OLD_STAGE/'resources/runtime/primary-runtime/runtime.json')
h=hashlib.sha256()
# Upstream uses this field for per-user runtime identity. Derive it from all shipped payload bytes.
count=0
for file in sorted((runtime/'dependencies').rglob('*')):
    if file.is_file():
        rel=file.relative_to(runtime).as_posix()
        h.update(rel.encode('utf8')+b'\0'+bytes.fromhex(digest(file)))
        count+=1
assert count>1000
tree_hash=h.hexdigest()
for file in [runtime/'runtime.json',STAGE/'resources/runtime/versions.json',STAGE/DESCRIPTOR]:
    data=read(file)
    if file.name=='desktop-runtime.json':
        data['release']['nodeVersion']=version
    else:
        assert data['node'] in [version,'24.21.0']
        data['node']=version
        if file.name=='runtime.json':data['payloadDigest']=tree_hash
    temp=file.with_name(file.name+'.candidate')
    assert not temp.exists()
    save(temp,data)
    os.replace(temp,file)
    if file.name!='desktop-runtime.json':paths.append(file.relative_to(STAGE).as_posix())
save(HERE/'standalone-node.json',dict(version=version,upstream=base+package+'.zip',checksum_url=base+'SHASUMS256.txt',
      archive_sha256=digest(archive),binary_sha256=digest(exe),previous=ids['node'],node=runtime_versions['node'],
      embedded_undici=runtime_versions['undici'],pnpm=pnpm,files=count,payloadDigest=tree_hash,paths=paths))
