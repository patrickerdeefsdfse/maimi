"""Actual ProjectSkillService/ProjectRuntime code with scoped in-memory ports."""
import asyncio,contextlib,importlib,json,sys,types
from pathlib import Path
from types import SimpleNamespace
import pytest
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[2]
BASE=ROOT/'services/gateway'
sys.path.insert(0,str(BASE))
import app
app.__path__=[str(BASE/'app'),*app.__path__]
package=types.ModuleType('maimi_projects');package.__path__=[str(BASE/'maimi_projects'),str(BASE/'maimi_projects')];sys.modules['maimi_projects']=package
service=importlib.import_module('maimi_projects.service');package.ProjectSkillService=service.ProjectSkillService
platform=importlib.import_module('app.project_platform')
SESSION='11111111-1111-4111-8111-111111111111';JOB='22222222-2222-4222-8222-222222222222'

def context(user='M00096389',selected=('service',),location='cloud'):
 return service.Context(user,SESSION,location,selected,'synthetic-lease')
class State:
 def __init__(self):self.values={}
 @contextlib.asynccontextmanager
 async def lock(self,ctx):yield
 async def get(self,ctx,key):return self.values.get(key)
 async def put(self,ctx,key,value):self.values[key]=value
class Port:
 def __init__(self):self.calls=[]
 def availability(self,ctx):return True,''
 async def invoke(self,ctx,action,args):self.calls.append((ctx,action,args));return {'protocol':'maimi-service-session-v2','connected':False,'server':None}
def skills(port=None):
 async def authorize(ctx):pass
 return service.ProjectSkillService(authorize=authorize,state=State(),files=object(),server_operations=port)

@pytest.mark.parametrize('user',['M00003307','M00001069','M00096389'])
def test_authorized_cloud_catalog_exposes_fifth_service_with_exact_schemas(user):
 svc=skills(Port());items=asyncio.run(svc.catalog(context(user)))
 assert len(items)==5
 item=next(item for item in items if item['plugin_key']=='service');assert item['status']=='ready'
 assert {tool['name'] for tool in item['tools']}=={'status','list_servers','disconnect','inspect_server','read_file','write_file','run_command'}

@pytest.mark.parametrize('user,location',[('M00000074','cloud'),('M00096389','local')])
def test_other_employee_or_local_has_no_service_catalog_and_cannot_invoke(user,location):
 port=Port();svc=skills(port);ctx=context(user,location=location)
 assert not any(item['plugin_key']=='service' for item in asyncio.run(svc.catalog(ctx)))
 with pytest.raises(service.ProjectFailure,match='not_authorized'):asyncio.run(svc.invoke(ctx,'service','list_servers',{}))
 assert port.calls==[]

def test_not_selected_or_unconfigured_cannot_invoke():
 port=Port();svc=skills(port)
 with pytest.raises(service.ProjectFailure,match='not_selected'):asyncio.run(svc.invoke(context(selected=()),'service','list_servers',{}))
 assert not port.calls
 svc=skills();items=asyncio.run(svc.catalog(context()));assert next(item for item in items if item['plugin_key']=='service')['status']=='unavailable'
 with pytest.raises(service.ProjectFailure,match='not_configured'):asyncio.run(svc.invoke(context(),'service','list_servers',{}))

def test_result_uses_existing_plain_project_card_and_untrusted_tool_value():
 port=Port();svc=skills(port);value=asyncio.run(svc.invoke(context(),'service','list_servers',{}))
 assert value['project_result']['connector']=='service' and value['untrusted_external_content'] is True
 assert value['service_result']['connected'] is False
 assert value['status']=='needs_input' and svc.state.values=={}

def access(user='M00096389'):
 return platform.Access(actor=SimpleNamespace(user_no=user),owner='a'*32,lease='synthetic-lease',session_id=SESSION,invocation_id='pi_'+'b'*43,
   permit={'selected':['service'],'source':'model','task_generation':7,'selection_revision':3,'deadline':1600,'connector':'service','action':'run_command'},worker_hash='c'*64,job_id=JOB,job_deadline=1500)

