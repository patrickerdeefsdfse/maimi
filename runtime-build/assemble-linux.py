from pathlib import Path
import hashlib,json,shutil,subprocess,sys
TASK=Path('/home/megmeet/dsh020-r41')
SRC=Path('/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime')
UP=TASK/'runtime-linux-x64'
assert json.loads((UP/'node_modules/@deepseek-ai/dsh/package.json').read_text())['version']=='0.2.0-rc.2'
PREBUILD='--prebuild' in sys.argv
LOCK=TASK/'lock-prebuild/package-lock.json' if PREBUILD else UP/'package-lock.json'
assert LOCK.exists()
TREE=TASK/'build/tree'
assert TREE.resolve()==Path('/home/megmeet/dsh020-r41/build/tree')
if TREE.exists():shutil.rmtree(TREE)
TREE.mkdir(parents=True,exist_ok=True)
D=TREE/'opt/dsh'; P=TREE/'opt/personal-cloud'
D.mkdir(parents=True,exist_ok=True)
def omit_office_wasm(folder,names):
 return {'libreoffice-kit-wasm'} if Path(folder).name=='@deepseek-ai' and 'libreoffice-kit-wasm' in names else set()
shutil.copytree(UP/'node_modules',D/'node_modules',dirs_exist_ok=True,symlinks=True,ignore=omit_office_wasm)
# npm creates empty foreign-platform optional directories before final cleanup.
# A prebuild has no deliverable status; remove only proven empty directories.
if PREBUILD:
 for p in (D/'node_modules/@deepseek-ai').iterdir():
  if p.is_dir() and not any(p.iterdir()):p.rmdir()
# Fixed optional package only; enterprise Office bridge uses shared service.
office_wasm=D/'node_modules/@deepseek-ai/libreoffice-kit-wasm'
if office_wasm.exists():shutil.rmtree(office_wasm)
shutil.copyfile(UP/'package.json',D/'package.json')
shutil.copyfile(LOCK,D/'package-lock.json')
shutil.copytree(TASK/'base-export/personal-cloud',P,dirs_exist_ok=True,symlinks=True)
for obsolete in ('candidate-manifest.json','rc2-source-pins.json','guard-runtime.mjs','verify-existing-runtime.mjs','manager.py'):
 if (P/obsolete).exists():(P/obsolete).unlink()
for n in ('toolkit','office-adapter'):shutil.copytree(TASK/'base-export'/n,D/n,dirs_exist_ok=True)
for n in ('enterprise-native.mjs','workspace-pinned-overlay.mjs','office-response-client.mjs','community.patch.json'):shutil.copyfile(TASK/'base-export'/n,D/n)
shutil.copytree(TASK/'base-export/@dshp',D/'node_modules/@dshp',dirs_exist_ok=True)
ui=json.loads((SRC/'ui-overlay-manifest.json').read_text())
for row in ui['adaptations']:
 p=D/'node_modules'/row['package']/'lib/client.js'
 assert hashlib.sha256(p.read_bytes()).hexdigest()==row['originalSha256'],str(p)
# Source/controller changes are guarded by exact reviewed anchors, record both hashes.
g=D/'node_modules/@deepseek-ai/dsh-api-workspace-controller/lib/index.js'
s=g.read_text();before=hashlib.sha256(g.read_bytes()).hexdigest()
changes=[
 ('\tcreate(request) {\n\t\treturn this.enqueue',"\tcreate(request) {\n\t\tif (request.path !== '/workspace') return Promise.reject(new RemoteError('workspace/cloud-fixed', '云端工作区路径不可更改', {}));\n\t\treturn this.enqueue"),
 ('\trename(request) {\n\t\tconst title',"\trename(request) {\n\t\tif (request.title !== '云端工作区') return Promise.reject(new RemoteError('workspace/cloud-fixed', '云端工作区名称不可更改', {}));\n\t\tconst title"),
 ('\tdelete(request) {\n\t\treturn this.enqueue',"\tdelete(request) {\n\t\treturn Promise.reject(new RemoteError('workspace/cloud-fixed', '云端工作区不可删除', {}));\n\t\treturn this.enqueue"),
 ('\t\tasync initializeDefault(signal) {\n','\t\tasync initializeDefault(signal) {\n\t\t\treturn undefined; // Enterprise worker registers fixed workspace.\n'),
]
for a,b in changes:
 assert s.count(a)==1,repr(a)
 s=s.replace(a,b)
g.write_text(s)
(SRC/'workspace-controller-adaptation.json').write_text(json.dumps({'runtime':'0.2.0-rc.2','original':before,'adapted':hashlib.sha256(g.read_bytes()).hexdigest(),'anchors_verified':len(changes)},indent=2)+'\n')
shutil.copytree(SRC/'overlay',TREE,dirs_exist_ok=True)
# Pin reviewed hand-written worker/bridge sources and every modified native bundle.
shutil.copyfile(SRC/'verify-runtime.mjs',P/'verify-runtime020.mjs')
pins={}
for folder in [P,D/'toolkit',D/'office-adapter',D/'node_modules/@dshp']:
 for p in folder.rglob('*'):
  if p.is_file() and p.suffix in {'.mjs','.js','.json','.yml'}:pins['/'+str(p.relative_to(TREE))]=hashlib.sha256(p.read_bytes()).hexdigest()
for n in ('enterprise-native.mjs','workspace-pinned-overlay.mjs','office-response-client.mjs','community.patch.json'):
 p=D/n;pins['/'+str(p.relative_to(TREE))]=hashlib.sha256(p.read_bytes()).hexdigest()
for row in ui['adaptations']:
 p=D/'node_modules'/row['package']/'lib/client.js';pins['/'+str(p.relative_to(TREE))]=hashlib.sha256(p.read_bytes()).hexdigest()
pins['/'+str(g.relative_to(TREE))]=hashlib.sha256(g.read_bytes()).hexdigest()
inventory={}
for p in sorted((D/'node_modules/@deepseek-ai').glob('*/package.json')):
 item=json.loads(p.read_text());inventory[item['name']]=item['version']
manifest={'schema':'megmeet-dsh020-runtime-pins-v1','runtime':'0.2.0-rc.2','packages':inventory,'source_targets':pins}
(P/'runtime020-source-pins.json').write_text(json.dumps(manifest,indent=2)+'\n')
(SRC/'source-pins.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'status':'PREPARED','prebuild_only':PREBUILD,'deepseek_packages':len(inventory),'source_targets':len(pins)}))
