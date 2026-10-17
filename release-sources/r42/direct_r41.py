"""Direct committed R41 + desktop 0.13.0 proof; never constructs old Products."""
from copy import deepcopy
import hashlib,json,re
from pathlib import PurePosixPath,Path

ROOT=PurePosixPath('/data/megmeet-dsh020-r41-20261008')
TRANSACTION='.dsh020-switch-20261008T054217Z-85a82b2e'
RELEASE='20261008-r41-dsh020';MODE='r41-dsh020'
FINGERPRINT='bf63492309a923d0280951ff43c94470d14e285803ec7d1928601d80686f5432'
DOWNLOAD_ROOT=PurePosixPath('/data/megmeet-desktop-distribution-0.13.0-r41-20261008')
DOWNLOAD_TRANSACTION='.publish-20261008T060436Z-e7ff2700'
DOWNLOAD_REVISION='desktop-download-0.13.0-r41-20261008'
CHANGED=frozenset(('gateway','personal-cloud-manager'))
HEX=re.compile('[a-f0-9]{64}')
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def require(ok,code):
 if not ok:raise Stop(code)

def mounts(policy):
 rows=policy['mounts']
 require(isinstance(rows,list) and all(isinstance(v,dict) and set(v)=={'type','source','destination','rw'} and isinstance(v['destination'],str) and type(v['rw']) is bool for v in rows),'R42_BASELINE_MOUNTS_INVALID')
 require(len({v['destination'] for v in rows})==len(rows),'R42_BASELINE_DUPLICATE_MOUNTS')
 return {v['destination']:v for v in rows}

def project_policies(baseline,pins):
 policies=deepcopy(baseline['policies'])
 require(set(policies)==set(pins['IDS']),'R42_BASELINE_SERVICES_INVALID')
 control='/opt/personal-cloud/runtime-r41'
 for service in CHANGED:
  p=policies[service];m=mounts(p)
  for path,spec in pins['TARGETS'].items():
   if ('personal-cloud-manager' if path.startswith('/opt/') else 'gateway')==service:
    m[path]={'type':'bind','source':str(ROOT/spec['file']),'destination':path,'rw':False}
  m[control]={'type':'bind','source':str(ROOT/'runtime-control'),'destination':control,'rw':False}
  env=p['environment']
  require('TMPDIR' not in env,'R42_UNEXPECTED_BASELINE_TMPDIR')
  if service=='personal-cloud-manager':
   # Match the R41 allowlisted projection, not every opaque environment field.
   env.update({k:'' for k in pins['EMPTY_OVERLAYS']})
   env.update(PERSONAL_CLOUD_IDLE_SECONDS='0',PERSONAL_CLOUD_WORKER_IMAGE=pins['WORKER_IMAGE'],PERSONAL_CLOUD_WORKER_RUNTIME='0.2.0-rc.2',PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE=control+'/activation.json',PERSONAL_CLOUD_RUNTIME_RELEASE_SHA=pins['MANIFEST'])
  else:
   path='/etc/tonggong/ssd-alerts.json';m[path]={'type':'bind','source':path,'destination':path,'rw':False}
   env.update(PERSONAL_LOGIN_ACTIVATION_FILE=control+'/activation.json',PERSONAL_LOGIN_RELEASE_SHA=pins['MANIFEST'])
  p['mounts']=sorted(m.values(),key=lambda v:v['destination'])
 m=mounts(policies['downloads'])
 for target,relative in (('/opt/distribution/app.py','app.py'),('/srv/megmeet-distribution','site')):
  require(target in m and m[target]['type']=='bind' and not m[target]['rw'],'R42_DOWNLOAD_BOUNDARY_CHANGED')
  m[target]['source']=str(DOWNLOAD_ROOT/'desktop-distribution'/relative)
 policies['downloads']['mounts']=sorted(m.values(),key=lambda v:v['destination'])
 return policies

def project_items(baseline,pins):
 items=deepcopy(baseline['items'])
 for service,item in items.items():
  item['id']=pins['IDS'][service]
  if service!='db-migrate':item['running']=True
  if service in CHANGED:
   item['runtime41']={'release':RELEASE,'transaction':TRANSACTION,'mode':MODE}
   item['runtime42']={'release':None,'transaction':None,'mode':None}
 return items

def evidence(read,exists,pins,core):
 try:return _evidence(read,exists,pins,core)
 except Stop:raise
 except Exception:raise Stop('R42_DIRECT_R41_PROOF_INVALID') from None

