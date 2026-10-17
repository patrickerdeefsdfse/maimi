"""One-generation R45 proof. Reads public evidence; never runs ancestor code."""
from copy import deepcopy
from pathlib import Path, PurePosixPath
import hashlib, json, re

ROOT=PurePosixPath('/data/megmeet-logistics-r45-20261009')
MODE='r45-logistics'; RELEASE='20261009-r45-logistics'
CONTROL='/opt/personal-cloud/runtime-r41'
TARGETS={
 '/app/app/enterprise_connectors.py':'public/app/enterprise_connectors.py',
 '/app/app/logistics_query.py':'public/app/logistics_query.py',
 '/app/app/kuaidi100_mcp.py':'public/app/kuaidi100_mcp.py',
}
CHANGED={'gateway','personal-cloud-manager'}
class Stop(ValueError):pass
def require(v,c):
 if not v:raise Stop(c)
def sha(v):return hashlib.sha256(v).hexdigest()
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()

def overlay(folder,manifest,image):
 s={n:{'labels':{'com.megmeet.runtime45.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':MODE}.items()},'volumes':[{'type':'bind','source':str(ROOT/'runtime-control'),'target':CONTROL,'read_only':True,'bind':{'create_host_path':False}}]} for n in CHANGED}
 for target,name in TARGETS.items():s['gateway']['volumes'].append({'type':'bind','source':str(ROOT/name),'target':target,'read_only':True,'bind':{'create_host_path':False}})
 s['gateway']['volumes'].append({'type':'bind','source':'/etc/megmeet/logistics/kuaidi100-mcp.json','target':'/run/secrets/maimi-logistics-kuaidi100-mcp.json','read_only':True,'bind':{'create_host_path':False}})
 s['gateway']['environment']={'PERSONAL_LOGIN_ACTIVATION_FILE':CONTROL+'/activation.json','PERSONAL_LOGIN_RELEASE_SHA':manifest,'MAIMI_LOGISTICS_ENABLED':'true','MAIMI_LOGISTICS_MES_USER_CODE':'M00001402','MAIMI_LOGISTICS_KUAIDI100_ENABLED':'false','MAIMI_LOGISTICS_KUAIDI100_MCP_ENABLED':'true','MAIMI_LOGISTICS_KUAIDI100_SINGLE_PROCESS':'true','WEB_CONCURRENCY':'1','UVICORN_WORKERS':'1'}
 s['personal-cloud-manager']['environment']={'PERSONAL_CLOUD_WORKER_IMAGE':image,'PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE':CONTROL+'/activation.json','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA':manifest}
 return {'services':s}

def evidence(read,exists,pins):
 try:return _evidence(read,exists,pins)
 except Stop:raise
 except Exception:raise Stop('R46_DIRECT_R45_PROOF_INVALID') from None

