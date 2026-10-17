"""Synthetic control HTTP; never connects to a company or SSH endpoint."""
import asyncio
from copy import deepcopy
import json
import pytest
from test_server_operations import s,port,response,ctx,CONFIG,SCOPE,HOST,TOKEN

FIELDS={'host':'10.4.240.14','port':'22','username':'root','password':'synthetic_login_secret_987'}
def state(connected=True):
 return {'protocol':s.PROTOCOL,'connected':connected,'server':{'host_id':HOST,'label':'10.4.240.14 (root)','actions':sorted(s.REMOTE_ACTIONS),'high_privilege':True} if connected else None}

@pytest.mark.parametrize('action',['status','list_servers','disconnect'])
def test_controls_bind_trusted_session_and_generation_and_have_no_operation_post(action):
 calls=[]
 def handler(request):
  calls.append(request)
  assert request.url.path=='/v1/connections'
  assert dict(request.url.params)=={'employee_no':'M00096389','session_id':SCOPE['session_id'],'generation':SCOPE['generation']}
  return response(state(action!='disconnect'))
 p=port(handler);result=asyncio.run(p.invoke(p.context,action,{}))
 assert result['connected']==(action!='disconnect')
 assert [r.method for r in calls]==['DELETE' if action=='disconnect' else 'GET']

def test_human_connect_normalizes_port_and_keeps_credentials_out_of_query_and_result():
 calls=[]
 def handler(request):
  calls.append(request)
  assert request.method=='POST' and request.url.path=='/v1/connections' and not request.url.params
  body=json.loads(request.content)
  assert set(body)=={'protocol','employee_no','scope','request_id','fields'}
  assert body['scope']==SCOPE and body['request_id']==SCOPE['job_id']
  assert body['fields']=={**FIELDS,'port':22}
  return response(state())
 p=port(handler,context=ctx(source='human'))
 result=asyncio.run(p.secure_connect(p.context,deepcopy(FIELDS)))
 assert result==state() and FIELDS['password'] not in json.dumps(result) and len(calls)==1

def test_model_context_cannot_connect_even_with_valid_fields():
 calls=[];p=port(lambda request:calls.append(request))
 with pytest.raises(s.ServiceFailure,match='human_authorization_required'):asyncio.run(p.secure_connect(p.context,deepcopy(FIELDS)))
 assert calls==[]

@pytest.mark.parametrize('host',['127.0.0.1','169.254.169.254','8.8.8.8','localhost','10.4.240.14.example','10.4.240.014','::1','10.4.240.14:22','10.4.240.14\n'])
def test_connection_destination_is_strict_company_ipv4(host):
 with pytest.raises(s.ServiceFailure):s.login_fields({**FIELDS,'host':host})

@pytest.mark.parametrize('port_value',[True,False,0,65536,'22x','22.0','-1','',None])
def test_invalid_ports_are_not_coerced(port_value):
 with pytest.raises(s.ServiceFailure):s.login_fields({**FIELDS,'port':port_value})

@pytest.mark.parametrize('extra',['command','employee_no','private_key','known_hosts','proxy','generation','host_id'])
def test_form_fields_are_closed(extra):
 with pytest.raises(s.ServiceFailure):s.login_fields({**FIELDS,extra:'x'})

def test_revoked_after_successful_login_disconnects_once_without_reauth():
 calls=[];revoked=False
 async def authorize(context):
  if revoked:raise s.ServiceFailure('project_selection_changed',409)
 def handler(request):
  nonlocal revoked
  calls.append(request.method)
  if request.method=='POST':revoked=True;return response(state())
  return response(state(False))
 p=port(handler,context=ctx(source='human'),authorize=authorize)
 with pytest.raises(s.ServiceFailure,match='selection_changed'):asyncio.run(p.secure_connect(p.context,deepcopy(FIELDS)))
 assert calls==['POST','DELETE']

def test_rejected_login_has_closed_error_and_one_cleanup_no_retry():
 calls=[]
 def handler(request):
  calls.append(request.method)
  return response({'error_code':'SSH_AUTH_FAILED'},401) if request.method=='POST' else response(state(False))
 p=port(handler,context=ctx(source='human'))
 with pytest.raises(s.ServiceFailure,match='ssh_auth_failed') as error:asyncio.run(p.secure_connect(p.context,deepcopy(FIELDS)))
 assert str(error.value)=='project_service_ssh_auth_failed' and calls==['POST','DELETE']

@pytest.mark.parametrize('mutation',[lambda r:r.update(password='x'),lambda r:r.update(connected=1),lambda r:r.update(server=None),lambda r:r['server'].update(host_id='guessed-host'),lambda r:r['server'].update(actions=['run_command']),lambda r:r['server'].update(high_privilege='true')])
def test_connection_result_cannot_inject_credentials_or_unbound_server(mutation):
 value=state();mutation(value)
 with pytest.raises(s.ServiceFailure,match='response_invalid'):s.connection_result(value)

def test_configuration_uses_employee_acl_and_never_shared_host_credentials(monkeypatch):
 value={'schema':'maimi-service-gateway-v2','enabled':True,'broker_origin':'http://service-broker:8090','token_file':'/run/secrets/maimi-service-broker-token','allowed_users':sorted(s.USERS)}
 monkeypatch.setenv('MAIMI_SERVICE_GATEWAY_ENABLED','true')
 monkeypatch.setattr(s,'private_read',lambda *_:json.dumps(value).encode())
 config=s.GatewayConfig.from_env();assert config.allowed('M00003307') and not config.allowed('M00000074')
 for field in ['hosts','password','target']:
  value[field]='forbidden'
  with pytest.raises(s.ServiceFailure,match='configuration_invalid'):s.GatewayConfig.from_env()
  value.pop(field)

def test_closed_form_password_values_do_not_echo_or_get_trimmed():
 assert s.login_fields({**FIELDS,'password':' a '})['password']==' a '
 for password in ['','x\n','x\r','\x00','中'*1400,'\ud800']:
  with pytest.raises(s.ServiceFailure):s.login_fields({**FIELDS,'password':password})
