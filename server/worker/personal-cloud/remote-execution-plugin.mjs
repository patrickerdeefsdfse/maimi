// A-host capability providers. No user command or document parser runs locally.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {posix} from 'node:path';
import {randomUUID} from 'node:crypto';
import {AsyncLocalStorage} from 'node:async_hooks';
import {RemoteProcess} from './remote-execution-client.mjs';
import {beforeDispatchError,remoteError,confirmedFailure} from './execution-outcome.mjs';
import {captureToolScope,acquireToolTicket,releaseToolTicket} from './native-task-context.mjs';
const require=createRequire((process.env.MAIMI_DSH_RUNTIME_ROOT||'/opt/dsh')+'/package.json');
const mod=async name=>import(pathToFileURL(require.resolve(name)).href);
const [{Context,Service},{FileSystem,FsError},{SubprocessRuntime},{LocalFileSystem},{LocalBashExecutor}]=await Promise.all([
 mod('@deepseek-ai/cordis'),mod('@deepseek-ai/dsh-fs'),mod('@deepseek-ai/dsh-subprocess'),mod('@deepseek-ai/dsh-fs-local'),mod('@deepseek-ai/dsh-bash-local')]);
const localAsset=p=>p.startsWith('/opt/dsh/node_modules/@deepseek-ai/')||p==='/opt/dsh/skills'||p.startsWith('/opt/dsh/skills/');
const workspace=p=>p==='/workspace'||p.startsWith('/workspace/');
const unavailable=()=>{throw new FsError('此路径不属于云端工作区。','FS_SANDBOX_DENIED');};
export const name='maimi-remote-execution';
export const inject=['agents'];

const RPC_BYTES=8*1024*1024;
export async function readWorkspaceBytes(call,target,signal,maxBytes,imageLimit=0){
 if(!Number.isSafeInteger(maxBytes)||maxBytes<0)throw new FsError('文件读取上限无效。','FS_IO_ERROR');
 const readOne=async limit=>Buffer.from((await call('fs.readBytes',{target,maxBytes:limit},{signal})).base64,'base64');
 if(maxBytes<=RPC_BYTES)return readOne(maxBytes);
 const info=await call('fs.stat',{target},{signal});
 if(!info)throw new FsError('找不到工作区文件。','FS_NOT_FOUND');
 if(info.type!=='file')throw new FsError('请选择普通文件。','FS_NOT_REGULAR_FILE');
 const limit=Math.min(maxBytes,Math.max(RPC_BYTES,imageLimit));
 if(!Number.isSafeInteger(info.size)||info.size<0||info.size>limit)throw new FsError('文件超过本工具的读取上限。','FS_TOO_LARGE');
 if(info.size<=RPC_BYTES)return readOne(Math.min(limit,RPC_BYTES));
 const changed=()=>new FsError('读取期间文件发生变化，请重新读取。','FS_STALE_VERSION');
 if(typeof info.version!=='string'||!info.version)throw changed();
 const result=Buffer.alloc(info.size);
 for(let offset=0;offset<info.size;offset+=RPC_BYTES){
  signal?.throwIfAborted();
  const length=Math.min(RPC_BYTES,info.size-offset);
  const block=await call('fs.readByteRange',{target,offset,length},{signal});
  const data=Buffer.from(block.base64,'base64');
  if(data.length!==length)throw changed();
  data.copy(result,offset);
 }
 const after=await call('fs.stat',{target},{signal});
 if(after?.type!=='file'||after.size!==info.size||after.version!==info.version)throw changed();
 return result;
}

