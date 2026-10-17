"""Ticket binding and project runtime admission use synthetic local state."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from test_project_service import platform,service,access,context,skills,Port,SESSION,JOB,Database,Cursor as ExistingCursor

FIELDS={'host':'10.4.240.14','port':'22','username':'root','password':'never_persist_this_synthetic_secret'}

def test_service_login_card_only_has_two_visible_fields_and_hidden_password():
 value=asyncio.run(skills(Port()).invoke(context(),'service','status',{}))
 action=value['project_result']['actions'][0]
 assert action['form']=='service-login' and action['kind']=='secure_form'
 assert [item['name'] for item in action['fields']]==['host','password']
 assert action['fields'][-1]['type']=='password'
 assert {row['name']:row.get('default') for row in action['fields']}=={'host':None,'password':None}

def test_service_method_enforces_human_and_does_not_persist_live_status():
 class LoginPort(Port):
  async def secure_connect(self,ctx,fields):
   self.calls.append((ctx.source,deepcopy(fields)))
   return {'protocol':'maimi-service-session-v2','connected':True,'server':{'host_id':JOB,'label':'synthetic target','actions':[],'high_privilege':True}}
 port=LoginPort();svc=skills(port)
 with pytest.raises(service.ProjectFailure,match='human_authorization_required'):asyncio.run(svc.secure_connect(context(),deepcopy(FIELDS)))
 human=service.Context('M00096389',SESSION,'cloud',('service',),'synthetic-lease',source='human')
 result=asyncio.run(svc.secure_connect(human,deepcopy(FIELDS)))
 assert result['service_result']['connected'] and svc.state.values=={}
 assert FIELDS['password'] not in json.dumps(result)
 assert [a['action'] for a in result['project_result']['actions']]==['status','disconnect']

class Store:
 def __init__(self):self.values={}
 async def pop(self,kind,user,ticket):return self.values.pop((kind,user,ticket),None)
class Connector:
 def __init__(self):self.store=Store()
 def seal(self,user,value):return json.dumps(value)
 def unseal(self,user,value):return json.loads(value)
class Runtime:
 def __init__(self,value):self.access=value;self.connector=Connector();self.calls=[]
 async def open(self,lease,sid,invocation,**kwargs):
  assert lease==self.access.lease and sid==self.access.session_id and invocation==self.access.invocation_id
  return self.access
 def service(self,value):return object(),object()
 async def submit(self,value,body,*,fields):
  self.calls.append((deepcopy(body),deepcopy(fields)))
  return {'status':'running','job_id':JOB}

def setup(monkeypatch,connector='service',action='secure_connect'):
 value=access();value.permit.update(source='human',connector=connector,action=action,selected=[connector]);value.epoch='synthetic-epoch'
 runtime=Runtime(value);monkeypatch.setattr(platform,'runtime',lambda:runtime)
 async def put(c,actor,sid,kind,ticket,sealed):c.store.values[(kind,actor.user_no,ticket)]=sealed
 monkeypatch.setattr(platform,'ephemeral_put',put)
 return runtime,value
def base(value):return {'native_session_id':value.session_id,'invocation_id':value.invocation_id}
async def ticket(value,form):return await platform.dispatch(value.lease,'form-ticket',{**base(value),'form':form})
async def submit(value,value_ticket,form='service-login'):
 body={**base(value),'ticket':value_ticket,'fields':deepcopy(FIELDS),'request_id':JOB}
 if form is not None:body['form']=form
 return await platform.dispatch(value.lease,'secure-form',body)

def test_service_form_ticket_binds_full_route_and_is_single_use(monkeypatch):
 runtime,value=setup(monkeypatch)
 async def run():
  result=await ticket(value,'service-login');raw=next(iter(runtime.connector.store.values.values()));binding=json.loads(raw)
  assert set(binding)=={'lease_hash','session_id','selection_revision','epoch','worker_hash','form','connector','action'}
  assert binding['form']=='service-login' and binding['connector']=='service' and binding['action']=='secure_connect'
  assert FIELDS['password'] not in raw
  await submit(value,result['ticket'])
  with pytest.raises(platform.IAMFailure,match='ticket_invalid'):await submit(value,result['ticket'])
 asyncio.run(run());assert len(runtime.calls)==1

@pytest.mark.parametrize('mutation',['form','connector','action','selection','epoch','worker','lease'])
def test_changed_ticket_binding_cannot_submit_credentials(monkeypatch,mutation):
 runtime,value=setup(monkeypatch)
 async def run():
  result=await ticket(value,'service-login')
  if mutation=='form':
   with pytest.raises(platform.IAMFailure):await submit(value,result['ticket'],'monitor-login')
  else:
   if mutation=='connector':value.permit.update(connector='infrastructure-monitor',action='secure_login',selected=['infrastructure-monitor'])
   elif mutation=='action':value.permit['action']='run_command'
   elif mutation=='selection':value.permit['selection_revision']+=1
   elif mutation=='epoch':value.epoch='new'
   elif mutation=='worker':value.worker_hash='d'*64
   elif mutation=='lease':value.lease='replacement-lease'
   with pytest.raises(platform.IAMFailure):await submit(value,result['ticket'],'monitor-login' if mutation=='connector' else 'service-login')
 asyncio.run(run());assert not runtime.calls

@pytest.mark.parametrize('mutation',['model','other_user','not_selected','missing_service_form'])
def test_model_or_unauthorized_actor_cannot_get_or_use_human_form(monkeypatch,mutation):
 runtime,value=setup(monkeypatch)
 async def run():
  result=await ticket(value,'service-login')
  if mutation=='model':value.permit['source']='model'
  elif mutation=='other_user':value.actor=SimpleNamespace(user_no='M00000074')
  elif mutation=='not_selected':value.permit['selected']=[]
  with pytest.raises(platform.IAMFailure):await submit(value,result['ticket'],None if mutation=='missing_service_form' else 'service-login')
 asyncio.run(run());assert not runtime.calls

def test_existing_monitor_route_still_works_without_submit_form(monkeypatch):
 runtime,value=setup(monkeypatch,'infrastructure-monitor','secure_login')
 async def run():
  result=await ticket(value,'monitor-login');await submit(value,result['ticket'],None)
 asyncio.run(run())
 assert runtime.calls[0][0]['connector']=='infrastructure-monitor' and runtime.calls[0][0]['action']=='secure_login'

def test_connection_generation_is_stable_across_human_and_next_model_task():
 runtime=object.__new__(platform.ProjectRuntime);runtime.options={}
 async def authorize(value):pass
 runtime.authorize=authorize
 human=access();human.permit.update(source='human',task_generation='form-1',action='secure_connect');human.epoch='epoch'
 model=deepcopy(human);model.permit.update(source='model',task_generation=881,action='run_command');model.invocation_id='pi_'+'d'*43;model.job_id='33333333-3333-4333-8333-333333333333'
 first=runtime.service(human)[0].server_operations.scope();second=runtime.service(model)[0].server_operations.scope()
 assert first['generation']==second['generation'] and first['invocation_id']!=second['invocation_id'] and first['job_id']!=second['job_id']
 for field in ['lease','worker','epoch','selection']:
  changed=deepcopy(model)
  if field=='lease':changed.lease='new'
  elif field=='worker':changed.worker_hash='x'*64
  elif field=='epoch':changed.epoch='changed'
  else:changed.permit['selection_revision']+=1
  assert runtime.service(changed)[0].server_operations.scope()['generation']!=second['generation']

def test_runtime_submit_secure_fields_fingerprint_is_ticket_only_and_not_secret(monkeypatch):
 class Cursor:
  def __init__(self):self.row=None;self.record=[]
  def execute(self,sql,args):
   self.record.append((sql,args))
   if 'count(*) AS total,count(*) FILTER' in sql:self.row={'total':0,'owner':0,'session':0}
   elif 'count(*) AS total' in sql:self.row={'total':0}
   elif 'INSERT INTO platform.project_jobs' in sql:self.row={'job_id':JOB}
   else:self.row=None
  def fetchone(self):return self.row
 class DB:
  def __init__(self):self.cursor=Cursor()
  async def tx(self,access,fn):return fn(self.cursor,{})
 async def run():
  runtime=object.__new__(platform.ProjectRuntime);runtime.db=DB();runtime.tasks={};runtime.process_id='synthetic';runtime.heartbeat_task=None
  async def noop(*args,**kwargs):pass
  runtime.authorize=noop;runtime.reap_interrupted=noop;runtime._heartbeat=noop
  runtime.connector=SimpleNamespace(store=SimpleNamespace(redis=SimpleNamespace(set=noop)))
  seen=[]
  async def operation(*args):seen.append(args)
  runtime._run=operation
  value=access();value.permit.update(source='human',action='secure_connect',deadline=__import__('time').time()+100)
  body={'connector':'service','action':'secure_connect','request_id':JOB,'ticket':'t'*43}
  fields=deepcopy(FIELDS)
  await runtime.submit(value,body,fields=fields)
  await asyncio.gather(*[task for task,_ in runtime.tasks.values()]);await runtime.heartbeat_task
  inserts=[args for sql,args in runtime.db.cursor.record if 'INSERT INTO platform.project_jobs' in sql]
  assert len(inserts)==1
  fingerprint=platform.digest(platform.encoded({'connector':'service','action':'secure_connect','arguments':{'ticket':'t'*43}}))
  assert inserts[0][8]==fingerprint
  assert FIELDS['password'] not in repr(runtime.db.cursor.record) and FIELDS['host'] not in repr(runtime.db.cursor.record)
  assert seen[0][4] is fields
 asyncio.run(run())

def test_actual_runtime_clears_form_memory_and_persists_only_public_connection_receipt():
 class Cursor(ExistingCursor):
  def execute(self,sql,args):
   if sql.startswith('SELECT COALESCE(sum(octet_length(encrypted_result))'):
    self.row={'total':0};self.db.sql.append((sql,args));return
   super().execute(sql,args)
 class DB(Database):
  async def tx(self,access,fn):return fn(Cursor(self),{'fence':1})
 class Operation:
  async def secure_connect(self,ctx,fields):
   assert fields==FIELDS
   return {'status':'completed','service_result':{'protocol':'maimi-service-session-v2','connected':True,'server':{'host_id':JOB,'label':'synthetic target','actions':[],'high_privilege':True}}}
 async def run():
  runtime=object.__new__(platform.ProjectRuntime);runtime.db=DB();runtime.db.status='running'
  runtime.service=lambda value:(Operation(),context())
  async def authorize(value):pass
  runtime.authorize=authorize;value=access();fields=deepcopy(FIELDS)
  await runtime._run(value,'service','secure_connect',{},fields,__import__('time').time()+30)
  assert fields=={} and runtime.db.status=='completed'
  assert FIELDS['password'] not in repr(runtime.db.sql)
 asyncio.run(run())
