"""Closed service-broker wire contract. Identity is supplied only by gateway."""
from __future__ import annotations
import hashlib
import json
import re
import time
import ipaddress
from uuid import UUID

PROTOCOL = 'maimi-service-session-v2'
USERS = frozenset({'M00003307', 'M00001069', 'M00096389'})
ACTIONS = frozenset({'list_servers','inspect_server','read_file','write_file','run_command'})
TERMINAL = frozenset({'completed','failed','cancelled','unknown'})
ERRORS = frozenset({'NOT_CONFIGURED','NOT_AUTHORIZED','HOST_NOT_REGISTERED','ACTION_NOT_ALLOWED','ARGUMENTS_INVALID','PATH_NOT_ALLOWED','FILE_NOT_FOUND','FILE_TOO_LARGE','FILE_CHANGED','SSH_HOST_KEY_REJECTED','SSH_AUTH_FAILED','SSH_UNAVAILABLE','OPERATION_TIMEOUT','OUTPUT_LIMIT','OPERATION_FAILED','CANCEL_UNKNOWN','RESTART_UNKNOWN','SCOPE_EXPIRED','RATE_LIMIT','IDEMPOTENCY_CONFLICT','SCOPE_MISMATCH'})
ERRORS = ERRORS | {'CONNECTION_REQUIRED','CONNECTION_BUSY','SSH_SESSION_EXPIRED','PYTHON_UNAVAILABLE'}
SID = re.compile(r'^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',re.I)
INVOCATION = re.compile(r'^pi_[A-Za-z0-9_-]{43}$')
HEX = re.compile(r'^[a-f0-9]{64}$')

class Rejected(Exception):
    def __init__(self, code='ARGUMENTS_INVALID', status=400):
        if code not in ERRORS: code='OPERATION_FAILED'
        self.code,self.status=code,status
        super().__init__(code)

def require(value,code='ARGUMENTS_INVALID',status=400):
    if not value: raise Rejected(code,status)

def exact(value, allowed, required=None):
    require(isinstance(value,dict) and not set(value)-set(allowed) and not set(allowed if required is None else required)-set(value))

def canonical(value):
    try:return isinstance(value,str) and str(UUID(value))==value
    except (ValueError,TypeError):return False

def integer(value,low,high):return type(value) is int and low<=value<=high

def dumps(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def decode(raw):
    def pairs(rows):
        value={}
        for key,item in rows:
            require(key not in value)
            value[key]=item
        return value
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:require(False))
    except Rejected:raise
    except (ValueError,UnicodeError):raise Rejected() from None

def relative(value,*,cwd=False):
    require(isinstance(value,str) and 1<=len(value)<=512 and not any(ord(c)<32 or ord(c)==127 for c in value) and '\\' not in value,'PATH_NOT_ALLOWED')
    if cwd and value=='.':return value
    require(not value.startswith('/') and all(p not in ('','.','..') for p in value.split('/')),'PATH_NOT_ALLOWED')
    # These endpoints edit source, not SSH credentials or process secrets.
    for part in value.split('/'):
        require(not part.startswith('.') and part.lower() not in {'id_rsa','id_ed25519','authorized_keys','known_hosts'} and not part.lower().endswith(('.pem','.key','.p12','.pfx')),'PATH_NOT_ALLOWED')
    return value

def arguments(action,value):
    require(isinstance(action,str) and action in ACTIONS)
    if action=='list_servers':exact(value,());return {}
    allowed={'inspect_server':{'host_id','category'},'read_file':{'host_id','relative_path','max_bytes'},'write_file':{'host_id','relative_path','content','expected_sha256'},'run_command':{'host_id','command','relative_cwd','timeout_seconds'}}[action]
    required={'host_id'}
    if action in {'read_file','write_file'}:required.add('relative_path')
    if action=='write_file':required.update({'content','expected_sha256'})
    if action=='run_command':required.add('command')
    exact(value,allowed,required)
    require(canonical(value['host_id']),'HOST_NOT_REGISTERED',403)
    result=dict(value)
    if action=='inspect_server':
        result.setdefault('category','environment');require(result['category'] in {'environment','oracle','all'})
    elif action=='read_file':
        relative(result['relative_path']);result.setdefault('max_bytes',32768);require(integer(result['max_bytes'],1,32768))
    elif action=='write_file':
        relative(result['relative_path']);content=result['content']
        try:size=len(content.encode('utf-8')) if isinstance(content,str) else None
        except UnicodeError:size=None
        require(size is not None and len(content)<=12000 and size<=16384 and '\x00' not in content)
        require(isinstance(result['expected_sha256'],str) and (result['expected_sha256']=='MISSING' or HEX.fullmatch(result['expected_sha256'])))
    elif action=='run_command':
        text=result['command'];require(isinstance(text,str) and 1<=len(text)<=8192 and '\x00' not in text)
        result.setdefault('relative_cwd','.');relative(result['relative_cwd'],cwd=True)
        result.setdefault('timeout_seconds',90);require(integer(result['timeout_seconds'],1,300))
    return result

def request(value,clock=time.time):
    exact(value,{'protocol','employee_no','scope','request_id','action','arguments'})
    require(value['protocol']==PROTOCOL)
    require(isinstance(value['employee_no'],str) and value['employee_no'] in USERS,'NOT_AUTHORIZED',403)
    scope=value['scope'];exact(scope,{'session_id','invocation_id','job_id','generation','expires_at'})
    require(isinstance(scope['session_id'],str) and SID.fullmatch(scope['session_id']))
    require(isinstance(scope['invocation_id'],str) and INVOCATION.fullmatch(scope['invocation_id']))
    require(canonical(scope['job_id']) and value['request_id']==scope['job_id'])
    require(isinstance(scope['generation'],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',scope['generation']))
    require(integer(scope['expires_at'],int(clock())+1,int(clock())+665),'SCOPE_EXPIRED',403)
    args=arguments(value['action'],value['arguments'])
    return {**value,'scope':dict(scope),'arguments':args}

def connection_request(value,clock=time.time):
    exact(value,{'protocol','employee_no','scope','request_id','fields'})
    validated=request({key:val for key,val in value.items() if key!='fields'} | {'action':'list_servers','arguments':{}},clock)
    fields=value['fields'];exact(fields,{'host','port','username','password'})
    host=fields['host'];require(isinstance(host,str) and len(host)<=15)
    try:address=ipaddress.IPv4Address(host)
    except (ValueError,TypeError):raise Rejected() from None
    require(str(address)==host and any(address in ipaddress.IPv4Network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')))
    require(integer(fields['port'],1,65535))
    require(isinstance(fields['username'],str) and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}',fields['username']))
    require(isinstance(fields['password'],str) and 1<=len(fields['password'])<=4096 and not any(c in fields['password'] for c in ('\x00','\r','\n')))
    return {key:validated[key] for key in ('protocol','employee_no','scope','request_id')} | {'fields':dict(fields)}

def connection_scope(employee,session,generation):
    require(employee in USERS,'NOT_AUTHORIZED',403)
    require(isinstance(session,str) and SID.fullmatch(session))
    require(isinstance(generation,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',generation))
    return employee,session,generation

def fingerprint(value,policy_generation):
    return hashlib.sha256(dumps({'request':value,'policy_generation':policy_generation}).encode()).hexdigest()
