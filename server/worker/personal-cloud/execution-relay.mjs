// Private native-process -> authenticated parent -> Gateway -> C. No local fallback.
import {setTimeout as delay} from 'node:timers/promises';
import {receiptSchema,operationLabel,isRead} from './execution-outcome.mjs';
export function createExecutionRelay({tickets,validateTask,modelBase,routeToken,generation,readJson,fetcher=fetch,documentOpenTimeoutMs=42000,transferScope}){
 const endpoint=new URL('/internal/execution/v1/',modelBase).href;
 async function call(action,body,task,signal,onDispatch){
  await validateTask(task);
  const encoded=JSON.stringify(body);
  signal.throwIfAborted();
  onDispatch?.();
  const response=await fetcher(endpoint+action,{method:'POST',redirect:'error',headers:{'Content-Type':'application/json',
   Authorization:'Bearer '+task.lease,'X-Maimi-Chat-Token':routeToken,'X-Maimi-Chat-Generation':String(generation),'X-Maimi-Task-Generation':String(task.generation)},
   body:encoded,signal:AbortSignal.any([AbortSignal.timeout(action==='open'?100000:50000),signal])});
  const chunks=[];let bytes=0;
  for await(const chunk of response.body||[]){bytes+=chunk.byteLength;if(bytes>16*1024*1024)throw Error('EXECUTION_RESPONSE_LIMIT');chunks.push(chunk);}
  const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(!response.ok){
   const code=value?.code;
   // A generic 409 (generation/auth/proxy failure) is not a worker receipt.
   const workerReplied=action==='call' && response.status===409 && typeof code==='string'
    && /^[A-Z_]{3,80}$/.test(code) && code!=='EXECUTION_UNAVAILABLE';
   const refusedBeforeDispatch=workerReplied&&code==='EXECUTION_DRAIN_BEFORE_DISPATCH';
   throw Object.assign(Error('EXECUTION_RPC_FAILED'),{workerReplied,refusedBeforeDispatch,status:response.status});
  }
  await validateTask(task);return value;
 }
 return async(req,res)=>{
  const grant=tickets.consume(String(req.headers.authorization||'').replace(/^Bearer /,''),'tool','execution');
  const stopped=new AbortController(),disconnect=()=>stopped.abort();req.once('aborted',disconnect);res.once('close',disconnect);
  let queue,execGeneration,operation='other',dispatched=false,stage='validate';
  try{
   if(req.method!=='POST'||req.headers.origin||req.headers['sec-fetch-site'])throw Error('EXECUTION_PRIVATE_ONLY');
   const body=await readJson(req,12*1024*1024),signal=AbortSignal.any([grant.signal,stopped.signal]);
   if(!body||!body.rpc||body.rpc.session_id!==grant.scope.rootSessionId)throw Error('EXECUTION_SCOPE_MISMATCH');
   operation=operationLabel(body.rpc.operation);
   execGeneration=body.generation;
   if(execGeneration===undefined){
    stage='open';
    const start=Date.now();
    // Leave time for a positive not-started receipt before the 60s native tool deadline.
    const openSignal=body.rpc.operation==='document.read'?AbortSignal.any([signal,AbortSignal.timeout(documentOpenTimeoutMs)]):signal;
    while(true){
     const host=await call('open',{resource_class:body.resource_class||'standard',...(queue?{queue_request_id:queue}:{})},grant.task,openSignal);
     if(host.request_id)queue=host.request_id;
     openSignal.throwIfAborted();
     if(host.status==='ready'){execGeneration=host.generation;break;}
     if(Date.now()-start>90000)throw Object.assign(Error('执行资源正在排队，请稍后重试；本次操作尚未开始。'),{code:'EXECUTION_QUEUE_WAIT_LIMIT'});
     await delay(2000,undefined,{signal:openSignal});
    }
   }
   if(!Number.isSafeInteger(execGeneration)||execGeneration<1)throw Error('EXECUTION_GENERATION_REQUIRED');
   stage='call';
   const trusted=grant.task.restrictedExecution?{transfer:await transferScope(grant.task,signal)}:{};
   const value=await call('call',{generation:execGeneration,rpc:body.rpc,...trusted},grant.task,signal,()=>{dispatched=true;});
   res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({...value,generation:execGeneration}));
  }catch(error){
   const disposition=!dispatched||error.refusedBeforeDispatch?'not_started':isRead(operation)&&error.workerReplied?'finished_error':'unknown';
   const record={schema:receiptSchema,operation,disposition};
   console.warn(JSON.stringify({...record,stage,upstream_status:Number.isInteger(error.status)?error.status:null}));
   if(!res.headersSent&&!res.destroyed){
    res.writeHead(disposition==='unknown'?503:409,{'Content-Type':'application/json','Cache-Control':'no-store'});
    res.end(JSON.stringify(record));
   }
  }finally{
   // Cancel only this owner's still-waiting request; never terminate an admitted
   // generation. Use a fresh bounded signal after the business request ends.
   if(queue&&execGeneration===undefined){
    try{const response=await fetcher(endpoint+'cancel',{method:'POST',redirect:'error',headers:{'Content-Type':'application/json',Authorization:'Bearer '+grant.task.lease,'X-Maimi-Chat-Token':routeToken,'X-Maimi-Chat-Generation':String(generation),'X-Maimi-Task-Generation':String(grant.task.generation)},body:JSON.stringify({request_id:queue}),signal:AbortSignal.timeout(5000)});await response.body?.cancel();}catch{}
   }
   req.removeListener('aborted',disconnect);res.removeListener('close',disconnect);grant.finish();
  }
 };
}