// Exact rc3 exec.name values. Bash's preparatory filesystem work must stay in
// its standard executor; unknown/composite tools conservatively use standard.
export function executionResourceClass(toolName,operation,requested){
 if(requested)return requested;
 if(['office_render','office_convert','office_recalculate'].includes(toolName))return 'office';
 return operation.startsWith('fs.')&&['read','write','edit','read_image','str_replace_editor'].includes(toolName)?'light':'standard';
}
// Use the same restricted parser on C for a small workbook already pinned by this session.
export function documentResourceClass(args,lease){
 const path=args?.file_path;
 if(typeof path!=='string'||!path.toLowerCase().endsWith('.xlsx'))return 'office';
 const bytes=lease.pinnedFileBytes(path);
 return Number.isSafeInteger(bytes)&&bytes>0&&bytes<=256*1024?'standard':'office';
}
export async function withExecutionCacheLease(sessionId,borrow,operation){
 const lease=await borrow(sessionId);
 let value;
 try{value=await operation(lease);}
 catch(error){await lease.release({confirmed:Boolean(confirmedFailure(error))});throw error;}
 await lease.release({confirmed:true});return value;
}
export function pinnedRemoteProcess({spec,call,borrow,sessionId}){
 spec.signal?.throwIfAborted();let lease,spawnAttempted=false;
 const handle=new RemoteProcess({spec,call:async(operation,args,options)=>{
  if(operation==='process.spawn'){
   options.signal?.throwIfAborted();lease=await borrow(sessionId);options.signal?.throwIfAborted();spawnAttempted=true;
  }
  try{return await call(operation,args,options);}
  catch(error){
   if(operation==='process.spawn'&&confirmedFailure(error)==='not_started')spawnAttempted=false;
   throw error;
  }
 }});
 const finish=async()=>{if(lease)await lease.release({confirmed:handle.empty===true||!spawnAttempted});};
 handle.done=handle.done.then(async value=>{await finish();return value;},async error=>{await finish();throw error;});
 handle.done.catch(()=>{});return handle;
}

