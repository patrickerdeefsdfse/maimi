"""Read only the committed R42 predecessor; no ancestor Product or controller."""
from copy import deepcopy
import hashlib,json,re
from pathlib import Path,PurePosixPath

ROOT=PurePosixPath('/data/megmeet-skills-cards-r42-20261008')
MODE='r42-skills-cards';RELEASE='20261008-r42-skills-cards'
CHANGED=frozenset(('gateway','personal-cloud-manager'))
CONTROL='/opt/personal-cloud/runtime-r41'
CONFIG='/etc/tonggong/ssd-alerts.json'
TEMP_HOST='/data/megmeet-project-temp';TEMP_TARGET='/var/tmp/megmeet-projects'
EMPTY=('PERSONAL_CLOUD_WORKER_OVERLAY_DIR','PERSONAL_CLOUD_HISTORY_OVERLAY_DIR','PERSONAL_CLOUD_WORKFLOW_OVERLAY_DIR',
 'PERSONAL_CLOUD_RUNTIME_FOLLOWUP_OVERLAY_DIR','PERSONAL_CLOUD_CANARY_WORKER_IMAGE','PERSONAL_CLOUD_CANARY_OWNERS','PERSONAL_CLOUD_CANARY_OVERLAY_DIR')
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def require(value,code):
 if not value:raise Stop(code)
def mount_map(policy):
 rows=policy['mounts']
 require(isinstance(rows,list) and all(set(row)=={'type','source','destination','rw'} and type(row['rw']) is bool for row in rows),'R43_PREDECESSOR_MOUNTS_INVALID')
 mapped={row['destination']:row for row in rows};require(len(mapped)==len(rows),'R43_PREDECESSOR_DUPLICATE_MOUNTS');return mapped
def runtime_environment(pins):
 return {**{name:'' for name in EMPTY},'PERSONAL_CLOUD_IDLE_SECONDS':'0','PERSONAL_CLOUD_WORKER_IMAGE':pins['worker_image'],
  'PERSONAL_CLOUD_WORKER_RUNTIME':'0.2.0-rc.2','PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE':CONTROL+'/activation.json','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA':pins['manifest_sha256']}
def login_environment(pins):return {'PERSONAL_LOGIN_ACTIVATION_FILE':CONTROL+'/activation.json','PERSONAL_LOGIN_RELEASE_SHA':pins['manifest_sha256'],'TMPDIR':TEMP_TARGET}
def tmpfs(baseline):
 value=deepcopy(baseline['policies']['gateway']['tmpfs']);parts=value['/tmp'].split(',')
 require(sum(part.startswith('size=') for part in parts)==1 and all(re.fullmatch(r'[A-Za-z0-9_.=-]+',part) for part in parts),'R43_PREDECESSOR_TMPFS_INVALID')
 value['/tmp']=','.join('size=268435456' if part.startswith('size=') else part for part in parts);return value
def overlay(pins):
 services={name:{'labels':{'com.megmeet.runtime42.'+k:v for k,v in {'release':RELEASE,'transaction':pins['transaction'],'mode':MODE}.items()},'volumes':[]} for name in CHANGED}
 for target,name in pins['targets'].items():
  services['gateway']['volumes'].append({'type':'bind','source':str(ROOT/name),'target':target,'read_only':True,'bind':{'create_host_path':False}})
 for service in CHANGED:services[service]['volumes'].append({'type':'bind','source':str(ROOT/'runtime-control'),'target':CONTROL,'read_only':True,'bind':{'create_host_path':False}})
 services['gateway']['volumes'].append({'type':'bind','source':CONFIG,'target':CONFIG,'read_only':True,'bind':{'create_host_path':False}})
 services['gateway']['volumes'].append({'type':'bind','source':TEMP_HOST,'target':TEMP_TARGET,'read_only':False,'bind':{'create_host_path':False}})
 services['gateway']['environment']=login_environment(pins);services['personal-cloud-manager']['environment']=runtime_environment(pins)
 return {'services':services}
def policies(baseline,pins):
 result=deepcopy(baseline['policies'])
 for service,spec in overlay(pins)['services'].items():
  mounts=mount_map(result[service])
  for value in spec['volumes']:
   mounts[value['target']]={'type':'bind','source':value['source'],'destination':value['target'],'rw':not value['read_only']}
  result[service]['mounts']=sorted(mounts.values(),key=lambda value:value['destination'])
  # All fields emitted here belong to the existing public metadata allowlist.
  result[service]['environment'].update(spec['environment'])
 result['gateway']['tmpfs']=tmpfs(baseline)
 return result
