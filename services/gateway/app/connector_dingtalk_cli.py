"""Pinned official DingTalk Workspace CLI, isolated by authenticated IAM owner.

No shell, arbitrary executable, raw API, auth token, global flag or local-file
argument is exposed to the model. Credentials remain in a gateway-only volume.
Business writes use the existing consumed-once human confirmation protocol.
"""
from __future__ import annotations
import asyncio
import csv
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import socket
import time
from urllib.parse import urlsplit, parse_qs, unquote

VERSION = '1.0.62'
PROVIDER = 'dingtalk_workspace_cli'
ROOT_DEFAULT = '/var/lib/megmeet-dingtalk-cli'
BIN_DEFAULT = '/opt/dingtalk-cli/dws'
CATALOG_DEFAULT = '/app/app/dingtalk-cli-catalog.json'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PUBLIC = 60 * 1024
HEADERS = {'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'}
MESSAGES = {
    'dingtalk_cli_unavailable':'钉钉连接服务暂时不可用，请稍后重试。',
    'dingtalk_cli_authorization_required':'连接本人的钉钉后，即可在对话中使用已授权功能。',
    'dingtalk_cli_authorizing':'请在钉钉授权页面选择本人公司账号并完成授权。',
    'dingtalk_cli_authorization_expired':'本次授权已过期，请重新点击连接钉钉。',
    'dingtalk_cli_admin_required':'公司尚未开放 CLI 访问，请联系钉钉管理员在开放平台的 CLI 接入管理中开通。',
    'dingtalk_cli_authorization_failed':'钉钉授权未完成，请重试；如提示组织未开放 CLI，请联系公司钉钉管理员。',
    'dingtalk_cli_busy':'此钉钉账号正在处理操作，请稍后重试。',
    'dingtalk_cli_arguments_invalid':'钉钉操作参数不符合已公布的命令格式。',
    'dingtalk_cli_command_unknown':'该钉钉命令尚未接入，请重新发现可用命令。',
    'dingtalk_cli_command_failed':'钉钉没有完成此操作，请检查授权范围和输入后再试。',
    'dingtalk_cli_response_too_large':'返回结果过多，请缩小查询范围或分页读取。',
    'dingtalk_cli_timeout':'钉钉响应超时，请稍后重试。',
    'dingtalk_cli_write_result_unknown':'此次写入结果暂无法确认，请先到钉钉核对，避免重复发送或创建。',
    'dingtalk_cli_identity_changed':'钉钉账号已改变，请重新发起操作。',
}

def failure(code, status=400):
    from .enterprise_connectors import ConnectorFailure
    return ConnectorFailure(status, code)

def packed(value, limit=MAX_RESPONSE):
    try:
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    except (ValueError,TypeError,RecursionError):
        raise failure('dingtalk_cli_arguments_invalid') from None
    if len(raw)>limit:
        raise failure('dingtalk_cli_response_too_large',413)
    return raw

def digest(value):
    return hashlib.sha256(packed(value)).hexdigest()

def authorization_url(value):
    try:
        if not isinstance(value,str) or len(value)>8192 or any(ord(c)<33 for c in value):
            return None
        u=urlsplit(value)
        if u.scheme!='https' or u.hostname not in {'login.dingtalk.com','open-dev.dingtalk.com'} or u.netloc not in {u.hostname,u.hostname+':443'} or u.username or u.password or '\\' in value:
            return None
        # Only the device-flow link printed by the pinned CLI is accepted.
        if u.hostname=='login.dingtalk.com':
            if not re.fullmatch(r'/[A-Za-z0-9_./-]+',u.path) or not parse_qs(u.query).get('user_code'): return None
        else:
            fragment=unquote(u.fragment)
            if u.path!='/fe/old' or not fragment.startswith('/personalAuthorization?'): return None
            query=parse_qs(fragment.split('?',1)[1])
            if not query.get('flowId') or not query.get('userCode'): return None
        return value
    except ValueError:
        return None

