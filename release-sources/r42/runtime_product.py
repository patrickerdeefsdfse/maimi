"""Offline worker build and immutable image proof, completed before service stops."""
import hashlib,json,re
from pathlib import Path
class Stop(ValueError):pass
def contract(p):
 value=json.loads(p.read(p.root/'worker-contract.json'))
 if (value.get('runtime')!='0.2.0-rc.2' or value.get('context')!='worker' or value.get('dockerfile')!='worker/Dockerfile'
     or not re.fullmatch('sha256:[a-f0-9]{64}',value.get('base_reference',''))
     or not re.fullmatch('sha256:[a-f0-9]{64}',value.get('base_platform_id',''))
     or not re.fullmatch('megmeet-personal-dsh:r42-020rc2-[a-f0-9]{12}',value.get('tag',''))
     or value.get('verifier')!='/opt/personal-cloud/verify-runtime020.mjs'
     or type(value.get('deepseek_packages')) is not int or not 1<=value['deepseek_packages']<=1024):raise Stop('R42_WORKER_CONTRACT_INVALID')
 targets=value.get('source_targets')
 if (not isinstance(targets,dict) or not targets or len(targets)>1024
     or any(not isinstance(k,str) or not k.startswith(('/opt/personal-cloud/','/opt/dsh/')) or '..' in k.split('/') or not re.fullmatch('[a-f0-9]{64}',v) for k,v in targets.items())):raise Stop('R42_WORKER_SOURCE_CONTRACT_INVALID')
 return value
def image(p,reference):
 fmt='{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},"runtime":{{json (index .Config.Labels "com.megmeet.dsh.runtime")}},"manifest":{{json (index .Config.Labels "com.megmeet.runtime42.manifest")}}}'
 v=json.loads(p.run('R42_WORKER_IMAGE_METADATA',['docker','image','inspect','--platform','linux/amd64','--format',fmt,reference],20))
 if (v.get('os')!='linux' or v.get('arch')!='amd64' or not re.fullmatch('sha256:[a-f0-9]{64}',v.get('id',''))):raise Stop('R42_WORKER_IMAGE_PLATFORM_INVALID')
 return v
def check_base(p):
 v=contract(p);info=image(p,v['base_reference'])
 if info['id']!=v['base_platform_id']:raise Stop('R42_WORKER_BASE_CHANGED')
 return info['id']

def local_base_reference(p,v):
 # Dockerfile FROM sha256:<config-id> is parsed as a registry image name by
 # BuildKit. Use an identity-checked local alias while keeping immutable proof.
 alias='megmeet-r42-base:'+p.manifest_sha[:16]
 rows=p.run('R42_LOCAL_BASE_TAG_CHECK',['docker','image','ls','--no-trunc','--filter','reference='+alias,'--format','{{.ID}}'],15).decode().splitlines()
 if len(rows)>1 or rows and rows!=[v['base_platform_id']]:raise Stop('R42_LOCAL_BASE_TAG_ALREADY_IN_USE')
 if not rows:p.run('R42_LOCAL_BASE_TAG',['docker','image','tag',v['base_reference'],alias],20)
 if image(p,alias)['id']!=v['base_platform_id']:raise Stop('R42_LOCAL_BASE_TAG_CHANGED')
 # Keep this release-owned alias. Removing a last tag can remove an otherwise
 # needed base image; no image or layer cleanup belongs to a runtime upgrade.
 return alias
def candidate(p):
 v=json.loads(p.read(p.root/'worker-candidate.json'))
 if set(v)!={'schema','manifest_sha256','image_id','runtime'} or v.get('schema')!='r42-worker-candidate-v1' or v.get('manifest_sha256')!=p.manifest_sha or v.get('runtime')!='0.2.0-rc.2' or not re.fullmatch('sha256:[a-f0-9]{64}',v.get('image_id','')):raise Stop('R42_WORKER_CANDIDATE_CHANGED')
 verify_candidate(p,v['image_id']);return v['image_id']