def items(baseline,pins):
 result=deepcopy(baseline['items'])
 for service,item in result.items():
  item['id']=pins['ids'][service]
  if service!='db-migrate':item['running']=True
  if service in CHANGED:item['runtime42']={'release':RELEASE,'transaction':pins['transaction'],'mode':MODE}
 return result
def evidence(read,exists,pins,core):
 try:return _evidence(read,exists,pins,core)
 except Stop:raise
 except Exception:raise Stop('R43_DIRECT_R42_PROOF_INVALID') from None
def _evidence(read,exists,pins,core):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R43_PREDECESSOR_READ_INVALID')
  if path in sampled:require(raw==sampled[path],'R43_PREDECESSOR_CHANGED_DURING_READ')
  sampled[path]=raw;return raw
 proof=core.evidence(take,exists,pins['manifest_sha256']);folder=ROOT/pins['transaction'];baseline=proof['baseline']
 require(PurePosixPath(proof['folder'])==folder and proof['ids']==pins['ids'],'R43_CURRENT_R42_IDS_CHANGED')
 require(proof['prepared']['baseline_sha256']==pins['baseline_sha256'] and baseline['worker_image_id']==pins['worker_image'],'R43_CURRENT_R42_BASELINE_CHANGED')
 manifest=json.loads(take(ROOT/'release-manifest.json'))
 for name,record in pins['files'].items():
  raw=take(ROOT/name)
  require(manifest['files'][name]==record and len(raw)==record['size_bytes'] and sha(raw)==record['sha256'],'R43_CURRENT_R42_SOURCE_CHANGED')
 command=baseline['command']
 require(command[:2]==['docker','compose'] or command[:4]==['docker','--host','unix:///var/run/docker.sock','compose'],'R43_COMPOSE_COMMAND_INVALID')
 require(set(baseline['compose_files'])=={command[i+1] for i,value in enumerate(command[:-1]) if value=='-f'},'R43_BASE_COMPOSE_SET_CHANGED')
 for path,digest in {**baseline['compose_files'],**baseline['original_sources']}.items():require(sha(take(path))==digest,'R43_INHERITED_FILE_CHANGED')
 actual=json.loads(take(folder/'runtime-r42-skills-cards.compose.json'));expected=overlay(pins)
 # Volume order is immaterial; every entry and mount option must match exactly.
 for value in (actual,expected):
  require(set(value)=={'services'} and set(value['services'])==CHANGED,'R43_R42_OVERLAY_SCOPE_CHANGED')
  for service in CHANGED:value['services'][service]['volumes']=sorted(value['services'][service]['volumes'],key=lambda row:row['target'])
 require(actual==expected,'R43_R42_OVERLAY_CHANGED')
 raw=('services:\n  gateway:\n    tmpfs: !override '+json.dumps([target+':'+flags for target,flags in sorted(tmpfs(baseline).items())])+'\n').encode()
 require(take(folder/'gateway-tmpfs-r42.compose.yaml')==raw,'R43_R42_TMPFS_OVERLAY_CHANGED')
 require(not exists(PurePosixPath('/data/megmeet-desktop-distribution-0.14.0-r42-20261008/transition.json')),'R43_DESKTOP0140_ALREADY_PUBLISHED')
 for path,raw in sampled.items():require(read(path)==raw,'R43_PREDECESSOR_CHANGED_DURING_READ')
 require(not exists(ROOT/'.update-incomplete'),'R43_PREDECESSOR_CHANGED_DURING_READ')
 return {**proof,'folder':Path(str(folder)),'fingerprint':sha(encoded({str(path):sha(raw) for path,raw in sampled.items()})),
  'items':items(baseline,pins),'policies':policies(baseline,pins),
  'command':[*command,'-f',str(folder/'runtime-r42-skills-cards.compose.json'),'-f',str(folder/'gateway-tmpfs-r42.compose.yaml')],
  'source_pins':deepcopy(pins['source_pins'])}
def live(proof,current,policy,source_pins,nginx):
 require(current==proof['items'],'R43_LIVE_R42_IDENTITIES_CHANGED')
 for service,item in current.items():
  require(policy(item['id'])==proof['policies'][service],'R43_LIVE_R42_POLICY_CHANGED')
  if service!='db-migrate':require(item['running'] is True,'R43_PREDECESSOR_NOT_READY')
 for service,pins in proof['source_pins'].items():require(source_pins(service,current[service]['id'],pins)==pins,'R43_LIVE_R42_SOURCE_CHANGED')
 require(nginx()==proof['baseline']['nginx'],'R43_PREDECESSOR_NGINX_CHANGED')
 return {'status':'VERIFIED','current_mode':MODE,'desktop_version':'0.13.0','current_service_ids':dict(proof['ids']),
  'live_changes':'NONE','recursive_ancestor_validation':False,'database_business_rows_read':False}