def _evidence(read,exists,pins,core):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R42_PROOF_READ_INVALID')
  if path in sampled:require(sampled[path]==raw,'R42_PROOF_CHANGED_DURING_READ')
  sampled[path]=raw;return raw
 proof=core.evidence(take,exists,pins['MANIFEST'])
 require(proof['fingerprint']==FINGERPRINT and proof['folder']==ROOT/TRANSACTION,'R42_CURRENT_R41_PROOF_CHANGED')
 require(proof['baseline']['worker_image_id']==pins['WORKER_IMAGE'],'R42_CURRENT_WORKER_IMAGE_CHANGED')
 require({s:v for s,v in proof['ids'].items() if s!='downloads'}=={s:v for s,v in pins['IDS'].items() if s!='downloads'},'R42_CURRENT_IDS_CHANGED')
 b=proof['baseline'];command=list(b['command'])
 require(command[:2]==['docker','compose'] or command[:4]==['docker','--host','unix:///var/run/docker.sock','compose'],'R42_COMPOSE_COMMAND_INVALID')
 paths={command[i+1] for i,a in enumerate(command[:-1]) if a=='-f'}
 require(set(b['compose_files'])==paths,'R42_COMPOSE_SET_CHANGED')
 for name,digest in b['compose_files'].items():require(sha(take(PurePosixPath(name)))==digest,'R42_COMPOSE_CHANGED')
 for name,digest in b['original_sources'].items():require(sha(take(PurePosixPath(name)))==digest,'R42_INHERITED_SOURCE_CHANGED')
 # The current R41 overlay is tied to the immutable prepared record and manifest.
 overlay_path=proof['folder']/('runtime-'+MODE+'.compose.json')
 overlay=json.loads(take(overlay_path));services=overlay.get('services',{})
 require(set(services)==CHANGED,'R42_CURRENT_OVERLAY_SERVICES_CHANGED')
 for service in CHANGED:
  spec=services[service]
  expected_labels={'com.megmeet.runtime41.'+k:v for k,v in {'release':RELEASE,'transaction':TRANSACTION,'mode':MODE}.items()}
  require(spec.get('labels')==expected_labels,'R42_CURRENT_OVERLAY_LABELS_CHANGED')
  targets={v['target']:v for v in spec.get('volumes',[])}
  expected={path:{'type':'bind','source':str(ROOT/entry['file']),'target':path,'read_only':True,'bind':{'create_host_path':False}} for path,entry in pins['TARGETS'].items() if ('personal-cloud-manager' if path.startswith('/opt/') else 'gateway')==service}
  control='/opt/personal-cloud/runtime-r41';expected[control]={'type':'bind','source':str(ROOT/'runtime-control'),'target':control,'read_only':True,'bind':{'create_host_path':False}}
  if service=='gateway':
   path='/etc/tonggong/ssd-alerts.json';expected[path]={'type':'bind','source':path,'target':path,'read_only':True,'bind':{'create_host_path':False}}
  require(len(targets)==len(spec['volumes']) and targets==expected,'R42_CURRENT_OVERLAY_MOUNTS_CHANGED')
  projected=project_policies(b,pins)[service]['environment']
  expected_env={k:v for k,v in projected.items() if k in {'PERSONAL_CLOUD_IDLE_SECONDS','PERSONAL_CLOUD_WORKER_IMAGE','PERSONAL_CLOUD_WORKER_RUNTIME','PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA'}|set(pins['EMPTY_OVERLAYS'])} if service=='personal-cloud-manager' else {k:v for k,v in projected.items() if k in {'PERSONAL_LOGIN_ACTIVATION_FILE','PERSONAL_LOGIN_RELEASE_SHA'}}
  require(set(spec)=={'labels','volumes','environment'} and spec['environment']==expected_env,'R42_CURRENT_OVERLAY_ENV_CHANGED')
 command+=['-f',str(overlay_path)]
 folder=DOWNLOAD_ROOT/DOWNLOAD_TRANSACTION
 forbidden=(DOWNLOAD_ROOT/'.publication-incomplete',DOWNLOAD_ROOT/'.extraction-incomplete',folder/'rolled-back.json')
 require(not any(exists(p) for p in forbidden),'R42_DESKTOP_PUBLICATION_INCOMPLETE')
 desktop_raw=take(DOWNLOAD_ROOT/'transition.json');desktop=json.loads(desktop_raw)
 dp={'revision':DOWNLOAD_REVISION,'status':'PREPARED','backup_directory':str(folder),'pins':pins['DOWNLOAD_PINS'],'previous_downloads_id':proof['ids']['downloads'],'cloud_fingerprint':FINGERPRINT}
 require(json.loads(take(folder/'prepared.json'))==dp,'R42_DESKTOP_PREPARED_CHANGED')
 require(take(folder/'r41-transition.json')==proof['raw'],'R42_DESKTOP_CLOUD_SNAPSHOT_CHANGED')
 expected={**dp,'status':'APPLIED','downloads_id':pins['IDS']['downloads'],'version':'0.13.0','only_downloads_changed':True,'nginx_configuration_changed':False,'tls_configuration_changed':False,'employee_acceptance_pending':True}
 require(desktop==expected and take(folder/'applied.json')==desktop_raw,'R42_DESKTOP_RECEIPT_CHANGED')
 stage=json.loads(take(folder/'progress.json'))
 require(stage.get('phase')=='FINISHED' and stage.get('new_id')==pins['IDS']['downloads'] and stage.get('downloads_id')==pins['IDS']['downloads'] and stage.get('rollback') is False and not stage.get('restored_id'),'R42_DESKTOP_JOURNAL_CHANGED')
 distribution=DOWNLOAD_ROOT/'desktop-distribution';raw=take(distribution/'distribution-manifest.json')
 require(sha(raw)==pins['DOWNLOAD_PINS']['manifest'],'R42_DESKTOP_MANIFEST_CHANGED')
 descriptor=json.loads(raw)
 for name in ('app.py','compose.yaml','compose.nginx-network.yaml','site/release.json'):
  raw=take(distribution/name)
  require(descriptor['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R42_DESKTOP_DESCRIPTOR_CHANGED')
 require(json.loads(sampled[distribution/'site/release.json'])==pins['DOWNLOAD_PINS']['release'],'R42_DESKTOP_RELEASE_CHANGED')
 for path,raw in list(sampled.items()):require(read(path)==raw,'R42_PROOF_CHANGED_DURING_READ')
 require(not any(exists(p) for p in forbidden) and not exists(ROOT/'.update-incomplete'),'R42_PROOF_CHANGED_DURING_READ')
 return {**proof,'folder':Path(str(proof['folder'])),'fingerprint':sha(encoded({str(k):sha(v) for k,v in sampled.items()})),
  'cloud_fingerprint':FINGERPRINT,'ids':deepcopy(pins['IDS']),'items':project_items(b,pins),'policies':project_policies(b,pins),'command':command,
  'desktop_raw':desktop_raw,'desktop_receipt':desktop,'desktop_folder':Path(str(folder))}
