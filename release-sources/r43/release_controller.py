"""R43 forward-only schema + gateway release; recovery never downgrades data."""
from datetime import datetime,timezone
import hashlib,json,re,uuid
from pathlib import Path

RELEASE='20261008-r43-card-storage';OLD='r42-skills-cards';NEW='r43-card-storage'
FOLDER=re.compile(r'\.card-storage-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')
HEX=re.compile(r'[a-f0-9]{64}')
PHASES={'PREPARED','DB_APPLYING','DB_APPLIED','GATEWAY_CREATING','GATEWAY_CREATED','GATEWAY_STARTED','FINISHED'}
class Stop(ValueError):pass
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def require(value,code):
 if not value:raise Stop(code)

class Controller:
 def __init__(self,root,payload_sha,product):self.root=Path(root);self.payload_sha=payload_sha;self.p=product
 def progress(self,phase):print(json.dumps({'status':'CARD_STORAGE_R43_PROGRESS','phase':phase,'changed_services':['gateway'],'manager_restarted':False,'workers_restarted':False}),flush=True)
 def write(self,path,value):self.p.write_new(path,encoded(value))
 def stage(self,folder,phase,created=None):
  require(phase in PHASES,'R43_PHASE_INVALID')
  value={'phase':phase,'created_gateway_id':created}
  path=folder/'progress.json'
  if self.p.exists(path):self.p.replace(path,encoded(value))
  else:self.write(path,value)
  self.progress(phase)
 def folder(self,value):
  folder=Path(value.get('backup_directory',''))
  require(folder.parent==self.root and FOLDER.fullmatch(folder.name),'R43_JOURNAL_PATH_INVALID');return folder
 def validate(self,value,folder):
  require(set(value)=={'schema','release','status','payload_sha256','backup_directory','previous_mode','baseline_sha256','original_service_ids'},'R43_PREPARED_SHAPE_CHANGED')
  require(value['schema']=='card-storage-r43-prepared-v1' and value['release']==RELEASE and value['status']=='PREPARED' and value['payload_sha256']==self.payload_sha and value['previous_mode']==OLD and self.folder(value)==folder,'R43_PREPARED_CHANGED')
  require(sha(self.p.read(folder/'baseline.json'))==value['baseline_sha256'],'R43_BASELINE_CHANGED')
  self.p.preserved(value,folder)
  stage=json.loads(self.p.read(folder/'progress.json'))
  require(set(stage)=={'phase','created_gateway_id'} and stage['phase'] in PHASES,'R43_JOURNAL_CHANGED')
  require(stage['created_gateway_id'] is None or isinstance(stage['created_gateway_id'],str) and HEX.fullmatch(stage['created_gateway_id']),'R43_CREATED_ID_INVALID')
  require((stage['phase'] in {'GATEWAY_CREATED','GATEWAY_STARTED','FINISHED'})==(stage['created_gateway_id'] is not None),'R43_JOURNAL_ID_PAIR_INVALID')
  return stage
 def recognized(self,value,folder):
  stage=self.validate(value,folder);items=self.p.snapshot();self.p.assert_preserved_services(items,value,folder)
  gateway=items.get('gateway');mode=None
  if gateway is None:require(stage['phase']=='GATEWAY_CREATING','R43_GATEWAY_UNEXPECTEDLY_MISSING')
  elif gateway['id']==value['original_service_ids']['gateway']:
   require(stage['phase'] not in {'GATEWAY_CREATED','GATEWAY_STARTED','FINISHED'},'R43_GATEWAY_REVERTED')
   self.p.static(gateway,OLD,value,folder);mode=OLD
  else:
   require(gateway.get('runtime43')=={'release':RELEASE,'transaction':folder.name,'mode':NEW},'R43_UNKNOWN_GATEWAY')
   require(stage['created_gateway_id']==gateway['id'] or stage['phase']=='GATEWAY_CREATING','R43_GATEWAY_NOT_OWNED')
   self.p.static(gateway,NEW,value,folder);mode=NEW
  return items,mode,stage
 def check(self):
  self.progress('CHECKING_R42_AND_DATABASE');self.p.verify_bundle()
  require(not self.p.exists(self.root/'.update-incomplete'),'R43_INCOMPLETE_USE_RECOVER')
  if not self.p.exists(self.root/'transition.json'):
   proof=self.p.baseline();database=self.p.database(proof['ids']['gateway'],False)
   require(database['mode']=='ORIGINAL','R43_UNJOURNALED_MIGRATION')
   self.p.assert_baseline_stable(proof)
   return {'schema':'card-storage-r43-check-v1','status':'VERIFIED','current_mode':OLD,'database':database,'current_service_ids':proof['ids'],'live_changes':'NONE','recursive_ancestor_validation':False,'next_change':'DATABASE_017_AND_GATEWAY_ONLY','maintenance_effect':'GATEWAY_REQUESTS_MAY_BE_INTERRUPTED','admission_idle_check':'TRANSACTION_CHECKPOINT_ONLY','desktop_version':'0.13.0','employee_acceptance':'PENDING'}
  receipt=json.loads(self.p.read(self.root/'transition.json'));folder=self.folder(receipt)
  value=json.loads(self.p.read(folder/'prepared.json'));items,mode,stage=self.recognized(value,folder)
  require(mode==NEW and stage['phase']=='FINISHED' and items['gateway']['running'],'R43_COMMITTED_GATEWAY_NOT_READY')
  expected=self.receipt(value,items)
  require(receipt==expected and self.p.read(folder/'applied.json')==encoded(expected),'R43_COMMIT_PAIR_CHANGED')
  health=self.p.health(items,value,folder)
  require(self.p.snapshot()==items,'R43_CHECK_CHANGED_DURING_READ')
  return {'schema':'card-storage-r43-check-v1','status':'VERIFIED','current_mode':NEW,'database':health['database'],'current_service_ids':{s:v['id'] for s,v in items.items()},'live_changes':'NONE','recursive_ancestor_validation':False,'employee_acceptance':'PENDING'}
 def prepare(self):
  check=self.check()
  if check['current_mode']==NEW:return None
  proof=self.p.baseline();self.p.assert_baseline_stable(proof)
  folder=self.root/('.card-storage-switch-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8])
  self.p.mkdir(folder);baseline=self.p.capture(proof);self.write(folder/'baseline.json',baseline)
  value={'schema':'card-storage-r43-prepared-v1','release':RELEASE,'status':'PREPARED','payload_sha256':self.payload_sha,'backup_directory':str(folder),'previous_mode':OLD,'baseline_sha256':sha(encoded(baseline)),'original_service_ids':proof['ids']}
  self.write(folder/'prepared.json',value);self.stage(folder,'PREPARED');self.p.prepare_compose(value,folder)
  self.p.nginx_preflight();self.p.assert_baseline_stable(proof)
  self.write(self.root/'.update-incomplete',value)
  return value,folder
 def receipt(self,value,items):
  return {**value,'schema':'card-storage-r43-receipt-v1','status':'APPLIED','effective_mode':NEW,'current_service_ids':{s:v['id'] for s,v in items.items()},'changed_services':['gateway'],'manager_restarted':False,'workers_restarted':False,'tonggong_restarted':False,'images_changed':False,'maintenance_effect':'GATEWAY_REQUESTS_MAY_BE_INTERRUPTED','admission_idle_check':'TRANSACTION_CHECKPOINT_ONLY','migration':'017_card_print_artifact_limit.sql','migration_sha256':self.p.migration_sha,'ordinary_limit_bytes':33554432,'card_print_limit_bytes':134217728,'business_rows_deleted':False,'existing_business_values_rewritten':False,'employee_card_acceptance':'PENDING','official_search':'PENDING_UPSTREAM_502_UNCHANGED','desktop_version':'0.13.0'}
 def finish(self,value,folder):
  require(self.p.read(self.root/'.update-incomplete')==self.p.read(folder/'prepared.json'),'R43_MARKER_CHANGED')
  items,mode,stage=self.recognized(value,folder)
  if stage['phase'] in {'PREPARED','DB_APPLYING','DB_APPLIED'}:
   require(mode==OLD and items['gateway']['running'],'R43_MIGRATION_GATEWAY_REQUIRED')
   self.stage(folder,'DB_APPLYING');self.p.database(items['gateway']['id'],True)
   self.stage(folder,'DB_APPLIED')
   items,mode,stage=self.recognized(value,folder)
   self.stage(folder,'GATEWAY_CREATING')
  items,mode,stage=self.recognized(value,folder)
  if stage['phase']=='GATEWAY_CREATING':
   if mode!=NEW:
    if items['gateway'] is not None and items['gateway']['running']:self.p.stop(items['gateway']['id'])
    self.p.create(value,folder)
    items,mode,stage=self.recognized(value,folder)
    require(mode==NEW,'R43_CREATE_NOT_RECOGNIZED')
   self.stage(folder,'GATEWAY_CREATED',items['gateway']['id'])
  items,mode,stage=self.recognized(value,folder)
  require(mode==NEW,'R43_NEW_GATEWAY_REQUIRED')
  if not items['gateway']['running']:
   self.p.start(items['gateway']['id'],value,folder);items,mode,stage=self.recognized(value,folder)
  if stage['phase'] not in {'GATEWAY_STARTED','FINISHED'}:self.stage(folder,'GATEWAY_STARTED',items['gateway']['id'])
  self.p.reload_nginx(value,folder);health=self.p.health(items,value,folder)
  require(self.p.snapshot()==items,'R43_CHANGED_DURING_FINALIZATION')
  receipt=self.receipt(value,items);raw=encoded(receipt)
  for path in (folder/'applied.json',self.root/'transition.json'):
   if self.p.exists(path):require(self.p.read(path)==raw,'R43_TERMINAL_PAIR_CHANGED')
   else:self.p.write_new(path,raw)
  self.stage(folder,'FINISHED',items['gateway']['id'])
  self.p.unlink_marker(self.root/'.update-incomplete')
  self.progress('COMPLETED');return {**receipt,'health':health}
 def apply(self):
  ready=self.prepare()
  return self.check() if ready is None else self.finish(*ready)
 def recover(self):
  self.p.verify_bundle();marker=self.root/'.update-incomplete'
  if not self.p.exists(marker):return self.check()
  value=json.loads(self.p.read(marker));folder=self.folder(value)
  require(self.p.read(marker)==self.p.read(folder/'prepared.json'),'R43_MARKER_PAIR_CHANGED')
  return self.finish(value,folder)
