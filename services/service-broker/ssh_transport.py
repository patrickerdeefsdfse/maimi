"""Live per-conversation SSH sessions, strict TOFU pins and bounded command channels."""
from __future__ import annotations
import asyncio
import contextlib
from dataclasses import dataclass,field
import hashlib
import re
import shlex
import time
from uuid import uuid4
import asyncssh
from protocol import ERRORS,PROTOCOL,Rejected,connection_request,connection_scope,decode,dumps,require
from remote_program import COMMAND,ENVIRONMENT_CHECKS,ORACLE_CHECKS

REMOTE_ACTIONS=('inspect_server','read_file','write_file','run_command')

class Execution:
    def __init__(self):
        self.dispatched=False;self.process=None;self.deadline=None;self.scope_expires=None;self.cancelled=False
    def close(self):
        if self.process is not None:
            with contextlib.suppress(Exception):self.process.close()

@dataclass(repr=False)
class Session:
    employee:str
    session_id:str
    generation:str
    host_id:str
    label:str
    request_id:str
    connection:object
    password:str=field(repr=False)
    last_used:float=0
    lock:object=field(default_factory=asyncio.Lock)
    high_privilege:bool=False
    def public(self):
        return {'host_id':self.host_id,'label':self.label,'actions':list(REMOTE_ACTIONS),'high_privilege':self.high_privilege}

class PinnedClient(asyncssh.SSHClient):
    def __init__(self,journal,employee,address,port):
        self.journal=journal;self.identity=(employee,address,port);self.verified=False
    def validate_host_public_key(self,host,addr,port,key):
        try:
            public=key.export_public_key('openssh').decode('ascii').strip()
            require(len(public)<=16384,'SSH_HOST_KEY_REJECTED',503)
            self.verified=self.journal.check_host_key(*self.identity,public)
            return self.verified
        except Exception:return False

