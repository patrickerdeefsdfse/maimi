"""Direct r41 committed/activated proof for downloads-only publication.

Never instantiate or call an ancestor controller, never enter an employee
workspace. Readers must enforce bounded root-owned non-symlink public files.
"""
from copy import deepcopy
import hashlib,json,re
from pathlib import PurePosixPath,Path

ROOT=PurePosixPath('/data/megmeet-skills-cards-r42-20261008')
RELEASE='20261008-r42-skills-cards'
MODE='r42-skills-cards'
RUNTIME='0.2.0-rc.2'
CHANGED=frozenset(('gateway','personal-cloud-manager'))
SERVICES=frozenset(('gateway','personal-cloud-manager','downloads','office-service','db-migrate','postgres','redis','retention-worker'))
FOLDER=re.compile(r'\.skills-cards-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')
HEX=re.compile(r'[a-f0-9]{64}')
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def require(value,code):
 if not value:raise Stop(code)

def _evidence(read,exists,manifest_sha):
 require(isinstance(manifest_sha,str) and HEX.fullmatch(manifest_sha),'DOWNLOAD_R42_NOT_FROZEN')
 sampled={}
 def take(path):
  path=PurePosixPath(path);raw=read(path)
  require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'DOWNLOAD_R42_PUBLIC_READ_INVALID')
  if path in sampled:require(sampled[path]==raw,'DOWNLOAD_R42_CHANGED_DURING_READ')
  sampled[path]=raw;return raw
 require(not exists(ROOT/'.update-incomplete'),'DOWNLOAD_R42_MAINTENANCE_INCOMPLETE')
 manifest_raw=take(ROOT/'release-manifest.json');manifest=json.loads(manifest_raw)
 require(sha(manifest_raw)==manifest_sha and manifest.get('schema')=='megmeet-skills-cards-release-r42-v1' and manifest.get('release')==RELEASE and manifest.get('status')=='FROZEN' and manifest.get('runtime')==RUNTIME,'DOWNLOAD_R42_MANIFEST_CHANGED')
 raw=take(ROOT/'transition.json');receipt=json.loads(raw);folder=PurePosixPath(receipt.get('backup_directory',''))
 require(folder.parent==ROOT and FOLDER.fullmatch(folder.name),'DOWNLOAD_R42_TRANSACTION_INVALID')
 forbidden=(ROOT/'.update-incomplete',folder/'rollback-intent.json',folder/'restored-r41.json')
 require(not any(exists(path) for path in forbidden),'DOWNLOAD_R42_NOT_ACTIVE_RELEASE')
 prepared_raw=take(folder/'prepared.json');prepared=json.loads(prepared_raw)
 baseline_raw=take(folder/'baseline.json');baseline=json.loads(baseline_raw)
 stage_raw=take(folder/'progress.json');stage=json.loads(stage_raw)
 prior_ids={s:v['id'] for s,v in baseline['items'].items()}
 expected={'release':RELEASE,'manifest_sha256':manifest_sha,'status':'PREPARED','backup_directory':str(folder),'previous_mode':'r41-dsh020','original_service_ids':prior_ids,'baseline_sha256':sha(baseline_raw),'changed_services':['gateway','personal-cloud-manager'],'manager_restarted':True,'workers_disconnected':True,'history_format_changed':False,'history_maintenance':'R41_OWNER_SCOPED_HISTORY_PRESERVED','images_changed':True,'previous_worker_runtime':'0.2.0-rc.2','worker_runtime':RUNTIME}
 require(prepared==expected and set(prior_ids)==SERVICES and all(isinstance(v,str) and HEX.fullmatch(v) for v in prior_ids.values()) and len(set(prior_ids.values()))==len(prior_ids),'DOWNLOAD_R42_PREPARED_CHANGED')
 ids=receipt.get('current_service_ids',{})
 require(receipt.get('status')=='APPLIED' and receipt.get('effective_mode')==MODE and receipt.get('nginx_configuration_changed') is False and receipt.get('health',{}).get('status')=='ready' and all(receipt.get(k)==v for k,v in prepared.items() if k!='status') and set(ids)==SERVICES and all(isinstance(v,str) and HEX.fullmatch(v) for v in ids.values()) and len(set(ids.values()))==len(ids) and all(ids[s]==prior_ids[s] for s in SERVICES-CHANGED) and all(ids[s]!=prior_ids[s] for s in CHANGED),'DOWNLOAD_R42_APPLIED_CHANGED')
 require(take(folder/'applied.json')==raw and stage.get('phase')=='FINISHED' and stage.get('target')==MODE and stage.get('creating') is None and stage.get('created',{}).get(MODE)=={s:ids[s] for s in CHANGED},'DOWNLOAD_R42_JOURNAL_CHANGED')
 image_id=baseline.get('worker_image_id')
 require(isinstance(image_id,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',image_id),'DOWNLOAD_R42_IMAGE_ID_INVALID')
 activation_raw=take(ROOT/'runtime-control/activation.json')
 activation={'schema':'megmeet-runtime-activation-r41-v1','runtime':RUNTIME,'release_manifest_sha256':manifest_sha,'image_id':image_id,'status':'ACTIVE'}
 require(activation_raw==encoded(activation),'DOWNLOAD_R42_ACTIVATION_CHANGED')
 intent=take(folder/'runtime-activation-intent.json')
 require(intent==encoded({'schema':'r41-runtime-activation-intent-v1','prepared_sha256':sha(prepared_raw),'activation_sha256':sha(activation_raw)}),'DOWNLOAD_R42_ACTIVATION_INTENT_CHANGED')
 for path,before in list(sampled.items()):require(read(path)==before,'DOWNLOAD_R42_CHANGED_DURING_READ')
 require(not any(exists(path) for path in forbidden),'DOWNLOAD_R42_CHANGED_DURING_READ')
 return {'raw':raw,'receipt':receipt,'prepared':prepared,'baseline':baseline,'folder':Path(str(folder)),'ids':deepcopy(ids),'fingerprint':sha(encoded({str(p):sha(b) for p,b in sampled.items()}))}

def evidence(read,exists,manifest_sha):
 try:return _evidence(read,exists,manifest_sha)
 except Stop:raise
 except (KeyError,TypeError,ValueError,AttributeError):raise Stop('DOWNLOAD_R42_PROOF_INVALID') from None
