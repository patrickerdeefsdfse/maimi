// Web-only native event persistence. All acknowledged appends are PG commits.
import {randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require=createRequire((process.env.MAIMI_DSH_RUNTIME_ROOT||'/opt/dsh')+'/package.json');
const {
 SessionPersistence,SessionPersistenceRevision,materializeCreateHeader,materializeAppendBatch,
 assertVersion,assertStoredId,assertContiguous,validateStoredEvents,
 SessionAlreadyExistsError,SessionAlreadyOwnedError,SessionPersistenceNotFoundError,
 SessionOwnershipLostError,SessionReadOnlyError,SessionHandleClosedError,
}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-session-persistence')).href);

export const name='maimi-pg-session-persistence';
export function httpTransport({endpoint='http://127.0.0.1:3086/__megmeet/history',token=process.env.MEGMEET_MODEL_TOKEN}={}){
 if(endpoint!=='http://127.0.0.1:3086/__megmeet/history'||!/^[-_A-Za-z0-9]{43}$/.test(token||''))throw Error('history bridge configuration invalid');
 return async(operation,body,{signal}={})=>{
  const bytes=JSON.stringify(body);
  for(let attempt=0;attempt<3;attempt++){
   signal?.throwIfAborted();
   try{
    const response=await fetch(endpoint+'/'+operation,{method:'POST',redirect:'error',
     headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:bytes,
     signal:AbortSignal.any([AbortSignal.timeout(15000),...(signal?[signal]:[])])});
    const data=await response.json();
    if(response.ok)return data;
    const code=data.detail;
    if(code==='history_already_exists')throw new SessionAlreadyExistsError(body.id);
    if(code==='history_already_owned')throw new SessionAlreadyOwnedError(body.id);
    if(['history_not_found','history_expired'].includes(code))throw new SessionPersistenceNotFoundError(body.id);
    if(['history_ownership_lost','history_generation_lost'].includes(code))throw new SessionOwnershipLostError(body.id);
    if(response.status<500)throw Object.assign(Error('history request refused: '+code),{permanent:true});
    if(attempt===2)throw Object.assign(Error('history temporarily unavailable'),{permanent:true});
   }catch(error){
    if(error.permanent||!(error instanceof TypeError||error.name==='TimeoutError')||attempt===2)throw error;
   }
   await new Promise(r=>setTimeout(r,200*(attempt+1)));
  }
 };
}

const snapshot=value=>({header:materializeCreateHeader(value.header),eventCount:value.eventCount,
 revision:SessionPersistenceRevision(value.revision),formatStatus:'current'});
export class PgSessionPersistence extends SessionPersistence {
 constructor(ctx,transport=httpTransport()){
  super(ctx);this.transport=transport;this.handles=new Set();this.writers=new Map();
 }
 async create(header,options={}){
  options.signal?.throwIfAborted();const meta=materializeCreateHeader(header);assertVersion(meta);
  const count=options.inheritedEventCount??0;
  if(!Number.isSafeInteger(count)||count<0||(meta.isSeeded===true?options.inheritedEventCount===undefined:count!==0))throw TypeError('invalid inherited event count');
  if(this.writers.has(meta.id))throw new SessionAlreadyOwnedError(meta.id);
  this.writers.set(meta.id,null);
  try{const writer=randomUUID();const value=await this.transport('create',{id:meta.id,header:meta,writer,inheritedEventCount:count},options);
   return this.handle(value,'write',writer);
  }catch(error){this.writers.delete(meta.id);throw error;}
 }
 async open(id,access,options={}){
  if(!['read','write'].includes(access))throw TypeError('invalid access');
  if(access==='write'&&this.writers.has(id))throw new SessionAlreadyOwnedError(id);
  if(access==='write')this.writers.set(id,null);
  try{const writer=access==='write'?randomUUID():null;
   const value=await this.transport(access==='write'?'open':'stat',{id,...(writer?{writer}:{})},options);
   assertStoredId(id,value.header);assertVersion(value.header);
   return this.handle(value,access,writer);
  }catch(error){if(access==='write')this.writers.delete(id);throw error;}
 }
 handle(value,access,writer){const handle=new PgHandle(this,value,access,writer);this.handles.add(handle);if(access==='write')this.writers.set(handle.id,handle);return handle;}
 async stat(id,options={}){
  try{const value=await this.transport('stat',{id},options);assertStoredId(id,value.header);assertVersion(value.header);return snapshot(value);}
  catch(error){if(error instanceof SessionPersistenceNotFoundError)return undefined;throw error;}
 }
 async list(options={}){
  const items=[];let after='';
  do{const value=await this.transport('list',{after},options);for(const item of value.items){assertVersion(item.header);items.push(snapshot(item));}after=value.next;}while(after!==null);
  return items;
 }
 async flush(){const results=await Promise.allSettled([...this.handles].filter(h=>h.access==='write').map(h=>h.flush()));aggregate(results,'history flush failed');}
 async close(){const results=await Promise.allSettled([...this.handles].map(h=>h.close()));aggregate(results,'history close failed');}
}
function aggregate(results,message){const failures=results.filter(r=>r.status==='rejected').map(r=>r.reason);if(failures.length)throw new AggregateError(failures,message);}
export const LIVE_BATCH_MS=50;
export function installLiveRouting(ctx,getService){
 return ctx.effect(function*(){
  yield ()=>getService()?.close();
  yield ctx.on('session/event',(session,event)=>getService()?.writers.get(session.id)?.enqueueLive(event));
  yield ctx.on('session/flush',session=>getService()?.writers.get(session.id)?.flush());
  yield ctx.on('session/disposed',session=>{getService()?.writers.get(session.id)?.close().catch(()=>ctx.logger.warn('PG history final drain failed; session durability is unconfirmed'));});
 },'maimi PG live event routing');
}

class PgHandle {
 constructor(service,value,access,writer){
  this.service=service;this.id=value.header.id;this.header=materializeCreateHeader(value.header);
  this.inheritedEventCount=value.inheritedEventCount;this.access=access;this.writer=writer;
  this.cursor=value.eventCount;this.closed=false;this.tail=Promise.resolve();this.failure=null;this.buffered=[];this.bufferedBytes=0;
  if(access==='write'){
   this.timer=setInterval(()=>{this.serial('renew',()=>this.call('renew')).catch(error=>{this.failure=error;});},30000);
   this.timer.unref();
  }
 }
 check(op){if(this.closed)throw new SessionHandleClosedError(this.id,op);if(this.failure)throw this.failure;}
 call(op,extra={},options={}){return this.service.transport(op,{id:this.id,...(this.writer?{writer:this.writer}:{}),...extra},options);}
 serial(op,fn,internal=false){
  if(this.closing&&!internal)return Promise.reject(new SessionHandleClosedError(this.id,op));
  const promise=this.tail.then(()=>{this.check(op);return fn();});
  this.tail=promise.catch(()=>{});return promise;
 }
 async read(offset=0,length,options={}){
  return this.serial('read',async()=>{
   options.signal?.throwIfAborted();
   if(!Number.isSafeInteger(offset)||offset<0||length!==undefined&&(!Number.isSafeInteger(length)||length<0))throw TypeError('invalid history range');
   const meta=await this.call('stat',{},options);const end=Math.min(meta.eventCount,length===undefined?meta.eventCount:offset+length);
   const events=[];
   for(let cursor=offset;cursor<end;){
    const part=await this.call('read',{offset:cursor,length:Math.min(256,end-cursor)},options);
    assertStoredId(this.id,part.header);assertContiguous(this.id,part.events,cursor);
    if(!part.events.length)throw Error('history unexpectedly truncated');
    events.push(...validateStoredEvents(this.header,part.events));cursor+=part.events.length;
   }
   // Each read owns fresh JSON-decoded records; rc5 calls this state detached.
   // This is an in-memory ownership contract, never a persisted event rewrite.
   return {eventState:'detached',events};
  });
 }
 append(events,options={}){
  // Snapshot before queueing: a caller may mutate its buffer after this call.
  const batch=materializeAppendBatch(events);
  return this.serial('append',()=>this.persist(batch,options));
 }
 async persist(batch,options={}){
   if(this.access!=='write')throw new SessionReadOnlyError(this.id,'append');
   options.signal?.throwIfAborted();assertContiguous(this.id,batch,this.cursor);
   validateStoredEvents(this.header,batch);
   for(let i=0;i<batch.length;i+=256){
    const part=batch.slice(i,i+256);
    try{await this.call('append',{offset:this.cursor,events:part},options);this.cursor+=part.length;}
    catch(error){this.failure=error;throw error;}
   }
 }
 enqueueLive(event){
  if(this.closed||this.failure)return;
  try{const batch=materializeAppendBatch([event]),bytes=Buffer.byteLength(JSON.stringify(batch));
   if(this.buffered.length>=4096||this.bufferedBytes+bytes>16*1024**2)throw Error('history pending write limit exceeded; durability unconfirmed');
   this.buffered.push(...batch);this.bufferedBytes+=bytes;
  }
  catch(error){this.failure=error;return;}
  if(this.batchTimer||this.failure)return;
  this.batchTimer=setTimeout(()=>{this.batchTimer=null;this.drainLive().catch(()=>{/* flush observes the retained failure */});},LIVE_BATCH_MS);
  this.batchTimer.unref();
 }
 drainLive(){
  if(this.draining)return this.draining;
  clearTimeout(this.batchTimer);this.batchTimer=null;
  this.draining=(async()=>{
   while(this.buffered.length){
    await this.serial('live append',async()=>{
     const batch=this.buffered.slice(0,256);
     await this.persist(batch);this.buffered.splice(0,batch.length);this.bufferedBytes=this.buffered.reduce((n,e)=>n+Buffer.byteLength(JSON.stringify([e])),0);
    },true);
   }
  })().finally(()=>{this.draining=null;});
  return this.draining;
 }
 async flush(options={}){if(this.closing)throw new SessionHandleClosedError(this.id,'flush');await this.drainLive();return this.serial('flush',async()=>{if(this.access==='write')await this.call('renew',{},options);});}
 close(){
  if(this.closing)return this.closing;
  clearInterval(this.timer);
  this.closing=(async()=>{
   try{
    do{await this.drainLive();await this.tail;}while(this.buffered.length);
    this.check('close');if(this.access==='write')await this.call('close');
   }finally{clearTimeout(this.batchTimer);this.closed=true;this.service.handles.delete(this);if(this.service.writers.get(this.id)===this)this.service.writers.delete(this.id);}
  })();return this.closing;
 }
 [Symbol.asyncDispose](){return this.close();}
}

export async function apply(ctx){
 let fiber,service;
 // Register teardown before the provider, keeping it reachable while dependent
 // consumers dispose and flush; reverse cleanup closes remaining handles last.
 ctx.effect(function*(){
  yield installLiveRouting(ctx,()=>service);
  fiber=ctx.plugin({name:'maimi-pg-persistence-service',apply(inner){service=new PgSessionPersistence(inner);}});
  yield fiber.dispose;
 },'maimi PG history');
 await fiber.await();
}