class Connections:
    def __init__(self,registry,journal,*,connection_factory=None,clock=time.time):
        self.registry=registry;self.journal=journal;self.connect=connection_factory or asyncssh.connect;self.clock=clock
        self.sessions={};self.connecting={};self.seen_requests={};self.closed=False
    def _key(self,employee,session,generation):
        connection_scope(employee,session,generation);self.registry.allowed(employee);return employee,session
    async def _drop(self,key):
        session=self.sessions.pop(key,None)
        if session:
            connection=session.connection;session.password='';session.connection=None
            if connection is not None:
                connection.close()
                with contextlib.suppress(Exception):await asyncio.wait_for(connection.wait_closed(),2)
    async def get(self,employee,session,generation,*,touch=False):
        key=self._key(employee,session,generation);value=self.sessions.get(key)
        if self.journal.connection_retired(employee,session,generation):
            if value and value.generation==generation:await self._drop(key)
            return None
        if value and value.generation!=generation:return None
        if value and (self.clock()-value.last_used>=7200 or value.connection is None or value.connection.is_closed()):
            await self._drop(key);value=None
        if value and touch:value.last_used=self.clock()
        return value
    async def status(self,employee_no,session_id,generation):
        value=await self.get(employee_no,session_id,generation)
        return {'protocol':PROTOCOL,'connected':value is not None,'server':value.public() if value else None}
    async def disconnect(self,employee_no,session_id,generation):
        key=self._key(employee_no,session_id,generation)
        # Fence before cancellation: a transport which delays cancellation cannot
        # publish a successful connection after the DELETE acknowledgement.
        pending=self.connecting.get(key)
        if pending is not None and pending[0]==generation:
            self.connecting.pop(key,None);task=pending[1]
            if task is not asyncio.current_task():task.cancel()
        current=self.sessions.get(key)
        if current is not None and current.generation!=generation:
            return {'protocol':PROTOCOL,'connected':False,'server':None}
        await self._drop(key)
        return {'protocol':PROTOCOL,'connected':False,'server':None}
    async def retire(self,employee_no,session_id,generation):
        self._key(employee_no,session_id,generation)
        self.journal.retire_connection(employee_no,session_id,generation)
        return await self.disconnect(employee_no,session_id,generation)
    async def open(self,raw):
        require(not self.closed,'NOT_CONFIGURED',503)
        value=connection_request(raw,self.clock);scope=value['scope'];fields=value['fields']
        key=self._key(value['employee_no'],scope['session_id'],scope['generation']);rid=value['request_id']
        require(not self.journal.connection_retired(*key,scope['generation']),'SCOPE_EXPIRED',403)
        require(key not in self.connecting,'CONNECTION_BUSY',409)
        # A replay cannot remain valid beyond its original short-lived scope.
        # Keep only bounded in-memory admission receipts, never credential hashes.
        for prior,record in list(self.seen_requests.items()):
            if record[-1]<=self.clock():self.seen_requests.pop(prior,None)
        if rid in self.seen_requests:
            previous=self.seen_requests[rid];current=await self.get(*key,scope['generation'])
            require(previous==(*key,scope['generation'],fields['host'],fields['port'],fields['username'],scope['expires_at']) and current is not None and current.request_id==rid and current.password==fields['password'],'IDEMPOTENCY_CONFLICT',409)
            # Never repeats SSH authentication even after a lost HTTP acknowledgement.
            return await self.status(*key,scope['generation'])
        require(len(self.seen_requests)<1000 and len(self.sessions)+len(self.connecting)<64 and len(self.connecting)<2,'RATE_LIMIT',429)
        old=self.sessions.get(key);require(old is None or not old.lock.locked(),'CONNECTION_BUSY',409)
        self.seen_requests[rid]=(*key,scope['generation'],fields['host'],fields['port'],fields['username'],scope['expires_at']);self.connecting[key]=(scope['generation'],asyncio.current_task())
        connection=None;accepted=False
        try:
            await self._drop(key)
            client=PinnedClient(self.journal,value['employee_no'],fields['host'],fields['port'])
            async with asyncio.timeout(min(15,max(0,scope['expires_at']-self.clock()))):
                connection=await self.connect(host=fields['host'],port=fields['port'],username=fields['username'],
                    known_hosts=(),client_factory=lambda:client,config=[],proxy_command=None,agent_path=None,
                    agent_forwarding=False,client_keys=[],password=fields['password'],public_key_auth=False,
                    password_auth=True,x509_trusted_certs=None,x509_trusted_cert_paths=[],preferred_auth=['password'],
                    kbdint_auth=False,host_based_auth=False,gss_auth=False,gss_kex=False,connect_timeout=5,
                    login_timeout=10,keepalive_interval=30,keepalive_count_max=1,request_pty=False,x11_forwarding=False,
                    send_env=[],env={},encoding=None)
            require(client.verified,'SSH_HOST_KEY_REJECTED',503)
            require(not self.closed and not self.journal.connection_retired(*key,scope['generation']) and self.connecting.get(key)==(scope['generation'],asyncio.current_task()) and self.clock()<scope['expires_at'],'SCOPE_EXPIRED',403)
            self.sessions[key]=Session(*key,scope['generation'],str(uuid4()),
                fields['username']+'@'+fields['host']+':'+str(fields['port']),rid,connection,fields['password'],
                self.clock(),high_privilege=fields['username']=='root')
            accepted=True
            return await self.status(*key,scope['generation'])
        except asyncssh.HostKeyNotVerifiable:raise Rejected('SSH_HOST_KEY_REJECTED',503) from None
        except asyncssh.PermissionDenied:raise Rejected('SSH_AUTH_FAILED',403) from None
        except TimeoutError:raise Rejected('SSH_UNAVAILABLE',503) from None
        except (OSError,asyncssh.Error):raise Rejected('SSH_UNAVAILABLE',503) from None
        finally:
            if self.connecting.get(key)==(scope['generation'],asyncio.current_task()):self.connecting.pop(key,None)
            fields['password']=''
            if connection is not None and not accepted:
                connection.close()
                with contextlib.suppress(Exception):await asyncio.wait_for(connection.wait_closed(),2)
    async def expire(self):
        for key,session in list(self.sessions.items()):
            if self.clock()-session.last_used>=7200 or session.connection is None or session.connection.is_closed():await self._drop(key)
    async def close(self):
        self.closed=True
        tasks=list(self.connecting.values());self.connecting.clear()
        for generation,task in tasks:
            if task is not asyncio.current_task():task.cancel()
        for key in list(self.sessions):await self._drop(key)
        self.seen_requests.clear()