def _evidence(read,exists,pins):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R46_PREDECESSOR_READ_INVALID')
  if path in sampled:require(sampled[path]==raw,'R46_PREDECESSOR_CHANGED')
  sampled[path]=raw;return raw
 expected=pins['receipt'];folder=PurePosixPath(expected['backup_directory'])
 require(folder.parent==ROOT and re.fullmatch(r'\.logistics-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',folder.name),'R46_R45_FOLDER_INVALID')
 forbidden=(ROOT/'.update-incomplete',folder/'rollback-intent.json',folder/'restored-r44.json')
 require(not any(exists(p) for p in forbidden),'R46_PREDECESSOR_TRANSITION_PENDING')
 manifest_raw=take(ROOT/'release-manifest.json');manifest=json.loads(manifest_raw)
 require(sha(manifest_raw)==pins['manifest_sha256'] and manifest['release']==RELEASE,'R46_R45_MANIFEST_CHANGED')
 receipt_raw=take(ROOT/'transition.json')
 require(json.loads(receipt_raw)==expected and take(folder/'applied.json')==receipt_raw,'R46_R45_RECEIPT_CHANGED')
 raw=take(folder/'baseline.json');baseline=json.loads(raw)
 require(sha(raw)==expected['baseline_sha256'] and baseline['worker_image_id']==pins['worker_image'],'R46_R45_BASELINE_CHANGED')
 stage=json.loads(take(folder/'progress.json'));ids=expected['current_service_ids']
 require(stage.get('phase')=='FINISHED' and stage.get('target')==MODE and stage.get('creating') is None and stage.get('created')=={MODE:{s:ids[s] for s in CHANGED}},'R46_R45_PROGRESS_CHANGED')
 activation=encoded({'schema':'megmeet-runtime-activation-r41-v1','runtime':'0.2.0-rc.2','release_manifest_sha256':pins['manifest_sha256'],'image_id':pins['worker_image'],'status':'ACTIVE'})
 require(take(ROOT/'runtime-control/activation.json')==activation,'R46_R45_ACTIVATION_CHANGED')
 prepared=take(folder/'prepared.json')
 require(take(folder/'runtime-activation-intent.json')==encoded({'schema':'r41-runtime-activation-intent-v1','prepared_sha256':sha(prepared),'activation_sha256':sha(activation)}),'R46_R45_ACTIVATION_INTENT_CHANGED')
 spec=overlay(folder,pins['manifest_sha256'],pins['worker_image']);overlay_path=folder/'runtime-r45-logistics.compose.json'
 require(json.loads(take(overlay_path))==spec,'R46_R45_COMPOSE_CHANGED')
 items=deepcopy(baseline['items']);policies=deepcopy(baseline['policies']);sources=deepcopy(baseline['source_pins'])
 require({s:v['id'] for s,v in items.items()}==expected['original_service_ids'],'R46_R45_ORIGINAL_IDENTITIES_CHANGED')
 for service in CHANGED:
  items[service].update(id=ids[service],running=True,runtime45={'release':RELEASE,'transaction':folder.name,'mode':MODE})
  mounts={r['destination']:r for r in policies[service]['mounts']}
  for row in spec['services'][service]['volumes']:mounts[row['target']]={'type':'bind','source':row['source'],'destination':row['target'],'rw':False}
  policies[service]['mounts']=sorted(mounts.values(),key=lambda r:r['destination']);policies[service]['environment'].update(spec['services'][service]['environment'])
 for target,name in TARGETS.items():
  raw=take(ROOT/name);require(manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R46_R45_SOURCE_CHANGED');sources['gateway'][target]=sha(raw)
 require({s:v['id'] for s,v in items.items()}==ids,'R46_R45_CURRENT_IDENTITIES_CHANGED')
 command=[*baseline['command'],'-f',str(overlay_path)]
 require(command[:2]==['docker','compose'] and all(isinstance(a,str) for a in command),'R46_COMPOSE_COMMAND_INVALID')
 compose_files={}
 for i,arg in enumerate(command[:-1]):
  if arg=='-f':
   p=PurePosixPath(command[i+1]);require(p.is_absolute() and p.name!='.env' and '..' not in p.parts and p.suffix in {'.json','.yaml','.yml'},'R46_COMPOSE_PATH_INVALID');compose_files[str(p)]=sha(take(p))
 require(compose_files,'R46_COMPOSE_FILES_MISSING')
 for path,before in sampled.items():require(read(path)==before,'R46_PREDECESSOR_CHANGED_DURING_READ')
 require(not any(exists(p) for p in forbidden),'R46_PREDECESSOR_CHANGED_DURING_READ')
 return {'ids':deepcopy(ids),'items':items,'policies':policies,'source_pins':sources,'command':command,'compose_files':compose_files,'fingerprint':sha(encoded({str(p):sha(v) for p,v in sampled.items()})),'baseline':{'nginx':deepcopy(baseline['nginx'])}}

def live(proof,current,policy,source_pins,nginx):
 require(current==proof['items'],'R46_LIVE_R45_IDENTITIES_CHANGED')
 for service,item in current.items():
  require(policy(item['id'])==proof['policies'][service],'R46_LIVE_R45_POLICY_CHANGED')
  if service!='db-migrate':require(item['running'] is True,'R46_R45_SERVICE_NOT_READY')
 for service,pins in proof['source_pins'].items():source_pins(service,current[service]['id'],pins)
 require(nginx()==proof['baseline']['nginx'],'R46_NGINX_CHANGED')
