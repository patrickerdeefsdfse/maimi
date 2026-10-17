"""Pure current R43 receipt proof; no predecessor Product/check execution."""
from copy import deepcopy
from pathlib import PurePosixPath,Path
import hashlib,json,re

ROOT=PurePosixPath('/data/megmeet-card-storage-r43-20261008')
RELEASE='20261008-r43-card-storage';MODE='r43-card-storage'
TARGET='/app/app/project_platform.py';PUBLIC='public/app/project_platform.py'
FOLDER=re.compile(r'\.card-storage-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')
class Stop(ValueError):pass
def require(v,c):
 if not v:raise Stop(c)
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def evidence(read,exists,pins,r42,r42pins,core):
 try:return _evidence(read,exists,pins,r42,r42pins,core)
 except Stop:raise
 except Exception:raise Stop('R44_DIRECT_R43_PROOF_INVALID') from None
def _evidence(read,exists,pins,r42,r42pins,core):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R44_PREDECESSOR_READ_INVALID')
  if path in sampled:require(raw==sampled[path],'R44_PREDECESSOR_CHANGED_DURING_READ')
  sampled[path]=raw;return raw
 require(not exists(ROOT/'.update-incomplete'),'R44_R43_MAINTENANCE_PENDING')
 # This is a fixed public-file/receipt proof, not a recursive runtime checker.
 old=r42.evidence(take,exists,r42pins,core)
 manifest_raw=take(ROOT/'release-manifest.json');manifest=json.loads(manifest_raw)
 require(sha(manifest_raw)==pins['manifest_sha256'] and manifest['schema']=='card-storage-r43-package-v1' and manifest['release']==RELEASE and manifest['status']=='FROZEN','R44_R43_MANIFEST_CHANGED')
 for name,record in manifest['files'].items():
  require(re.fullmatch(r'[A-Za-z0-9_./-]+',name) and not name.startswith('/') and '..' not in name.split('/'),'R44_R43_PUBLIC_PATH_INVALID')
  raw=take(ROOT/name);require(record=={'sha256':sha(raw),'size_bytes':len(raw)},'R44_R43_SOURCE_CHANGED')
 raw=take(ROOT/'transition.json');receipt=json.loads(raw)
 require(receipt==pins['receipt'],'R44_R43_COMPANY_RECEIPT_CHANGED')
 folder=PurePosixPath(receipt['backup_directory']);require(folder.parent==ROOT and FOLDER.fullmatch(folder.name),'R44_R43_FOLDER_INVALID')
 baseline_raw=take(folder/'baseline.json');baseline=json.loads(baseline_raw)
 expected={key:deepcopy(old[key]) for key in ('items','policies','command','source_pins','fingerprint')}|{'nginx':deepcopy(old['baseline']['nginx'])}
 require(baseline==expected and sha(baseline_raw)==receipt['baseline_sha256'],'R44_R43_BASELINE_CHANGED')
 prepared={key:receipt[key] for key in ('release','payload_sha256','backup_directory','previous_mode','baseline_sha256','original_service_ids')}
 prepared.update(schema='card-storage-r43-prepared-v1',status='PREPARED')
 require(json.loads(take(folder/'prepared.json'))==prepared and prepared['payload_sha256']==pins['payload_sha256'] and prepared['original_service_ids']==old['ids'],'R44_R43_PREPARED_CHANGED')
 ids=receipt['current_service_ids']
 require(take(folder/'applied.json')==raw and json.loads(take(folder/'progress.json'))=={'phase':'FINISHED','created_gateway_id':ids['gateway']},'R44_R43_APPLIED_PAIR_CHANGED')
 spec={'services':{'gateway':{'labels':{'com.megmeet.card-storage43.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':MODE}.items()},'volumes':[{'type':'bind','source':str(ROOT/PUBLIC),'target':TARGET,'read_only':True,'bind':{'create_host_path':False}}]}}}
 require(json.loads(take(folder/'gateway-r43.compose.json'))==spec,'R44_R43_COMPOSE_CHANGED')
 items=deepcopy(old['items']);items['gateway'].update(id=ids['gateway'],running=True,runtime43={'release':RELEASE,'transaction':folder.name,'mode':MODE})
 require({s:v['id'] for s,v in items.items()}==ids,'R44_R43_IDENTITIES_CHANGED')
 policies=deepcopy(old['policies']);mounts={row['destination']:row for row in policies['gateway']['mounts']}
 require(TARGET in mounts,'R44_R43_BASE_BIND_MISSING')
 mounts[TARGET]={'type':'bind','source':str(ROOT/PUBLIC),'destination':TARGET,'rw':False};policies['gateway']['mounts']=sorted(mounts.values(),key=lambda row:row['destination'])
 source_pins=deepcopy(old['source_pins']);source_pins['gateway'][TARGET]=manifest['files'][PUBLIC]['sha256']
 for path,before in sampled.items():require(read(path)==before,'R44_PREDECESSOR_CHANGED_DURING_READ')
 require(not exists(ROOT/'.update-incomplete'),'R44_PREDECESSOR_CHANGED_DURING_READ')
 return {'ids':deepcopy(ids),'items':items,'policies':policies,'source_pins':source_pins,'command':[*old['command'],'-f',str(folder/'gateway-r43.compose.json')],'fingerprint':sha(encoded({str(path):sha(value) for path,value in sampled.items()})),'baseline':{'nginx':deepcopy(baseline['nginx'])},'folder':Path(str(folder))}
def live(proof,current,policy,source_pins,nginx):
 require(current==proof['items'],'R44_LIVE_R43_IDENTITIES_CHANGED')
 for service,item in current.items():
  require(policy(item['id'])==proof['policies'][service],'R44_LIVE_R43_POLICY_CHANGED')
  if service!='db-migrate':require(item['running'] is True,'R44_PREDECESSOR_NOT_READY')
 for service,pins in proof['source_pins'].items():require(source_pins(service,current[service]['id'],pins)==pins,'R44_LIVE_R43_SOURCE_CHANGED')
 require(nginx()==proof['baseline']['nginx'],'R44_PREDECESSOR_NGINX_CHANGED')