def verify_candidate(p,identifier):
 if identifier in p._runtime_verified:return
 top=json.loads(p.run('R42_CANDIDATE_TOP_ID',['docker','image','inspect','--format','{{json .Id}}',identifier],20))
 if top!=identifier:raise Stop('R42_CANDIDATE_TOP_PLATFORM_MISMATCH')
 v=contract(p);info=image(p,identifier)
 if info!={'id':identifier,'os':'linux','arch':'amd64','runtime':v['runtime'],'manifest':p.manifest_sha}:raise Stop('R42_WORKER_CANDIDATE_LABEL_CHANGED')
 code="""import fs from 'node:fs';import crypto from 'node:crypto';
const targets=__TARGETS__;
const result={};for(const [path,expected]of Object.entries(targets)){
 const s=fs.lstatSync(path);if(!s.isFile()||s.isSymbolicLink()||s.size>16777216)throw new Error('SOURCE_SHAPE');
 if(crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')!==expected)throw new Error('SOURCE_MISMATCH');
 result[path]=true;
}
const pkg=JSON.parse(fs.readFileSync('/opt/dsh/node_modules/@deepseek-ai/dsh/package.json','utf8'));
if(pkg.version!=='0.2.0-rc.2')throw new Error('RUNTIME_VERSION');
process.stdout.write(JSON.stringify({status:'VERIFIED',runtime:pkg.version,source_count:Object.keys(result).length}));
""".replace('__TARGETS__',json.dumps(v['source_targets'],separators=(',',':')))
 got=json.loads(p.run('R42_WORKER_CONTENT_PROOF',['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
  '--security-opt','no-new-privileges','--user','1000:1000','--entrypoint','/usr/local/bin/node',identifier,'--input-type=module','-e',code],45))
 if got!={'status':'VERIFIED','runtime':v['runtime'],'source_count':len(v['source_targets'])}:raise Stop('R42_WORKER_CONTENT_INVALID')
 checked=json.loads(p.run('R42_WORKER_NATIVE_PROOF',['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
  '--security-opt','no-new-privileges','--user','1000:1000','--entrypoint','/usr/local/bin/node',identifier,v['verifier']],45))
 if (checked.get('status')!='VERIFIED' or checked.get('runtime')!=v['runtime'] or checked.get('node')!='v22.20.0'
     or checked.get('deepseek_packages')!=v['deepseek_packages'] or checked.get('sharp') is not True):raise Stop('R42_WORKER_NATIVE_PROOF_INVALID')
 p._runtime_verified.add(identifier)
def prepare(p):
 p.verify_bundle();v=contract(p);check_base(p)
 if (p.root/'worker-candidate.json').exists():return candidate(p)
 rows=p.run('R42_WORKER_TAG_CHECK',['docker','image','ls','--no-trunc','--filter','reference='+v['tag'],'--format','{{.ID}}'],15).decode().splitlines()
 if len(rows)>1:raise Stop('R42_WORKER_TAG_NOT_UNIQUE')
 if rows:
  info=image(p,v['tag'])
  if info['manifest']!=p.manifest_sha or info['runtime']!=v['runtime']:raise Stop('R42_WORKER_TAG_ALREADY_IN_USE')
 else:
  base_alias=local_base_reference(p,v)
  print(json.dumps({'status':'R42_BUILDING_OFFLINE_RUNTIME','runtime':v['runtime'],'production_services_changed':False,'maximum_seconds':600}),flush=True)
  p.run('R42_OFFLINE_IMAGE_BUILD',['docker','build','--quiet','--provenance=false','--sbom=false','--network','none','--pull=false','--platform','linux/amd64',
   '--build-arg','CORE_BASE_IMAGE='+base_alias,'--label','com.megmeet.runtime42.manifest='+p.manifest_sha,
   '--tag',v['tag'],'--file',str(p.root/v['dockerfile']),str(p.root/v['context'])],600)
  info=image(p,v['tag'])
 verify_candidate(p,info['id'])
 record={'schema':'r42-worker-candidate-v1','manifest_sha256':p.manifest_sha,'image_id':info['id'],'runtime':v['runtime']}
 p.write_new(p.root/'worker-candidate.json',(json.dumps(record,sort_keys=True,separators=(',',':'))+'\n').encode())
 print(json.dumps({'status':'R42_OFFLINE_RUNTIME_VERIFIED','runtime':v['runtime'],'image_id':info['id'],'production_services_changed':False}),flush=True)
 return info['id']