class SSHTransport:
    def __init__(self,registry):self.registry=registry
    def redact(self,text,session):
        for value in sorted((self.registry.token,session.password),key=len,reverse=True):
            if value:text=text.replace(value,'[REDACTED]')
        return re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)','[REDACTED PRIVATE KEY]',text,flags=re.S)
    async def command(self,connection,command,input_bytes,execution,*,limit=65536):
        if execution.cancelled:raise asyncio.CancelledError()
        if execution.deadline is not None:require(time.time()<execution.deadline,'SCOPE_EXPIRED' if time.time()>=execution.scope_expires else 'OPERATION_TIMEOUT',403)
        execution.dispatched=True
        process=await connection.create_process(command,encoding=None,stderr=asyncssh.STDOUT,request_pty=False,env={},send_env=[])
        execution.process=process
        if input_bytes is not None:process.stdin.write(input_bytes)
        process.stdin.write_eof();output=bytearray()
        while True:
            chunk=await process.stdout.read(4096)
            if not chunk:break
            require(len(output)+len(chunk)<=limit,'OUTPUT_LIMIT',503);output.extend(chunk)
        await process.wait_closed()
        require(process.exit_status is not None and process.exit_status>=0,'OPERATION_FAILED',503)
        return process.exit_status,bytes(output)
    async def execute(self,session,action,args,execution):
        connection=session.connection;require(connection is not None and not connection.is_closed(),'CONNECTION_REQUIRED',409)
        try:
            if action=='run_command':
                shell='cd -- "$HOME" && cd -- '+shlex.quote(args['relative_cwd'])+' && exec /bin/sh -c '+shlex.quote(args['command'])
                code,raw=await self.command(connection,'/bin/sh -c '+shlex.quote(shell),None,execution)
                text=self.redact(raw.decode('utf-8','replace'),session).replace('\x00','')
                text=text.encode('utf-8')[:65536].decode('utf-8','ignore')
                return {'host_id':session.host_id,'exit_code':code,'output':text,'truncated':len(raw)>len(text.encode()),'timed_out':False,'stop_confirmed':True,'high_privilege':session.high_privilege,'sandboxed':False}
            if action in {'read_file','write_file'}:
                payload=dumps({'action':action,'root':'__AUTHENTICATED_HOME__','arguments':args}).encode('utf-8')
                code,raw=await self.command(connection,COMMAND,payload,execution,limit=256*1024)
                if code==127:raise Rejected('PYTHON_UNAVAILABLE',503)
                require(code==0,'OPERATION_FAILED',503);value=decode(raw)
                require(isinstance(value,dict) and type(value.get('ok')) is bool,'OPERATION_FAILED',503)
                if not value['ok']:
                    error=value.get('error_code');raise Rejected(error if error in ERRORS else 'OPERATION_FAILED',503)
                require(set(value)=={'ok','result'} and isinstance(value['result'],dict),'OPERATION_FAILED',503)
                result={'host_id':session.host_id,'relative_path':args['relative_path'],**value['result']}
                if action=='read_file':
                    require(set(value['result'])=={'content','sha256','size_bytes','truncated'},'OPERATION_FAILED',503)
                    text=result.get('content');require(isinstance(text,str) and '\x00' not in text,'OPERATION_FAILED',503)
                    data=text.encode('utf-8');require(len(data)<=args['max_bytes'] and type(result.get('size_bytes')) is int and result['size_bytes']==len(data) and result.get('sha256')==hashlib.sha256(data).hexdigest() and result.get('truncated') is False,'OPERATION_FAILED',503)
                    require(self.redact(text,session)==text,'PATH_NOT_ALLOWED',403)
                else:
                    data=args['content'].encode('utf-8');require(set(value['result'])=={'sha256','size_bytes','created'} and result.get('sha256')==hashlib.sha256(data).hexdigest() and type(result.get('size_bytes')) is int and result['size_bytes']==len(data) and type(result.get('created')) is bool and result['created']==(args['expected_sha256']=='MISSING'),'OPERATION_FAILED',503)
                return result
            require(action=='inspect_server')
            checks=ENVIRONMENT_CHECKS if args['category']=='environment' else ORACLE_CHECKS if args['category']=='oracle' else ENVIRONMENT_CHECKS+ORACLE_CHECKS
            result=[];remaining=65536
            for name,command in checks:
                code,raw=await self.command(connection,command,None,execution,limit=remaining)
                text=self.redact(raw.decode('utf-8','replace'),session).replace('\x00','')
                text=text.encode('utf-8')[:remaining].decode('utf-8','ignore');remaining-=len(text.encode('utf-8'))
                result.append({'name':name,'status':'ok' if code==0 else 'unavailable' if code==127 else 'failed','output':text,'exit_code':code})
            return {'host_id':session.host_id,'category':args['category'],'checks':result,'truncated':False}
        except (OSError,asyncssh.Error):raise Rejected('SSH_UNAVAILABLE',503) from None
        finally:execution.close()
