"""Desktop-only service authority. Install as app.local_service, never as a cloud bypass.

The desktop main process owns its IAM credential and the control grant. A reviewed
native plugin receives only an in-memory local capability, not either credential.
This router deliberately does not accept employee/device/broker-session claims.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import secrets
import time
from types import SimpleNamespace
from uuid import uuid4
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from maimi_projects import server_operations as wire

PREFIX='/v1/connectors/local-service'
CAP=re.compile(r'^[A-Za-z0-9_-]{43}$')
HEADERS={'Cache-Control':'no-store','Pragma':'no-cache'}
digest=lambda v:hashlib.sha256(v.encode()).hexdigest()
opaque=lambda:secrets.token_urlsafe(32)

class Denied(Exception):
    def __init__(self,code='LOCAL_SERVICE_DENIED',status=403):
        self.code,self.status=code,status
        super().__init__(code)

def require(value,code='LOCAL_SERVICE_DENIED',status=403):
    if not value:raise Denied(code,status)

def exact(value,fields):
    require(isinstance(value,dict) and set(value)==set(fields),'ARGUMENTS_INVALID',400)

@dataclass(frozen=True)
class Identity:
    employee:str
    iam_session:str=field(repr=False)
    instance:str=field(repr=False)
    desktop_grant_hash:str=field(repr=False)
    expires_at:int
    def binding(self):
        return digest(json.dumps([self.employee,self.iam_session,self.instance,self.desktop_grant_hash]))

async def identity_for(service,request,clock=time.time):
    """Read the actual existing desktop grant; no cookie or request identity claims."""
    from app import desktop_auth as auth
    require(os.getenv('AUTH_MODE','iam_direct')=='iam_direct')
    require(not request.headers.get('origin') and not request.headers.get('sec-fetch-site'))
    values=request.headers.getlist('authorization')
    require(len(values)==1 and values[0].startswith('Bearer '))
    token=values[0][7:]
    require(auth.TOKEN.fullmatch(token))
    record=await auth.desktop_store(service).read_grant(token)
    require(isinstance(record,dict) and record.get('expires_at',0)>clock(),'DESKTOP_SESSION_EXPIRED',401)
    user=await service.authenticate(record['sid'])
    require(isinstance(user,dict) and user.get('user_no') in wire.USERS,'SERVICE_NOT_AUTHORIZED')
    require(wire.canonical_uuid(record.get('instance')),'DESKTOP_IDENTITY_INVALID')
    return Identity(user['user_no'],record['sid'],record['instance'],digest(token),int(record['expires_at']))

class RedisGrants:
    """Only non-secret scope metadata and one-way capability digests are persisted."""
    PREFIX='megmeet:desktop-service:r65:'
    def __init__(self,redis):self.redis=redis
    def key(self,suffix):return self.PREFIX+suffix
    async def create(self,identity,native_session,clock=time.time):
        require(isinstance(native_session,str) and wire.SID.fullmatch(native_session),'NATIVE_SESSION_INVALID',400)
        control,tool=opaque(),opaque();gid=digest(control)
        until=min(identity.expires_at,int(clock())+600)
        require(until>clock()+5,'DESKTOP_SESSION_EXPIRED',401)
        slot=digest(identity.binding()+':'+native_session)
        record={'binding':identity.binding(),'employee':identity.employee,'native_session':native_session,
            'broker_session':str(uuid4()),'generation':opaque(),'expires_at':until,
            'control_hash':gid,'tool_hash':digest(tool),'slot':slot}
        # Atomic pointer replacement fences every earlier grant for this actual
        # desktop+native session. A bounded rate prevents grant-store flooding.
        script="""local n=redis.call('INCR',KEYS[4]);if n==1 then redis.call('EXPIRE',KEYS[4],600) end
        if n>64 then return {0,''} end
        local old=redis.call('GET',KEYS[3]);local prior=''
        if old then prior=redis.call('GET',ARGV[3]..old) or '' end
        redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]);redis.call('SET',KEYS[2],ARGV[1],'EX',ARGV[2]);
        redis.call('SET',KEYS[3],ARGV[4],'EX',ARGV[2]);return {1,prior}"""
        result=await self.redis.eval(script,4,self.key('grant:'+gid),self.key('grant:'+record['tool_hash']),
            self.key('slot:'+slot),self.key('rate:'+identity.binding()),json.dumps(record),until-int(clock()),self.key('grant:'),gid)
        require(result and result[0]==1,'SERVICE_RATE_LIMIT',429)
        previous=json.loads(result[1]) if result[1] else None
        return record,{'control_grant':control,'tool_grant':tool,'expires_at':until},previous
    async def read(self,token,identity,kind,clock=time.time):
        require(isinstance(token,str) and CAP.fullmatch(token))
        raw=await self.redis.get(self.key('grant:'+digest(token)))
        require(raw,'SERVICE_GRANT_EXPIRED')
        record=json.loads(raw)
        require(record['binding']==identity.binding() and record[kind+'_hash']==digest(token)
                and record['expires_at']>clock(),'SERVICE_GRANT_EXPIRED')
        active=await self.redis.get(self.key('slot:'+record['slot']))
        if isinstance(active,bytes):active=active.decode()
        require(active==record['control_hash'],'SERVICE_GRANT_REPLACED')
        return record
    async def revoke(self,record):
        await self.redis.eval("""redis.call('DEL',KEYS[1],KEYS[2]);if redis.call('GET',KEYS[3])==ARGV[1] then redis.call('DEL',KEYS[3]) end;return 1""",3,
            self.key('grant:'+record['control_hash']),self.key('grant:'+record['tool_hash']),self.key('slot:'+record['slot']),record['control_hash'])
    async def lock(self,record):
        value=opaque();ok=await self.redis.set(self.key('busy:'+record['control_hash']),value,nx=True,ex=360)
        require(ok,'SERVICE_BUSY',409);return value
    async def unlock(self,record,value):
        await self.redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end;return 0",1,self.key('busy:'+record['control_hash']),value)

class BrokerPort:
    """Reuses the existing closed broker wire contract, without a cloud Context."""
    def __init__(self,configuration=wire.GatewayConfig.from_env,read_secret=wire.private_read,transport=None,clock=time.time,sleep=asyncio.sleep):
        self.configuration,self.read_secret,self.transport,self.clock,self.sleep=configuration,read_secret,transport,clock,sleep
        self.validation=wire.ServerOperations(context=None,scope=None,authorize=None,clock=clock,sleep=sleep)
    def configured(self,record):
        config=self.configuration();require(config is not None and config.allowed(record['employee']),'SERVICE_NOT_CONFIGURED',503)
        token=self.read_secret(config.token_file,4096).decode('ascii').strip()
        require(re.fullmatch(r'[A-Za-z0-9_-]{32,512}',token),'SERVICE_NOT_CONFIGURED',503)
        return config,token
    def scope(self,record):
        require(record['expires_at']>self.clock()+1,'SERVICE_GRANT_EXPIRED')
        return {'session_id':record['broker_session'],'invocation_id':'pi_'+opaque(),'job_id':str(uuid4()),
                'generation':record['generation'],'expires_at':min(record['expires_at'],int(self.clock())+330)}
    async def execute(self,record,action,args,validate,*,cleanup=False):
        config,token=self.configured(record)
        query={'employee_no':record['employee'],'session_id':record['broker_session'],'generation':record['generation']}
        operation=None;scope=None;submitted=False;connect_sent=False
        async def check():
            if cleanup:return
            await validate()
            require(self.configuration()==config,'SERVICE_CONFIGURATION_CHANGED')
        async with httpx.AsyncClient(base_url=config.broker_origin,headers={'Authorization':'Bearer '+token,'Accept-Encoding':'identity'},timeout=25,trust_env=False,follow_redirects=False,transport=self.transport) as client:
            try:
                await check()
                if action in ('connect','status','list_servers','disconnect','retire'):
                    if action=='connect':
                        exact(args,{'ip','password'})
                        fields=wire.login_fields({'host':args['ip'],'password':args['password'],'port':22,'username':'root'})
                        scope=self.scope(record);connect_sent=True
                        try:
                            _,result=await self.validation._request(client,'POST','/v1/connections',body={'protocol':wire.PROTOCOL,'employee_no':record['employee'],'scope':scope,'request_id':scope['job_id'],'fields':fields},deadline=25)
                        finally:fields['password']=''
                    else:
                        exact(args,{})
                        _,result=await self.validation._request(client,'DELETE' if action in ('disconnect','retire') else 'GET','/v1/connection-grants' if action=='retire' else '/v1/connections',query=query)
                    result=wire.connection_result(result)
                    require((action!='connect' or result['connected']) and (action not in ('disconnect','retire') or not result['connected']),'SERVICE_RESPONSE_INVALID',502)
                else:
                    args=wire.arguments(action,args);scope=self.scope(record);submitted=True
                    status,reply=await self.validation._request(client,'POST','/v1/operations',body={'protocol':wire.PROTOCOL,'employee_no':record['employee'],'scope':scope,'request_id':scope['job_id'],'action':action,'arguments':args})
                    require(status==202,'SERVICE_RESPONSE_INVALID',502);operation=self.validation._response(reply,scope,initial=True)
                    while True:
                        await check();await self.sleep(.25);await check()
                        _,reply=await self.validation._request(client,'GET','/v1/operations/'+operation,query={'employee_no':record['employee'],'job_id':scope['job_id']})
                        self.validation._response(reply,scope,operation);await check()
                        if reply['status'] in wire.TERMINAL:break
                    require(reply['status']=='completed','SERVICE_OPERATION_'+reply['status'].upper(),409)
                    result=wire.model_view(action,wire.validate_result(action,args,reply['result']))
                serialized=json.dumps(result,ensure_ascii=False)
                require(token not in serialized and (action!='connect' or len(args.get('password',''))<8 or args['password'] not in serialized),'SERVICE_RESPONSE_INVALID',502)
                await check();return result
            except BaseException as error:
                if operation:
                    stopped=await self.validation._cancel_join(client,operation,scope,SimpleNamespace(user_no=record['employee']))
                    if not stopped:raise Denied('REMOTE_STOP_UNCONFIRMED',409) from None
                elif submitted and not getattr(error,'remote_rejected',False):raise Denied('REMOTE_RESULT_UNCONFIRMED',409) from None
                if connect_sent:
                    async def disconnect():
                        _,value=await self.validation._request(client,'DELETE','/v1/connection-grants',query=query)
                        require(not wire.connection_result(value)['connected'])
                    task=asyncio.create_task(disconnect())
                    try:await asyncio.wait_for(asyncio.shield(task),10)
                    except BaseException:raise Denied('REMOTE_STOP_UNCONFIRMED',409) from None
                    finally:
                        if not task.done():task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                raise
            finally:
                if action=='connect':args['password']=''

def make_router(*,with_service=None,authenticate=identity_for,store_factory=RedisGrants,port_factory=BrokerPort):
    router=APIRouter(prefix=PREFIX)
    async def dispatch(request,action):
        try:
            require(not request.url.query,'ARGUMENTS_INVALID',400)
            require(request.headers.get('content-type','').split(';')[0]=='application/json','ARGUMENTS_INVALID',400)
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk);require(len(raw)<=32768,'REQUEST_TOO_LARGE',413)
            def pairs(rows):
                value={}
                for key,item in rows:
                    require(key not in value,'ARGUMENTS_INVALID',400);value[key]=item
                return value
            try:body=json.loads(raw,object_pairs_hook=pairs)
            except (ValueError,UnicodeError):raise Denied('ARGUMENTS_INVALID',400) from None
            finally:raw[:]=b'\x00'*len(raw)
            async def run(service):
                identity=await authenticate(service,request);store=store_factory(service.store.redis);port=port_factory()
                if action=='grants':
                    exact(body,{'native_session_id'})
                    record,result,prior=await store.create(identity,body['native_session_id'])
                    if prior:
                        try:await port.execute(prior,'retire',{},lambda:None,cleanup=True)
                        except Exception:
                            await store.revoke(record);raise Denied('PRIOR_DISCONNECT_UNCONFIRMED',409) from None
                    return result
                kind='control' if action in ('connect','revoke') else 'tool'
                tokens=request.headers.getlist('x-maimi-service-grant');require(len(tokens)==1)
                record=await store.read(tokens[0],identity,kind)
                async def validate():
                    fresh=await authenticate(service,request)
                    require(fresh.binding()==identity.binding(),'DESKTOP_SESSION_CHANGED')
                    require(await store.read(tokens[0],fresh,kind)==record,'SERVICE_GRANT_CHANGED')
                if action=='revoke':
                    exact(body,{})
                    await store.revoke(record)
                    result=await port.execute(record,'retire',{},validate,cleanup=True)
                    return {'revoked':True,'disconnect_confirmed':result['connected'] is False}
                if action=='connect':exact(body,{'ip','password'});operation='connect';args=body
                else:
                    exact(body,{'action','arguments'});operation=body['action'];args=body['arguments']
                    require(operation in wire.ACTIONS,'ACTION_NOT_ALLOWED',400)
                lock=await store.lock(record)
                try:
                    async def monitor(task):
                        while not task.done():
                            await asyncio.sleep(.2)
                            try:
                                if await request.is_disconnected():raise Denied('CLIENT_DISCONNECTED')
                                await validate()
                            except BaseException:task.cancel();return
                    task=asyncio.create_task(port.execute(record,operation,args,validate));watch=asyncio.create_task(monitor(task))
                    try:
                        async with asyncio.timeout(350):return await task
                    finally:
                        watch.cancel();await asyncio.gather(watch,return_exceptions=True)
                finally:await store.unlock(record,lock)
            async def guarded_run(service):
                # IAM's resource owner intentionally normalizes arbitrary
                # exceptions. Preserve our closed public error categories
                # without asking it to expose an underlying raw exception.
                try:return {'ok':True,'value':await run(service)}
                except (Denied,wire.ServiceFailure) as error:
                    return {'ok':False,'code':error.code if isinstance(error,Denied) else error.code.upper(),'status':error.status}
            if with_service is None:
                from app import iam_oauth as iam
                envelope=await iam._with_service(guarded_run)
            else:envelope=await with_service(guarded_run)
            if not envelope['ok']:raise Denied(envelope['code'],envelope['status'])
            return JSONResponse(envelope['value'],headers=HEADERS)
        except (Denied,wire.ServiceFailure) as e:
            code=e.code if isinstance(e,Denied) else e.code.upper()
            return JSONResponse({'error_code':code},status_code=e.status,headers=HEADERS)
        except BaseException:
            return JSONResponse({'error_code':'LOCAL_SERVICE_UNAVAILABLE'},status_code=503,headers=HEADERS)
        finally:
            if 'body' in locals() and isinstance(body,dict) and 'password' in body:body['password']=''
    @router.post('/grants')
    async def grants(request:Request):return await dispatch(request,'grants')
    @router.post('/connect')
    async def connect(request:Request):return await dispatch(request,'connect')
    @router.post('/invoke')
    async def invoke(request:Request):return await dispatch(request,'invoke')
    @router.post('/revoke')
    async def revoke(request:Request):return await dispatch(request,'revoke')
    return router

router=make_router()
