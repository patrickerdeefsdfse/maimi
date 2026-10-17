"""Read the committed R48 receipt and captured baseline; no ancestor execution."""
from copy import deepcopy
import hashlib,json,re
from pathlib import PurePosixPath
ROOT=PurePosixPath('/data/megmeet-skills-logistics-r48-20261012')
RELEASE='20261012-r48-skills-logistics';MODE='r48-skills-logistics'
CHANGED=('gateway','personal-cloud-manager')
SERVICES={'db-migrate','downloads','gateway','office-service','personal-cloud-manager','postgres','redis','retention-worker'}
TARGETS={'gateway':{'/app/app/logistics_query.py':'public/app/logistics_query.py','/app/app/enterprise_connectors.py':'public/app/enterprise_connectors.py','/app/app/personal_cloud_plugin_static.py':'public/app/personal_cloud_plugin_static.py'},'personal-cloud-manager':{'/opt/personal-cloud/manager.py':'public/manager/manager.py'}}
NATIVE=str(ROOT/'public/native')
class Stop(ValueError):pass
def require(value,code):
 if not value:raise Stop(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def overlay(folder):
 services={}
 for service in CHANGED:
  row={'labels':{'com.megmeet.skills-logistics48.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':MODE}.items()},'volumes':[{'type':'bind','source':str(ROOT/name),'target':target,'read_only':True,'bind':{'create_host_path':False}} for target,name in TARGETS[service].items()]}
  if service=='personal-cloud-manager':
   row['environment']={'PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR':NATIVE}
   row['volumes'].append({'type':'bind','source':NATIVE,'target':NATIVE,'read_only':True,'bind':{'create_host_path':False}})
  services[service]=row
 return {'services':services}
def evidence(read,exists,pins):
 try:return _evidence(read,exists,pins)
 except Stop:raise
 except Exception:raise Stop('R52_DIRECT_R48_PROOF_INVALID') from None
