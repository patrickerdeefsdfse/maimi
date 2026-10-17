"""Two-service forward recovery; all destructive boundaries have a prior journal."""
from datetime import datetime,timezone
import hashlib,json,re,uuid
from pathlib import Path
RELEASE='20261012-r48-skills-logistics';OLD='r47-fast-entry';NEW='r48-skills-logistics'
CHANGED=('gateway','personal-cloud-manager')
HEX=re.compile('[a-f0-9]{64}')
FOLDER=re.compile(r'\.skills-logistics-switch-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')
PHASES={'PREPARED','CREATING','CREATED','STARTING','STARTED','FINISHED'}
class Stop(ValueError):pass
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def sha(v):return hashlib.sha256(v).hexdigest()
def require(v,c):
 if not v:raise Stop(c)
class Controller:
 def __init__(self,root,payload_sha,product):self.root=Path(root);self.payload_sha=payload_sha;self.p=product
 def progress(self,phase,service=None):print(json.dumps({'status':'SKILLS_LOGISTICS_R48_PROGRESS','phase':phase,'service':service,'changed_services':list(CHANGED),'recursive_ancestor_validation':False}),flush=True)
 def write(self,path,value):self.p.write_new(path,encoded(value))
 def folder(self,value):
  path=Path(value.get('backup_directory',''));require(path.parent==self.root and FOLDER.fullmatch(path.name),'R48_JOURNAL_PATH_INVALID');return path
 def stage(self,folder,phase,created,creating=None):
  require(phase in PHASES and creating in (None,*CHANGED),'R48_PHASE_INVALID')
  value={'phase':phase,'created':created,'creating':creating};path=folder/'progress.json'
  if self.p.exists(path):self.p.replace(path,encoded(value))
  else:self.write(path,value)
  self.progress(phase,creating)
 def validate(self,value,folder):
  require(set(value)=={'schema','release','status','payload_sha256','backup_directory','previous_mode','baseline_sha256','original_service_ids'},'R48_PREPARED_SHAPE_CHANGED')
  require(value['schema']=='skills-logistics-r48-prepared-v1' and value['release']==RELEASE and value['status']=='PREPARED' and value['payload_sha256']==self.payload_sha and value['previous_mode']==OLD and self.folder(value)==folder,'R48_PREPARED_CHANGED')
  require(sha(self.p.read(folder/'baseline.json'))==value['baseline_sha256'],'R48_BASELINE_CHANGED')
  self.p.preserved(value,folder)
  stage=json.loads(self.p.read(folder/'progress.json'))
  require(set(stage)=={'phase','created','creating'} and stage['phase'] in PHASES and stage['creating'] in (None,*CHANGED),'R48_JOURNAL_CHANGED')
  require(isinstance(stage['created'],dict) and set(stage['created'])<=set(CHANGED) and all(isinstance(v,str) and HEX.fullmatch(v) for v in stage['created'].values()),'R48_CREATED_IDS_INVALID')
  require((stage['phase']=='CREATING')==(stage['creating'] is not None),'R48_PHASE_CREATING_INVALID')
  if stage['phase'] in ('STARTING','STARTED','FINISHED'):require(set(stage['created'])==set(CHANGED),'R48_PAIR_NOT_CREATED')
  return stage
 def recognized(self,value,folder):
  stage=self.validate(value,folder);items=self.p.snapshot();self.p.assert_preserved_services(items,value,folder);modes={}
  for service in CHANGED:
   item=items.get(service)
   if item is None:
    require(stage['phase']=='CREATING' and stage['creating']==service,'R48_SERVICE_UNEXPECTEDLY_MISSING');modes[service]=None;continue
   if item['id']==value['original_service_ids'][service]:
    require(service not in stage['created'],'R48_SERVICE_REVERTED');self.p.static(service,item,OLD,value,folder);modes[service]=OLD
   else:
    require(item.get('runtime48')=={'release':RELEASE,'transaction':folder.name,'mode':NEW},'R48_UNKNOWN_SERVICE')
    require(stage['created'].get(service)==item['id'] or stage['creating']==service,'R48_SERVICE_NOT_OWNED')
    self.p.static(service,item,NEW,value,folder);modes[service]=NEW
  return items,modes,stage
 def check(self):
  self.progress('CHECKING_DIRECT_R47');self.p.verify_bundle()
  require(not self.p.exists(self.root/'.update-incomplete'),'R48_INCOMPLETE_USE_RECOVER')
  if not self.p.exists(self.root/'transition.json'):
   proof=self.p.baseline();self.p.assert_baseline_stable(proof)
   return {'schema':'skills-logistics-r48-check-v1','status':'VERIFIED','current_mode':OLD,'current_service_ids':proof['ids'],'live_changes':'NONE','changed_services_on_maintenance':list(CHANGED),'closed_retention_seconds':7200,'persistent_login':True,'maximum_hosts':0,'database_changed':False,'images_changed':False,'workers_disconnected_on_manager_restart':True,'recursive_ancestor_validation':False,'employee_acceptance':'PENDING'}
  receipt=json.loads(self.p.read(self.root/'transition.json'));folder=self.folder(receipt);value=json.loads(self.p.read(folder/'prepared.json'))
  items,modes,stage=self.recognized(value,folder)
  require(set(modes.values())=={NEW} and stage['phase']=='FINISHED' and all(items[s]['running'] for s in CHANGED),'R48_COMMITTED_SERVICES_NOT_READY')
  expected=self.receipt(value,items)
  require(receipt==expected and self.p.read(folder/'applied.json')==encoded(expected),'R48_COMMIT_PAIR_CHANGED')
  health=self.p.health(items,value,folder);require(self.p.snapshot()==items,'R48_CHECK_CHANGED_DURING_READ')
  return {'schema':'skills-logistics-r48-check-v1','status':'VERIFIED','current_mode':NEW,'current_service_ids':{s:v['id'] for s,v in items.items()},'health':health,'live_changes':'NONE','employee_acceptance':'PENDING'}
 def prepare(self):
  check=self.check()
  if check['current_mode']==NEW:return None
  proof=self.p.baseline();self.p.assert_baseline_stable(proof)
  folder=self.root/('.skills-logistics-switch-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8])
  self.p.mkdir(folder);baseline=self.p.capture(proof);self.write(folder/'baseline.json',baseline)
  value={'schema':'skills-logistics-r48-prepared-v1','release':RELEASE,'status':'PREPARED','payload_sha256':self.payload_sha,'backup_directory':str(folder),'previous_mode':OLD,'baseline_sha256':sha(encoded(baseline)),'original_service_ids':proof['ids']}
  self.write(folder/'prepared.json',value);self.stage(folder,'PREPARED',{});self.p.prepare_compose(value,folder)
  self.p.nginx_preflight();self.p.assert_baseline_stable(proof);self.write(self.root/'.update-incomplete',value)
  return value,folder
 def receipt(self,value,items):
  return {**value,'schema':'skills-logistics-r48-receipt-v1','status':'APPLIED','effective_mode':NEW,'current_service_ids':{s:v['id'] for s,v in items.items()},'changed_services':list(CHANGED),'manager_restarted':True,'workers_disconnected':True,'tonggong_restarted':False,'images_changed':False,'database_changed':False,'closed_retention_seconds':7200,'persistent_login':True,'tokenkey_cache_preserved':True,'maximum_hosts':0,'worker_runtime':'0.2.0-rc.2','desktop_publication_pending':True,'desktop_version':'0.13.0','employee_acceptance':'PENDING'}
 def finish(self,value,folder):
  require(self.p.read(self.root/'.update-incomplete')==self.p.read(folder/'prepared.json'),'R48_MARKER_CHANGED')
  for service in CHANGED:
   items,modes,stage=self.recognized(value,folder)
   if service in stage['created']:continue
   require(stage['creating'] in (None,service),'R48_SWITCH_ORDER_CHANGED')
   if stage['creating'] is None:self.stage(folder,'CREATING',stage['created'],service)
   items,modes,stage=self.recognized(value,folder)
   if modes[service]!=NEW:
    if items[service] is not None and items[service]['running']:self.p.stop(service,items[service]['id'])
    self.p.create(service,value,folder);items,modes,stage=self.recognized(value,folder)
    require(modes[service]==NEW,'R48_CREATE_NOT_RECOGNIZED')
   self.stage(folder,'CREATED',{**stage['created'],service:items[service]['id']})
  items,modes,stage=self.recognized(value,folder)
  if stage['phase'] not in ('STARTING','STARTED','FINISHED'):self.stage(folder,'STARTING',stage['created'])
  for service in reversed(CHANGED):
   items,modes,stage=self.recognized(value,folder)
   require(modes[service]==NEW,'R48_NEW_SERVICE_REQUIRED')
   if not items[service]['running']:self.p.start(service,items[service]['id'],value,folder)
  items,modes,stage=self.recognized(value,folder)
  if stage['phase']!='FINISHED':self.stage(folder,'STARTED',stage['created'])
  self.p.reload_nginx(value,folder);health=self.p.health(items,value,folder)
  require(self.p.snapshot()==items,'R48_CHANGED_DURING_FINALIZATION')
  receipt=self.receipt(value,items);raw=encoded(receipt)
  for path in (folder/'applied.json',self.root/'transition.json'):
   if self.p.exists(path):require(self.p.read(path)==raw,'R48_TERMINAL_PAIR_CHANGED')
   else:self.p.write_new(path,raw)
  self.stage(folder,'FINISHED',stage['created']);self.p.unlink_marker(self.root/'.update-incomplete');self.progress('COMPLETED')
  return {**receipt,'health':health}
 def apply(self):
  ready=self.prepare();return self.check() if ready is None else self.finish(*ready)
 def recover(self):
  self.p.verify_bundle();marker=self.root/'.update-incomplete'
  if not self.p.exists(marker):return self.check()
  value=json.loads(self.p.read(marker));folder=self.folder(value)
  require(self.p.read(marker)==self.p.read(folder/'prepared.json'),'R48_MARKER_PAIR_CHANGED');return self.finish(value,folder)
