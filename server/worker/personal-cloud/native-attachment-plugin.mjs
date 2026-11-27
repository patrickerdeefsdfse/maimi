// Web-only NAS attachment provider. The upstream normalizer and SHA reference
// format are retained; local objects are bounded, short-lived execution copies.
import {orderDocumentPart} from './order-document-receipts.mjs';
import {writeSharedCopy} from './write-shared-cache.mjs';
import {AsyncLocalStorage} from 'node:async_hooks';
import {createHash,randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {mkdtemp,mkdir,rm,readdir,lstat,readFile,writeFile,rename,open,opendir} from 'node:fs/promises';
import {constants,createReadStream} from 'node:fs';
import {join,resolve,relative,basename,sep} from 'node:path';
import {Readable} from 'node:stream';

const require=createRequire((process.env.MAIMI_DSH_RUNTIME_ROOT||'/opt/dsh')+'/package.json');
const {AttachmentStore,AttachmentError}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-attachment')).href);
const upstream=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-attachment-local')).href);
const scope=new AsyncLocalStorage();
const SID=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const MAX=32*1024**2, CACHE_MAX=128*1024**2, CACHE_SECONDS=300;
const CACHE_ENTRIES_MAX=128, CACHE_PINS_MAX=64;
export const CACHE_MARKER='maimi-cache-entry.json',CACHE_SCHEMA='maimi-r73-attachment-cache/v1';
const fail=message=>{throw new AttachmentError(message,'INVALID_ATTACHMENT_REF');};
const cacheError=(code,message)=>Object.assign(Error(message),{code});
export const name='maimi-managed-attachments';
export function withAttachmentScope(value,fn){
 if(!SID.test(value?.sessionId||''))throw Error('attachment session context invalid');
 return scope.run(Object.freeze({sessionId:value.sessionId,requestId:value.requestId||randomUUID()}),fn);
}

export function httpTransport({token=process.env.MEGMEET_MODEL_TOKEN,endpoint='http://127.0.0.1:3086/__megmeet/attachments'}={}){
 if(endpoint!=='http://127.0.0.1:3086/__megmeet/attachments'||!/^[-_A-Za-z0-9]{43}$/.test(token||''))throw Error('attachment bridge configuration invalid');
 return async(operation,{sessionId='',requestId='',name,sha256,size,mediaType,body,signal}={})=>{
  if(!['put','resolve','read'].includes(operation))throw Error('attachment operation invalid');
  const headers={Authorization:'Bearer '+token,'X-Maimi-Session-ID':sessionId};
  let content=body;
  if(operation==='put')Object.assign(headers,{'X-Maimi-Request-ID':requestId,'X-Maimi-Filename':Buffer.from(name,'utf8').toString('base64url'),
   'X-Maimi-SHA256':sha256,'Content-Length':String(size),'Content-Type':mediaType||'application/octet-stream'});
  else {headers['Content-Type']='application/json';content=JSON.stringify(body);}
  const response=await fetch(endpoint+'/'+operation,{method:'POST',headers,body:content,duplex:'half',redirect:'error',
   signal:AbortSignal.any([AbortSignal.timeout(45000),...(signal?[signal]:[])])});
  if(!response.ok){
   await response.body?.cancel();
   const message=response.status===404||response.status===410?'云端附件已过期或找不到，请重新上传。':
    response.status===413||response.status===507?'附件超过上传或存储限额。':
    response.status===401||response.status===403?'当前登录无法访问这个附件。':'附件服务暂时不可用，请稍后重试。';
   throw Object.assign(Error(message),{status:response.status});
  }
  return operation==='read'?response:response.json();
 };
}

class Gate {
 constructor(){this.active=0;this.queue=[];}
 async run(fn){if(this.active>=2)await new Promise(r=>this.queue.push(r));else this.active++;try{return await fn();}finally{const next=this.queue.shift();if(next)next();else this.active--;}}
}
class Mutex {
 constructor(){this.tail=Promise.resolve();}
 run(fn){const value=this.tail.then(fn);this.tail=value.catch(()=>{});return value;}
}
const keyOf=ref=>`${ref.attachmentId}:${ref.name||'image'}:${ref.bytes}`;
const removeLocalCopy=path=>rm(path,{recursive:true,force:true,maxRetries:4,retryDelay:100});
const shellShape=/^attachments(?:\/v1(?:\/tmp|\/file-objects(?:\/[a-f0-9]{2})?|\/files(?:\/[a-f0-9]{2}(?:\/[a-f0-9]{64})?)?)?)?$/;
// Only the fenced retention helper may remove a shell. Here we count it as
// an occupied slot and admit new copies only when the old tree contains no files.
async function emptyCacheShell(root){
 if(process.platform!=='linux')return false;
 const uid=process.getuid(),flags=constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW;
 let visited=0,device;
 async function visit(path,prefix){
  if(++visited>24)return false;
  const handle=await open(path,flags);
  try{
   const stat=await handle.stat();
   if(!prefix)device=stat.dev;
   if(stat.dev!==device||stat.uid!==uid||(stat.mode&0o022)||(!prefix&&(stat.mode&0o777)!==0o700))return false;
   const fdPath='/proc/self/fd/'+handle.fd;
   for await(const child of await opendir(fdPath)){
    const relative=prefix?prefix+'/'+child.name:child.name;
    if(!child.isDirectory()||!shellShape.test(relative)||!await visit(join(fdPath,child.name),relative))return false;
   }
   return true;
  }finally{await handle.close();}
 }
 return visit(root,'');
}
class UnavailableImage extends Error {
 constructor(ref,cause){super('云端图片已过期或找不到，请重新上传。',{cause});this.ref=ref;}
}
function checkedRef(ref){
 if(!/^sha256:[a-f0-9]{64}$/.test(ref?.attachmentId||'')||!Number.isSafeInteger(ref.bytes)||ref.bytes<0||ref.bytes>MAX)fail('Attachment reference is invalid.');
 return ref.attachmentId.slice(7);
}

export class ManagedAttachmentStore extends AttachmentStore {
 constructor(ctx,{transport=httpTransport(),tempRoot=process.env.MAIMI_SHARED_ATTACHMENT_CACHE||'/tmp/maimi-managed-attachments',now=()=>Date.now(),owner=process.env.CLOUD_OWNER_KEY,generation=Number(process.env.CLOUD_WORKER_GENERATION)}={}){
  super(ctx);this.transport=transport;this.tempRoot=resolve(tempRoot);this.now=now;this.gate=new Gate();this.cacheLock=new Mutex();this.cache=new Map();this.cacheBytes=0;this.closed=false;
  if(!/^[a-f0-9]{32}$/.test(owner||'')||!Number.isSafeInteger(generation)||generation<1)throw Error('ATTACHMENT_CACHE_OWNER_REQUIRED');
  this.owner=owner;this.generation=generation;this.leases=new Map();this.cacheBlocked=false;this.emptyShells=0;
  this.stagingRoot=this.tempRoot==='/workspace/.maimi-cache'?'/tmp/maimi-managed-attachments':this.tempRoot;
  this.ready=this.recoverCache();this.ready.catch(()=>{});
  this.imageLimits=Object.freeze({maxImageBytes:upstream.DEFAULT_MAX_IMAGE_BYTES,maxImagesPerMessage:upstream.DEFAULT_MAX_IMAGES_PER_MESSAGE,
   maxMessageImageBytes:upstream.DEFAULT_MAX_MESSAGE_IMAGE_BYTES,maxImagePixels:upstream.DEFAULT_MAX_IMAGE_PIXELS,
   maxImageDimension:upstream.DEFAULT_MAX_IMAGE_DIMENSION,mediaTypes:Object.freeze(['image/png','image/jpeg','image/webp','image/gif'])});
  this.normalizationPolicy=Object.freeze({maxPixels:upstream.DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS,maxDimension:upstream.DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION,maxBytes:upstream.DEFAULT_NORMALIZED_IMAGE_MAX_BYTES});
  this.timer=setInterval(()=>{this.sweep().catch(()=>{});},10000);this.timer.unref();
 }
 async recoverCache(){
  let entries;try{entries=await readdir(this.tempRoot,{withFileTypes:true});}catch(error){if(error.code==='ENOENT')return;throw error;}
  if(entries.length>CACHE_ENTRIES_MAX){this.cacheBlocked=true;this.cacheBytes=CACHE_MAX;return;}
  for(const entry of entries){
   const root=join(this.tempRoot,entry.name);
   try{
    if(!entry.isDirectory()||entry.isSymbolicLink()||!/^cache-[A-Za-z0-9]{6}$/.test(entry.name))throw Error('unmanaged cache entry');
    const markerPath=join(root,CACHE_MARKER);let stat;
    try{stat=await lstat(markerPath);}catch(error){
     if(error.code==='ENOENT'&&await emptyCacheShell(root)){this.emptyShells++;continue;}
     throw error;
    }
    if(!stat.isFile()||stat.isSymbolicLink()||stat.size>16384)throw Error('unmanaged cache marker');
    const marker=JSON.parse(await readFile(markerPath,'utf8'));
    if(marker.schema!==CACHE_SCHEMA||marker.owner!==this.owner||marker.cache_id!==entry.name||!Number.isSafeInteger(marker.generation)||marker.generation<1||marker.generation>this.generation
     ||!SID.test(marker.session_id||'')||!Number.isSafeInteger(marker.bytes)||marker.bytes<0||marker.bytes>MAX||!Number.isFinite(Date.parse(marker.expires_at))
     ||!Number.isFinite(Date.parse(marker.source_expires_at))||Date.parse(marker.expires_at)>Date.parse(marker.source_expires_at)
     ||!/^sha256:[a-f0-9]{64}$/.test('sha256:'+marker.sha256)||!Array.isArray(marker.relative_files)||marker.relative_files.length!==2)throw Error('invalid cache marker');
    const object='attachments/v1/file-objects/'+marker.sha256.slice(0,2)+'/'+marker.sha256;
    const alias='attachments/v1/files/'+marker.sha256.slice(0,2)+'/'+marker.sha256+'/';
    const [one,two]=marker.relative_files;
    if(one?.path!==object||!two?.path?.startsWith(alias)||!two.path.slice(alias.length)||/[\\/\x00]/.test(two.path.slice(alias.length))
     ||[one,two].some(file=>file.bytes!==marker.bytes||file.sha256!==marker.sha256))throw Error('invalid cache files');
    const expected=new Set([CACHE_MARKER,one.path,two.path]);
    for(const file of [one,two]){const pieces=file.path.split('/');for(let i=1;i<pieces.length;i++)expected.add(pieces.slice(0,i).join('/'));}
    const pending=[''];let visited=0;
    while(pending.length){const prefix=pending.pop();for(const child of await readdir(join(root,prefix),{withFileTypes:true})){
     if(++visited>32)throw Error('cache tree limit');const name=prefix?prefix+'/'+child.name:child.name;
     if(!expected.has(name)||child.isSymbolicLink())throw Error('unknown cache content');
     if(child.isDirectory())pending.push(name);else if(!child.isFile())throw Error('invalid cache file');
    }}
    for(const file of [one,two]){const found=await lstat(join(root,file.path));if(!found.isFile()||found.isSymbolicLink()||found.size!==file.bytes)throw Error('cache bytes changed');}
    // Restart cannot prove that a C process using an old copy is gone. Charge
    // it to the budget and leave it solely to the independently fenced helper.
    this.cache.set('recovered:'+entry.name,{root,bytes:marker.bytes,expires:Date.parse(marker.expires_at),sessionId:marker.session_id,
     fileId:marker.file_id,pins:new Set(),uncertain:true,recovered:true,marker});this.cacheBytes+=marker.bytes;
   }catch{this.cacheBlocked=true;}
  }
  if(this.cacheBlocked)this.cacheBytes=Math.max(this.cacheBytes,CACHE_MAX);
 }
 async writeMarker(item){
  const marker={...item.marker,pins:item.pins.size,uncertain:item.uncertain};
  const next=join(item.root,CACHE_MARKER+'.new-'+randomUUID());
  try{await writeFile(next,JSON.stringify(marker)+'\n',{mode:0o600,flag:'wx'});await rename(next,join(item.root,CACHE_MARKER));item.marker=marker;}
  finally{await rm(next,{force:true}).catch(()=>{});}
 }
 async borrowSession(sessionId){
  return this.cacheLock.run(async()=>{
   await this.ready;if(this.closed||this.cacheBlocked)throw cacheError('ATTACHMENT_CACHE_UNAVAILABLE','工作区暂时无法读取附件，请稍后重试。');
   if(!SID.test(sessionId||''))throw Error('ATTACHMENT_CACHE_SESSION_INVALID');
   if([...this.leases.values()].some(lease=>lease.sessionId===sessionId&&lease.uncertain))throw cacheError('ATTACHMENT_CACHE_OUTCOME_UNKNOWN','上一次使用附件的操作结果尚未确认。请先核对任务是否完成，再决定是否重试。');
   if(this.leases.size>=CACHE_PINS_MAX)throw Error('ATTACHMENT_CACHE_PINS_FULL');
   // Never grant a fresh operation access to expired or uncertain copies.
   // A recovered copy with no outstanding use stays charged and untouched.
   // Only newly staged bytes from the current generation can be lent again.
   const items=[...this.cache.values()].filter(item=>item.sessionId===sessionId
    &&!(item.recovered&&item.marker.pins===0&&item.marker.uncertain===false));
   if(items.some(item=>item.recovered||item.uncertain||item.revoked||item.expires<=this.now()))throw cacheError('ATTACHMENT_CACHE_LEASE_EXPIRED','工作区暂时无法继续使用这份附件。请先核对上一项任务的状态，再重试。');
   const id=randomUUID();this.leases.set(id,{sessionId,everPinned:items.length>0});
   try{for(const item of items){item.pins.add(id);await this.writeMarker(item);}}
   catch(error){this.leases.get(id).uncertain=true;for(const item of items)item.uncertain=true;throw error;}
   let completed=false;
   return {pinnedFileBytes(path){
    if(completed||typeof path!=='string')return undefined;
    const item=items.find(entry=>entry.path===path||entry.marker.relative_files.some(file=>join(entry.root,file.path)===path));
    return item?.bytes;
   },release:async({confirmed=false}={})=>{
    if(completed)return;completed=true;
    return this.cacheLock.run(async()=>{
     const held=[...this.cache.values()].filter(item=>item.pins.has(id));
     // An empty cache lease is not an execution receipt. Release only its
     // empty bookkeeping; the failed remote operation still throws and must
     // never be replayed. Previously pinned or unknown leases stay fenced.
     if(!confirmed&&held.length===0&&this.leases.get(id)?.everPinned===false){this.leases.delete(id);return false;}
     if(!confirmed){this.leases.get(id).uncertain=true;for(const item of held){item.uncertain=true;await this.writeMarker(item).catch(()=>{});}return false;}
     this.leases.delete(id);
     for(const item of held){item.pins.delete(id);try{await this.writeMarker(item);}catch{item.uncertain=true;}}
     try{await this._sweep();return true;}catch{return false;}
    });
   }};
  });
 }
 async temporary(fn){
  await this.ready;
  if(this.closed)throw Error('attachment store closed');
  await mkdir(this.stagingRoot,{recursive:true,mode:0o700});
  const root=await mkdtemp(join(this.stagingRoot,'request-'));
  // Upstream treats dirname(dirname(root)) as its private DSH_HOME and
  // chmods that boundary. Preserve attachments/v1 depth so /tmp is untouched.
  try{return await fn(join(root,'attachments','v1'));}finally{await removeLocalCopy(root);}
 }
 context(){return scope.getStore()||{sessionId:'',requestId:randomUUID()};}
 async existing(ref,signal){try{return await this.metadata(ref,signal);}catch(error){if(error.status===404||error.status===410)return null;throw error;}}
 async validateImage(input){await this.gate.run(()=>upstream.validateImageFile(input,this.imageLimits,this.normalizationPolicy));}
 async saveImage(input){return (await this.saveImages([input]))[0];}
 async saveImages(inputs){
  this.validateImageBatch(inputs);
  // All source images pass upstream admission before any object is committed.
  const prepared=[];
  for(const input of inputs)prepared.push(await this.gate.run(()=>upstream.prepareImageFile(input,this.imageLimits,this.normalizationPolicy)));
  const context=this.context();
  for(let index=0;index<prepared.length;index++){
   const image=prepared[index];
   if(await this.existing(image.ref))continue;
   await this.transport('put',{...context,requestId:`${context.requestId}:image:${index}:${image.ref.attachmentId.slice(7)}`,
    name:image.ref.name||'image',sha256:checkedRef(image.ref),size:image.ref.bytes,mediaType:image.ref.mediaType,body:image.data});
  }
  return prepared.map(image=>image.ref);
 }
 async saveFile(input){return this.saveFileStream({name:input.name,data:(async function*(){yield input.data;})()});}
 async saveFileStream(input){
  const context=this.context();
  return this.gate.run(()=>this.temporary(async root=>{
   let count=0;const bounded=(async function*(){for await(const chunk of input.data){input.signal?.throwIfAborted();count+=chunk.byteLength;if(count>MAX)fail('File exceeds the 32 MiB web limit.');yield chunk;}})();
   // Calling the actual upstream implementation preserves its filename rules,
   // streaming digest, immutable publication, and exact SHA reference shape.
   const ref=await upstream.LocalAttachmentStore.prototype.saveFileStream.call({root},{...input,data:bounded});
   const path=upstream.LocalAttachmentStore.prototype.fileHostPath.call({root},ref);
   const metadata=await this.existing(ref,input.signal)||await this.transport('put',{...context,requestId:`${context.requestId}:file:${checkedRef(ref)}:${createHash('sha256').update(ref.name).digest('hex').slice(0,16)}`,
    name:ref.name,sha256:checkedRef(ref),size:ref.bytes,body:Readable.toWeb(createReadStream(path)),signal:input.signal});
   // Populate a separate bounded execution cache, never DSH_HOME/file-objects.
   await this.cacheFile(context.sessionId,ref,metadata,createReadStream(path));
   return ref;
  }));
 }
 async metadata(ref,signal){
  const body={sha256:checkedRef(ref),name:ref.name||'image',bytes:ref.bytes};
  if(ref.name===undefined&&this.imageLimits.mediaTypes.includes(ref.mediaType)){body.name=null;body.imageMediaType=ref.mediaType;}
  return this.transport('resolve',{...this.context(),body,signal});
 }
 async fetchFile(ref,signal){
  const metadata=await this.metadata(ref,signal);
  const response=await this.transport('read',{...this.context(),body:{file_id:metadata.file_id},signal});
  return {metadata,response};
 }
 async readImage(ref,signal){
  const {response}=await this.fetchFile(ref,signal);const chunks=[];let bytes=0;const hash=createHash('sha256');
  for await(const chunk of response.body){signal?.throwIfAborted();bytes+=chunk.byteLength;if(bytes>ref.bytes)fail('Attachment bytes changed.');hash.update(chunk);chunks.push(chunk);}
  if(bytes!==ref.bytes||hash.digest('hex')!==checkedRef(ref))fail('Attachment bytes changed.');
  return {ref,data:Buffer.concat(chunks,bytes)};
 }
 async readImageRequest(ref,target,signal){
  return this.gate.run(()=>this.temporary(async root=>{
   let stored;
   try{stored=await this.readImage(ref,signal);}
   catch(error){if(error.status===404||error.status===410)throw new UnavailableImage(ref,error);throw error;}
   return upstream.readRequestImageFile(root,stored,target,signal);
  }));
 }
 async *streamWithHistory(options,stream){
  let request=options;
  for(;;){
   let dispatched=false;
   try{
    for await(const chunk of stream(request)){dispatched=true;yield chunk;}
    return;
   }catch(error){
    // Only our pre-provider object read can throw this error. Provider, auth,
    // integrity and active-turn failures must never be replayed or concealed.
    if(dispatched||!(error instanceof UnavailableImage)||options.signal?.aborted)throw error;
    const session=this.ctx.get('sessions')?.get(options.sessionId);
    const events=session?.snapshotEvents();
    const start=events?.findLast(e=>e.type==='turn/start');
    if(!start)throw error;
    const earlier=new Set();
    for(const e of events){
     if(e.seq>=start.seq)break;
     if(e.type==='user/message')earlier.add(e.data.id);
     if(e.type==='tool/result')earlier.add(e.data.message.id);
    }
    let changed=false;
    const messages=request.messages.map(m=>{
     const matches=b=>b.type==='image'&&b.offloaded!==true&&b.attachment.attachmentId===error.ref.attachmentId
      &&b.attachment.name===error.ref.name&&b.attachment.bytes===error.ref.bytes&&b.attachment.mediaType===error.ref.mediaType;
     if(!m.content.some(matches))return m;
     if(!earlier.has(m.id))throw error;
     changed=true;
     return {...m,content:m.content.map(b=>matches(b)?{type:'text',text:'[历史图片已失效，本轮未提供其像素。如需查看，请用户重新上传。]'}:b)};
    });
    if(!changed)throw error;
    request={...request,messages};
   }
  }
 }
 async *readFileStream(ref,signal){
  const {metadata,response}=await this.fetchFile(ref,signal);
  const context=this.context();
  const cached=await this.cacheFile(context.sessionId,ref,metadata,response.body,signal);
  const lease=await this.borrowSession(context.sessionId);
  try{for await(const chunk of createReadStream(cached.path)){signal?.throwIfAborted();if(cached.revoked||cached.expires<=this.now())fail('Temporary attachment lease expired.');yield chunk;}}
  finally{await lease.release({confirmed:true});}
 }
 async cacheFile(sessionId,ref,metadata,chunks,signal){
  try{return await this.cacheLock.run(()=>this._cacheFile(sessionId,ref,metadata,chunks,signal));}
  catch(error){if(typeof chunks.cancel==='function')await chunks.cancel().catch(()=>{});else chunks.destroy?.();throw error;}
 }
 async _cacheFile(sessionId,ref,metadata,chunks,signal){
  await this.ready;checkedRef(ref);if(!SID.test(sessionId||''))fail('Attachment session is invalid.');await this._sweep();
  if(this.closed||this.cacheBlocked||this.cache.size+this.emptyShells>=CACHE_ENTRIES_MAX)fail('工作区暂时无法准备附件，请稍后重试。');
  if([...this.leases.values()].some(lease=>lease.sessionId===sessionId&&lease.uncertain))fail('上一次使用附件的操作结果尚未确认，请先核对任务状态。');
  const key=sessionId+':'+keyOf(ref),previous=this.cache.get(key);
  if(previous&&(previous.pins.size||previous.uncertain||previous.recovered))fail('这份附件仍在使用，请等待当前任务完成。');
  if(ref.bytes>CACHE_MAX-this.cacheBytes)fail('工作区附件的临时可用空间不足。');
  const expiry=Math.min(Date.parse(metadata.expires_at),this.now()+CACHE_SECONDS*1000);
  if(!Number.isFinite(expiry)||expiry<=this.now())fail('Attachment expired.');
  await mkdir(this.tempRoot,{recursive:true,mode:0o700});
  const root=await mkdtemp(join(this.tempRoot,'cache-'));this.cacheBytes+=ref.bytes;
  const localRoot=join(root,'attachments','v1');
  let retained=false;
  try{
   const sha256=checkedRef(ref);
   const path=upstream.LocalAttachmentStore.prototype.fileHostPath.call({root:localRoot},ref);
   await writeSharedCopy(localRoot,path,ref,chunks,signal);
   const files=[join('attachments','v1','file-objects',sha256.slice(0,2),sha256),relative(root,path)].map(p=>({path:p.split(sep).join('/'),sha256,bytes:ref.bytes}));
   const item={root,path,sessionId,bytes:ref.bytes,expires:expiry,fileId:metadata.file_id,pins:new Set([...this.leases].filter(([,lease])=>lease.sessionId===sessionId).map(([id])=>id)),uncertain:false,recovered:false,
    marker:{schema:CACHE_SCHEMA,owner:this.owner,generation:this.generation,cache_id:basename(root),session_id:sessionId,created_at:new Date(this.now()).toISOString(),expires_at:new Date(expiry).toISOString(),
     source_expires_at:new Date(Date.parse(metadata.expires_at)).toISOString(),file_id:metadata.file_id,bytes:ref.bytes,sha256,relative_files:files}};
   for(const id of item.pins)this.leases.get(id).everPinned=true;
   await this.writeMarker(item);
   if(previous){await removeLocalCopy(previous.root);this.cacheBytes-=previous.bytes;}
   this.cache.set(key,item);retained=true;return item;
  }finally{if(!retained){this.cacheBytes-=ref.bytes;await removeLocalCopy(root);}}
 }
 fileHostPath(ref){
  checkedRef(ref);const session=scope.getStore()?.sessionId;if(!session)return undefined;
  const item=this.cache.get(session+':'+keyOf(ref));return item&&!item.uncertain&&!item.revoked&&item.expires>this.now()?item.path:undefined;
 }
 imageHostPath(ref){checkedRef(ref);return undefined;}
 async _sweep(ids=null){await this.ready;for(const [key,item]of this.cache){if(!item.revoked&&item.expires>this.now()&&!ids?.has(item.fileId))continue;item.revoked=true;if(item.pins.size||item.uncertain||item.recovered)continue;await removeLocalCopy(item.root);this.cache.delete(key);this.cacheBytes-=item.bytes;}}
 async sweep(){return this.cacheLock.run(()=>this._sweep());}
 async invalidate(ids){return this.cacheLock.run(()=>this._sweep(new Set(ids)));}
 async close(){this.closed=true;clearInterval(this.timer);await this.cacheLock.run(async()=>{await this.ready;for(const [key,item]of this.cache){item.revoked=true;if(item.pins.size||item.uncertain||item.recovered)continue;await removeLocalCopy(item.root);this.cache.delete(key);this.cacheBytes-=item.bytes;}});}
 async hydrateSession(ctx,sessionId,signal){
  const refs=new Map();let events=0,bytes=0;
  const inspect=items=>{for(const event of items){events++;if(events>50000)fail('Conversation has too many attachment records.');
   if(event?.type!=='user/message'||event.data?.source?.kind!=='user'||!Array.isArray(event.data.content))continue;
   for(const part of event.data.content){if(part?.type!=='file')continue;checkedRef(part.attachment);refs.set(keyOf(part.attachment),part.attachment);}
  }};
  const attached=ctx.sessions.get(sessionId);
  if(attached)inspect(attached.snapshotEvents());
  else {
   const stat=await ctx.sessionPersistence.stat(sessionId,{signal});if(!stat)return;
   const handle=await ctx.sessionPersistence.open(sessionId,'read',{signal});
   try{for(let offset=0;offset<stat.eventCount;){const page=await handle.read(offset,256,{signal});if(!page.events.length)break;inspect(page.events);offset+=page.events.length;}}
   finally{await handle.close();}
  }
  // Metadata probes exclude expired attachments before charging the bounded
  // execution cache; reads never extend the 24-hour server deadline.
  const current=[];
  for(const ref of refs.values()){
   let metadata;try{metadata=await this.metadata(ref,signal);}catch(error){if(error.status===410||error.status===404)continue;throw error;}
   bytes+=ref.bytes;if(bytes>CACHE_MAX||current.length>=32)fail('Too many live files for one temporary workspace; remove files or start a new conversation.');
   current.push({ref,metadata});
  }
  for(const {ref,metadata}of current){
   const existing=this.cache.get(sessionId+':'+keyOf(ref));if(existing&&existing.expires>this.now())continue;
   let response;
   try{response=await this.transport('read',{...this.context(),body:{file_id:metadata.file_id},signal});}
   catch(error){if(error.status===404||error.status===410)continue;throw error;}
   await this.cacheFile(sessionId,ref,metadata,response.body,signal);
  }
 }
}

export function installScopeWrappers(ctx){
 ctx.inject(['fileUploads'],inner=>{
  const service=inner.fileUploads;
  const resolve=service.resolve;
  const resolvePart=function(agent,id){return orderDocumentPart(agent,id,resolve.call(this,agent,id));};
  service.resolve=resolvePart;
  inner.on('dispose',()=>{if(service.resolve===resolvePart)service.resolve=resolve;});
  for(const method of ['upload','uploadStream','uploadImageStream']){
   const original=service[method];
   const wrapped=function(...args){const sessionId=method==='upload'?args[0]?.session?.id:args[0]?.sessionId;
    return withAttachmentScope({sessionId,requestId:randomUUID()},()=>original.apply(this,args));};
   service[method]=wrapped;inner.on('dispose',()=>{if(service[method]===wrapped)service[method]=original;});
  }
 });
 ctx.inject(['sessionController','attachments','sessions','sessionPersistence'],inner=>{
  const service=inner.sessionController;
  for(const method of ['prompt','attachment','updateQueue']){
   const original=service[method];if(typeof original!=='function')throw Error('rc3 session attachment seam unavailable');
   const wrapped=function(request,...rest){return withAttachmentScope({sessionId:request?.sessionId,requestId:request?.requestId||randomUUID()},async()=>{
    if(method==='prompt')await inner.attachments.hydrateSession(inner,request.sessionId,rest[0]);
    return original.call(this,request,...rest);
   });};
   service[method]=wrapped;inner.on('dispose',()=>{if(service[method]===wrapped)service[method]=original;});
  }
 });
 ctx.inject(['connection','attachments'],inner=>{
  inner.effect(()=>inner.connection.fetch.register({path:'/api/megmeet-cloud/attachments/expire',methods:['POST'],requestBody:'streaming',
   fetch:async request=>{
    // Parent wrapper excludes the entire /api/megmeet-cloud prefix from users.
    if(request.headers.has('origin'))return new Response(null,{status:403});
    let bytes=0;const chunks=[];for await(const chunk of request.body||[]){bytes+=chunk.byteLength;if(bytes>8192)return new Response(null,{status:413});chunks.push(chunk);}
    let data;try{data=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{return new Response(null,{status:400});}
    if(!data||Object.keys(data).join(',')!=='file_ids'||!Array.isArray(data.file_ids)||data.file_ids.length>100||data.file_ids.some(id=>!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(id)))return new Response(null,{status:400});
    await inner.attachments.invalidate(data.file_ids);return Response.json({expired:true});
   }}),'managed attachment cache expiry');
 });
}
export async function apply(ctx){
 let fiber,service;
 ctx.effect(function*(){yield ()=>service?.close();fiber=ctx.plugin({name:'maimi-managed-attachment-service',apply(inner){service=new ManagedAttachmentStore(inner);}});yield fiber.dispose;},'maimi NAS attachments');
 await fiber.await();installScopeWrappers(ctx);
}
