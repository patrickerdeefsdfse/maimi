// Web-only generated outputs. Scan only a newly allocated, parent-owned root.
import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {open,lstat,opendir} from 'node:fs/promises';
import path from 'node:path';
const SID=/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const UUID=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const PROTECTED=new Set(['.agents','.dsh','.git','.maimi','skills','templates']);
export const LIMITS=Object.freeze({entries:128,files:32,bytes:128*1024**2,fileBytes:32*1024**2,depth:4,sessions:64});
export const name='maimi-managed-outputs';
export const inject=['systemPrompt','sessionProjections','connection'];
class BoundedWork {
 constructor(limit=2){this.limit=limit;this.active=0;this.waiters=[];}
 async run(fn){if(this.active>=this.limit)await new Promise(resolve=>this.waiters.push(resolve));else this.active++;
  try{return await fn();}finally{const next=this.waiters.shift();if(next)next();else this.active--;}}
}
const version=s=>`${s.dev}:${s.ino}:${s.size}:${s.mtimeNs}:${s.ctimeNs}`;
const identity=s=>`${s.dev}:${s.ino}`;
const warning='部分文件仍在临时工作区，尚未保存到云端，请稍后重试或手动下载。';
const RETAINED=new Set(['CAPACITY_UNAVAILABLE','SOURCE_EXPIRED','BACKEND_UNAVAILABLE','SOURCE_CHANGED','SOURCE_OR_BACKEND_UNAVAILABLE']);

export function httpTransport({token=process.env.MEGMEET_MODEL_TOKEN}={}){
 if(!/^[A-Za-z0-9_-]{43}$/.test(token||''))throw Error('managed output bridge configuration invalid');
 return async(operation,sessionId,body,signal)=>{
  if(!['allocate','publish'].includes(operation)||!SID.test(sessionId))throw Error('managed output request invalid');
  const response=await fetch('http://127.0.0.1:3086/__megmeet/outputs/'+operation,{method:'POST',redirect:'error',
   headers:{Authorization:'Bearer '+token,'Content-Type':'application/json','X-Maimi-Session-ID':sessionId},
   body:JSON.stringify(body),signal:AbortSignal.any([AbortSignal.timeout(75000),...(signal?[signal]:[])])});
  if(!response.ok){await response.body?.cancel();throw Error('generated outputs were not saved to cloud');}
  return response.json();
 };
}

async function directoryIdentity(directory,workspaceRoot){
 const root=path.resolve(workspaceRoot),base=path.join(root,'maimi-outputs');
 if(path.dirname(path.resolve(directory))!==base)throw Error('managed output directory outside allocated root');
 for(const p of [root,base,directory]){
  const stat=await lstat(p,{bigint:true});if(!stat.isDirectory()||stat.isSymbolicLink())throw Error('managed output directory unsafe');
 }
 return identity(await lstat(directory,{bigint:true}));
}

