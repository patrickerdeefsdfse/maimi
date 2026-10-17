"""Server-side service port; credentials and trusted scope never enter tool arguments."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from uuid import UUID
import httpx

PROTOCOL='maimi-service-session-v2'
USERS=frozenset({'M00003307','M00001069','M00096389'})
ACTIONS=('status','list_servers','disconnect','inspect_server','read_file','write_file','run_command')
CONTROLS=frozenset({'status','list_servers','disconnect'})
REMOTE_ACTIONS=frozenset(set(ACTIONS)-CONTROLS)
ERRORS=frozenset({'NOT_CONFIGURED','NOT_AUTHORIZED','HOST_NOT_REGISTERED','ACTION_NOT_ALLOWED','ARGUMENTS_INVALID','PATH_NOT_ALLOWED','FILE_NOT_FOUND','FILE_TOO_LARGE','FILE_CHANGED','SSH_HOST_KEY_REJECTED','SSH_AUTH_FAILED','SSH_UNAVAILABLE','OPERATION_TIMEOUT','OUTPUT_LIMIT','OPERATION_FAILED','CANCEL_UNKNOWN','RESTART_UNKNOWN','SCOPE_EXPIRED','RATE_LIMIT','IDEMPOTENCY_CONFLICT','SCOPE_MISMATCH'})
ERRORS=ERRORS|{'CONNECTION_REQUIRED','CONNECTION_BUSY','SSH_SESSION_EXPIRED','PYTHON_UNAVAILABLE'}
TERMINAL=frozenset({'completed','failed','cancelled','unknown'})
HOST=re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
HEX=re.compile(r'^[a-f0-9]{64}$')
SID=re.compile(r'^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',re.I)
INVOCATION=re.compile(r'^pi_[A-Za-z0-9_-]{43}$')

class ServiceFailure(Exception):
    def __init__(self,code,status=503,*,remote_status_unknown=False):
        self.code,self.status,self.remote_status_unknown=code,status,remote_status_unknown
        super().__init__(code)

def require(value,code='project_service_arguments_invalid',status=400):
    if not value:raise ServiceFailure(code,status)

def exact(value,allowed,required=()):
    require(isinstance(value,dict) and not set(value)-set(allowed) and not set(required)-set(value))

def integer(value,low,high):return type(value) is int and low<=value<=high

def utf8_size(value):
    try:return len(value.encode('utf-8')) if isinstance(value,str) else None
    except UnicodeError:return None

def strings(value):return isinstance(value,list) and all(isinstance(item,str) for item in value)

def relative(value,*,cwd=False):
    require(isinstance(value,str) and 1<=len(value)<=512 and not any(ord(c)<32 for c in value) and '\\' not in value)
    if cwd and value=='.':return value
    require(not value.startswith('/') and not value.endswith('/') and all(p not in ('','.','..') for p in value.split('/')))
    require(str(PurePosixPath(value))==value)
    return value

def arguments(action,value):
    require(action in ACTIONS)
    if action in CONTROLS:exact(value,());return {}
    required={'host_id'}
    allowed={'inspect_server':{'host_id','category'},'read_file':{'host_id','relative_path','max_bytes'},'write_file':{'host_id','relative_path','content','expected_sha256'},'run_command':{'host_id','command','relative_cwd','timeout_seconds'}}[action]
    if action in ('read_file','write_file'):required.add('relative_path')
    if action=='write_file':required.update(('content','expected_sha256'))
    if action=='run_command':required.add('command')
    exact(value,allowed,required);require(isinstance(value['host_id'],str) and HOST.fullmatch(value['host_id']))
    result=dict(value)
    if action=='inspect_server':
        result.setdefault('category','environment');require(result['category'] in ('environment','oracle','all'))
    elif action=='read_file':
        relative(result['relative_path']);result.setdefault('max_bytes',32768);require(integer(result['max_bytes'],1,32768))
    elif action=='write_file':
        relative(result['relative_path']);content=result['content']
        require(isinstance(content,str) and len(content)<=12000 and utf8_size(content) is not None and utf8_size(content)<=16384 and '\x00' not in content)
        expected=result['expected_sha256'];require(isinstance(expected,str) and (expected=='MISSING' or HEX.fullmatch(expected)))
    elif action=='run_command':
        command=result['command'];require(isinstance(command,str) and 1<=len(command)<=8192 and '\x00' not in command)
        result.setdefault('relative_cwd','.');relative(result['relative_cwd'],cwd=True)
        result.setdefault('timeout_seconds',90);require(integer(result['timeout_seconds'],1,300))
    return result

def shape(properties=None,required=()):return {'type':'object','properties':properties or {},'required':list(required),'additionalProperties':False}
HOST_SCHEMA={'type':'string','minLength':1,'maxLength':64,'pattern':'^[a-z0-9][a-z0-9_-]{0,63}$'}
PATH_SCHEMA={'type':'string','minLength':1,'maxLength':512,'description':'仅登录账号工作目录内的相对POSIX路径；不得为绝对路径、..、符号链接或凭据路径。'}
TOOLS=[
 ('status','检查当前对话的服务器连接；未连接时在对话内显示 IP 和密码两个输入框及连接按钮。使用 root 和端口 22；密码只能在输入框中填写，不得在聊天中索取。',shape()),
 ('list_servers','查看当前对话已连接的服务器；未连接时显示对话中的连接区域，仅需填写 IP 和密码。不能扫描或猜测IP。',shape()),
 ('disconnect','断开当前对话的服务器连接并清除会话凭据；已发送的远端任务可能仍有执行结果未确认。',shape()),
 ('inspect_server','检查已授权服务器环境或Oracle状态；category可选environment/oracle/all，缺失工具如实返回。',shape({'host_id':HOST_SCHEMA,'category':{'type':'string','enum':['environment','oracle','all']}},('host_id',))),
 ('read_file','读取当前连接工作目录内的代码文件，返回完整文本和SHA；只接受relative_path，不读取凭据或任意绝对路径。',shape({'host_id':HOST_SCHEMA,'relative_path':PATH_SCHEMA,'max_bytes':{'type':'integer','minimum':1,'maximum':32768}},('host_id','relative_path'))),
 ('write_file','在授权工作目录创建或修改代码。必须先read_file取得expected_sha256；仅新文件可用MISSING，禁止盲目覆盖。',shape({'host_id':HOST_SCHEMA,'relative_path':PATH_SCHEMA,'content':{'type':'string','maxLength':12000},'expected_sha256':{'type':'string','pattern':'^(?:[a-f0-9]{64}|MISSING)$'}},('host_id','relative_path','content','expected_sha256'))),
 ('run_command','在当前对话已连接的服务器运行用户要求的代码或验证命令；root登录是管理员高权限，不是沙箱。最长300秒，输出64KiB；不得索取或尝试读取SSH凭据。',shape({'host_id':HOST_SCHEMA,'command':{'type':'string','minLength':1,'maxLength':8192},'relative_cwd':{'type':'string','minLength':1,'maxLength':512},'timeout_seconds':{'type':'integer','minimum':1,'maximum':300}},('host_id','command'))),
]

def private_read(filename,maximum):
    path=Path(filename)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)),'project_service_configuration_invalid',503)
    for parent in path.parents:
        info=parent.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0,os.geteuid()) and not stat.S_IMODE(info.st_mode)&0o022,'project_service_configuration_invalid',503)
    before=path.stat();require(stat.S_ISREG(before.st_mode) and before.st_uid in (0,os.geteuid()) and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==0o600 and 1<=before.st_size<=maximum,'project_service_configuration_invalid',503)
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
        opened=os.fstat(stream.fileno());raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    identity=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns)
    require(identity(before)==identity(opened)==identity(after) and len(raw)==before.st_size,'project_service_configuration_changed',503)
    return raw

@dataclass(frozen=True)
class GatewayConfig:
    broker_origin:str
    token_file:str
    allowed_users:tuple[str,...]
    @classmethod
    def from_env(cls):
        if os.getenv('MAIMI_SERVICE_GATEWAY_ENABLED')!='true':return None
        try:
            raw=private_read(os.getenv('MAIMI_SERVICE_GATEWAY_CONFIG_FILE','/run/secrets/maimi-service-gateway.json'),65536)
            value=json.loads(raw)
            require(isinstance(value,dict) and set(value)=={'schema','enabled','broker_origin','token_file','allowed_users'} and value['schema']=='maimi-service-gateway-v2','project_service_configuration_invalid',503)
            if value['enabled'] is not True:return None
            require(value['broker_origin']=='http://service-broker:8090' and isinstance(value['token_file'],str) and value['token_file'].startswith('/run/secrets/'),'project_service_configuration_invalid',503)
            users=value['allowed_users']
            require(strings(users) and bool(users) and set(users)<=USERS and len(set(users))==len(users),'project_service_configuration_invalid',503)
            return cls(value['broker_origin'],value['token_file'],tuple(users))
        except ServiceFailure:raise
        except Exception:raise ServiceFailure('project_service_configuration_invalid') from None
    def allowed(self,user):return user in self.allowed_users

def login_fields(value):
    exact(value,{'host','port','username','password'},{'host','port','username','password'})
    host=value['host'];port=value['port'];username=value['username'];password=value['password']
    require(isinstance(host,str) and len(host)<=15)
    try:address=ipaddress.IPv4Address(host)
    except (ValueError,TypeError):raise ServiceFailure('project_service_arguments_invalid',400) from None
    require(str(address)==host and any(address in ipaddress.ip_network(network) for network in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')))
    if isinstance(port,str) and re.fullmatch(r'[0-9]{1,5}',port):port=int(port)
    require(integer(port,1,65535) and isinstance(username,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}',username))
    require(isinstance(password,str) and 1<=len(password)<=4096 and utf8_size(password) is not None and utf8_size(password)<=4096 and not any(c in password for c in ('\x00','\r','\n')))
    return {'host':host,'port':port,'username':username,'password':password}

def connection_result(value):
    require(isinstance(value,dict) and set(value)=={'protocol','connected','server'} and value['protocol']==PROTOCOL and type(value['connected']) is bool,'project_service_response_invalid',502)
    server=value['server']
    if not value['connected']:
        require(server is None,'project_service_response_invalid',502)
    else:
        require(isinstance(server,dict) and set(server)=={'host_id','label','actions','high_privilege'} and canonical_uuid(server['host_id']) and bounded_text(server['label'],400) and strings(server['actions']) and len(set(server['actions']))==len(server['actions']) and set(server['actions'])==REMOTE_ACTIONS and type(server['high_privilege']) is bool,'project_service_response_invalid',502)
    return value

def canonical_uuid(value):
    try:return isinstance(value,str) and str(UUID(value))==value
    except (ValueError,TypeError):return False

class ServerOperations:
    """One current ProjectPlatform Access; HTTP polls cannot outlive its permit."""
    def __init__(self,*,context,scope,authorize,configuration=GatewayConfig.from_env,read_secret=private_read,transport=None,clock=time.time,sleep=asyncio.sleep,cancel_seconds=10):
        self.context,self.scope,self.authorize=context,scope,authorize
        self.configuration,self.read_secret,self.transport=configuration,read_secret,transport
        self.clock,self.sleep,self.cancel_seconds=clock,sleep,cancel_seconds
    def availability(self,ctx):
        if ctx!=self.context or getattr(ctx,'user_no',None) not in USERS or getattr(ctx,'location',None)!='cloud':return False,'project_service_not_authorized'
        try:
            config=self.configuration()
            if config is None or not config.allowed(ctx.user_no):return False,'project_service_not_configured'
            token=self.read_secret(config.token_file,4096).decode('ascii').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,512}',token):return False,'project_service_configuration_invalid'
            return True,''
        except Exception:return False,'project_service_configuration_invalid'
    async def _check(self,ctx,action,args,config,scope):
        require(ctx==self.context and ctx.user_no in USERS and ctx.location=='cloud','project_service_not_authorized',403)
        await self.authorize(ctx)
        current=self.configuration();require(current is not None and current==config,'project_service_configuration_changed',403)
        require(config.allowed(ctx.user_no),'project_service_not_authorized',403)
        require(self.scope()==scope and self.clock()<scope['expires_at'],'project_service_scope_expired',403)
    def _scope(self,ctx):
        value=self.scope();require(isinstance(value,dict) and set(value)=={'session_id','invocation_id','job_id','generation','expires_at'},'project_service_scope_invalid',403)
        require(isinstance(value['session_id'],str) and isinstance(value['invocation_id'],str) and value['session_id']==ctx.native_session_id and SID.fullmatch(value['session_id']) and INVOCATION.fullmatch(value['invocation_id']) and canonical_uuid(value['job_id']),'project_service_scope_invalid',403)
        require(isinstance(value['generation'],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value['generation']) and integer(value['expires_at'],int(self.clock())+1,int(self.clock())+665),'project_service_scope_invalid',403)
        return dict(value)
    async def _request(self,client,method,path,*,body=None,query=None,deadline=10):
        try:
            # The whole response has a deadline, including a slow-drip body.
            async with asyncio.timeout(deadline):
                async with client.stream(method,path,json=body,params=query) as response:
                    require(response.headers.get('content-encoding','identity').lower()=='identity','project_service_response_invalid',502)
                    raw=bytearray()
                    async for chunk in response.aiter_raw():
                        raw.extend(chunk);require(len(raw)<=512*1024,'project_service_response_limit',502)
                    value=json.loads(raw);require(isinstance(value,dict),'project_service_response_invalid',502)
                    if response.status_code not in (200,202):
                        if set(value)=={'error_code'} and isinstance(value['error_code'],str) and value['error_code'] in ERRORS:
                            error=ServiceFailure('project_service_'+value['error_code'].lower(),503)
                            error.remote_rejected=True
                            raise error
                        raise ServiceFailure('project_service_broker_rejected',503)
                    return response.status_code,value
        except ServiceFailure:raise
        except asyncio.CancelledError:raise
        except Exception:raise ServiceFailure('project_service_broker_unavailable') from None
    def _response(self,value,scope,operation_id=None,*,initial=False):
        allowed={'protocol','operation_id','status','scope','expires_at'}|({'result','error_code'} if not initial else set())
        require(isinstance(value,dict) and set(value)==allowed and value['protocol']==PROTOCOL and canonical_uuid(value['operation_id']) and value['scope']==scope and type(value['expires_at']) is int and value['expires_at']==scope['expires_at'],'project_service_response_invalid',502)
        require(operation_id is None or value['operation_id']==operation_id,'project_service_response_invalid',502)
        require(isinstance(value['status'],str) and value['status'] in ({'queued','running'} if initial else TERMINAL|{'queued','running','cancelling'}),'project_service_response_invalid',502)
        if not initial:
            require(value['error_code'] is None or isinstance(value['error_code'],str) and value['error_code'] in ERRORS,'project_service_response_invalid',502)
            require((value['error_code'] is None) if value['status']=='completed' else (value['result'] is None),'project_service_response_invalid',502)
        return value['operation_id']
    async def _cancel_join(self,client,operation_id,scope,ctx):
        query={'employee_no':ctx.user_no,'job_id':scope['job_id']}
        async def join():
            _,value=await self._request(client,'DELETE','/v1/operations/'+operation_id,query=query)
            self._response(value,scope,operation_id)
            while value['status'] not in TERMINAL:
                await self.sleep(.25)
                _,value=await self._request(client,'GET','/v1/operations/'+operation_id,query=query)
                self._response(value,scope,operation_id)
            return value['status']!='unknown'
        work=asyncio.create_task(join())
        try:return await asyncio.wait_for(asyncio.shield(work),timeout=self.cancel_seconds)
        except (Exception,asyncio.CancelledError):return False
        finally:
            if not work.done():work.cancel()
            await asyncio.gather(work,return_exceptions=True)
    async def invoke(self,ctx,action,value):
        args=arguments(action,value)
        if action in CONTROLS:return await self._connection(ctx,action)
        require(ctx==self.context and ctx.user_no in USERS and ctx.location=='cloud','project_service_not_authorized',403)
        config=self.configuration();require(config is not None,'project_service_not_configured',503)
        scope=self._scope(ctx);await self._check(ctx,action,args,config,scope)
        try:
            token=self.read_secret(config.token_file,4096).decode('ascii').strip()
            require(re.fullmatch(r'[A-Za-z0-9_-]{32,512}',token),'project_service_configuration_invalid',503)
        except ServiceFailure:raise
        except Exception:raise ServiceFailure('project_service_configuration_invalid') from None
        operation_id=None;submitted=False
        async with httpx.AsyncClient(base_url=config.broker_origin,headers={'Authorization':'Bearer '+token,'Accept-Encoding':'identity'},timeout=httpx.Timeout(10,connect=5),trust_env=False,follow_redirects=False,transport=self.transport) as client:
            try:
                await self._check(ctx,action,args,config,scope)
                body={'protocol':PROTOCOL,'employee_no':ctx.user_no,'scope':scope,'request_id':scope['job_id'],'action':action,'arguments':args}
                submitted=True
                status,reply=await self._request(client,'POST','/v1/operations',body=body)
                require(status==202,'project_service_response_invalid',502);operation_id=self._response(reply,scope,initial=True)
                while True:
                    await self._check(ctx,action,args,config,scope)
                    await self.sleep(1)
                    await self._check(ctx,action,args,config,scope)
                    _,reply=await self._request(client,'GET','/v1/operations/'+operation_id,query={'employee_no':ctx.user_no,'job_id':scope['job_id']})
                    self._response(reply,scope,operation_id)
                    await self._check(ctx,action,args,config,scope)
                    if reply['status'] in TERMINAL:break
                if reply['status']=='unknown':raise ServiceFailure('project_service_operation_unknown',remote_status_unknown=True)
                if reply['status']=='cancelled':raise ServiceFailure('project_service_cancelled',409)
                if reply['status']=='failed':raise ServiceFailure('project_service_'+(reply['error_code'] or 'OPERATION_FAILED').lower(),503)
                result=validate_result(action,args,reply['result'])
                require(token not in json.dumps(result,ensure_ascii=False),'project_service_response_invalid',502)
                return model_view(action,result)
            except BaseException as error:
                if operation_id is not None:
                    stopped=await self._cancel_join(client,operation_id,scope,ctx)
                    if not stopped:raise ServiceFailure('project_service_stop_unknown',remote_status_unknown=True) from None
                elif submitted and not getattr(error,'remote_rejected',False):raise ServiceFailure('project_service_operation_unknown',remote_status_unknown=True) from None
                raise

    async def secure_connect(self,ctx,fields):
        require(getattr(ctx,'source',None)=='human','project_human_authorization_required',403)
        data=login_fields(fields)
        try:return await self._connection(ctx,'secure_connect',data)
        finally:data.clear()

    async def _connection(self,ctx,action,fields=None):
        require(ctx==self.context and ctx.user_no in USERS and ctx.location=='cloud','project_service_not_authorized',403)
        config=self.configuration();require(config is not None,'project_service_not_configured',503)
        scope=self._scope(ctx);await self._check(ctx,action,{},config,scope)
        try:
            token=self.read_secret(config.token_file,4096).decode('ascii').strip()
            require(re.fullmatch(r'[A-Za-z0-9_-]{32,512}',token),'project_service_configuration_invalid',503)
        except ServiceFailure:raise
        except Exception:raise ServiceFailure('project_service_configuration_invalid') from None
        query={'employee_no':ctx.user_no,'session_id':scope['session_id'],'generation':scope['generation']}
        body=None;connect_sent=False
        async with httpx.AsyncClient(base_url=config.broker_origin,headers={'Authorization':'Bearer '+token,'Accept-Encoding':'identity'},timeout=httpx.Timeout(25,connect=5),trust_env=False,follow_redirects=False,transport=self.transport) as client:
            try:
                await self._check(ctx,action,{},config,scope)
                if action=='secure_connect':
                    require(getattr(ctx,'source',None)=='human','project_human_authorization_required',403)
                    body={'protocol':PROTOCOL,'employee_no':ctx.user_no,'scope':scope,'request_id':scope['job_id'],'fields':fields}
                    connect_sent=True
                    status,value=await self._request(client,'POST','/v1/connections',body=body,deadline=25)
                else:
                    status,value=await self._request(client,'DELETE' if action=='disconnect' else 'GET','/v1/connections',query=query)
                require(status==200,'project_service_response_invalid',502)
                value=connection_result(value)
                require(action!='secure_connect' or value['connected'],'project_service_response_invalid',502)
                require(action!='disconnect' or not value['connected'],'project_service_response_invalid',502)
                serialized=json.dumps(value,ensure_ascii=False)
                require(token not in serialized and (fields is None or len(fields['password'])<8 or fields['password'] not in serialized),'project_service_response_invalid',502)
                await self._check(ctx,action,{},config,scope)
                return value
            except BaseException:
                if connect_sent:
                    # A login which completes after permit revocation must not
                    # leave a usable connection. Never retry authentication.
                    async def revoke():
                        status,result=await self._request(client,'DELETE','/v1/connections',query=query)
                        require(status==200 and connection_result(result)['connected'] is False,'project_service_response_invalid',502)
                    cleanup=asyncio.create_task(revoke())
                    try:await asyncio.wait_for(asyncio.shield(cleanup),timeout=10)
                    except (Exception,asyncio.CancelledError):pass
                    finally:
                        if not cleanup.done():cleanup.cancel()
                        await asyncio.gather(cleanup,return_exceptions=True)
                raise
            finally:
                if body is not None:body.clear()

def bounded_text(value,maximum):
    size=utf8_size(value)
    return size is not None and size<=maximum and not any(ord(c)<32 and c not in '\n\r\t' or ord(c)==127 for c in value)

def output_text(value,maximum):
    size=utf8_size(value)
    return size is not None and size<=maximum and '\x00' not in value

def model_view(action,value):
    """Keep valid JSON under the existing native tool's90k text boundary.

    Remote output may contain controls or quotes which expand during JSON
    serialization. File contents remain complete for the returned SHA binding;
    command/inspection output is explicitly marked truncated when projected.
    """
    value=json.loads(json.dumps(value,ensure_ascii=False))
    if action not in {'run_command','inspect_server'}:return value
    budget=24000
    rows=[value] if action=='run_command' else value['checks']
    for row in rows:
        raw=row['output'];text=''.join(c for c in raw if ord(c)>=32 and ord(c)!=127 or c in '\n\r\t')
        encoded=text.encode('utf-8')
        if len(encoded)>budget:
            half=max(0,(budget-64)//2)
            text=encoded[:half].decode('utf-8','ignore')+'\n[output shortened; middle omitted]\n'+encoded[-half:].decode('utf-8','ignore') if half else ''
        if text!=raw:value['truncated']=True
        row['output']=text;budget=max(0,budget-len(text.encode('utf-8')))
    return value

def validate_result(action,args,value,grants=None):
    require(isinstance(value,dict),'project_service_response_invalid',502)
    require(action in REMOTE_ACTIONS,'project_service_response_invalid',502)
    require(value.get('host_id')==args['host_id'],'project_service_response_invalid',502)
    if action=='inspect_server':
        require(set(value)=={'host_id','category','checks','truncated'} and value['category']==args['category'] and isinstance(value['checks'],list) and len(value['checks'])<=32 and type(value['truncated']) is bool,'project_service_response_invalid',502)
        size=0
        for row in value['checks']:
            require(isinstance(row,dict) and set(row)=={'name','status','output','exit_code'} and bounded_text(row['name'],100) and row['status'] in ('ok','unavailable','failed') and output_text(row['output'],65536) and (row['exit_code'] is None or integer(row['exit_code'],-255,255)),'project_service_response_invalid',502);size+=len(row['output'].encode('utf-8'))
        require(size<=65536,'project_service_response_limit',502)
    elif action=='read_file':
        require(set(value)=={'host_id','relative_path','content','sha256','size_bytes','truncated'} and value['relative_path']==args['relative_path'] and bounded_text(value['content'],args['max_bytes']) and value['truncated'] is False,'project_service_response_invalid',502)
        require(integer(value['size_bytes'],0,args['max_bytes']) and len(value['content'].encode('utf-8'))==value['size_bytes'] and value['sha256']==hashlib.sha256(value['content'].encode()).hexdigest(),'project_service_response_invalid',502)
    elif action=='write_file':
        content=args['content'].encode('utf-8')
        require(set(value)=={'host_id','relative_path','sha256','size_bytes','created'} and value['relative_path']==args['relative_path'] and value['sha256']==hashlib.sha256(content).hexdigest() and integer(value['size_bytes'],0,16384) and value['size_bytes']==len(content) and type(value['created']) is bool and value['created']==(args['expected_sha256']=='MISSING'),'project_service_response_invalid',502)
    elif action=='run_command':
        require(set(value)=={'host_id','exit_code','output','truncated','timed_out','stop_confirmed','high_privilege','sandboxed'} and output_text(value['output'],65536) and (value['exit_code'] is None or integer(value['exit_code'],-255,255)) and all(type(value[k]) is bool for k in ('truncated','timed_out','stop_confirmed','high_privilege','sandboxed')) and value['sandboxed'] is False and value['stop_confirmed'] is True,'project_service_response_invalid',502)
    return value
