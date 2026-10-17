from pathlib import Path
import hashlib,json,shutil,subprocess
R=Path('/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime')
T=Path('/home/megmeet/dsh020-r41/patch-build');T.mkdir(exist_ok=True)
pins=json.loads((R/'source-pins.json').read_text());changed=[]
for src in Path('/home/megmeet/dsh020-r41/base-export/@dshp').rglob('*'):
 if src.is_file() and src.suffix in {'.mjs','.js','.json','.yml'}:
  target='/opt/dsh/node_modules/@dshp/'+str(src.relative_to('/home/megmeet/dsh020-r41/base-export/@dshp'))
  pins['source_targets'].setdefault(target,hashlib.sha256(src.read_bytes()).hexdigest())
for src in (R/'overlay').rglob('*'):
 if not src.is_file():continue
 target='/'+str(src.relative_to(R/'overlay'));sha=hashlib.sha256(src.read_bytes()).hexdigest()
 if pins['source_targets'].get(target)==sha:continue
 pins['source_targets'][target]=sha;dest=T/target.lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest);changed.append(target)
assert changed,'NO_PATCH_FILES'
manifest=json.dumps(pins,indent=2)+'\n'
(R/'source-pins.json').write_text(manifest)
p=T/'opt/personal-cloud/runtime020-source-pins.json';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(manifest)
base=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}','maimi-dsh020-r41:local'],text=True).strip()
tag='maimi-dsh020-r41-patch-base:'+base[7:23]
subprocess.run(['docker','tag',base,tag],check=True)
lines=['FROM '+tag,'USER root']
for target in changed+['/opt/personal-cloud/runtime020-source-pins.json']:lines.append('COPY '+target.lstrip('/')+' '+target)
lines+=['RUN node /opt/personal-cloud/verify-runtime020.mjs','USER 1000:1000']
(T/'Dockerfile').write_text('\n'.join(lines)+'\n')
print(json.dumps({'changed':changed,'base':base}))
subprocess.run(['docker','build','--network','none','--pull=false','-t','maimi-dsh020-r41:local',str(T)],check=True)