def test_real_runtime_service_supplies_trusted_scope_and_uses_same_access_for_authorize():
 runtime=object.__new__(platform.ProjectRuntime);runtime.options={};calls=[]
 async def authorize(value):calls.append(value)
 runtime.authorize=authorize;value=access();svc,ctx=runtime.service(value)
 assert svc.server_operations.scope()=={'session_id':SESSION,'invocation_id':value.invocation_id,'job_id':JOB,'generation':platform.digest(platform.encoded({'lease_hash':platform.digest(value.lease),'worker_hash':value.worker_hash,'epoch':value.epoch,'selection_revision':3})),'expires_at':1500}
 asyncio.run(svc.server_operations.authorize(ctx));assert calls==[value]

class Cursor:
 def __init__(self,db):self.db=db;self.row=None
 def execute(self,sql,args):
  self.db.sql.append((sql,args))
  if sql.startswith('UPDATE platform.project_jobs'):
   if "status='failed',encrypted_result=NULL" in sql:self.db.status='failed';self.db.code=args[0]
   elif 'RETURNING status' in sql:self.db.status='cancelled' if self.db.status=='cancelling' else args[0];self.db.code='project_task_cancelled' if self.db.status=='cancelled' else args[2]
   self.row={'status':self.db.status}
 def fetchone(self):return self.row
class Database:
 def __init__(self):self.sql=[];self.status='cancelling';self.code=None
 async def tx(self,access,fn):return fn(Cursor(self),{'fence':1})
 def seal(self,*args):return json.dumps(args[-1])

@pytest.mark.parametrize('unknown',[True,False])
def test_actual_run_cancellation_joins_remote_cleanup_and_persists_unknown(unknown):
 async def scenario():
  started=asyncio.Event()
  class Operation:
   async def invoke(self,*args):
    started.set()
    try:await asyncio.Future()
    except asyncio.CancelledError:
     await asyncio.sleep(0)
     if unknown:
      error=service.ProjectFailure(503,'project_service_stop_unknown');error.remote_status_unknown=True;raise error
     raise
  runtime=object.__new__(platform.ProjectRuntime);runtime.db=Database();runtime.service=lambda value:(Operation(),context())
  async def authorize(value):pass
  runtime.authorize=authorize;value=access()
  task=asyncio.create_task(runtime._run(value,'service','run_command',{},None,__import__('time').time()+30))
  await started.wait();value.cancelled.set();task.cancel();await task
  return runtime,value
 runtime,value=asyncio.run(scenario())
 assert runtime.db.status==('failed' if unknown else 'cancelled')
 assert runtime.db.code==('project_service_stop_unknown' if unknown else 'project_task_cancelled')
 if unknown:assert "status='failed',encrypted_result=NULL" in runtime.db.sql[-1][0] and 'CASE WHEN' not in runtime.db.sql[-1][0]

def test_unknown_job_value_preserves_fixed_flags_and_rejects_other_employee():
 runtime=object.__new__(platform.ProjectRuntime)
 row={'status':'failed','job_id':JOB,'connector':'service','encrypted_result':None,'error_code':'project_service_stop_unknown'}
 value=runtime.job_value(access(),row);assert value['remote_status_unknown'] is True and value['remote_stop_confirmed'] is False
 with pytest.raises(platform.IAMFailure,match='not_authorized'):runtime.job_value(access('M00000074'),row)

def test_root_run_warning_and_project_payload_fit_native_text_budget():
 class OutputPort(Port):
  async def invoke(self,*args):
   raw={'host_id':'server-10-4-240-14','exit_code':0,'output':'"\\\n'*20000,'truncated':False,'timed_out':False,'stop_confirmed':True,'high_privilege':True,'sandboxed':False}
   from maimi_projects.server_operations import model_view
   return model_view('run_command',raw)
 value=asyncio.run(skills(OutputPort()).invoke(context(),'service','run_command',{}))
 assert len(json.dumps(value,ensure_ascii=False))<80000
 assert value['service_result']['truncated'] is True
 assert any('不是隔离沙箱' in section.get('text','') for section in value['project_result']['sections'])
