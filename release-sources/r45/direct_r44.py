"""Direct committed R44 proof, with flat immutable inherited input verification.

No ancestor module is imported or executed. Older public data are read only to
reproduce the exact inherited fingerprint bound by the pinned R44 baseline.
"""
from copy import deepcopy
from pathlib import Path,PurePosixPath
import hashlib,json,re

ROOT=PurePosixPath('/data/megmeet-card-photos-r44-20261009')
R42=PurePosixPath('/data/megmeet-skills-cards-r42-20261008')
R43=PurePosixPath('/data/megmeet-card-storage-r43-20261008')
MODE='r44-card-photos';RELEASE='20261009-r44-card-photos'
CONTROL='/opt/personal-cloud/runtime-r41'
TARGET='/app/maimi_projects/service.py';PUBLIC='public/maimi_projects/service.py'
CHANGED=frozenset(('gateway','personal-cloud-manager'))
SERVICES=frozenset(('gateway','personal-cloud-manager','downloads','postgres','redis','retention-worker','office-service','db-migrate'))
FOLDER=re.compile(r'\.card-photos-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')
HEX=re.compile('[a-f0-9]{64}')
class Stop(ValueError):pass
def require(v,c):
 if not v:raise Stop(c)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()

def flat_inherited(take,root_files,baseline):
 """Rebuild R43's flat file fingerprint; never run its evidence checker."""
 sampled={}
 def old(path):
  path=PurePosixPath(path)
  require(path.is_absolute() and '..' not in path.parts and path.name!='.env'
          and not str(path).startswith(('/run/secrets/','/etc/tonggong/')),'R45_INHERITED_PUBLIC_PATH_INVALID')
  raw=take(path);sampled[path]=raw;return raw
 pins42=json.loads(root_files['direct-r42-pins.json'])
 pins43=json.loads(root_files['direct-r43-pins.json'])
 f42=R42/pins42['transaction']
 require(f42.parent==R42 and re.fullmatch(r'\.skills-cards-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',f42.name),'R45_R42_DATA_FOLDER_INVALID')
 for path in (R42/'release-manifest.json',R42/'transition.json',f42/'prepared.json',f42/'progress.json',
              f42/'applied.json',R42/'runtime-control/activation.json',f42/'runtime-activation-intent.json'):
  old(path)
 raw42=old(f42/'baseline.json');b42=json.loads(raw42)
 require(sha(raw42)==pins42['baseline_sha256'],'R45_INHERITED_BASELINE_CHANGED')
 for name in pins42['files']:
  require(re.fullmatch(r'[A-Za-z0-9_./-]+',name) and '..' not in name.split('/') and not name.startswith('/'),'R45_INHERITED_NAME_INVALID')
  old(R42/name)
 command=b42['command']
 require(set(b42['compose_files'])=={command[i+1] for i,v in enumerate(command[:-1]) if v=='-f'},'R45_INHERITED_COMPOSE_SET_CHANGED')
 for path,digest in {**b42['compose_files'],**b42['original_sources']}.items():
  require(sha(old(path))==digest,'R45_INHERITED_FILE_CHANGED')
 for name in ('runtime-r42-skills-cards.compose.json','gateway-tmpfs-r42.compose.yaml'):old(f42/name)
 manifest43=json.loads(old(R43/'release-manifest.json'))
 for name in manifest43['files']:
  require(re.fullmatch(r'[A-Za-z0-9_./-]+',name) and '..' not in name.split('/') and not name.startswith('/'),'R45_INHERITED_NAME_INVALID')
  old(R43/name)
 f43=PurePosixPath(pins43['receipt']['backup_directory'])
 require(f43.parent==R43 and re.fullmatch(r'\.card-storage-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',f43.name),'R45_R43_DATA_FOLDER_INVALID')
 for path in (R43/'transition.json',f43/'baseline.json',f43/'prepared.json',f43/'applied.json',f43/'progress.json',f43/'gateway-r43.compose.json'):
  old(path)
 require(sha(encoded({str(p):sha(raw) for p,raw in sampled.items()}))==baseline['fingerprint'],'R45_INHERITED_FLAT_FINGERPRINT_CHANGED')
 expected_command=[*command,'-f',str(f42/'runtime-r42-skills-cards.compose.json'),'-f',str(f42/'gateway-tmpfs-r42.compose.yaml'),'-f',str(f43/'gateway-r43.compose.json')]
 require(baseline['command']==expected_command,'R45_INHERITED_COMPOSE_COMMAND_CHANGED')

