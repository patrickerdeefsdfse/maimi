"""Direct committed R43 proof and dual-service R44 maintenance ports."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib,json,os,stat,uuid
from pathlib import Path

ROOT=Path('/data/megmeet-card-photos-r44-20261009')
R42=Path('/data/megmeet-skills-cards-r42-20261008')
R43=Path('/data/megmeet-card-storage-r43-20261008')
OLD='r43-card-storage';NEW='r44-card-photos';RELEASE='20261009-r44-card-photos'
TARGET='/app/maimi_projects/service.py';PUBLIC='public/maimi_projects/service.py'
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def require(value,code):
 if not value:raise Stop(code)
def public(path):
 path=Path(path)
 require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)),'R44_UNSAFE_READ_PATH')
 for parent in path.parents:
  info=parent.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not stat.S_IMODE(info.st_mode)&0o022,'R44_UNSAFE_READ_PARENT')
 before=path.stat();require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid==0 and not stat.S_IMODE(before.st_mode)&0o022 and before.st_size<=8*1024**2,'R44_PUBLIC_FILE_INVALID')
 identity=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns)
 with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as handle:
  opened=os.fstat(handle.fileno());raw=handle.read(8*1024**2+1);after=os.fstat(handle.fileno())
 require(identity(before)==identity(opened)==identity(after) and len(raw)==before.st_size,'R44_PUBLIC_FILE_CHANGED');return raw


CHANGED=("gateway","personal-cloud-manager")
LABEL="com.megmeet.runtime44."
CONTROL_TARGET="/opt/personal-cloud/runtime-r41"
RUNTIME="0.2.0-rc.2"


class Product:
 def __init__(self,root,files,manifest,payload_sha,modules):
  self.root=Path(root);self.files=files;self.manifest=manifest;self.payload_sha=payload_sha;self.m=modules
  self.manifest_sha=sha(encoded(manifest));self._runtime_verified=set();self.readonly=True
  require(self.root==ROOT and manifest.get('runtime')==RUNTIME,'R44_FIXED_ROOT_REQUIRED')
  self.io=modules['release_io'];self.meta=modules['release_metadata'];self.direct=modules['direct_r43'];self.core=modules['direct_r42_core'];self.r42=modules['direct_r42'];self.db=modules['readonly_database'];self.runtime=modules['runtime_product']
  self.pins=json.loads(files['direct-r43-pins.json']);self.r42pins=json.loads(files['direct-r42-pins.json'])
  self.meta.LABELS={**self.meta.LABELS,'runtime43':('com.megmeet.card-storage43.',{'gateway'}),'runtime44':(LABEL,set(CHANGED))}
 read=staticmethod(public)
 exists=staticmethod(os.path.lexists)
 def bundle(self,name):
  if self.root.exists():require(public(self.root/name)==self.files[name],'R44_INSTALLED_PACKAGE_CHANGED')
  return self.files[name]

 def verify_bundle(self):
  require(set(self.files)==set(self.manifest['files']),'R44_PACKAGE_SET_CHANGED')
  for name,raw in self.files.items():
   require(self.manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R44_PACKAGE_PIN_CHANGED')
   if self.root.exists():require(public(self.root/name)==raw,'R44_INSTALLED_PACKAGE_CHANGED')
  if self.root.exists():require(public(self.root/'release-manifest.json')==encoded(self.manifest),'R44_MANIFEST_CHANGED')


 def materialize(self):
  if not self.root.exists():
   stage=self.root.parent/('.r44-package-'+uuid.uuid4().hex)
   self.io.mkdir(stage);stage.chmod(0o755)
   for name,raw in self.files.items():
    path=stage/name
    for parent in reversed(path.parents):
     if parent==stage:continue
     if stage in parent.parents and not parent.exists():self.io.mkdir(parent);parent.chmod(0o755)
    self.io.write_new(path,raw)
    if name.startswith(('public/','worker/')):path.chmod(0o644)
   self.io.write_new(stage/'release-manifest.json',encoded(self.manifest))
   self.io.syncdir(stage)
   require(not os.path.lexists(self.root),'R44_PACKAGE_APPEARED_DURING_PREPARATION')
   os.rename(stage,self.root);self.io.syncdir(self.root.parent)
  self.verify_bundle()
  require(public(self.root/'release-manifest.json')==encoded(self.manifest),'R44_MANIFEST_CHANGED')


 @contextmanager
 def locked(self):
  paths=[*self.meta.LOCK_PATHS,Path('/data/megmeet-remembered-login-20260930-r40/.apply.lock'),Path('/data/megmeet-dsh020-r41-20261008/.apply.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0130-publication.lock'),R42/'.apply.lock',Path('/data/maimi-upload-20260921-r11/.desktop0140-publication.lock'),R43/'.apply.lock',self.root/'.apply.lock']
  with self.io.locked(paths):yield


 def run(self,label,args,timeout=60):return self.io.run(label,args,timeout)


 def write_new(self,path,raw):self.io.write_new(path,raw)


 def replace(self,path,raw):self.io.replace(path,raw)


 def mkdir(self,path):self.io.mkdir(path)


 def unlink_marker(self,path):self.io.unlink_marker(path)


 def evidence(self):return self.direct.evidence(public,os.path.lexists,self.pins,self.r42,self.r42pins,self.core)


 def policy(self,identifier):return deepcopy(self._policies[identifier])


 def source_pins(self,service,identifier,pins):
  code='import json,hashlib;from pathlib import Path;ps='+repr(list(pins))+';print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() and not Path(p).is_symlink() else None for p in ps}))'
  result=json.loads(self.run('R44_SOURCE_PINS',['docker','exec',identifier,'python','-I','-B','-c',code],20))
  require(result==pins,'R44_LIVE_SOURCE_CHANGED');return result


 def nginx_metadata(self):return self.meta.nginx_metadata(self.run,public)


 def assert_baseline_stable(self,proof):
  require(self.evidence()['fingerprint']==proof['fingerprint'] and self.snapshot()==proof['items'],'R44_BASELINE_CHANGED_DURING_CHECK')


 def database(self,identifier):
  # Both stages require a running, policy/source-verified gateway. This is
  # called only after baseline()/recognized(); no external credential copy.
  code=self.db.remote_code(self.files['readonly_database.py'],self.files['artifact_schema.py'],json.loads(self.files['artifact-schema-expected.json']),self.files['project_schema.py'],json.loads(self.files['project-schema-preserved.json']))
  raw=self.run('R44_DATABASE_READ_ONLY_CHECK',['docker','exec',identifier,'python','-I','-B','-c',code],70)
  require(len(raw)<=8192,'R44_DATABASE_OUTPUT_LIMIT');result=json.loads(raw)
  if result.get('status')=='STOP':
   fixed={'R44_DATABASE_CONNECTION_INVALID','R44_DATABASE_LEDGER_CHANGED','R44_DATABASE_017_NOT_MIGRATED','ARTIFACT_TABLE_ABSENT','ARTIFACT_SCHEMA_MISMATCH','R43_PRESERVED_SCHEMA_EXPECTATION_INVALID','R43_PRESERVED_PROJECT_SCHEMA_CHANGED','R44_DATABASE_BUSY','R44_DATABASE_TIMEOUT','R44_DATABASE_UNAVAILABLE','R44_DATABASE_CHECK_FAILED'}
   raise Stop(result['reason'] if result.get('reason') in fixed else 'R44_DATABASE_CHECK_FAILED')
  require(result.get('schema')=='card-photos-r44-database-v1' and result.get('status')=='VERIFIED' and result.get('mode')=='MIGRATED' and result.get('transaction')=='READ_ONLY' and result.get('database_changed') is False and result.get('checksum')==self.db.SQL_SHA and result.get('preserved_project_tables_verified') is True,'R44_DATABASE_RESULT_INVALID')
  return result


 def nginx_preflight(self):self.meta.nginx(self.run,'-t')


 def reload_nginx(self,value,folder):self.preserved(value,folder);self.meta.nginx(self.run,'-t');self.meta.nginx(self.run,'-s','reload')


 def snapshot(self):
  items,policies=self.meta.collect(self.run,allow_missing_services=CHANGED);self._policies=policies
  for item in items.values():
   if item and item.get('runtime44')=={'release':None,'transaction':None,'mode':None}:item.pop('runtime44')
  return items
 def baseline(self):
  self.verify_bundle();proof=self.evidence();items=self.snapshot()
  self.direct.live(proof,items,self.policy,self.source_pins,self.nginx_metadata)
  database=self.database(items['gateway']['id']);self.runtime.check_base(self)
  self.assert_baseline_stable(proof);return {**proof,'database':database}
 def prepare_runtime(self):
  self.runtime.prepare(self)
  directory=self.root/'runtime-control'
  if not directory.exists():self.mkdir(directory);directory.chmod(0o755);self.io.syncdir(directory.parent)
  require(not directory.is_symlink() and directory.is_dir() and directory.stat().st_uid==0 and stat.S_IMODE(directory.stat().st_mode)==0o755,'R44_RUNTIME_CONTROL_DIRECTORY_INVALID')
  require(not os.path.lexists(directory/'activation.json'),'R44_RUNTIME_ALREADY_ACTIVATED')
 def proof_baseline(self,proof):
  return {key:deepcopy(proof[key]) for key in ('items','policies','command','source_pins','fingerprint')}|{'nginx':deepcopy(proof['baseline']['nginx'])}
 def capture(self,items,folder):
  proof=self.baseline();require(items==proof['items'],'R44_BASELINE_CHANGED')
  return self.proof_baseline(proof)|{'worker_image_id':self.runtime.candidate(self)}
 def preserved(self,value,folder):
  self.verify_bundle();proof=self.evidence();baseline=json.loads(public(folder/'baseline.json'))
  require({k:v for k,v in baseline.items() if k!='worker_image_id'}==self.proof_baseline(proof) and value['original_service_ids']==proof['ids'],'R44_PREDECESSOR_CHANGED')
  require(self.nginx_metadata()==baseline['nginx'],'R44_NGINX_CHANGED')
  require(self.runtime.candidate(self)==baseline['worker_image_id'],'R44_RUNTIME_CANDIDATE_CHANGED')
  self.prepare_compose(value,folder,readonly=True)
 def unchanged(self,service,item,value,folder):
  baseline=json.loads(public(folder/'baseline.json'))
  require(item==baseline['items'][service] and self.policy(item['id'])==baseline['policies'][service],'R44_PRESERVED_SERVICE_CHANGED')
 def runtime_environment(self,b):
  return {'PERSONAL_CLOUD_WORKER_IMAGE':b['worker_image_id'],'PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE':CONTROL_TARGET+'/activation.json','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA':self.manifest_sha}
 def login_environment(self):
  return {'PERSONAL_LOGIN_ACTIVATION_FILE':CONTROL_TARGET+'/activation.json','PERSONAL_LOGIN_RELEASE_SHA':self.manifest_sha}
 def expected_policy(self,service,mode,baseline):
  expected=deepcopy(baseline['policies'][service])
  if mode==NEW:
   rows={row['destination']:row for row in expected['mounts']}
   require(CONTROL_TARGET in rows,'R44_RUNTIME_CONTROL_BIND_MISSING')
   rows[CONTROL_TARGET]={'type':'bind','source':str(self.root/'runtime-control'),'destination':CONTROL_TARGET,'rw':False}
   if service=='gateway':
    require(TARGET in rows,'R44_OLD_SOURCE_BIND_MISSING')
    rows[TARGET]={'type':'bind','source':str(self.root/PUBLIC),'destination':TARGET,'rw':False}
   expected['mounts']=sorted(rows.values(),key=lambda row:row['destination'])
   expected['environment'].update(self.login_environment() if service=='gateway' else self.runtime_environment(baseline))
  else:require(mode==OLD,'R44_MODE_INVALID')
  return expected
 def static(self,service,item,mode,value,folder,original=False):
  baseline=json.loads(public(folder/'baseline.json'));before=baseline['items'][service]
  require({k:v for k,v in item.items() if k not in ('id','running','runtime44')}=={k:v for k,v in before.items() if k not in ('id','running','runtime44')},'R44_SERVICE_STATIC_CHANGED')
  if original:require(item['id']==before['id'] and item.get('runtime44')==before.get('runtime44'),'R44_ORIGINAL_SERVICE_CHANGED')
  require(self.policy(item['id'])==self.expected_policy(service,mode,baseline),'R44_SERVICE_POLICY_CHANGED')
 def live_sources(self,service,identifier,mode,baseline):
  pins=deepcopy(baseline['source_pins'][service])
  if mode==NEW and service=='gateway':pins[TARGET]=self.manifest['files'][PUBLIC]['sha256']
  self.source_pins(service,identifier,pins)
 def overlay(self,mode,value,folder):
  require(mode in (OLD,NEW),'R44_MODE_INVALID')
  services={s:{'labels':{LABEL+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':mode}.items()}} for s in CHANGED}
  if mode==NEW:
   for service in CHANGED:services[service]['volumes']=[{'type':'bind','source':str(self.root/'runtime-control'),'target':CONTROL_TARGET,'read_only':True,'bind':{'create_host_path':False}}]
   services['gateway']['volumes'].append({'type':'bind','source':str(self.root/PUBLIC),'target':TARGET,'read_only':True,'bind':{'create_host_path':False}})
   services['gateway']['environment']=self.login_environment()
   services['personal-cloud-manager']['environment']=self.runtime_environment(json.loads(public(folder/'baseline.json')))
  return {'services':services}
 def compose(self,mode,value,folder):return json.loads(public(folder/'baseline.json'))['command']+['-f',str(folder/('runtime-'+mode+'.compose.json'))]
 def prepare_compose(self,value,folder,readonly=False):
  for mode in (OLD,NEW):
   path=folder/('runtime-'+mode+'.compose.json');raw=encoded(self.overlay(mode,value,folder))
   if path.exists():require(public(path)==raw,'R44_COMPOSE_CHANGED')
   elif readonly:raise Stop('R44_COMPOSE_MISSING')
   else:self.write_new(path,raw)
   if not readonly:self.run('R44_COMPOSE_CHECK',self.compose(mode,value,folder)+['config','--quiet'],30)

 def stop(self,service,identifier):self.run('R44_STOP_'+service.upper().replace('-','_'),['docker','stop','--timeout','20',identifier],35)


 def create(self,service,mode,value,folder):self.run('R44_CREATE_'+service.upper().replace('-','_'),self.compose(mode,value,folder)+['up','--no-deps','--no-start','--no-build','--pull','never','--force-recreate',service],120)


 def start(self,service,identifier,mode,value,folder):
  if self.snapshot()[service]['id']!=identifier:raise Stop('R44_CHANGED_BEFORE_START')
  self.run('R44_START_'+service.upper().replace('-','_'),self.compose(mode,value,folder)+['start','--wait','--wait-timeout','90',service],110)
  if self.snapshot()[service]['id']!=identifier:raise Stop('R44_CHANGED_DURING_START')


 def post_start(self,value,folder):
  items=self.snapshot()
  if any(not items[s] or not items[s]['running'] for s in CHANGED):raise Stop('R44_START_NOT_READY')


 def activation_value(self,folder):
  b=json.loads(public(folder/'baseline.json'))
  return {'schema':'megmeet-runtime-activation-r41-v1','runtime':RUNTIME,'release_manifest_sha256':self.manifest_sha,'image_id':b['worker_image_id'],'status':'ACTIVE'}


 def activate(self,value,folder):
  path=self.root/'runtime-control/activation.json';raw=encoded(self.activation_value(folder))
  stage=json.loads(public(folder/'progress.json'))
  if stage.get('phase')!='FINISHED' or stage.get('target')!=NEW or public(self.root/'transition.json')!=public(folder/'applied.json'):raise Stop('R44_COMMIT_REQUIRED_BEFORE_ACTIVATION')
  intent_path=folder/'runtime-activation-intent.json'
  intent=encoded({'schema':'r41-runtime-activation-intent-v1','prepared_sha256':sha(public(folder/'prepared.json')),'activation_sha256':sha(raw)})
  if os.path.lexists(intent_path):
   if public(intent_path)!=intent:raise Stop('R44_ACTIVATION_INTENT_CHANGED')
  else:self.write_new(intent_path,intent)
  if path.exists():
   if public(path)!=raw:raise Stop('R44_ACTIVATION_CHANGED')
   # Recover a prior interruption after content commit but before permission sync.
   if stat.S_IMODE(path.stat().st_mode)!=0o644:path.chmod(0o644)
   with path.open('rb') as stream:os.fsync(stream.fileno())
   self.io.syncdir(path.parent)
  else:
   # Publish an already-durable readable record atomically while release locks
   # exclude another updater. A process interruption cannot expose partial JSON.
   temporary=path.parent/('.activation-'+uuid.uuid4().hex)
   self.write_new(temporary,raw);temporary.chmod(0o644)
   with temporary.open('rb') as stream:os.fsync(stream.fileno())
   if os.path.lexists(path):raise Stop('R44_ACTIVATION_APPEARED_DURING_COMMIT')
   os.replace(temporary,path);self.io.syncdir(path.parent)
  print(json.dumps({'status':'R44_RUNTIME_ACTIVATED','runtime':RUNTIME,'rollback':'FORWARD_RECOVERY_AFTER_ACTIVATION'}),flush=True)


 def rollback_preflight(self,value,folder):
  if os.path.lexists(self.root/'runtime-control/activation.json') or os.path.lexists(folder/'runtime-activation-intent.json'):raise Stop('R44_RUNTIME_ACTIVATED_USE_FORWARD_RECOVERY')


 def recover_activation_preflight(self,value,folder):
  raw=encoded(self.activation_value(folder))
  expected=encoded({'schema':'r41-runtime-activation-intent-v1','prepared_sha256':sha(public(folder/'prepared.json')),'activation_sha256':sha(raw)})
  if public(folder/'runtime-activation-intent.json')!=expected:raise Stop('R44_ACTIVATION_RECOVERY_INTENT_INVALID')
  path=self.root/'runtime-control/activation.json'
  if os.path.lexists(path) and public(path)!=raw:raise Stop('R44_ACTIVATION_RECOVERY_RECORD_CHANGED')


 def preapply_rollback_guard(self,value,folder):self.rollback_preflight(value,folder)


 def health(self,mode,ids,value,folder):
  current=self.snapshot();baseline=json.loads(public(folder/'baseline.json'))
  require({s:v['id'] if v else None for s,v in current.items()}==ids,'R44_HEALTH_IDS_CHANGED')
  for service in ids:
   if service in CHANGED:self.static(service,current[service],mode,value,folder)
   else:self.unchanged(service,current[service],value,folder)
   if service!='db-migrate':require(current[service]['running'],'R44_SERVICE_NOT_READY')
  for service in baseline['source_pins']:self.live_sources(service,ids[service],mode,baseline)
  database=self.database(ids['gateway'])
  code=self.bundle('gateway-health.py').decode()
  enabled=mode==NEW and (self.root/'runtime-control/activation.json').exists()
  if enabled:require(public(self.root/'runtime-control/activation.json')==encoded(self.activation_value(folder)),'R44_ACTIVATION_CHANGED')
  health=json.loads(self.run('R44_GATEWAY_READY',['docker','exec',ids['gateway'],'python','-I','-B','-c',code,mode,'active' if enabled else 'held'],30))
  require(health.get('status')=='READY','R44_GATEWAY_HEALTH_FAILED')
  self.nginx_preflight();require(self.snapshot()==current,'R44_CHANGED_DURING_HEALTH')
  return {'status':'READY','checks':health,'database':database,'worker_runtime':RUNTIME,'runtime_admission_enabled':enabled if mode==NEW else True,
   'desktop_version':'0.13.0','desktop0140_publication_pending':True,'manager_restarted':True,'workers_disconnected':True,
   'history_format_changed':False,'schema_changed':False,'remembered_login_preserved':True,'persistent_login':True,'tokenkey_cached_between_logins':True,
   'idle_release_seconds':0,'maximum_hosts':0,'minimum_available_mib':2048,'tonggong_restarted':False,'business_generation_requested':False,'card_print_employee_acceptance':'PENDING'}
