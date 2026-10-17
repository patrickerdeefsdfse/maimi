"""Synthetic HTTP only: no SSH, network, company data or real credentials."""
import asyncio,hashlib,importlib.util,json,sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
HERE=Path(__file__).resolve().parent
SOURCE=HERE.parents[2]/'services/gateway/maimi_projects/server_operations.py'
spec=importlib.util.spec_from_file_location('r55_server_operations',SOURCE);s=importlib.util.module_from_spec(spec);sys.modules[spec.name]=s;spec.loader.exec_module(s)
SESSION='11111111-1111-4111-8111-111111111111';JOB='22222222-2222-4222-8222-222222222222';OP='33333333-3333-4333-8333-333333333333'
HOST='44444444-4444-4444-8444-444444444444';TOKEN='synthetic_gateway_token_not_a_real_credential_00000'
SCOPE={'session_id':SESSION,'invocation_id':'pi_'+'a'*43,'job_id':JOB,'generation':'7','expires_at':1600}
CONFIG=s.GatewayConfig('http://service-broker:8090','/run/secrets/synthetic',tuple(sorted(s.USERS)))

def ctx(user='M00096389',location='cloud',source='model'):
 return SimpleNamespace(user_no=user,native_session_id=SESSION,location=location,source=source)
def reply(status='completed',result=None,error=None,scope=None):
 return {'protocol':s.PROTOCOL,'operation_id':OP,'status':status,'scope':scope or deepcopy(SCOPE),'expires_at':1600,'result':result,'error_code':error}
def accepted():
 value=reply('queued');value.pop('result');value.pop('error_code');return value
def output(text='test ok'):
 return {'host_id':HOST,'exit_code':0,'output':text,'truncated':False,'timed_out':False,'stop_confirmed':True,'high_privilege':True,'sandboxed':False}
def response(value,status=200):return httpx.Response(status,stream=httpx.ByteStream(json.dumps(value).encode()),headers={'content-type':'application/json'})
def port(handler,*,context=None,scope=None,authorize=None,configuration=None,read_secret=None,cancel_seconds=.1,sleep=None):
 context=context or ctx()
 async def permitted(candidate):assert candidate==context
 async def instant(_):await asyncio.sleep(0)
 return s.ServerOperations(context=context,scope=scope or (lambda:deepcopy(SCOPE)),authorize=authorize or permitted,configuration=configuration or (lambda:CONFIG),read_secret=read_secret or (lambda *_:TOKEN.encode()),transport=httpx.MockTransport(handler),clock=lambda:1000,cancel_seconds=cancel_seconds,sleep=sleep or instant)

def test_default_configuration_is_unavailable_and_never_contacts_broker(monkeypatch):
 monkeypatch.delenv('MAIMI_SERVICE_GATEWAY_ENABLED',raising=False);calls=[];p=port(lambda request:calls.append(request),configuration=s.GatewayConfig.from_env)
 assert p.availability(p.context)==(False,'project_service_not_configured')
 with pytest.raises(s.ServiceFailure,match='not_configured'):asyncio.run(p.invoke(p.context,'list_servers',{}))
 assert not calls

@pytest.mark.parametrize('user,location',[('M00000074','cloud'),('M00096389','local'),('M00003307','local')])
def test_unauthorized_or_local_cannot_see_or_call_service(user,location):
 calls=[];p=port(lambda request:calls.append(request),context=ctx(user,location))
 assert p.availability(p.context)[0] is False
 with pytest.raises(s.ServiceFailure,match='not_authorized'):asyncio.run(p.invoke(p.context,'list_servers',{}))
 assert calls==[]

def test_missing_private_broker_token_is_not_advertised_ready():
 def missing(*args):raise FileNotFoundError('PRIVATE_PATH_MUST_NOT_ESCAPE')
 p=port(lambda request:None,read_secret=missing)
 assert p.availability(p.context)==(False,'project_service_configuration_invalid')

