"""Read the committed R46 receipt and its captured baseline; no ancestor code."""
from copy import deepcopy
import hashlib,json,re
from pathlib import PurePosixPath
ROOT=PurePosixPath('/data/megmeet-logistics-phone-r46-20261009')
RELEASE='20261009-r46-logistics-phone';MODE='r46-logistics-phone'
SERVICES={'db-migrate','downloads','gateway','office-service','personal-cloud-manager','postgres','redis','retention-worker'}
class Stop(ValueError):pass
def require(v,c):
 if not v:raise Stop(c)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def overlay(folder,payload):
 return {'services':{'gateway':{'labels':{'com.megmeet.logistics-phone46.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':MODE}.items()},'volumes':[{'type':'bind','source':str(ROOT/'public/app/logistics_query.py'),'target':'/app/app/logistics_query.py','read_only':True,'bind':{'create_host_path':False}}]}}}
def evidence(read,exists,pins):
 try:return _evidence(read,exists,pins)
 except Stop:raise
 except Exception:raise Stop('R47_DIRECT_R46_PROOF_INVALID') from None
def _evidence(read,exists,pins):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R47_PREDECESSOR_READ_INVALID')
  if path in sampled:require(sampled[path]==raw,'R47_PREDECESSOR_CHANGED')
  sampled[path]=raw;return raw
 expected=pins['receipt'];folder=PurePosixPath(expected['backup_directory'])
 require(folder.parent==ROOT and re.fullmatch(r'\.logistics-phone-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',folder.name),'R47_R46_FOLDER_INVALID')
 require(not exists(ROOT/'.update-incomplete'),'R47_PREDECESSOR_TRANSITION_PENDING')
 manifest_raw=take(ROOT/'release-manifest.json');manifest=json.loads(manifest_raw)
 require(sha(manifest_raw)==pins['manifest_sha256'] and manifest['release']==RELEASE,'R47_R46_MANIFEST_CHANGED')
 raw=take(ROOT/'transition.json')
 require(json.loads(raw)==expected and take(folder/'applied.json')==raw,'R47_R46_RECEIPT_CHANGED')
 require(expected['status']=='APPLIED' and expected['effective_mode']==MODE and expected['payload_sha256']==pins['payload_sha256'],'R47_R46_RECEIPT_INVALID')
 raw=take(folder/'baseline.json');baseline=json.loads(raw)
 require(sha(raw)==expected['baseline_sha256'],'R47_R46_BASELINE_CHANGED')
 original=expected['original_service_ids'];ids=expected['current_service_ids']
 require(set(ids)==set(original)==SERVICES and all(ids[s]==original[s] for s in SERVICES-{'gateway'}),'R47_R46_IDENTITIES_INVALID')
 require(json.loads(take(folder/'progress.json'))=={'phase':'FINISHED','created_gateway_id':ids['gateway']},'R47_R46_PROGRESS_CHANGED')
 prepared={k:v for k,v in expected.items() if k in ('release','payload_sha256','backup_directory','previous_mode','baseline_sha256','original_service_ids')}
 prepared.update(schema='logistics-phone-r46-prepared-v1',status='PREPARED')
 require(json.loads(take(folder/'prepared.json'))==prepared,'R47_R46_PREPARED_CHANGED')
 spec=overlay(folder,pins['payload_sha256']);overlay_path=folder/'gateway-r46.compose.json'
 require(json.loads(take(overlay_path))==spec,'R47_R46_COMPOSE_CHANGED')
 items=deepcopy(baseline['items']);policies=deepcopy(baseline['policies']);sources=deepcopy(baseline['source_pins'])
 require({s:v['id'] for s,v in items.items()}==original,'R47_R46_ORIGINAL_IDENTITIES_CHANGED')
 items['gateway'].update(id=ids['gateway'],running=True,runtime46={'release':RELEASE,'transaction':folder.name,'mode':MODE})
 mounts={r['destination']:r for r in policies['gateway']['mounts']}
 target='/app/app/logistics_query.py';name='public/app/logistics_query.py'
 require(target in mounts,'R47_R46_SOURCE_BIND_MISSING')
 mounts[target]={'type':'bind','source':str(ROOT/name),'destination':target,'rw':False}
 policies['gateway']['mounts']=sorted(mounts.values(),key=lambda r:r['destination'])
 raw=take(ROOT/name)
 require(manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R47_R46_SOURCE_CHANGED')
 sources['gateway'][target]=sha(raw)
 command=baseline['command']+['-f',str(overlay_path)]
 require((command[:2]==['docker','compose'] or command[:4]==['docker','--host','unix:///var/run/docker.sock','compose']) and all(isinstance(v,str) for v in command),'R47_COMPOSE_COMMAND_INVALID')
 compose_files={}
 for i,arg in enumerate(command[:-1]):
  if arg=='-f':
   path=PurePosixPath(command[i+1]);require(path.is_absolute() and '..' not in path.parts and path.suffix in ('.json','.yml','.yaml') and path.name!='.env','R47_COMPOSE_PATH_INVALID')
   digest=sha(take(path));compose_files[str(path)]=digest
   if path!=overlay_path:require(baseline['compose_files'].get(str(path))==digest,'R47_BASE_COMPOSE_CHANGED')
 require(set(compose_files)==set(baseline['compose_files'])|{str(overlay_path)},'R47_COMPOSE_FILES_CHANGED')
 # Activation remains the R45 control contract; inspect its fixed active marker,
 # not the old release controller or a recursively replayed health check.
 activation_path=PurePosixPath('/data/megmeet-logistics-r45-20261009/runtime-control/activation.json')
 require(json.loads(take(activation_path))==pins['activation'],'R47_RUNTIME_ACTIVATION_CHANGED')
 for service in ('gateway','personal-cloud-manager'):
  key='PERSONAL_LOGIN_RELEASE_SHA' if service=='gateway' else 'PERSONAL_CLOUD_RUNTIME_RELEASE_SHA'
  require(policies[service]['environment'][key]==pins['activation']['release_manifest_sha256'],'R47_RUNTIME_POLICY_CHANGED')
 require(policies['personal-cloud-manager']['environment']['PERSONAL_CLOUD_WORKER_IMAGE']==pins['activation']['image_id'],'R47_WORKER_IMAGE_CHANGED')
 for path,before in sampled.items():require(read(path)==before,'R47_PREDECESSOR_CHANGED_DURING_READ')
 require(not exists(ROOT/'.update-incomplete'),'R47_PREDECESSOR_CHANGED_DURING_READ')
 return {'ids':ids,'items':items,'policies':policies,'source_pins':sources,'command':command,'compose_files':compose_files,'nginx':deepcopy(baseline['nginx']),'fingerprint':sha(encoded({str(p):sha(v) for p,v in sampled.items()}))}
def live(proof,current,policy,source_pins,nginx):
 require(current==proof['items'],'R47_LIVE_R46_IDENTITIES_CHANGED')
 for service,item in current.items():
  require(policy(item['id'])==proof['policies'][service],'R47_LIVE_R46_POLICY_CHANGED')
  if service!='db-migrate':require(item['running'] is True,'R47_R46_SERVICE_NOT_READY')
 for service,values in proof['source_pins'].items():source_pins(service,current[service]['id'],values)
 require(nginx()==proof['nginx'],'R47_NGINX_CHANGED')