def overlay(folder,manifest_sha,worker_image):
 services={s:{'labels':{'com.megmeet.runtime44.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':MODE}.items()},
    'volumes':[{'type':'bind','source':str(ROOT/'runtime-control'),'target':CONTROL,'read_only':True,'bind':{'create_host_path':False}}]} for s in CHANGED}
 services['gateway']['volumes'].append({'type':'bind','source':str(ROOT/PUBLIC),'target':TARGET,'read_only':True,'bind':{'create_host_path':False}})
 services['gateway']['environment']={'PERSONAL_LOGIN_ACTIVATION_FILE':CONTROL+'/activation.json','PERSONAL_LOGIN_RELEASE_SHA':manifest_sha}
 services['personal-cloud-manager']['environment']={'PERSONAL_CLOUD_WORKER_IMAGE':worker_image,'PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE':CONTROL+'/activation.json','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA':manifest_sha}
 return {'services':services}

def evidence(read,exists,pins):
 try:return _evidence(read,exists,pins)
 except Stop:raise
 except Exception:raise Stop('R45_DIRECT_R44_PROOF_INVALID') from None

def _evidence(read,exists,pins):
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R45_PREDECESSOR_READ_INVALID')
  if path in sampled:require(sampled[path]==raw,'R45_PREDECESSOR_CHANGED_DURING_READ')
  sampled[path]=raw;return raw
 receipt=pins['receipt'];folder=PurePosixPath(receipt['backup_directory'])
 require(folder.parent==ROOT and FOLDER.fullmatch(folder.name),'R45_R44_FOLDER_INVALID')
 forbidden=(ROOT/'.update-incomplete',folder/'rollback-intent.json',folder/'restored-r43.json',
            R42/'.update-incomplete',R43/'.update-incomplete',
            PurePosixPath('/data/megmeet-desktop-distribution-0.14.0-r42-20261008/transition.json'))
 require(not any(exists(path) for path in forbidden),'R45_PREDECESSOR_TRANSITION_PENDING')
 raw_manifest=take(ROOT/'release-manifest.json');manifest=json.loads(raw_manifest)
 require(sha(raw_manifest)==pins['manifest_sha256'] and manifest.get('schema')=='card-photos-r44-package-v1'
         and manifest.get('release')==RELEASE and manifest.get('status')=='FROZEN' and manifest.get('runtime')=='0.2.0-rc.2'
         and manifest.get('changed_services')==['gateway','personal-cloud-manager'] and manifest.get('database_changed') is False,'R45_R44_MANIFEST_CHANGED')
 root_files={}
 for name,record in manifest['files'].items():
  require(re.fullmatch(r'[A-Za-z0-9_@./-]+',name) and not name.startswith('/') and '..' not in name.split('/'),'R45_R44_PUBLIC_PATH_INVALID')
  raw=take(ROOT/name);require(record=={'sha256':sha(raw),'size_bytes':len(raw)},'R45_R44_PACKAGED_FILE_CHANGED');root_files[name]=raw
 raw=take(ROOT/'transition.json')
 require(json.loads(raw)==receipt and 'runtime_activation_verified_at_completion' not in receipt
         and receipt['status']=='APPLIED' and receipt['effective_mode']==MODE
         and receipt['manifest_sha256']==pins['manifest_sha256'],'R45_R44_RECEIPT_CHANGED')
 require(take(folder/'applied.json')==raw,'R45_R44_APPLIED_PAIR_CHANGED')
 baseline_raw=take(folder/'baseline.json');baseline=json.loads(baseline_raw)
 require(sha(baseline_raw)==receipt['baseline_sha256'] and baseline['worker_image_id']==pins['worker_image'],'R45_R44_BASELINE_CHANGED')
 flat_inherited(take,root_files,baseline)
 original={s:v['id'] for s,v in baseline['items'].items()};ids=receipt['current_service_ids']
 require(set(ids)==SERVICES and original==receipt['original_service_ids']
         and all(isinstance(v,str) and HEX.fullmatch(v) for v in ids.values())
         and len(set(ids.values()))==8 and all(ids[s]==original[s] for s in SERVICES-CHANGED)
         and all(ids[s]!=original[s] for s in CHANGED),'R45_R44_IDENTITIES_CHANGED')
 keys=('release','manifest_sha256','backup_directory','previous_mode','original_service_ids','baseline_sha256',
       'changed_services','manager_restarted','workers_disconnected','history_format_changed','history_maintenance',
       'images_changed','previous_worker_runtime','worker_runtime')
 expected_prepared={k:receipt[k] for k in keys}|{'status':'PREPARED'}
 prepared_raw=take(folder/'prepared.json')
 require(json.loads(prepared_raw)==expected_prepared and expected_prepared['previous_mode']=='r43-card-storage','R45_R44_PREPARED_CHANGED')
 stage=json.loads(take(folder/'progress.json'))
 require(stage.get('phase')=='FINISHED' and stage.get('target')==MODE and stage.get('creating') is None
         and stage.get('created')=={MODE:{s:ids[s] for s in CHANGED}},'R45_R44_PROGRESS_CHANGED')
 spec=overlay(folder,pins['manifest_sha256'],pins['worker_image'])
 require(json.loads(take(folder/'runtime-r44-card-photos.compose.json'))==spec,'R45_R44_COMPOSE_CHANGED')
 activation=encoded({'schema':'megmeet-runtime-activation-r41-v1','runtime':'0.2.0-rc.2',
         'release_manifest_sha256':pins['manifest_sha256'],'image_id':pins['worker_image'],'status':'ACTIVE'})
 require(take(ROOT/'runtime-control/activation.json')==activation,'R45_R44_ACTIVATION_CHANGED')
 require(take(folder/'runtime-activation-intent.json')==encoded({'schema':'r41-runtime-activation-intent-v1',
         'prepared_sha256':sha(prepared_raw),'activation_sha256':sha(activation)}),'R45_R44_ACTIVATION_INTENT_CHANGED')
 require(json.loads(take(ROOT/'worker-candidate.json'))=={'schema':'r44-worker-candidate-v1','manifest_sha256':pins['manifest_sha256'],
         'image_id':pins['worker_image'],'runtime':'0.2.0-rc.2','source_contract_sha256':sha(root_files['worker-contract.json']),
         'verified_source_count':88,'native_verified':True},'R45_R44_WORKER_RECORD_CHANGED')
 items=deepcopy(baseline['items']);policies=deepcopy(baseline['policies']);sources=deepcopy(baseline['source_pins'])
 for service in CHANGED:
  items[service].update(id=ids[service],running=True,runtime44={'release':RELEASE,'transaction':folder.name,'mode':MODE})
  mounts={r['destination']:r for r in policies[service]['mounts']}
  require(CONTROL in mounts and (service!='gateway' or TARGET in mounts),'R45_R44_OLD_MOUNT_MISSING')
  for row in spec['services'][service]['volumes']:
   mounts[row['target']]={'type':'bind','source':row['source'],'destination':row['target'],'rw':False}
  policies[service]['mounts']=sorted(mounts.values(),key=lambda r:r['destination'])
  policies[service]['environment'].update(spec['services'][service]['environment'])
 sources['gateway'][TARGET]=manifest['files'][PUBLIC]['sha256']
 for path,before in sampled.items():require(read(path)==before,'R45_PREDECESSOR_CHANGED_DURING_READ')
 require(not any(exists(path) for path in forbidden),'R45_PREDECESSOR_CHANGED_DURING_READ')
 return {'ids':deepcopy(ids),'items':items,'policies':policies,'source_pins':sources,
         'command':[*baseline['command'],'-f',str(folder/'runtime-r44-card-photos.compose.json')],
         'fingerprint':sha(encoded({str(p):sha(v) for p,v in sampled.items()})),
         'baseline':{'nginx':deepcopy(baseline['nginx'])},'folder':Path(str(folder))}

def live(proof,current,policy,source_pins,nginx):
 require(current==proof['items'],'R45_LIVE_R44_IDENTITIES_CHANGED')
 for service,item in current.items():
  require(policy(item['id'])==proof['policies'][service],'R45_LIVE_R44_POLICY_CHANGED')
  if service!='db-migrate':require(item['running'] is True,'R45_R44_SERVICE_NOT_READY')
 for service,pins in proof['source_pins'].items():require(source_pins(service,current[service]['id'],pins)==pins,'R45_LIVE_R44_SOURCE_CHANGED')
 require(nginx()==proof['baseline']['nginx'],'R45_PREDECESSOR_NGINX_CHANGED')
