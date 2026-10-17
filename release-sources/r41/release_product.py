"""Direct r40 successor. No old controller imports or recursive health replay."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib,json,os,re,stat,uuid
from pathlib import Path
from types import SimpleNamespace

ROOT=Path('/data/megmeet-dsh020-r41-20261008')
OLD,NEW='r40-remembered-login','r41-dsh020'
RELEASE='20261008-r41-dsh020'
LABEL='com.megmeet.runtime41.'
CHANGED=('gateway','personal-cloud-manager')
RUNTIME='0.2.0-rc.2'
CHAT_MODEL='deepseek-v4.1-flash'
CONTROL_TARGET='/opt/personal-cloud/runtime-r41'
CONFIG=Path('/etc/tonggong/ssd-alerts.json')
TARGETS={'/opt/personal-cloud/manager.py':'public/manager.py','/app/app/project_platform.py':'public/project_platform.py',
 '/app/app/personal_cloud_static.py':'public/personal_cloud_static.py',
 '/app/app/personal_model_proxy.py':'public/personal_model_proxy.py','/app/app/main.py':'public/main.py',
 '/app/app/personal_cloud.py':'public/personal_cloud.py','/app/app/personal_cloud_web.py':'public/personal_cloud_web.py',
 **{'/app/app/'+n:'public/'+n for n in ('iam_oauth.py','desktop_device_auth.py','desktop_auth.py','persistent_login.py')},
 **{'/app/maimi_projects/'+n:'public/maimi_projects/'+n for n in ('card_batch.py','card_renderer.py','disk_alerts.py','disk_semantics.py','monitor.py','monitor_projection.py','service.py')}}
EMPTY_OVERLAYS=('PERSONAL_CLOUD_WORKER_OVERLAY_DIR','PERSONAL_CLOUD_HISTORY_OVERLAY_DIR',
 'PERSONAL_CLOUD_WORKFLOW_OVERLAY_DIR','PERSONAL_CLOUD_RUNTIME_FOLLOWUP_OVERLAY_DIR',
 'PERSONAL_CLOUD_CANARY_WORKER_IMAGE','PERSONAL_CLOUD_CANARY_OWNERS','PERSONAL_CLOUD_CANARY_OVERLAY_DIR')
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(v):return (json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def public(path):
 path=Path(path)
 if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):raise Stop('R41_UNSAFE_PATH')
 if os.name=='posix':
  for parent in path.parents:
   info=parent.stat()
   if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022:raise Stop('R41_UNSAFE_PUBLIC_PARENT')
 a=path.stat()
 if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or a.st_size>8*1024**2:raise Stop('R41_PUBLIC_FILE_INVALID')
 if os.name=='posix' and (a.st_uid!=0 or stat.S_IMODE(a.st_mode)&0o022):raise Stop('R41_PUBLIC_PERMISSIONS')
 with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb') as f:
  b=os.fstat(f.fileno());raw=f.read(8*1024**2+1);c=os.fstat(f.fileno())
 identity=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns)
 if identity(a)!=identity(b) or identity(b)!=identity(c) or len(raw)!=a.st_size:raise Stop('R41_PUBLIC_FILE_CHANGED')
 return raw
def load(path,digest,name):
 raw=public(path)
 if sha(raw)!=digest:raise Stop('R41_CONTROL_PIN_CHANGED')
 m=SimpleNamespace(__name__=name,__file__=str(path));exec(compile(raw,str(path),'exec'),vars(m));return m

class Product:
 def __init__(self,root,manifest,manifest_sha):
  self.root=Path(root);self.manifest=manifest;self.manifest_sha=manifest_sha;self._hash_cache={};self._runtime_verified=set()
  if self.root!=ROOT or manifest.get('runtime')!=RUNTIME:raise Stop('R41_FIXED_RELEASE_REQUIRED')
  self.io=load(self.root/'release_io.py',manifest['files']['release_io.py']['sha256'],'r41_io')
  self.meta=load(self.root/'release_metadata.py',manifest['files']['release_metadata.py']['sha256'],'r41_metadata')
  self.direct=load(self.root/'direct_r40.py',manifest['files']['direct_r40.py']['sha256'],'r41_direct')
  self.runtime=load(self.root/'runtime_product.py',manifest['files']['runtime_product.py']['sha256'],'r41_runtime')
  self.base_pins=json.loads(public(self.root/'baseline-pins.json'))['pins']
  self.verify_bundle()
 read=staticmethod(public)
 @contextmanager
 def locked(self):
  with self.io.locked([*(Path(p) for p in self.meta.LOCK_PATHS),Path('/data/megmeet-remembered-login-20260930-r40/.apply.lock'),self.root/'.apply.lock']):yield
 def run(self,label,args,timeout=60):return self.io.run(label,args,timeout)
 def write_new(self,path,raw):self.io.write_new(path,raw)
 def replace(self,path,raw):self.io.replace(path,raw)
 def mkdir(self,path):self.io.mkdir(path)
 def unlink_marker(self,path):self.io.unlink_marker(path)
 def verify_bundle(self):
  for name,record in self.manifest['files'].items():
   if not isinstance(name,str) or name.startswith('/') or '..' in name.split('/') or '\\' in name:raise Stop('R41_MANIFEST_PATH_INVALID')
   path=self.root/name
   if any(p.is_symlink() for p in (path,*path.parents)):raise Stop('R41_BUNDLE_LINK')
   a=path.stat();identity=(a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns)
   if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or a.st_uid!=0 or stat.S_IMODE(a.st_mode)&0o022:raise Stop('R41_BUNDLE_PERMISSIONS')
   if self._hash_cache.get(name)==identity:continue
   h=hashlib.sha256()
   with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb') as f:
    b=os.fstat(f.fileno())
    for block in iter(lambda:f.read(1024**2),b''):h.update(block)
    c=os.fstat(f.fileno())
   if identity!=(b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns) or identity!=(c.st_dev,c.st_ino,c.st_size,c.st_mtime_ns):raise Stop('R41_BUNDLE_CHANGED_DURING_READ')
   if record!={'sha256':h.hexdigest(),'size_bytes':a.st_size}:raise Stop('R41_BUNDLE_CHANGED')
   self._hash_cache[name]=identity
 def evidence(self):return self.direct.evidence(public,lambda p:os.path.lexists(p))
 def snapshot(self):
  items,policies=self.meta.collect(self.run,allow_missing_services=CHANGED);self._policies=policies;return items
 def policy(self,identifier):
  if identifier not in self._policies:raise Stop('R41_UNKNOWN_POLICY')
  return deepcopy(self._policies[identifier])
 def nginx_metadata(self):return self.meta.nginx_metadata(self.run,public)
 def live_sources(self,service,identifier,mode):
  pins=dict(self.base_pins[service])
  if mode==NEW:pins.update({p:self.manifest['files'][n]['sha256'] for p,n in TARGETS.items() if ('personal-cloud-manager' if p.startswith('/opt/') else 'gateway')==service})
  code='import hashlib,json;from pathlib import Path;paths='+repr(list(pins))+';print(json.dumps({p:("UNSAFE" if Path(p).is_symlink() or Path(p).exists() and not Path(p).is_file() else hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else None) for p in paths}))'
  got=json.loads(self.run('R41_SOURCE_PINS',['docker','exec',identifier,'python','-I','-B','-c',code],20))
  if got!=pins:raise Stop('R41_LIVE_SOURCE_CHANGED')
 def baseline(self):
  self.verify_bundle();e=self.evidence();items=self.snapshot()
  if {s:v['id'] if v else None for s,v in items.items()}!=e['ids']:raise Stop('R41_CURRENT_IDENTITIES_CHANGED')
  for service,item in items.items():
   clean={k:v for k,v in item.items() if k!='runtime41'}
   expected={k:v for k,v in e['items'][service].items() if k!='runtime41'}
   if clean!=expected or item.get('runtime41',{'release':None,'transaction':None,'mode':None})!={'release':None,'transaction':None,'mode':None}:raise Stop('R41_BASELINE_IDENTITY_CHANGED')
   if self.policy(item['id'])!=e['policies'][service]:raise Stop('R41_BASELINE_POLICY_CHANGED')
   if service!='db-migrate' and not item['running']:raise Stop('R41_BASELINE_NOT_RUNNING')
  for service in self.base_pins:self.live_sources(service,items[service]['id'],OLD)
  if self.nginx_metadata()!=e['baseline']['nginx']:raise Stop('R41_NGINX_CHANGED')
  if self.evidence()['fingerprint']!=e['fingerprint'] or self.snapshot()!=items:raise Stop('R41_BASELINE_CHANGED_DURING_CHECK')
  self.runtime.check_base(self)
  return {'ids':e['ids']}
 def config_check(self):
  v=json.loads(public(CONFIG))
  if set(v)!={'warning_used_pct','critical_used_pct'} or any(type(v[k]) not in (int,float) for k in v) or not 0<v['warning_used_pct']<v['critical_used_pct']<100:raise Stop('R41_DISK_ALERT_CONFIGURATION_INVALID')
  return v
 def prepare_runtime(self):
  self.config_check();self.runtime.prepare(self)
  directory=self.root/'runtime-control'
  if not directory.exists():self.mkdir(directory);directory.chmod(0o755);self.io.syncdir(directory.parent)
  if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid!=0 or stat.S_IMODE(directory.stat().st_mode)!=0o755:raise Stop('R41_RUNTIME_CONTROL_DIRECTORY_INVALID')
  if (directory/'activation.json').exists():raise Stop('R41_RUNTIME_ALREADY_ACTIVATED')
 def capture(self,items,folder):
  if {s:v['id'] for s,v in items.items()}!=self.baseline()['ids']:raise Stop('R41_BASELINE_CHANGED')
  e=self.evidence();command=e['command'];files={command[i+1]:sha(public(Path(command[i+1]))) for i,a in enumerate(command[:-1]) if a=='-f'}
  for name,key in [('r40-transition.json','raw'),('desktop0120-transition.json','desktop_raw')]:self.write_new(folder/name,e[key])
  policies={s:self.policy(v['id']) for s,v in items.items()};sources={}
  for service,pins in self.base_pins.items():
   for mount in policies[service]['mounts']:
    if mount['destination'] in pins and mount['type']=='bind':
     if mount['rw'] is not False or sha(public(Path(mount['source'])))!=pins[mount['destination']]:raise Stop('R41_SOURCE_BIND_CHANGED')
     sources[mount['source']]=pins[mount['destination']]
  return {'items':items,'policies':policies,'command':command,'compose_files':files,'original_sources':sources,'r40_fingerprint':e['fingerprint'],
   'nginx':self.nginx_metadata(),'worker_image_id':self.runtime.candidate(self),'disk_alert_configuration':self.config_check()}
 def preserved(self,value,folder):
  self.verify_bundle();b=json.loads(public(folder/'baseline.json'));e=self.evidence()
  if e['fingerprint']!=b['r40_fingerprint'] or public(folder/'r40-transition.json')!=e['raw'] or public(folder/'desktop0120-transition.json')!=e['desktop_raw']:raise Stop('R41_PREDECESSOR_PROOF_CHANGED')
  if {s:v['id'] for s,v in b['items'].items()}!=e['ids'] or b['command']!=e['command']:raise Stop('R41_BASELINE_IDENTITY_CHANGED')
  if set(b['compose_files'])!={e['command'][i+1] for i,a in enumerate(e['command'][:-1]) if a=='-f'}:raise Stop('R41_COMPOSE_SCOPE_CHANGED')
  for path,digest in {**b['compose_files'],**b['original_sources']}.items():
   if sha(public(Path(path)))!=digest:raise Stop('R41_INHERITED_FILE_CHANGED')
  if b['policies']!=e['policies']:raise Stop('R41_BASELINE_POLICIES_CHANGED')
  if self.nginx_metadata()!=b['nginx']:raise Stop('R41_NGINX_CHANGED')
  self.runtime.verify_candidate(self,b['worker_image_id']);self.config_check();self.prepare_compose(value,folder,readonly=True)
 def unchanged(self,service,item,value,folder):
  b=json.loads(public(folder/'baseline.json'))
  if item!=b['items'][service] or self.policy(item['id'])!=b['policies'][service]:raise Stop('R41_PRESERVED_SERVICE_CHANGED')
 def runtime_environment(self,b):
  return {**{key:'' for key in EMPTY_OVERLAYS},'PERSONAL_CLOUD_IDLE_SECONDS':'0','PERSONAL_CLOUD_WORKER_IMAGE':b['worker_image_id'],'PERSONAL_CLOUD_WORKER_RUNTIME':RUNTIME,
   'PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE':CONTROL_TARGET+'/activation.json','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA':self.manifest_sha}
 def login_environment(self):
  return {'PERSONAL_LOGIN_ACTIVATION_FILE':CONTROL_TARGET+'/activation.json','PERSONAL_LOGIN_RELEASE_SHA':self.manifest_sha}
 def expected_policy(self,service,mode,b):
  expected=deepcopy(b['policies'][service])
  if mode==NEW:
   mounts={r['destination']:r for r in expected['mounts']}
   for target,name in TARGETS.items():
    if ('personal-cloud-manager' if target.startswith('/opt/') else 'gateway')==service:mounts[target]={'type':'bind','source':str(self.root/name),'destination':target,'rw':False}
   mounts[CONTROL_TARGET]={'type':'bind','source':str(self.root/'runtime-control'),'destination':CONTROL_TARGET,'rw':False}
   if service=='personal-cloud-manager':
    # The metadata reader only exposes its fixed public environment allowlist.
    expected['environment'].update({k:v for k,v in self.runtime_environment(b).items() if k in self.meta.PUBLIC_ENV})
   else:
    mounts[str(CONFIG)]={'type':'bind','source':str(CONFIG),'destination':str(CONFIG),'rw':False}
    expected['environment'].update(self.login_environment())
   expected['mounts']=sorted(mounts.values(),key=lambda r:r['destination'])
  elif mode!=OLD:raise Stop('R41_MODE_INVALID')
  return expected
 def static(self,service,item,mode,value,folder,original=False):
  b=json.loads(public(folder/'baseline.json'));before=b['items'][service]
  if any(item.get(k)!=before.get(k) for k in before if k not in ('id','running','runtime41')):raise Stop('R41_SERVICE_IDENTITY_CHANGED')
  if original and (item['id']!=before['id'] or item.get('runtime41')!=before.get('runtime41')):raise Stop('R41_ORIGINAL_SERVICE_CHANGED')
  if self.policy(item['id'])!=self.expected_policy(service,mode,b):raise Stop('R41_SERVICE_POLICY_CHANGED')
 def overlay(self,mode,value,folder):
  if mode not in (OLD,NEW):raise Stop('R41_MODE_INVALID')
  services={s:{'labels':{LABEL+'release':RELEASE,LABEL+'transaction':folder.name,LABEL+'mode':mode}} for s in CHANGED}
  if mode==NEW:
   for service in CHANGED:
    services[service]['volumes']=[{'type':'bind','source':str(self.root/n),'target':p,'read_only':True,'bind':{'create_host_path':False}} for p,n in TARGETS.items() if ('personal-cloud-manager' if p.startswith('/opt/') else 'gateway')==service]
   for service in CHANGED:
    services[service]['volumes'].append({'type':'bind','source':str(self.root/'runtime-control'),'target':CONTROL_TARGET,'read_only':True,'bind':{'create_host_path':False}})
   services['gateway']['volumes'].append({'type':'bind','source':str(CONFIG),'target':str(CONFIG),'read_only':True,'bind':{'create_host_path':False}})
   services['personal-cloud-manager']['environment']=self.runtime_environment(json.loads(public(folder/'baseline.json')))
   services['gateway']['environment']=self.login_environment()
  return {'services':services}
 def compose(self,mode,value,folder):return json.loads(public(folder/'baseline.json'))['command']+['-f',str(folder/('runtime-'+mode+'.compose.json'))]
 def prepare_compose(self,value,folder,readonly=False):
  for mode in (OLD,NEW):
   path=folder/('runtime-'+mode+'.compose.json');raw=encoded(self.overlay(mode,value,folder))
   if path.exists():
    if public(path)!=raw:raise Stop('R41_TRANSACTION_COMPOSE_CHANGED')
   elif readonly:raise Stop('R41_TRANSACTION_COMPOSE_MISSING')
   else:self.write_new(path,raw)
   if not readonly:self.run('R41_COMPOSE_PREFLIGHT',self.compose(mode,value,folder)+['config','--quiet'],30)
 def nginx_preflight(self):self.meta.nginx(self.run,'-t')
 def stop(self,service,identifier):self.run('R41_STOP_'+service.upper().replace('-','_'),['docker','stop','--timeout','20',identifier],35)
 def create(self,service,mode,value,folder):self.run('R41_CREATE_'+service.upper().replace('-','_'),self.compose(mode,value,folder)+['up','--no-deps','--no-start','--no-build','--pull','never','--force-recreate',service],120)
 def start(self,service,identifier,mode,value,folder):
  if self.snapshot()[service]['id']!=identifier:raise Stop('R41_CHANGED_BEFORE_START')
  self.run('R41_START_'+service.upper().replace('-','_'),self.compose(mode,value,folder)+['start','--wait','--wait-timeout','90',service],110)
  if self.snapshot()[service]['id']!=identifier:raise Stop('R41_CHANGED_DURING_START')
 def reload_nginx(self,value,folder):self.preserved(value,folder);self.meta.nginx(self.run,'-t');self.meta.nginx(self.run,'-s','reload')
 def post_start(self,value,folder):
  items=self.snapshot()
  if any(not items[s] or not items[s]['running'] for s in CHANGED):raise Stop('R41_START_NOT_READY')
 def activation_value(self,folder):
  b=json.loads(public(folder/'baseline.json'))
  return {'schema':'megmeet-runtime-activation-r41-v1','runtime':RUNTIME,'release_manifest_sha256':self.manifest_sha,'image_id':b['worker_image_id'],'status':'ACTIVE'}
 def activate(self,value,folder):
  path=self.root/'runtime-control/activation.json';raw=encoded(self.activation_value(folder))
  stage=json.loads(public(folder/'progress.json'))
  if stage.get('phase')!='FINISHED' or stage.get('target')!=NEW or public(self.root/'transition.json')!=public(folder/'applied.json'):raise Stop('R41_COMMIT_REQUIRED_BEFORE_ACTIVATION')
  intent_path=folder/'runtime-activation-intent.json'
  intent=encoded({'schema':'r41-runtime-activation-intent-v1','prepared_sha256':sha(public(folder/'prepared.json')),'activation_sha256':sha(raw)})
  if os.path.lexists(intent_path):
   if public(intent_path)!=intent:raise Stop('R41_ACTIVATION_INTENT_CHANGED')
  else:self.write_new(intent_path,intent)
  if path.exists():
   if public(path)!=raw:raise Stop('R41_ACTIVATION_CHANGED')
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
   if os.path.lexists(path):raise Stop('R41_ACTIVATION_APPEARED_DURING_COMMIT')
   os.replace(temporary,path);self.io.syncdir(path.parent)
  print(json.dumps({'status':'R41_RUNTIME_ACTIVATED','runtime':RUNTIME,'rollback':'FORWARD_RECOVERY_AFTER_ACTIVATION'}),flush=True)
 def rollback_preflight(self,value,folder):
  if os.path.lexists(self.root/'runtime-control/activation.json') or os.path.lexists(folder/'runtime-activation-intent.json'):raise Stop('R41_RUNTIME_ACTIVATED_USE_FORWARD_RECOVERY')
 def recover_activation_preflight(self,value,folder):
  raw=encoded(self.activation_value(folder))
  expected=encoded({'schema':'r41-runtime-activation-intent-v1','prepared_sha256':sha(public(folder/'prepared.json')),'activation_sha256':sha(raw)})
  if public(folder/'runtime-activation-intent.json')!=expected:raise Stop('R41_ACTIVATION_RECOVERY_INTENT_INVALID')
  path=self.root/'runtime-control/activation.json'
  if os.path.lexists(path) and public(path)!=raw:raise Stop('R41_ACTIVATION_RECOVERY_RECORD_CHANGED')
 def preapply_rollback_guard(self,value,folder):self.rollback_preflight(value,folder)
 def health(self,mode,ids,value,folder):
  current=self.snapshot()
  if {s:v['id'] if v else None for s,v in current.items()}!=ids:raise Stop('R41_HEALTH_IDS_CHANGED')
  for service in ids:
   if service in CHANGED:self.static(service,current[service],mode,value,folder)
   else:self.unchanged(service,current[service],value,folder)
   if service!='db-migrate' and not current[service]['running']:raise Stop('R41_SERVICE_NOT_READY')
  for service in self.base_pins:self.live_sources(service,ids[service],mode)
  code=public(self.root/'gateway-health.py').decode()
  enabled=mode==NEW and (self.root/'runtime-control/activation.json').exists()
  health=json.loads(self.run('R41_GATEWAY_READY',['docker','exec',ids['gateway'],'python','-I','-B','-c',code,mode,'active' if enabled else 'held'],30))
  if health.get('status')!='READY':raise Stop('R41_GATEWAY_HEALTH_FAILED')
  self.nginx_preflight()
  if self.snapshot()!=current:raise Stop('R41_CHANGED_DURING_HEALTH')
  return {'status':'ready','checks':health,'worker_runtime':RUNTIME if mode==NEW else '0.1.7-rc.2','runtime_admission_enabled':enabled if mode==NEW else True,
   'chat_model':CHAT_MODEL if mode==NEW else None,
   'desktop_version':'0.12.0','desktop0130_publication_pending':True,'manager_restarted':True,'workers_disconnected':True,
   'history_backup':'OWNER_SCOPED_BEFORE_NEW_RUNTIME_WRITE','history_snapshot_contains_user_data_output':False,
   'schema_changed':False,'remembered_login_preserved':True,'persistent_login':mode==NEW,'tokenkey_cached_between_logins':mode==NEW,'idle_release_seconds':0 if mode==NEW else 2400,'maximum_hosts':0,
   'minimum_available_mib':2048,'tonggong_restarted_by_cloud_release':False,'real_employee_acceptance':'PENDING'}