def _evidence(read,exists,pins):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R52_PREDECESSOR_READ_INVALID')
  if path in sampled:require(sampled[path]==raw,'R52_PREDECESSOR_CHANGED')
  sampled[path]=raw;return raw
 expected=pins['receipt'];folder=PurePosixPath(expected['backup_directory'])
 require(folder.parent==ROOT and re.fullmatch(r'\.skills-logistics-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',folder.name),'R52_R48_FOLDER_INVALID')
 require(not exists(ROOT/'.update-incomplete'),'R52_PREDECESSOR_TRANSITION_PENDING')
 raw=take(ROOT/'release-manifest.json');manifest=json.loads(raw)
 require(sha(raw)==pins['manifest_sha256'] and manifest['release']==RELEASE and manifest['publication_status']=='READY','R52_R48_MANIFEST_CHANGED')
 raw=take(ROOT/'transition.json')
 require(json.loads(raw)==expected and take(folder/'applied.json')==raw,'R52_R48_RECEIPT_CHANGED')
 require(expected['status']=='APPLIED' and expected['effective_mode']==MODE and expected['payload_sha256']==pins['payload_sha256'],'R52_R48_RECEIPT_INVALID')
 raw=take(folder/'baseline.json');baseline=json.loads(raw)
 require(sha(raw)==expected['baseline_sha256'],'R52_R48_BASELINE_CHANGED')
 original=expected['original_service_ids'];ids=expected['current_service_ids']
 require(set(ids)==set(original)==SERVICES and len(set(ids.values()))==len(SERVICES) and all(ids[s]==original[s] for s in SERVICES-set(CHANGED)),'R52_R48_IDENTITIES_INVALID')
 require(json.loads(take(folder/'progress.json'))=={'phase':'FINISHED','created':{s:ids[s] for s in CHANGED},'creating':None},'R52_R48_PROGRESS_CHANGED')
 prepared={k:v for k,v in expected.items() if k in ('release','payload_sha256','backup_directory','previous_mode','baseline_sha256','original_service_ids')};prepared.update(schema='skills-logistics-r48-prepared-v1',status='PREPARED')
 require(json.loads(take(folder/'prepared.json'))==prepared,'R52_R48_PREPARED_CHANGED')
 overlay_path=folder/'skills-logistics-r48.compose.json'
 require(json.loads(take(overlay_path))==overlay(folder),'R52_R48_COMPOSE_CHANGED')
 items=deepcopy(baseline['items']);policies=deepcopy(baseline['policies']);sources=deepcopy(baseline['source_pins'])
 require({s:v['id'] for s,v in items.items()}==original,'R52_R48_ORIGINAL_IDENTITIES_CHANGED')
 for service in CHANGED:
  items[service].update(id=ids[service],running=True,runtime48={'release':RELEASE,'transaction':folder.name,'mode':MODE})
  mounts={row['destination']:row for row in policies[service]['mounts']}
  for target,name in TARGETS[service].items():
   require(target in mounts,'R52_R48_SOURCE_BIND_MISSING')
   mounts[target]={'type':'bind','source':str(ROOT/name),'destination':target,'rw':False}
   raw=take(ROOT/name);require(manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R52_R48_SOURCE_CHANGED');sources[service][target]=sha(raw)
  if service=='personal-cloud-manager':
   mounts[NATIVE]={'type':'bind','source':NATIVE,'destination':NATIVE,'rw':False}
   policies[service]['environment'].update(PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR=NATIVE)
   for filename in ('cloud-brand-client.js','logistics-index.mjs','logistics-SKILL.md','runtime020-source-pins.json'):
    name='public/native/'+filename;raw=take(ROOT/name)
    require(manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R52_R48_NATIVE_SOURCE_CHANGED');sources[service][NATIVE+'/'+filename]=sha(raw)
  policies[service]['mounts']=sorted(mounts.values(),key=lambda row:row['destination'])
 command=baseline['command']+['-f',str(overlay_path)]
 require((command[:2]==['docker','compose'] or command[:4]==['docker','--host','unix:///var/run/docker.sock','compose']) and all(isinstance(v,str) for v in command),'R52_COMPOSE_COMMAND_INVALID')
 compose_files={}
 for i,arg in enumerate(command[:-1]):
  if arg=='-f':
   path=PurePosixPath(command[i+1]);require(path.is_absolute() and '..' not in path.parts and path.suffix in ('.json','.yml','.yaml') and path.name!='.env','R52_COMPOSE_PATH_INVALID')
   digest=sha(take(path));compose_files[str(path)]=digest
   if path!=overlay_path:require(baseline['compose_files'].get(str(path))==digest,'R52_BASE_COMPOSE_CHANGED')
 require(set(compose_files)==set(baseline['compose_files'])|{str(overlay_path)},'R52_COMPOSE_FILES_CHANGED')
 activation=PurePosixPath('/data/megmeet-logistics-r45-20261009/runtime-control/activation.json')
 require(json.loads(take(activation))==pins['activation'],'R52_RUNTIME_ACTIVATION_CHANGED')
 for service in CHANGED:
  key='PERSONAL_LOGIN_RELEASE_SHA' if service=='gateway' else 'PERSONAL_CLOUD_RUNTIME_RELEASE_SHA'
  require(policies[service]['environment'][key]==pins['activation']['release_manifest_sha256'],'R52_RUNTIME_POLICY_CHANGED')
 require(policies['personal-cloud-manager']['environment']['PERSONAL_CLOUD_WORKER_IMAGE']==pins['activation']['image_id'],'R52_WORKER_IMAGE_CHANGED')
 for path,before in sampled.items():require(read(path)==before,'R52_PREDECESSOR_CHANGED_DURING_READ')
 require(not exists(ROOT/'.update-incomplete'),'R52_PREDECESSOR_CHANGED_DURING_READ')
 return {'ids':ids,'items':items,'policies':policies,'source_pins':sources,'command':command,'compose_files':compose_files,'nginx':deepcopy(baseline['nginx']),'fingerprint':sha(encoded({str(p):sha(v) for p,v in sampled.items()}))}
def live(proof,current,policy,source_pins,nginx):
 require(current==proof['items'],'R52_LIVE_R48_IDENTITIES_CHANGED')
 for service,item in current.items():
  require(policy(item['id'])==proof['policies'][service],'R52_LIVE_R48_POLICY_CHANGED')
  if service!='db-migrate':require(item['running'] is True,'R52_R48_SERVICE_NOT_READY')
 for service,values in proof['source_pins'].items():source_pins(service,current[service]['id'],values)
 require(nginx()==proof['nginx'],'R52_NGINX_CHANGED')