export async function scanDirectory(directory,{workspaceRoot='/workspace',expectedIdentity,acknowledged=new Map(),signal,limits=LIMITS}={}){
 const before=await directoryIdentity(directory,workspaceRoot);
 if(expectedIdentity!==before)throw Error('managed output directory changed');
 let entries=0,bytes=0;const files=[],retained=[],seen=new Set();let capped=false;
 const walk=async(dir,depth)=>{
  const parent=await lstat(dir,{bigint:true});
  if(!parent.isDirectory()||parent.isSymbolicLink())throw Error('managed output subtree changed');
  for await(const item of await opendir(dir)){
   signal?.throwIfAborted();if(++entries>limits.entries){capped=true;break;}
   const p=path.join(dir,item.name);seen.add(p);
   if(item.isSymbolicLink()||item.name.startsWith('.')||PROTECTED.has(item.name.toLowerCase())){retained.push({path:p,reason:'UNMANAGED_ENTRY'});continue;}
   if(item.isDirectory()){
    if(depth>=limits.depth){retained.push({path:p,reason:'SCAN_DEPTH_LIMIT'});continue;}
    await walk(p,depth+1);if(capped)break;continue;
   }
   if(!item.isFile()){retained.push({path:p,reason:'NOT_REGULAR'});continue;}
   let handle;
   try{
    const initial=await lstat(p,{bigint:true});
    if(!initial.isFile()||initial.isSymbolicLink()||initial.nlink!==1n)throw Error('not regular');
    const ver=version(initial);
    if(acknowledged.get(p)===ver)continue;
    const size=Number(initial.size);
    if(size>limits.fileBytes){retained.push({path:p,reason:'FILE_TOO_LARGE'});continue;}
    if(files.length>=limits.files||bytes+size>limits.bytes){capped=true;break;}
    handle=await open(p,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
    if(version(await handle.stat({bigint:true}))!==ver)throw Error('source changed');
    const hash=createHash('sha256');let count=0;
    for await(const chunk of handle.createReadStream({autoClose:false})){
     signal?.throwIfAborted();count+=chunk.length;if(count>size)throw Error('source grew');hash.update(chunk);
    }
    if(count!==size||version(await handle.stat({bigint:true}))!==ver||version(await lstat(p,{bigint:true}))!==ver)throw Error('source changed');
    bytes+=size;files.push({path:p,version:ver,sha256:hash.digest('hex'),bytes:size,name:item.name});
   }catch(error){if(signal?.aborted)throw error;retained.push({path:p,reason:'SOURCE_UNCONFIRMED'});}
   finally{await handle?.close();}
  }
  if(identity(await lstat(dir,{bigint:true}))!==identity(parent))throw Error('managed output subtree changed');
 };
 await walk(directory,0);
 if(await directoryIdentity(directory,workspaceRoot)!==before)throw Error('managed output directory changed');
 if(!capped)for(const p of acknowledged.keys())if(!seen.has(p))acknowledged.delete(p);
 return {files,retained,capped,bytes};
}

export class ManagedOutputs {
 constructor({transport=httpTransport(),workspaceRoot='/workspace',logger={warn(){}},scan=scanDirectory}={}){
  this.transport=transport;this.workspaceRoot=path.resolve(workspaceRoot);this.logger=logger;this.scan=scan;
  this.roots=new Map();this.allocating=new Map();this.aliases=new Map();this.closed=false;this.work=new BoundedWork(2);
 }
 async ensure(sessionId,signal){
  if(this.closed||!SID.test(sessionId))throw Error('managed output session invalid');
  if(this.roots.has(sessionId))return this.roots.get(sessionId);
  if(this.allocating.has(sessionId))return this.allocating.get(sessionId);
  if(this.roots.size+this.allocating.size>=LIMITS.sessions)throw Error('managed output session capacity reached');
  const operation=(async()=>{
   const value=await this.transport('allocate',sessionId,{},signal),directory=value?.directory;
   if(typeof directory!=='string'||!path.isAbsolute(directory))throw Error('managed output allocation invalid');
   const stem=path.basename(directory),prefix=sessionId.replace(/^session-/,'')+'-';
   if(!stem.startsWith(prefix)||!UUID.test(stem.slice(prefix.length)))throw Error('managed output identity invalid');
   const id=await directoryIdentity(directory,this.workspaceRoot);
   // Parent creates an exclusive new directory. Never adopt pre-existing data.
   const listing=await opendir(directory);try{if(await listing.read())throw Error('managed output allocation was not empty');}finally{await listing.close();}
   const record={directory,identity:id,acknowledged:new Map(),status:'idle',published_files:0,pending_files:0,message:'',dirty:false,flight:null,timer:null};
   this.roots.set(sessionId,record);return record;
  })();this.allocating.set(sessionId,operation);
  try{return await operation;}finally{this.allocating.delete(sessionId);}
 }
 bind(sessionId,root){if(!SID.test(sessionId)||!SID.test(root))throw Error('managed output scope invalid');this.aliases.set(sessionId,root);}
 root(session){return this.aliases.get(session)||session;}
 status(session){const r=this.roots.get(this.root(session));return r?{status:r.status,published_files:r.published_files,pending_files:r.pending_files,message:r.message}:{status:'idle',published_files:0,pending_files:0,message:''};}
 schedule(session){const root=this.root(session),r=this.roots.get(root);if(!r||this.closed)return;r.dirty=true;if(!r.timer&&!r.flight){r.timer=setTimeout(()=>{r.timer=null;void this.flush(root);},100);r.timer.unref();}}
 async flush(session,signal,followup=true){
  const root=this.root(session),r=this.roots.get(root);if(!r||this.closed)return this.status(session);
  if(r.flight){r.dirty=true;return r.flight;}
  clearTimeout(r.timer);r.timer=null;r.dirty=false;r.status='saving';
  r.flight=this.work.run(async()=>{
   try{
    const snapshot=await this.scan(r.directory,{workspaceRoot:this.workspaceRoot,expectedIdentity:r.identity,acknowledged:r.acknowledged,signal});
    const requested=new Map(snapshot.files.map(f=>[f.path,f.version]));let retained=snapshot.retained.length+(snapshot.capped?1:0);
    if(snapshot.files.length){
     const value=await this.transport('publish',root,{directory:r.directory,files:snapshot.files},signal);
     if(!value||!Array.isArray(value.published)||!Array.isArray(value.retained))throw Error('managed output acknowledgement invalid');
     const seen=new Set();
     for(const file of value.published){if(seen.has(file.path)||requested.get(file.path)!==file.version)throw Error('managed output acknowledgement invalid');seen.add(file.path);}
     for(const file of value.retained){if(seen.has(file.path)||!requested.has(file.path)||!RETAINED.has(file.reason))throw Error('managed output acknowledgement invalid');seen.add(file.path);}
     if(seen.size!==requested.size)throw Error('managed output acknowledgement incomplete');
     for(const file of value.published)r.acknowledged.set(file.path,file.version);
     retained+=value.retained.length;
    }
    r.published_files=r.acknowledged.size;r.pending_files=retained;r.status=retained?'attention':'saved';r.message=retained?warning:'';
   }catch(error){r.status='attention';r.pending_files=Math.max(1,r.pending_files);r.message=warning;this.logger.warn('MANAGED_OUTPUTS_NOT_PUBLISHED');}
   return this.status(root);
  }).finally(()=>{r.flight=null;});
  const result=await r.flight;
  // One bounded follow-up catches writes signalled while a prior publication
  // was running. No infinite retry loop on quota, expiry or backend failure.
  if(followup&&r.dirty&&!signal?.aborted)return this.flush(root,signal,false);
  return result;
 }
 async close(){for(const r of this.roots.values())clearTimeout(r.timer);for(const id of this.roots.keys())await this.flush(id).catch(()=>{});this.closed=true;}
}

export function install(ctx,service,{scopeOf}={}){
 const capture=scopeOf??(async agent=>(await import('./native-task-context.mjs')).captureToolScope(agent));
 const safeFlush=session=>service.flush(session.id).catch(()=>{});
 ctx.on('system-prompt/assemble',async(_assembly,context,next)=>{
  if(!context.agent)return next();
  const boundary=ctx.sessionProjections.stateOf(context.agent.session,'turnBoundary');
  if(!boundary||boundary.openTurnStartSeq===null)return next();
  const scope=await capture(context.agent),root=scope.rootSessionId;
  service.bind(context.agent.session.id,root);const record=await service.ensure(root,context.signal);
  const assembly=await next();assembly.contexts.push({name:'maimi-managed-outputs',interpolate:false,
   text:`Create generated deliverables only inside this conversation's temporary output directory: ${JSON.stringify(record.directory)}. It is separate from the project, skills and templates; do not move or copy those stores into it. Files up to 32 MiB are automatically saved to cloud after generation, even if the user does not download them, and retained for 24 hours after first publication. Use present for finished deliverables. Files elsewhere are not automatically managed. Do not claim cloud publication succeeded if the managed output status reports attention. ${record.message}`});return assembly;
 },{prepend:true});
 ctx.on('session/event',(session,event)=>{if(event.type==='deliverables/presented'||event.type==='turn/end')service.schedule(session.id);});
 ctx.on('agent/turn-stopping',({agent})=>safeFlush(agent.session));
 ctx.on('session/flush',session=>safeFlush(session));
 ctx.on('session/disposed',session=>{void safeFlush(session);});
 ctx.effect(()=>()=>service.close(),'maimi managed output final flush');
 ctx.effect(()=>ctx.connection.fetch.register({path:'/api/maimi/outputs/status',methods:['POST'],requestBody:'buffered',
  fetch:async request=>{let body;try{body=await request.json();}catch{return new Response(null,{status:400});}
   if(!body||Object.keys(body).join(',')!=='sessionId'||!SID.test(body.sessionId||''))return new Response(null,{status:400});
   return Response.json(service.status(body.sessionId),{headers:{'Cache-Control':'no-store'}});}}),'maimi managed output status');
 return service;
}
export function apply(ctx){return install(ctx,new ManagedOutputs({logger:ctx.logger}));}
