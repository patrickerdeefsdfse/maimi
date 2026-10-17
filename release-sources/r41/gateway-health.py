"""Read-only technical readiness through the consumer gateway; no employee actions."""
import json,os,re,sys,urllib.request
from pathlib import Path
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def get(url,headers=None):
 with opener.open(urllib.request.Request(url,headers=headers or {}),timeout=5) as r:
  raw=r.read(8193)
  if r.status!=200 or len(raw)>8192:raise ValueError()
  return json.loads(raw)
try:
 mode,activation=sys.argv[1:]
 if mode not in ('r40-remembered-login','r41-dsh020') or activation not in ('active','held'):raise ValueError()
 if get('http://127.0.0.1:8080/ready')!={'status':'ready','database':'ready','redis':'ready'}:raise ValueError()
 base=os.getenv('PERSONAL_CLOUD_MANAGER_URL','http://personal-cloud-manager:8092')
 if not re.fullmatch(r'http://[a-z0-9-]+:8092',base):raise ValueError()
 secret=Path(os.environ['PERSONAL_CLOUD_MANAGER_SECRET_FILE']).read_text().strip()
 if len(secret)<32:raise ValueError()
 manager=get(base+'/v1/status',{'Authorization':'Bearer '+secret})
 expected={'status':'ready','maximum_hosts':0,'queue_limit':128,'host_memory_limit_mib':1536,'idle_release_seconds':2400,'general_maximum_hosts':0,'reserved_slots':1,'memory_guard_enabled':True,'minimum_available_mib':2048,'host_slot_limit_enabled':False}
 if mode=='r41-dsh020':expected.update(idle_release_seconds=0,worker_runtime='0.2.0-rc.2',runtime_admission_enabled=activation=='active')
 if any(type(manager.get(k)) is not type(v) or manager.get(k)!=v for k,v in expected.items()):raise ValueError()
 capabilities=get('http://127.0.0.1:8080/v1/desktop/auth/session?capabilities=remembered-login-v1')
 if capabilities!={'remembered_login':True,'protocol':1,'device_ttl_seconds':0 if mode=='r41-dsh020' else 2592000,'web_ttl_seconds':0 if mode=='r41-dsh020' else 604800,'retry_window_seconds':120}:raise ValueError()
 if mode=='r41-dsh020':
  sys.path.insert(0,'/app')
  from app.personal_model_proxy import allowed_models,DEFAULT_MODEL
  if DEFAULT_MODEL!='deepseek-v4.1-flash' or allowed_models()!=(DEFAULT_MODEL,):raise ValueError()
  from app.persistent_login import enabled as persistent_enabled
  if persistent_enabled() is not (activation=='active'):raise ValueError()
 print(json.dumps({'status':'READY','database':'ready','redis':'ready','manager_policy':'VERIFIED','remembered_login':'VERIFIED',
  'runtime_admission_checked':activation if mode=='r41-dsh020' else 'LEGACY',
  'persistent_login_admission_checked':activation if mode=='r41-dsh020' else 'LEGACY',
  'chat_model_policy':'deepseek-v4.1-flash' if mode=='r41-dsh020' else 'LEGACY','business_requests':0,'credential_values_output':False}))
except Exception:
 print(json.dumps({'status':'FAILED','credential_values_output':False,'raw_response_output':False}));raise SystemExit(1)
