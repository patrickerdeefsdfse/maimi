"""Direct R42 source proof and gateway-only R43 maintenance ports."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib,json,os,stat,uuid
from pathlib import Path

ROOT=Path('/data/megmeet-card-storage-r43-20261008')
R42=Path('/data/megmeet-skills-cards-r42-20261008')
OLD='r42-skills-cards';NEW='r43-card-storage';RELEASE='20261008-r43-card-storage'
TARGET='/app/app/project_platform.py';PUBLIC='public/app/project_platform.py'
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def require(value,code):
 if not value:raise Stop(code)
def public(path):
 path=Path(path)
 require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)),'R43_UNSAFE_READ_PATH')
 for parent in path.parents:
  info=parent.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not stat.S_IMODE(info.st_mode)&0o022,'R43_UNSAFE_READ_PARENT')
 before=path.stat();require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid==0 and not stat.S_IMODE(before.st_mode)&0o022 and before.st_size<=8*1024**2,'R43_PUBLIC_FILE_INVALID')
 identity=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns)
 with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as handle:
  opened=os.fstat(handle.fileno());raw=handle.read(8*1024**2+1);after=os.fstat(handle.fileno())
 require(identity(before)==identity(opened)==identity(after) and len(raw)==before.st_size,'R43_PUBLIC_FILE_CHANGED');return raw

class Product:
 def __init__(self,root,files,manifest,payload_sha,modules):
  self.root=Path(root);self.files=files;self.manifest=manifest;self.payload_sha=payload_sha;self.m=modules
  require(self.root==ROOT,'R43_FIXED_ROOT_REQUIRED')
  self.io=modules['release_io'];self.meta=modules['release_metadata'];self.direct=modules['direct_r42'];self.core=modules['direct_r42_core'];self.migration=modules['migration_product']
  self.pins=json.loads(files['direct-r42-pins.json']);self.migration_sha=self.migration.SQL_SHA
  self.meta.LABELS={**self.meta.LABELS,'runtime43':('com.megmeet.card-storage43.',{'gateway'})}
 read=staticmethod(public)
 exists=staticmethod(os.path.lexists)
 def verify_bundle(self):
  require(set(self.files)==set(self.manifest['files']),'R43_PACKAGE_SET_CHANGED')
  for name,raw in self.files.items():
   require(self.manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R43_PACKAGE_PIN_CHANGED')
   if self.root.exists():require(public(self.root/name)==raw,'R43_INSTALLED_PACKAGE_CHANGED')
  if self.root.exists():require(public(self.root/'release-manifest.json')==encoded(self.manifest),'R43_MANIFEST_CHANGED')
 def materialize(self):
  if not self.root.exists():
   stage=self.root.parent/('.r43-package-'+uuid.uuid4().hex)
   self.io.mkdir(stage);stage.chmod(0o755)
   for name,raw in self.files.items():
    path=stage/name
    for parent in reversed(path.parents):
     if parent==stage:continue
     if stage in parent.parents and not parent.exists():self.io.mkdir(parent);parent.chmod(0o755)
    self.io.write_new(path,raw)
    if name==PUBLIC:path.chmod(0o644)
   self.io.write_new(stage/'release-manifest.json',encoded(self.manifest))
   self.io.syncdir(stage)
   require(not os.path.lexists(self.root),'R43_PACKAGE_APPEARED_DURING_PREPARATION')
   os.rename(stage,self.root);self.io.syncdir(self.root.parent)
  self.verify_bundle()
  require(public(self.root/'release-manifest.json')==encoded(self.manifest),'R43_MANIFEST_CHANGED')
 @contextmanager
 def locked(self):
  paths=[*self.meta.LOCK_PATHS,Path('/data/megmeet-remembered-login-20260930-r40/.apply.lock'),Path('/data/megmeet-dsh020-r41-20261008/.apply.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0130-publication.lock'),R42/'.apply.lock',Path('/data/maimi-upload-20260921-r11/.desktop0140-publication.lock'),self.root/'.apply.lock']
  with self.io.locked(paths):yield
 def run(self,label,args,timeout=60):return self.io.run(label,args,timeout)
 def write_new(self,path,raw):self.io.write_new(path,raw)
 def replace(self,path,raw):self.io.replace(path,raw)
 def mkdir(self,path):self.io.mkdir(path)
 def unlink_marker(self,path):self.io.unlink_marker(path)
 def evidence(self):return self.direct.evidence(public,os.path.lexists,self.pins,self.core)
 def snapshot(self):
  items,policies=self.meta.collect(self.run,allow_missing_gateway=True);self._policies=policies
  for item in items.values():
   if item and item.get('runtime43')=={'release':None,'transaction':None,'mode':None}:item.pop('runtime43')
  return items
 def policy(self,identifier):return deepcopy(self._policies[identifier])
 def source_pins(self,service,identifier,pins):
  code='import json,hashlib;from pathlib import Path;ps='+repr(list(pins))+';print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() and not Path(p).is_symlink() else None for p in ps}))'
  result=json.loads(self.run('R43_SOURCE_PINS',['docker','exec',identifier,'python','-I','-B','-c',code],20))
  require(result==pins,'R43_LIVE_SOURCE_CHANGED');return result
 def nginx_metadata(self):return self.meta.nginx_metadata(self.run,public)
 def baseline(self):
  self.verify_bundle();proof=self.evidence();items=self.snapshot()
  self.direct.live(proof,items,self.policy,self.source_pins,self.nginx_metadata)
  self.assert_baseline_stable(proof);return proof
 def assert_baseline_stable(self,proof):
  require(self.evidence()['fingerprint']==proof['fingerprint'] and self.snapshot()==proof['items'],'R43_BASELINE_CHANGED_DURING_CHECK')
 def capture(self,proof):
  return {key:deepcopy(proof[key]) for key in ('items','policies','command','source_pins','fingerprint')}|{'nginx':deepcopy(proof['baseline']['nginx'])}
 def preserved(self,value,folder):
  self.verify_bundle();proof=self.evidence();baseline=json.loads(public(folder/'baseline.json'))
  require(baseline==self.capture(proof) and value['original_service_ids']==proof['ids'],'R43_PREDECESSOR_CHANGED')
  require(self.nginx_metadata()==baseline['nginx'],'R43_NGINX_CHANGED')
  self.prepare_compose(value,folder,readonly=True)
 def assert_preserved_services(self,items,value,folder):
  baseline=json.loads(public(folder/'baseline.json'))
  require(set(items)==set(baseline['items']),'R43_SERVICE_SET_CHANGED')
  for service,item in items.items():
   if service=='gateway':continue
   require(item==baseline['items'][service] and self.policy(item['id'])==baseline['policies'][service],'R43_PRESERVED_SERVICE_CHANGED')
  for service,pins in baseline['source_pins'].items():
   if service!='gateway':self.source_pins(service,items[service]['id'],pins)
 def expected_policy(self,mode,baseline):
  expected=deepcopy(baseline['policies']['gateway'])
  if mode==NEW:
   rows={row['destination']:row for row in expected['mounts']}
   require(TARGET in rows,'R43_OLD_SOURCE_BIND_MISSING')
   rows[TARGET]={'type':'bind','source':str(self.root/PUBLIC),'destination':TARGET,'rw':False}
   expected['mounts']=sorted(rows.values(),key=lambda row:row['destination'])
  else:require(mode==OLD,'R43_MODE_INVALID')
  return expected
 def static(self,item,mode,value,folder):
  baseline=json.loads(public(folder/'baseline.json'));original=baseline['items']['gateway']
  require({k:v for k,v in item.items() if k not in ('id','running','runtime43')}=={k:v for k,v in original.items() if k not in ('id','running','runtime43')},'R43_GATEWAY_STATIC_CHANGED')
  require(self.policy(item['id'])==self.expected_policy(mode,baseline),'R43_GATEWAY_POLICY_CHANGED')
  if mode==OLD:require('runtime43' not in item,'R43_OLD_GATEWAY_UNEXPECTED_LABEL')
  if item['running']:
   pins=deepcopy(baseline['source_pins']['gateway'])
   if mode==NEW:pins[TARGET]=self.manifest['files'][PUBLIC]['sha256']
   self.source_pins('gateway',item['id'],pins)
 def overlay(self,value,folder):
  return {'services':{'gateway':{'labels':{'com.megmeet.card-storage43.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':NEW}.items()},'volumes':[{'type':'bind','source':str(self.root/PUBLIC),'target':TARGET,'read_only':True,'bind':{'create_host_path':False}}]}}}
 def compose(self,folder):return json.loads(public(folder/'baseline.json'))['command']+['-f',str(folder/'gateway-r43.compose.json')]
 def prepare_compose(self,value,folder,readonly=False):
  path=folder/'gateway-r43.compose.json';raw=encoded(self.overlay(value,folder))
  if path.exists():require(public(path)==raw,'R43_COMPOSE_CHANGED')
  elif readonly:raise Stop('R43_COMPOSE_MISSING')
  else:self.write_new(path,raw)
  if not readonly:self.run('R43_COMPOSE_CHECK',self.compose(folder)+['config','--quiet'],30)
 def database(self,identifier,apply):
  # Both stages require a running, policy/source-verified gateway. This is
  # called only after baseline()/recognized(); no external credential copy.
  code=self.migration.remote_code(self.files['migration_product.py'],self.files['artifact_schema.py'],json.loads(self.files['artifact-schema-expected.json']),self.files['migrations/017_card_print_artifact_limit.sql'],apply,self.files['project_schema.py'],json.loads(self.files['project-schema-preserved.json']))
  raw=self.run('R43_DATABASE_'+('APPLY' if apply else 'CHECK'),['docker','exec',identifier,'python','-I','-B','-c',code],70)
  require(len(raw)<=8192,'R43_DATABASE_OUTPUT_LIMIT');result=json.loads(raw)
  if result.get('status')=='STOP':
   fixed={'R43_MIGRATION_LEDGER_SCOPE_CHANGED','R43_PREDECESSOR_MIGRATION_CHANGED','R43_MIGRATION_CHECKSUM_CHANGED','R43_MIGRATION_SCHEMA_LEDGER_MISMATCH','R43_MIGRATION_PAYLOAD_CHANGED','R43_CONNECTION_REQUIRES_AUTOCOMMIT','R43_ACTIVE_PROJECT_TASKS_WAIT_BEFORE_MAINTENANCE','R43_MIGRATION_NOT_VERIFIED','ARTIFACT_TABLE_ABSENT','ARTIFACT_SCHEMA_MISMATCH','R43_DATABASE_BUSY','R43_DATABASE_TIMEOUT','R43_DATABASE_UNAVAILABLE','R43_DATABASE_CONCURRENT_CHANGE','R43_DATABASE_CHECK_FAILED','R43_PRESERVED_SCHEMA_EXPECTATION_INVALID','R43_PRESERVED_PROJECT_SCHEMA_CHANGED'}
   raise Stop(result['reason'] if result.get('reason') in fixed else 'R43_DATABASE_CHECK_FAILED')
  require(result.get('schema')=='card-storage-r43-database-v1' and result.get('status') in ({'APPLIED','ALREADY_APPLIED'} if apply else {'VERIFIED'}) and result.get('mode') in {'ORIGINAL','MIGRATED'} and result.get('checksum')==self.migration_sha and result.get('preserved_project_tables_verified') is True,'R43_DATABASE_RESULT_INVALID')
  return result
 def nginx_preflight(self):self.meta.nginx(self.run,'-t')
 def stop(self,identifier):self.run('R43_GATEWAY_STOP',['docker','stop','--timeout','20',identifier],35)
 def create(self,value,folder):self.run('R43_GATEWAY_CREATE',self.compose(folder)+['up','--no-deps','--no-start','--no-build','--pull','never','--force-recreate','gateway'],120)
 def start(self,identifier,value,folder):
  require(self.snapshot()['gateway']['id']==identifier,'R43_GATEWAY_CHANGED_BEFORE_START')
  self.run('R43_GATEWAY_START',self.compose(folder)+['start','--wait','--wait-timeout','60','gateway'],80)
  require(self.snapshot()['gateway']['id']==identifier,'R43_GATEWAY_CHANGED_DURING_START')
 def reload_nginx(self,value,folder):self.preserved(value,folder);self.meta.nginx(self.run,'-t');self.meta.nginx(self.run,'-s','reload')
 def health(self,items,value,folder):
  require(items['gateway']['running'],'R43_GATEWAY_NOT_RUNNING');self.static(items['gateway'],NEW,value,folder);self.assert_preserved_services(items,value,folder)
  result=self.database(items['gateway']['id'],False);require(result['mode']=='MIGRATED','R43_DATABASE_NOT_MIGRATED')
  code="import json,urllib.request;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));r=op.open('http://127.0.0.1:8080/ready',timeout=5);b=r.read(8193);assert r.status==200 and len(b)<=8192 and json.loads(b)=={'status':'ready','database':'ready','redis':'ready'};print('READY')"
  require(self.run('R43_GATEWAY_READY',['docker','exec',items['gateway']['id'],'python','-I','-B','-c',code],12).strip()==b'READY','R43_GATEWAY_HEALTH_NOT_READY')
  self.preserved(value,folder)
  return {'status':'READY','database':result,'manager_and_workers':'UNCHANGED','employee_card_acceptance':'PENDING','business_generation_requested':False}