def test_trusted_actor_and_scope_are_not_model_arguments_and_success_is_bound():
 calls=[]
 def handler(request):
  calls.append(request)
  assert request.headers['authorization']=='Bearer '+TOKEN
  assert request.headers['accept-encoding']=='identity'
  if request.method=='POST':
   body=json.loads(request.content);assert set(body)=={'protocol','employee_no','scope','request_id','action','arguments'}
   assert body['employee_no']=='M00096389' and body['scope']==SCOPE and body['request_id']==JOB
   assert body['arguments']=={'host_id':HOST,'command':'python3 -V','relative_cwd':'.','timeout_seconds':90}
   return response(accepted(),202)
  assert dict(request.url.params)=={'employee_no':'M00096389','job_id':JOB}
  return response(reply(result=output()))
 p=port(handler);result=asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'python3 -V'}))
 assert result['output']=='test ok' and result['sandboxed'] is False
 assert [r.method for r in calls]==['POST','GET']

@pytest.mark.parametrize('extra',['employee_no','session_id','job_id','invocation_id','generation','expires_at','token','password','private_key','address','ssh_port','user','endpoint','path'])
def test_model_cannot_supply_identity_network_or_credential_fields(extra):
 with pytest.raises(s.ServiceFailure,match='arguments_invalid'):s.arguments('run_command',{'host_id':HOST,'command':'true',extra:'injected'})

@pytest.mark.parametrize('path',['/etc/passwd','../x','a/../b','a//b','a/./b','a\\b','a\x00b','a/'])
def test_relative_file_paths_reject_traversal_and_control(path):
 with pytest.raises(s.ServiceFailure):s.arguments('read_file',{'host_id':HOST,'relative_path':path})

def test_write_requires_expected_file_sha_or_exclusive_create_and_content_bound():
 with pytest.raises(s.ServiceFailure):s.arguments('write_file',{'host_id':HOST,'relative_path':'x.py','content':'x'})
 assert s.arguments('write_file',{'host_id':HOST,'relative_path':'x.py','content':'x','expected_sha256':'MISSING'})['expected_sha256']=='MISSING'
 for value in ('0'*63,'FORCE',None):
  with pytest.raises(s.ServiceFailure):s.arguments('write_file',{'host_id':HOST,'relative_path':'x.py','content':'x','expected_sha256':value})
 for content in ('a'*12001,'中'*6000,'\ud800'):
  with pytest.raises(s.ServiceFailure):s.arguments('write_file',{'host_id':HOST,'relative_path':'x.py','content':content,'expected_sha256':'MISSING'})

def test_live_connection_scope_is_enforced_by_broker_without_static_host_grants():
 calls=[]
 def handler(request):
  calls.append(request.method)
  return response({'error_code':'CONNECTION_REQUIRED'},409)
 p=port(handler)
 with pytest.raises(s.ServiceFailure,match='connection_required'):asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))
 assert calls==['POST']

@pytest.mark.parametrize('change',['scope','generation','config'])
def test_revocation_after_submission_cancels_and_joins_before_return(change):
 calls=[];current=deepcopy(SCOPE);config=CONFIG
 def handler(request):
  nonlocal config
  calls.append(request.method)
  if request.method=='POST':
   if change=='scope':current['expires_at']=1500
   elif change=='generation':current['generation']='8'
   else:config=s.GatewayConfig(CONFIG.broker_origin,CONFIG.token_file,())
   return response(accepted(),202)
  if request.method=='DELETE':return response(reply('cancelling'))
  return response(reply('cancelled'))
 p=port(handler,scope=lambda:deepcopy(current),configuration=lambda:config)
 with pytest.raises(s.ServiceFailure) as caught:asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))
 assert calls==['POST','DELETE','GET'];assert caught.value.remote_status_unknown is False

def test_live_permit_denial_after_post_still_allows_bound_cleanup():
 calls=[];revoked=False
 async def authorize(context):
  if revoked:raise s.ServiceFailure('project_selection_changed',409)
 def handler(request):
  nonlocal revoked
  calls.append(request.method)
  if request.method=='POST':revoked=True;return response(accepted(),202)
  return response(reply('cancelled'))
 p=port(handler,authorize=authorize)
 with pytest.raises(s.ServiceFailure,match='selection_changed'):asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))
 assert calls==['POST','DELETE']

