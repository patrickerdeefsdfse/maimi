"""Direct R48 proof and two-service R52 maintenance; no runtime image rebuild."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib,json,os,stat,uuid
from pathlib import Path
ROOT=Path('/data/megmeet-logistics-eta-r52-20261014')
R48=Path('/data/megmeet-skills-logistics-r48-20261012')
OLD='r48-skills-logistics';NEW='r52-logistics-eta';RELEASE='20261014-r52-logistics-eta'
CHANGED=('gateway','personal-cloud-manager')
TARGETS={'gateway':{'/app/app/personal_cloud_plugin_static.py':'public/app/personal_cloud_plugin_static.py','/app/app/logistics_query.py':'public/app/logistics_query.py','/app/app/kuaidi100_mcp.py':'public/app/kuaidi100_mcp.py'},'personal-cloud-manager':{'/opt/personal-cloud/manager.py':'public/manager/manager.py'}}
NATIVE=str(ROOT/'public/native')
NATIVE_FILES=('cloud-brand-client.js','logistics-index.mjs','logistics-SKILL.md','runtime020-source-pins.json')
class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def require(value,code):
 if not value:raise Stop(code)
def public(path):
 path=Path(path)
 require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)),'R52_UNSAFE_READ_PATH')
 for parent in path.parents:
  info=parent.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not stat.S_IMODE(info.st_mode)&0o022,'R52_UNSAFE_READ_PARENT')
 before=path.stat();require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid==0 and not stat.S_IMODE(before.st_mode)&0o022 and before.st_size<=8*1024**2,'R52_PUBLIC_FILE_INVALID')
 identity=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns)
 with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as handle:
  opened=os.fstat(handle.fileno());raw=handle.read(8*1024**2+1);after=os.fstat(handle.fileno())
 require(identity(before)==identity(opened)==identity(after) and len(raw)==before.st_size,'R52_PUBLIC_FILE_CHANGED');return raw

class Product:
 def __init__(self,root,files,manifest,payload_sha,modules):
  self.root=Path(root);self.files=files;self.manifest=manifest;self.payload_sha=payload_sha;self.m=modules
  require(self.root==ROOT,'R52_FIXED_ROOT_REQUIRED')
  self.io=modules['release_io'];self.meta=modules['release_metadata'];self.direct=modules['direct_r48']
  self.private=modules['release_private_policy'];self._private_identity=None
  self.pins=json.loads(files['direct-r48-pins.json'])
  self.meta.LABELS={**self.meta.LABELS,'runtime43':('com.megmeet.card-storage43.',{'gateway'}),'runtime44':('com.megmeet.runtime44.',set(CHANGED)),'runtime45':('com.megmeet.runtime45.',set(CHANGED)),'runtime46':('com.megmeet.logistics-phone46.',{'gateway'}),'runtime47':('com.megmeet.fast-entry47.',set(CHANGED)),'runtime48':('com.megmeet.skills-logistics48.',set(CHANGED)),'runtime52':('com.megmeet.logistics-eta52.',set(CHANGED))}
 read=staticmethod(public)
 exists=staticmethod(os.path.lexists)
 def verify_bundle(self):
  require(set(self.files)==set(self.manifest['files']),'R52_PACKAGE_SET_CHANGED')
  for name,raw in self.files.items():
   require(self.manifest['files'][name]=={'sha256':sha(raw),'size_bytes':len(raw)},'R52_PACKAGE_PIN_CHANGED')
   if self.root.exists():require(public(self.root/name)==raw,'R52_INSTALLED_PACKAGE_CHANGED')
  if self.root.exists():require(public(self.root/'release-manifest.json')==encoded(self.manifest),'R52_MANIFEST_CHANGED')
 def materialize(self):
  if not self.root.exists():
   stage=self.root.parent/('.r52-package-'+uuid.uuid4().hex)
   self.io.mkdir(stage);stage.chmod(0o755)
   for name,raw in self.files.items():
    path=stage/name
    for parent in reversed(path.parents):
     if parent==stage:continue
     if stage in parent.parents and not parent.exists():self.io.mkdir(parent);parent.chmod(0o755)
    self.io.write_new(path,raw)
    if name.startswith('public/'):path.chmod(0o644)
   self.io.write_new(stage/'release-manifest.json',encoded(self.manifest));self.io.syncdir(stage)
   require(not os.path.lexists(self.root),'R52_PACKAGE_APPEARED_DURING_PREPARATION')
   os.rename(stage,self.root);self.io.syncdir(self.root.parent)
  self.verify_bundle()
 @contextmanager
 def locked(self):
  # All r49/r50 maintenance uses these common locks; their unapplied roots need not exist.
  roots=['/data/megmeet-remembered-login-20260930-r40','/data/megmeet-dsh020-r41-20261008','/data/megmeet-skills-cards-r42-20261008','/data/megmeet-card-storage-r43-20261008','/data/megmeet-card-photos-r44-20261009','/data/megmeet-logistics-r45-20261009','/data/megmeet-logistics-phone-r46-20261009','/data/megmeet-fast-entry-r47-20261009',str(R48)]
  paths=[*self.meta.LOCK_PATHS,*[Path(p)/'.apply.lock' for p in roots],Path('/data/maimi-upload-20260921-r11/.desktop0130-publication.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0140-publication.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0150-publication.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0160-publication.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0161-publication.lock'),Path('/data/maimi-upload-20260921-r11/.desktop0162-publication.lock'),self.root/'.apply.lock']
  with self.io.locked(paths):yield
 def run(self,label,args,timeout=60):return self.io.run(label,args,timeout)
 def write_new(self,path,raw):self.io.write_new(path,raw)
 def replace(self,path,raw):self.io.replace(path,raw)
 def mkdir(self,path):self.io.mkdir(path)
 def unlink_marker(self,path):self.io.unlink_marker(path)
 def evidence(self):return self.direct.evidence(public,os.path.lexists,self.pins)
 def snapshot(self):
  items,policies=self.meta.collect(self.run,allow_missing_services=CHANGED);self._policies=policies
  for item in items.values():
   if item and item.get('runtime52')=={'release':None,'transaction':None,'mode':None}:item.pop('runtime52')
  return items
 def policy(self,identifier):return deepcopy(self._policies[identifier])
 def source_pins(self,service,identifier,pins):
  code='import json,hashlib;from pathlib import Path;ps='+repr(list(pins))+';print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() and not Path(p).is_symlink() else None for p in ps}))'
  result=json.loads(self.run('R52_SOURCE_PINS',['docker','exec',identifier,'python','-I','-B','-c',code],20))
  require(result==pins,'R52_LIVE_SOURCE_CHANGED');return result
 def nginx_metadata(self):return self.meta.nginx_metadata(self.run,public)
 def private_preflight(self,expected=None):
  current=self.private.verify(expected if expected is not None else self._private_identity)
  self._private_identity=current;return deepcopy(current)
 def transaction_private(self,folder):
  baseline=json.loads(public(folder/'baseline.json'))
  require('private_policy_file' in baseline,'R52_PRIVATE_POLICY_IDENTITY_MISSING')
  return self.private_preflight(baseline['private_policy_file'])
 def baseline(self):
  self.verify_bundle();proof=self.evidence();items=self.snapshot()
  self.direct.live(proof,items,self.policy,self.source_pins,self.nginx_metadata)
  # Validate all candidate mount/environment assumptions while services are
  # still untouched; a policy mismatch must not first surface after creation.
  for service in CHANGED:self.expected_policy(service,NEW,proof)
  self.private_preflight()
  result=json.loads(self.run('R52_PREDECESSOR_READY',['docker','exec',items['gateway']['id'],'python','-I','-B','-c',self.files['gateway-health.py'].decode(),OLD],30))
  require(result.get('status')=='READY','R52_PREDECESSOR_NOT_READY')
  self.assert_baseline_stable(proof);return proof
 def assert_baseline_stable(self,proof):
  self.private_preflight()
  require(self.evidence()['fingerprint']==proof['fingerprint'],'R52_BASELINE_PROOF_CHANGED')
  items=self.snapshot();self.direct.live(proof,items,self.policy,self.source_pins,self.nginx_metadata)
 def capture(self,proof):return {**{key:deepcopy(proof[key]) for key in ('items','policies','command','source_pins','fingerprint','compose_files','nginx')},'private_policy_file':self.private_preflight()}
 def preserved(self,value,folder):
  self.verify_bundle();proof=self.evidence();baseline=json.loads(public(folder/'baseline.json'))
  self.transaction_private(folder)
  require(baseline==self.capture(proof) and value['original_service_ids']==proof['ids'],'R52_PREDECESSOR_CHANGED')
  require(self.nginx_metadata()==baseline['nginx'],'R52_NGINX_CHANGED');self.prepare_compose(value,folder,readonly=True)
 def assert_preserved_services(self,items,value,folder):
  baseline=json.loads(public(folder/'baseline.json'));require(set(items)==set(baseline['items']),'R52_SERVICE_SET_CHANGED')
  for service,item in items.items():
   if service in CHANGED:continue
   require(item==baseline['items'][service] and self.policy(item['id'])==baseline['policies'][service],'R52_PRESERVED_SERVICE_CHANGED')
  for service,pins in baseline['source_pins'].items():
   if service not in CHANGED:self.source_pins(service,items[service]['id'],pins)
 def expected_policy(self,service,mode,baseline):
  expected=deepcopy(baseline['policies'][service])
  if mode==NEW:
   rows={row['destination']:row for row in expected['mounts']}
   for target,name in TARGETS[service].items():
    require(target in rows,'R52_OLD_SOURCE_BIND_MISSING')
    rows[target]={'type':'bind','source':str(self.root/name),'destination':target,'rw':False}
   if service=='personal-cloud-manager':
    require(NATIVE not in rows,'R52_OVERLAY_ALREADY_PRESENT')
    rows[NATIVE]={'type':'bind','source':NATIVE,'destination':NATIVE,'rw':False}
    require(expected['environment'].get('PERSONAL_CLOUD_CLOSED_SECONDS')=='7200' and expected['environment'].get('PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR')==str(R48/'public/native'),'R52_R48_POLICY_CHANGED')
    expected['environment']['PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR']=NATIVE
   expected['mounts']=sorted(rows.values(),key=lambda row:row['destination'])
   if service=='gateway':
    require(expected.get('user')=='10001:10001','R52_PRIVATE_POLICY_GATEWAY_USER_CHANGED')
    expected=self.private.expected_gateway_policy(expected)
  else:require(mode==OLD,'R52_MODE_INVALID')
  return expected
 def static(self,service,item,mode,value,folder):
  baseline=json.loads(public(folder/'baseline.json'));original=baseline['items'][service]
  ignore=('id','running','runtime52')
  require({k:v for k,v in item.items() if k not in ignore}=={k:v for k,v in original.items() if k not in ignore},'R52_SERVICE_STATIC_CHANGED')
  require(self.policy(item['id'])==self.expected_policy(service,mode,baseline),'R52_SERVICE_POLICY_CHANGED')
  if mode==OLD:require('runtime52' not in item,'R52_OLD_SERVICE_UNEXPECTED_LABEL')
  if item['running']:
   pins=deepcopy(baseline['source_pins'][service])
   if mode==NEW:
    for target,name in TARGETS[service].items():pins[target]=self.manifest['files'][name]['sha256']
    if service=='personal-cloud-manager':
     for name,meta in self.manifest['files'].items():
      if name.startswith('public/native/'):pins[NATIVE+'/'+name[len('public/native/'):]]=meta['sha256']
   self.source_pins(service,item['id'],pins)
 def overlay(self,value,folder):
  services={}
  for service in CHANGED:
   row={'labels':{'com.megmeet.logistics-eta52.'+k:v for k,v in {'release':RELEASE,'transaction':folder.name,'mode':NEW}.items()},'volumes':[{'type':'bind','source':str(self.root/name),'target':target,'read_only':True,'bind':{'create_host_path':False}} for target,name in TARGETS[service].items()]}
   if service=='personal-cloud-manager':
    row['environment']={'PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR':NATIVE}
    row['volumes'].append({'type':'bind','source':NATIVE,'target':NATIVE,'read_only':True,'bind':{'create_host_path':False}})
   else:
    row['environment']={self.private.ENABLED_ENV:'true'}
    row['volumes'].append(self.private.gateway_mount())
   services[service]=row
  return {'services':services}
 def compose(self,folder):return json.loads(public(folder/'baseline.json'))['command']+['-f',str(folder/'logistics-eta-r52.compose.json')]
 def prepare_compose(self,value,folder,readonly=False):
  path=folder/'logistics-eta-r52.compose.json';raw=encoded(self.overlay(value,folder))
  if path.exists():require(public(path)==raw,'R52_COMPOSE_CHANGED')
  elif readonly:raise Stop('R52_COMPOSE_MISSING')
  else:self.write_new(path,raw)
  if not readonly:self.run('R52_COMPOSE_CHECK',self.compose(folder)+['config','--quiet'],30)
 def nginx_preflight(self):self.meta.nginx(self.run,'-t')
 def stop(self,service,identifier):
  require(self._private_identity is not None,'R52_PRIVATE_POLICY_NOT_CHECKED');self.private_preflight()
  self.run('R52_STOP_'+service.upper().replace('-','_'),['docker','stop','--timeout','20',identifier],35)
 def create(self,service,value,folder):
  self.transaction_private(folder)
  self.run('R52_CREATE_'+service.upper().replace('-','_'),self.compose(folder)+['up','--no-deps','--no-start','--no-build','--pull','never','--force-recreate',service],120)
 def start(self,service,identifier,value,folder):
  self.transaction_private(folder)
  require(self.snapshot()[service]['id']==identifier,'R52_SERVICE_CHANGED_BEFORE_START')
  self.run('R52_START_'+service.upper().replace('-','_'),self.compose(folder)+['start','--wait','--wait-timeout','90',service],110)
  require(self.snapshot()[service]['id']==identifier,'R52_SERVICE_CHANGED_DURING_START')
 def reload_nginx(self,value,folder):self.preserved(value,folder);self.meta.nginx(self.run,'-t');self.meta.nginx(self.run,'-s','reload')
 def health(self,items,value,folder):
  private_identity=self.transaction_private(folder)
  for service in CHANGED:
   require(items[service]['running'],'R52_SERVICE_NOT_RUNNING');self.static(service,items[service],NEW,value,folder)
  self.assert_preserved_services(items,value,folder)
  mounted=json.loads(self.run('R52_PRIVATE_POLICY_MOUNT',['docker','exec',items['gateway']['id'],'python','-I','-B','-c',self.private.container_identity_code(private_identity)],20))
  require(mounted=={'matches':True},'R52_PRIVATE_POLICY_MOUNT_CHANGED')
  result=json.loads(self.run('R52_GATEWAY_READY',['docker','exec',items['gateway']['id'],'python','-I','-B','-c',self.files['gateway-health.py'].decode(),NEW],30))
  require(result.get('status')=='READY','R52_HEALTH_NOT_READY');self.preserved(value,folder)
  return {'status':'READY','checks':result,'closed_retention_seconds':7200,'persistent_login':True,'maximum_hosts':0,'database_changed':False,'images_changed':False,'employee_acceptance':'PENDING','business_requests':0}