export async function apply(ctx){
 const toolContext=new AsyncLocalStorage();
 for(const event of ['tools/pre-execute','tools/execute'])ctx.on(event,(exec,next)=>{
  if(!exec.agent)return next();
  return toolContext.run({scope:captureToolScope(exec.agent),toolName:exec.name},next);
 },{global:true,prepend:true});
 const assets=new Context();const assetFiber=assets.plugin(LocalFileSystem,{cwd:'/opt/dsh',diffBasisMaxBytes:1024*1024});await assetFiber.await();
 ctx.on('dispose',()=>assets.fiber.dispose());
 async function assetTarget(target){
  if(!localAsset(target.displayPath))unavailable();
  const canonical=await assets.fs.resolve(target.displayPath),p=assets.fs.processPath(canonical);
  if(!p.startsWith('/opt/dsh/node_modules/')&&p!=='/opt/dsh/skills'&&!p.startsWith('/opt/dsh/skills/'))unavailable();
  return canonical;
 }
 function scoped(){
  const scope=toolContext.getStore()?.scope;if(!scope)throw Error('EXECUTION_TOOL_SCOPE_REQUIRED');
  return async(operation,args,{generation,signal,resourceClass='standard'}={})=>{
   let body,ticket;
   try{
    signal?.throwIfAborted();
    body=JSON.stringify({generation,resource_class:resourceClass,rpc:{request_id:randomUUID(),session_id:scope.rootSessionId,operation,args}});
    ticket=await acquireToolTicket(scope,'execution');
   }catch{throw beforeDispatchError();}
   try{
    if(signal?.aborted)throw beforeDispatchError();
    const response=await fetch('http://127.0.0.1:3086/__megmeet/execution',{method:'POST',redirect:'error',
     headers:{Authorization:'Bearer '+ticket,'Content-Type':'application/json'},
     body,
     signal:AbortSignal.any([AbortSignal.timeout(150000),...(signal?[signal]:[])])});
    if(!response.ok)throw remoteError(response.status,await response.json().catch(()=>null),operation);
    return response.json();
   }finally{await releaseToolTicket(ticket).catch(()=>{});}
  };
 }
 const borrow=sessionId=>{
  const store=ctx.get('attachments');if(typeof store?.borrowSession!=='function')throw Error('ATTACHMENT_CACHE_PROVIDER_REQUIRED');
  return store.borrowSession(sessionId);
 };
 const call=async(operation,args,options={})=>{
  const current=toolContext.getStore();if(!current)throw Error('EXECUTION_TOOL_SCOPE_REQUIRED');
  const invoke=scoped();
  return withExecutionCacheLease(current.scope.rootSessionId,borrow,async lease=>{
   const resourceClass=operation==='document.read'?documentResourceClass(args,lease):executionResourceClass(current.toolName,operation,options.resourceClass);
   return (await invoke(operation,args,{...options,resourceClass})).value;
  });
 };
 class RemoteFS extends FileSystem {
  get sandboxMode(){return 'workspace-write';}
  path(value){const p=posix.resolve('/workspace',value);if(toolContext.getStore()&&!workspace(p)&&!localAsset(p)&&!p.startsWith('/tmp/'))unavailable();return p;}
  async resolve(value,options={}){
   const p=this.path(posix.resolve(options.cwd||'/workspace',value));
   if(localAsset(p))return assetTarget({displayPath:p});
   // Workspace discovery and skill discovery must not allocate an executor.
   if(!toolContext.getStore())return {targetKey:p,displayPath:p};
   return call('fs.resolve',{path:p},{signal:options.signal});
  }
  processPath(target){return this.path(target.displayPath);}
  processPathFromHostPath(p){return workspace(p)||localAsset(p)?p:undefined;}
  fileUrl(target){return pathToFileURL(this.processPath(target)).href;}
  contains(parent,child){const p=this.processPath(parent),c=this.processPath(child);return c===p||c.startsWith(p+'/');}
  async stat(target,signal){
   const p=this.processPath(target);if(localAsset(p))return assets.fs.stat(await assetTarget(target),signal);
   if(!toolContext.getStore())return p==='/workspace'?{type:'directory',version:'managed-workspace'}:undefined;
   return await call('fs.stat',{target},{signal})??undefined;
  }
  async lstat(path,options={},signal){const p=this.path(posix.resolve(options.cwd||'/workspace',path));if(localAsset(p)){const t=await assetTarget({displayPath:p});return assets.fs.lstat(assets.fs.processPath(t),options,signal);}if(!toolContext.getStore())return p==='/workspace'?{type:'directory',version:'managed-workspace'}:undefined;return await call('fs.lstat',{path:p},{signal})??undefined;}
  async readText(target,signal){if(localAsset(this.processPath(target)))return assets.fs.readText(await assetTarget(target),signal);return call('fs.readText',{target},{signal});}
  async streamText(target,signal){const text=await this.readText(target,signal);return (async function*(){yield text;})();}
  async readBytes(target,signal,maxBytes=RPC_BYTES){
   if(localAsset(this.processPath(target)))return assets.fs.readBytes(await assetTarget(target),signal,maxBytes);
   const imageLimit=toolContext.getStore()?.toolName==='read_image'?ctx.get('attachments')?.imageLimits.maxImageBytes:0;
   return readWorkspaceBytes(call,target,signal,maxBytes,imageLimit);
  }
  async readByteRange(target,range,signal){if(localAsset(this.processPath(target)))return assets.fs.readByteRange(await assetTarget(target),range,signal);return Buffer.from((await call('fs.readByteRange',{target,...range},{signal})).base64,'base64');}
  async listDir(target,signal){if(localAsset(this.processPath(target)))return assets.fs.listDir(await assetTarget(target),signal);if(!toolContext.getStore())return [];return call('fs.listDir',{target},{signal});}
  checkWrite(target,policy){if(!workspace(this.processPath(target))||policy?.mode==='read-only')unavailable();}
  writeText(target,content,expected,signal,policy){this.checkWrite(target,policy);return call('fs.writeText',{target,content,expected},{signal});}
  editText(target,edit,expected,signal,policy){this.checkWrite(target,policy);return call('fs.editText',{target,edit,expected},{signal});}
 }
 class RemoteSubprocess extends SubprocessRuntime {
  async resolveExecutable(command,_env,signal){return call('process.resolve',{command},{signal});}
  async terminalEnvironment(){return {platform:'posix',defaultShell:'/bin/bash'};}
  spawn(spec){const current=toolContext.getStore();if(!current)throw Error('EXECUTION_TOOL_SCOPE_REQUIRED');
   const invoke=scoped(),resourceClass=executionResourceClass(current.toolName,'process.spawn');
   return pinnedRemoteProcess({spec,call:(op,args,options)=>invoke(op,args,{...options,resourceClass}),borrow,sessionId:current.scope.rootSessionId});}
  async spawnTerminal(){throw Error('网页版暂不提供持续交互终端；请使用受管的单次命令。');}
 }
 class RemoteShell extends LocalBashExecutor {
  get sandboxMode(){return 'workspace-write';}
  execute(spec){
   if(spec.sandboxPolicy?.mode==='read-only')throw Error('当前只读会话不可运行命令；可以使用文件读取和搜索工具。');
   if(!workspace(posix.resolve(spec.workdir)))throw Error('云端命令必须在工作区中执行。');
   return super.execute(spec);
  }
 }
 class ExecutionService extends Service {
  constructor(inner){super(inner,'maimiExecution');}
  async readDocument(args,{signal}={}){return call('document.read',args,{signal});}
 }
 ctx.plugin(RemoteFS);ctx.plugin(RemoteSubprocess);ctx.plugin(RemoteShell);ctx.plugin(ExecutionService);
}