def test_unconfirmed_cancel_never_claims_stopped():
 calls=[];current=deepcopy(SCOPE)
 def handler(request):
  calls.append(request.method)
  if request.method=='POST':current['generation']='8';return response(accepted(),202)
  return response(reply('unknown',error='CANCEL_UNKNOWN'))
 p=port(handler,scope=lambda:deepcopy(current))
 with pytest.raises(s.ServiceFailure,match='stop_unknown') as caught:asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))
 assert caught.value.remote_status_unknown is True and calls==['POST','DELETE']

def test_lost_post_ack_is_unknown_and_not_retried():
 calls=[]
 def handler(request):calls.append(request.method);raise httpx.ReadError('PRIVATE_DETAILS',request=request)
 p=port(handler)
 with pytest.raises(s.ServiceFailure,match='operation_unknown') as caught:asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))
 assert calls==['POST'] and caught.value.remote_status_unknown

@pytest.mark.parametrize('mutation',[lambda r:r.update(status=[]),lambda r:r.update(error_code={}),lambda r:r.update(expires_at=1600.0),lambda r:r.update(scope={**SCOPE,'generation':'8'}),lambda r:r.update(operation_id='44444444-4444-4444-8444-444444444444'),lambda r:r.update(error_code='PRIVATE_UNKNOWN_CODE')])
def test_malformed_poll_cannot_return_unbound_output(mutation):
 calls=[]
 def handler(request):
  calls.append(request.method)
  if request.method=='POST':return response(accepted(),202)
  if request.method=='DELETE':return response(reply('cancelled'))
  value=reply(result=output('MUST_NOT_RETURN'));mutation(value);return response(value)
 p=port(handler)
 with pytest.raises(s.ServiceFailure,match='response_invalid'):asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))
 assert calls==['POST','GET','DELETE']

def test_broker_token_echo_is_never_returned_as_tool_data():
 def handler(request):
  if request.method=='POST':return response(accepted(),202)
  if request.method=='DELETE':return response(reply('cancelled'))
  return response(reply(result=output(TOKEN)))
 p=port(handler)
 with pytest.raises(s.ServiceFailure,match='response_invalid'):asyncio.run(p.invoke(p.context,'run_command',{'host_id':HOST,'command':'true'}))

def test_file_result_must_be_full_text_and_match_exact_sha_path_and_size():
 args=s.arguments('read_file',{'host_id':HOST,'relative_path':'main.py'})
 value={'host_id':HOST,'relative_path':'main.py','content':'print(1)','sha256':hashlib.sha256(b'print(1)').hexdigest(),'size_bytes':8,'truncated':False}
 assert s.validate_result('read_file',args,value)==value
 for field,data in [('sha256','0'*64),('relative_path','other.py'),('size_bytes',9),('truncated',True),('content','\ud800')]:
  with pytest.raises(s.ServiceFailure):s.validate_result('read_file',args,{**value,field:data})

def test_command_projection_is_bounded_valid_json_and_explicitly_truncated():
 value=output('"\\\n\x1b'*16000);assert s.validate_result('run_command',{'host_id':HOST},value)==value
 projected=s.model_view('run_command',value)
 assert projected['truncated'] and '\x1b' not in projected['output'] and len(projected['output'].encode())<=24000
 assert len(json.dumps(projected,ensure_ascii=False))<60000 and value['truncated'] is False

def test_inspect_projection_uses_one_total_budget():
 value={'host_id':HOST,'category':'all','checks':[{'name':'check'+str(i),'status':'ok','output':'x'*8192,'exit_code':0} for i in range(8)],'truncated':False}
 projected=s.model_view('inspect_server',value)
 assert sum(len(r['output'].encode()) for r in projected['checks'])<=24000 and projected['truncated'] is True

def test_catalog_contains_seven_actions_and_no_credentials_or_secure_login():
 assert set(s.ACTIONS)=={row[0] for row in s.TOOLS}
 assert 'secure_connect' not in s.ACTIONS
 for action,_,schema in s.TOOLS:
  assert not {'host','port','username','password'} & set(schema['properties'])