def safe_arguments(value, depth=0):
    if depth>15:
        raise failure('dingtalk_cli_arguments_invalid')
    if isinstance(value,str):
        # dws supports @file expansion. Never allow server files via a model.
        if '\x00' in value or value.lstrip().startswith('@') or len(value)>16000:
            raise failure('dingtalk_cli_arguments_invalid')
        # JSON-valued flags also support @file strings in nested values.
        if value.lstrip().startswith(('{','[')):
            try: decoded=json.loads(value)
            except (ValueError,RecursionError): return
            safe_arguments(decoded,depth+1)
    elif isinstance(value,list):
        if len(value)>200: raise failure('dingtalk_cli_arguments_invalid')
        for item in value: safe_arguments(item,depth+1)
    elif isinstance(value,dict):
        if len(value)>200: raise failure('dingtalk_cli_arguments_invalid')
        for key,item in value.items():
            if not isinstance(key,str): raise failure('dingtalk_cli_arguments_invalid')
            safe_arguments(item,depth+1)
    elif value is not None and type(value) not in {int,float,bool}:
        raise failure('dingtalk_cli_arguments_invalid')

def scrub(value, depth=0):
    if depth>25: return '[内容层级过深]'
    if isinstance(value,dict):
        return {str(k): ('[已隐藏]' if re.sub(r'[-_]','',str(k)).lower() in {
            'accesstoken','refreshtoken','clientsecret','appsecret','authorization','devicecode'} else scrub(v,depth+1)) for k,v in value.items()}
    if isinstance(value,list): return [scrub(x,depth+1) for x in value]
    return value

class CLIRunner:
    def __init__(self,binary=None,root=None,store=None):
        self.binary=Path(binary or os.getenv('DINGTALK_CLI_BINARY',BIN_DEFAULT))
        self.root=Path(root or os.getenv('DINGTALK_CLI_STATE_DIR',ROOT_DEFAULT)).absolute()
        self.store=store
        try:self.maximum_processes=int(os.getenv('DINGTALK_CLI_MAX_PROCESSES','2'))
        except ValueError:raise failure('dingtalk_cli_unavailable',503) from None
        if not 1<=self.maximum_processes<=8:raise failure('dingtalk_cli_unavailable',503)
        # Uvicorn workers share HOSTNAME; a replaced gateway cannot be blocked
        # by dead-process leases from its predecessor container.
        self.process_namespace='gateway:'+socket.gethostname()

    async def acquire_process(self,auth=False):
        if self.store is None:return None
        token=secrets.token_hex(24)
        for slot in range(self.maximum_processes):
            if await self.store.put('dws-process',self.process_namespace,str(slot),token,660 if auth else 90,nx=True):
                return (str(slot),token)
        raise failure('dingtalk_cli_busy',409)

    async def release_process(self,lease):
        if lease is not None:
            await self.store.replace_if_equal('dws-process',self.process_namespace,lease[0],lease[1],None)

    def home(self,owner):
        if not isinstance(owner,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',owner):
            raise failure('connector_identity_invalid',403)
        target=self.root/hashlib.sha256(('dingtalk-cli-v1:'+owner).encode()).hexdigest()
        if self.root.is_symlink() or target.is_symlink(): raise failure('dingtalk_cli_unavailable',503)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        target.mkdir(mode=0o700,exist_ok=True)
        if target.resolve().parent!=self.root.resolve(): raise failure('dingtalk_cli_unavailable',503)
        target.chmod(0o700)
        return target

    def environment(self,owner):
        home=self.home(owner)
        # Explicit allowlist: no inherited DWS_CLIENT_SECRET, TOKEN, endpoint,
        # proxy or shell initialization variables from gateway/another user.
        env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(home),'LANG':'C.UTF-8',
             'DWS_LANG':'zh','NO_COLOR':'1','TERM':'dumb','DO_NOT_TRACK':'1',
             'BROWSER':'/bin/false',
             'DWS_CONFIG_DIR':str(home/'config'),'DWS_KEYCHAIN_DIR':str(home/'keychain'),
             'XDG_CONFIG_HOME':str(home/'xdg-config'),'XDG_DATA_HOME':str(home/'xdg-data'),
             'TMPDIR':str(home/'tmp')}
        for name in ('config','keychain','tmp'):
            child=home/name
            if child.is_symlink(): raise failure('dingtalk_cli_unavailable',503)
            child.mkdir(mode=0o700,exist_ok=True)
        return env

    async def spawn(self,owner,args):
        if not self.binary.is_file(): raise failure('dingtalk_cli_unavailable',503)
        env=self.environment(owner)
        lease=await self.acquire_process(auth=args[:2]==['auth','login'])
        try:
            proc=await asyncio.create_subprocess_exec(str(self.binary),*args,
                stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                env=env,cwd=str(self.home(owner)),start_new_session=True)
            proc._maimi_process_lease=lease
            return proc
        except BaseException:
            await self.release_process(lease)
            raise

    async def stop(self,proc):
        try:
            if proc.returncode is None:
                try:
                    if os.name=='posix':os.killpg(proc.pid,signal.SIGKILL)
                    else:proc.kill()
                except ProcessLookupError:pass
            await proc.wait()
        finally:
            lease=getattr(proc,'_maimi_process_lease',None)
            proc._maimi_process_lease=None
            await self.release_process(lease)

    def discard_logs(self,owner):
        # Upstream always creates DEBUG diagnostics. Do not retain requests or
        # output bodies. Only this exact SDK-owned logs directory is removed.
        for relative in ('config/logs','tmp'):
            target=self.home(owner)/relative
            if target.is_dir() and not target.is_symlink(): shutil.rmtree(target)

    async def run(self,owner,args,timeout=45):
        proc=await self.spawn(owner,args)
        async def bounded(stream):
            result=bytearray()
            while chunk:=await stream.read(65536):
                result.extend(chunk)
                if len(result)>MAX_RESPONSE: raise failure('dingtalk_cli_response_too_large',413)
            return bytes(result)
        try:
            async with asyncio.timeout(timeout):
                stdout,stderr=await asyncio.gather(bounded(proc.stdout),bounded(proc.stderr))
                await proc.wait()
            try: value=json.loads(stdout)
            except (ValueError,RecursionError): raise failure('dingtalk_cli_command_failed',502) from None
            if proc.returncode or (isinstance(value,dict) and value.get('success') is False):
                # Raw upstream diagnostics can contain credentials or inputs.
                raise failure('dingtalk_cli_command_failed',502)
            return value
        except TimeoutError:
            raise failure('dingtalk_cli_timeout',504) from None
        finally:
            await self.stop(proc)
            self.discard_logs(owner)

class CLIConnections:
    def __init__(self,svc,runner=None,catalog=None):
        self.svc,self.store=svc,svc.store
        self.runner=runner or CLIRunner(store=self.store)
        self._tasks=set()
        self.catalog_path=Path(os.getenv('DINGTALK_CLI_CATALOG',CATALOG_DEFAULT))
        self._catalog=catalog

    async def close(self):
        tasks=list(self._tasks)
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

    def contract(self):
        if self._catalog is None:
            try: self._catalog=json.loads(self.catalog_path.read_text(encoding='utf8'))
            except (OSError,ValueError): raise failure('dingtalk_cli_unavailable',503) from None
        if self._catalog.get('version')!=VERSION: raise failure('dingtalk_cli_unavailable',503)
        return self._catalog

    async def generation(self,owner,rotate=False):
        if not rotate:
            value=await self.store.get('dws-generation',owner,'current')
            if value:return value
        value=secrets.token_hex(24)
        if await self.store.put('dws-generation',owner,'current',value,366*86400,nx=not rotate):return value
        return await self.store.get('dws-generation',owner,'current')

    @asynccontextmanager
    async def lock(self,owner,ttl=90):
        token=secrets.token_hex(24)
        if not await self.store.put('dws-lock',owner,'operation',token,ttl,nx=True):
            raise failure('dingtalk_cli_busy',409)
        try: yield
        finally: await self.store.replace_if_equal('dws-lock',owner,'operation',token,None)

    def public(self,status,reason='',**extra):
        result={'provider':PROVIDER,'version':VERSION,'status':status,'reason':reason or None,
            'status_message':MESSAGES.get(reason,'已连接本人的钉钉，写操作会先请你确认。'),
            'authorization_url':None,'expires_at':None,'account':None,
            'capabilities':self.contract()['products']}
        result.update(extra)
        return result

    async def profiles(self,owner):
        # v1.0.62 has no auth status --readonly. profile list reads local tokens
        # without refreshing. Serialized because it can migrate old config.
        value=await self.runner.run(owner,['profile','list','--format=json'],timeout=10)
        rows=value.get('profiles',[]) if isinstance(value,dict) else []
        row=next((r for r in rows if r.get('isCurrent')),None)
        if row is None and len(rows)==1: row=rows[0]
        if not row:
            return None
        usable=row.get('status') in {'active','valid'}
        # profile list calls an expired access token "expired" even when the
        # OAuth refresh token remains valid. Let the official business command
        # refresh it under this owner's lock rather than force a new login.
        if row.get('status')=='expired':
            try:usable=datetime.fromisoformat(row.get('refreshExpAt','').replace('Z','+00:00')).timestamp()>time.time()
            except (ValueError,TypeError,AttributeError):usable=False
        if not usable:return None
        profile=row.get('profile','')
        if not isinstance(profile,str) or not re.fullmatch(r'[A-Za-z0-9._-]+:[A-Za-z0-9._-]+',profile): return None
        return {'profile':profile,'name':str(row.get('userName',''))[:100],
            'organization':str(row.get('corpName',''))[:100]}

    async def status(self,owner):
        raw=await self.store.get('dws-login',owner,'current')
        if raw:
            job=self.svc.unseal(owner,raw)
            if job.get('expires_at',0)>time.time(): return job
        cached=await self.store.get('dws-status',owner,'current')
        if cached: return self.svc.unseal(owner,cached)
        try:
            async with self.lock(owner): account=await self.profiles(owner)
            result=self.public('ready',account=account) if account else self.public('authorization_required','dingtalk_cli_authorization_required')
        except Exception as exc:
            code=getattr(exc,'code','dingtalk_cli_unavailable')
            result=self.public('unavailable',code if code in MESSAGES else 'dingtalk_cli_unavailable')
        await self.store.put('dws-status',owner,'current',self.svc.seal(owner,result),20)
        return result

    async def login(self,owner):
        await self.store.budget(owner)
        existing=await self.store.get('dws-login',owner,'current')
        if existing:
            job=self.svc.unseal(owner,existing)
            if job.get('status')=='authorizing' and job.get('expires_at',0)>time.time(): return job
        lease=secrets.token_hex(24)
        if not await self.store.put('dws-lock',owner,'operation',lease,660,nx=True): raise failure('dingtalk_cli_busy',409)
        try:
            await self.generation(owner,rotate=True)
            public=self.public('authorizing','dingtalk_cli_authorizing',expires_at=int(time.time())+600)
            await self.store.delete('dws-cancel',owner,'current')
            await self.store.put('dws-login',owner,'current',self.svc.seal(owner,public),610)
            task=asyncio.create_task(self._login(owner,public,lease))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return public
        except BaseException:
            await self.store.replace_if_equal('dws-lock',owner,'operation',lease,None)
            raise

    async def _login(self,owner,public,lease):
        proc=None
        try:
            proc=await self.runner.spawn(owner,['auth','login','--device','--recommend','--no-browser','--format=table','--yes'])
            async def lines(stream):
                total=0
                while line:=await stream.readline():
                    total+=len(line)
                    if total>MAX_RESPONSE: raise failure('dingtalk_cli_response_too_large',413)
                    # Do not retain raw CLI stdout/stderr. Only expose a strict
                    # official device authorization URL to this logged-in user.
                    text=line.decode('utf8','replace')
                    for item in re.findall(r'https://[^\s\x1b]+',text):
                        url=authorization_url(item)
                        if url:
                            public['authorization_url']=url
                            await self.store.put('dws-login',owner,'current',self.svc.seal(owner,public),610)
            async def cancelled():
                while proc.returncode is None:
                    if await self.store.get('dws-cancel',owner,'current'):
                        await self.runner.stop(proc)
                        return
                    await asyncio.sleep(1)
            async with asyncio.timeout(610):
                await asyncio.gather(lines(proc.stderr),lines(proc.stdout),proc.wait(),cancelled())
            if await self.store.get('dws-cancel',owner,'current'): return
            if proc.returncode!=0: raise failure('dingtalk_cli_authorization_failed',403)
            # Release the waiting auth process before launching profile list.
            await self.runner.stop(proc)
            account=await self.profiles(owner)
            if not account: raise failure('dingtalk_cli_authorization_failed',403)
            await self.store.put('dws-status',owner,'current',self.svc.seal(owner,self.public('ready',account=account)),20)
            await self.store.delete('dws-login',owner,'current')
        except BaseException as exc:
            reason='dingtalk_cli_authorization_expired' if isinstance(exc,TimeoutError) else 'dingtalk_cli_authorization_failed'
            await self.store.put('dws-login',owner,'current',self.svc.seal(owner,self.public('authorization_required',reason,expires_at=int(time.time())+30)),30)
        finally:
            try:
                if proc is not None: await self.runner.stop(proc)
                self.runner.discard_logs(owner)
            finally:
                await self.store.replace_if_equal('dws-lock',owner,'operation',lease,None)

    async def disconnect(self,owner):
        await self.store.budget(owner)
        await self.store.put('dws-cancel',owner,'current','1',660)
        # Cancellation crosses gateway workers using Redis; do not delete files
        # while a CLI process is writing refreshed credentials.
        for _ in range(30):
            if not await self.store.get('dws-lock',owner,'operation'): break
            await asyncio.sleep(.1)
        async with self.lock(owner):
            await self.generation(owner,rotate=True)
            home=self.runner.home(owner)
            if home.is_symlink() or home.resolve().parent!=self.runner.root.resolve(): raise failure('dingtalk_cli_unavailable',503)
            shutil.rmtree(home)
            for kind in ('dws-login','dws-status'): await self.store.delete(kind,owner,'current')
        return {'status':'disconnected'}

    async def catalog(self,owner):
        state=await self.status(owner)
        tools=[{'name':'cli_list_commands','description':'发现钉钉官方 CLI 已接入命令及精确参数。可按产品或关键词筛选，先发现再调用；这一步不读取钉钉业务数据。',
            'input_schema':{'type':'object','additionalProperties':False,'properties':{'product':{'type':'string'},'query':{'type':'string','maxLength':100},'cursor':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':8}}}},
            {'name':'cli_call_command','description':'执行 cli_list_commands 返回的钉钉官方命令；command 必须完全匹配，arguments 使用其参数定义。所有写入先展示本人确认卡，不能自行确认或把预览当执行成功。',
             'input_schema':{'type':'object','additionalProperties':False,'required':['command','arguments'],'properties':{'command':{'type':'string','maxLength':180},'arguments':{'type':'object'}}}}]
        for tool in tools: tool.update(available=state['status']=='ready',requires_confirmation=False,provider=PROVIDER)
        return tools,state

    def list_commands(self,args):
        if not isinstance(args,dict) or set(args)-{'product','query','cursor','limit'}: raise failure('dingtalk_cli_arguments_invalid')
        query=args.get('query','');product=args.get('product');cursor=args.get('cursor',0);limit=args.get('limit',5)
        if not isinstance(query,str) or len(query)>100 or (product is not None and not isinstance(product,str)) or type(cursor)is not int or cursor<0 or type(limit)is not int or not 1<=limit<=8: raise failure('dingtalk_cli_arguments_invalid')
        rows=[c for c in self.contract()['commands'] if (not product or c['product']==product) and query.casefold() in (c['cli_path']+' '+c['description']).casefold()]
        selected=rows[cursor:cursor+limit]
        return {'commands':selected,'next_cursor':cursor+len(selected) if cursor+len(selected)<len(rows) else None,'total':len(rows),'products':self.contract()['products']}

    def command(self,path,args):
        command=next((c for c in self.contract()['commands'] if c['cli_path']==path),None)
        if not command: raise failure('dingtalk_cli_command_unknown',404)
        if not isinstance(args,dict): raise failure('dingtalk_cli_arguments_invalid')
        packed(args,64*1024);safe_arguments(args)
        spec=command['input_schema'];props=spec['properties']
        if set(args)-set(props) or set(spec.get('required',[]))-set(args): raise failure('dingtalk_cli_arguments_invalid')
        argv=path.split()
        for key,value in args.items():
            typ=props[key]['type']
            if not ((typ=='string' and isinstance(value,str)) or (typ=='integer' and type(value)is int) or (typ=='boolean' and type(value)is bool) or (typ=='number' and type(value)in {float,int}) or (typ=='array' and isinstance(value,list) and all(isinstance(x,str) for x in value))): raise failure('dingtalk_cli_arguments_invalid')
            if 'enum' in props[key] and value not in props[key]['enum']: raise failure('dingtalk_cli_arguments_invalid')
            if typ=='array':
                flag_type=command['parameters'][key].get('flag_type')
                if flag_type=='stringArray':
                    argv.extend('--'+key+'='+item for item in value)
                    continue
                if flag_type in {'strings','stringSlice'}:
                    buffer=io.StringIO();csv.writer(buffer,lineterminator='\n').writerow(value)
                    value=buffer.getvalue().removesuffix('\n')
                elif flag_type=='string':value=json.dumps(value,ensure_ascii=False,separators=(',',':'))
                else:raise failure('dingtalk_cli_unavailable',503)
            elif type(value)is bool: value='true' if value else 'false'
            else: value=str(value)
            argv.append('--'+key+'='+value)
        constraints=command.get('constraints') or {}
        for group in constraints.get('mutually_exclusive',[]):
            if sum(k in args for k in group)>1: raise failure('dingtalk_cli_arguments_invalid')
        for group in constraints.get('require_one_of',[]):
            if not any(k in args for k in group): raise failure('dingtalk_cli_arguments_invalid')
        return command,argv

    async def invoke(self,owner,body):
        if body.action=='cli_list_commands': return {'status':'completed','result':self.list_commands(body.arguments)}
        if body.action!='cli_call_command' or set(body.arguments)!={'command','arguments'}: raise failure('dingtalk_cli_arguments_invalid')
        command,argv=self.command(body.arguments['command'],body.arguments['arguments'])
        async with self.lock(owner):
            account=await self.profiles(owner)
            if not account: raise failure('dingtalk_cli_authorization_required',403)
            if not command['requires_confirmation']:
                return {'status':'completed','result':await self.execute(owner,argv,account['profile'])}
            request_key=str(body.request_id)
            generation=await self.generation(owner)
            fingerprint=digest([body.arguments,account,command,VERSION,generation])
            saved=await self.store.get('request',owner,request_key)
            if saved:
                saved=self.svc.unseal(owner,saved)
                if saved['request_hash']!=fingerprint: raise failure('connector_request_immutable',409)
                if not await self.store.get('pending',owner,saved['confirmation_id']): raise failure('connector_confirmation_consumed_or_expired',409)
                return saved['public']
            ticket=secrets.token_urlsafe(32)
            public={'status':'requires_confirmation','confirmation_id':ticket,'expires_at':int(time.time())+300,
                'preview':{'action':command['cli_path'],'summary':command['description'],'arguments':body.arguments['arguments'],
                    'recipients':[],'account':account,'provider':PROVIDER}}
            pending={'provider':PROVIDER,'arguments':body.arguments,'account':account,'generation':generation,'command_hash':digest(command),'expires_at':public['expires_at']}
            if not await self.store.put('request',owner,request_key,self.svc.seal(owner,{'request_hash':fingerprint,'confirmation_id':ticket,'public':public}),86400,nx=True): raise failure('connector_request_in_progress',409)
            await self.store.put('pending',owner,ticket,self.svc.seal(owner,pending),300)
            return public

    async def execute(self,owner,argv,profile,write=False):
        args=argv+['--profile='+profile,'--format=json']+(['--yes'] if write else [])
        result=scrub(await self.runner.run(owner,args))
        packed(result,MAX_PUBLIC)
        return {'data':result,'untrusted_external_content':True}

    async def confirm(self,owner,ticket,raw,pending):
        async with self.lock(owner):
            command,argv=self.command(pending['arguments']['command'],pending['arguments']['arguments'])
            account=await self.profiles(owner)
            if pending['expires_at']<=time.time() or account!=pending['account'] or pending['generation']!=await self.generation(owner) or digest(command)!=pending['command_hash']: raise failure('connector_confirmation_scope_changed',403)
            if await self.store.pop('pending',owner,ticket)!=raw: raise failure('connector_confirmation_consumed_or_expired',409)
            try:
                result=await self.execute(owner,argv,account['profile'],write=True)
            except BaseException:
                raise failure('dingtalk_cli_write_result_unknown',502) from None
            return {'status':'completed','result':result}
