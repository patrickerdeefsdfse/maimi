// Worker-only adapter. Private native task tickets, never renderer parameters,
// choose a task. The captured task/lease/generation is held until search ends.
import { timingSafeEqual } from 'node:crypto';
import { createSearchPermits, deny, TICKET } from './search-permits.mjs';
import { createSearchLoopback } from './search-loopback.mjs';

const MTT=/^mtt_[A-Za-z0-9_-]{43}$/;
const SAFE=new Set(['SEARCH_AUTH_REQUIRED','SEARCH_SCOPE_REVOKED','SEARCH_SELECTION_CHANGED','SEARCH_NOT_SELECTED',
 'SEARCH_MODEL_UNAVAILABLE_FOR_ACCOUNT','SEARCH_UPSTREAM_UNSUPPORTED','SEARCH_UPSTREAM_UNAVAILABLE','SEARCH_RESPONSE_INVALID',
 'SEARCH_TIMEOUT','SEARCH_BUSY','SEARCH_REQUEST_INVALID','SEARCH_REPLAY','SEARCH_NO_RESULT','SEARCH_BING_FAILED']);
const matches=(left,right)=>typeof left==='string'&&Buffer.byteLength(left)===Buffer.byteLength(right)&&timingSafeEqual(Buffer.from(left),Buffer.from(right));
async function boundedJson(stream,maximum=8192){let size=0;const parts=[];for await(const part of stream||[]){size+=part.byteLength;if(size>maximum)deny('SEARCH_REQUEST_INVALID',413);parts.push(Buffer.from(part));}try{return JSON.parse(Buffer.concat(parts).toString('utf8'));}catch{deny('SEARCH_REQUEST_INVALID',400);}}

export function createSearchHost({consumeToolTicket,resolveScope,validateTask,toolkit,routeToken,modelBase,fetchImpl=fetch}){
 if(!/^[A-Za-z0-9_-]{43}$/.test(routeToken||'')||!/^http:\/\/[a-z0-9-]+:8080\/internal\/personal-cloud\/model\/v1$/.test(modelBase||''))throw Error('SEARCH_HOST_CONFIG_INVALID');
 const grants=new Map();
 const permits=createSearchPermits({resolveTask:resolveScope,authorize:validateTask,
  selection:(session,context)=>toolkit.snapshot(session,context),
  onRelease:permit=>{const grant=grants.get(permit.id);if(grant){grants.delete(permit.id);grant.signal?.removeEventListener('abort',grant.abort);grant.finish();}}});
 const loopback=createSearchLoopback({permits,remote:async(operation,body,{lease,signal})=>{
   const path=operation==='messages'?'messages':operation==='fallback'?'bing':null;
   if(!path)deny('SEARCH_REQUEST_INVALID',400);
   let response;
   try{response=await fetchImpl(new URL('/internal/personal-cloud/search/'+path,modelBase),{method:'POST',redirect:'error',headers:{Authorization:'Bearer '+lease,'Content-Type':'application/json'},body:JSON.stringify(body),signal});}
   catch(error){if(signal.aborted)deny('SEARCH_SCOPE_REVOKED');deny(error?.name==='TimeoutError'?'SEARCH_TIMEOUT':'SEARCH_UPSTREAM_UNAVAILABLE',502);}
   let value;try{value=await boundedJson(response.body,512*1024);}catch{deny('SEARCH_RESPONSE_INVALID',502);}
   if(!response.ok)deny(SAFE.has(value?.code)?value.code:'SEARCH_SCOPE_REVOKED',response.status);
   return value;
 }});
 async function handle(req,res){
  const reply=(status,value)=>{if(!res.headersSent&&!res.destroyed){res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));}};
  // A browser never calls this native-only endpoint, including same-host pages.
  if(req.headers.origin||req.headers['sec-fetch-site']){reply(403,{code:'SEARCH_SCOPE_REVOKED'});return;}
  if(!['/__megmeet/search/issue','/__megmeet/search/release'].includes(req.url))return loopback.handle(req,res);
  let grant,credential;
  try{
   if(req.method!=='POST'||!matches(req.headers.authorization,'Bearer '+routeToken))deny('SEARCH_SCOPE_REVOKED');
   const body=await boundedJson(req);if(!body||Object.keys(body).join(',')!=='ticket')deny('SEARCH_REQUEST_INVALID',400);
   if(req.url.endsWith('/release')){if(!TICKET.test(body.ticket||''))deny('SEARCH_REQUEST_INVALID',400);permits.release(body.ticket);reply(200,{status:'released'});return;}
   if(!MTT.test(body.ticket||''))deny('SEARCH_NATIVE_SCOPE_REQUIRED');
   grant=await consumeToolTicket(body.ticket,'web-search');
   if(!grant?.scope||!grant?.task||typeof grant.finish!=='function'||grant.signal?.aborted)deny('SEARCH_SCOPE_REVOKED');
   if(await resolveScope(grant.scope)!==grant.task)deny('SEARCH_SCOPE_REVOKED');
   credential=await permits.issue({...grant.scope,purpose:'web-search'});
   const held=grant;held.abort=()=>permits.release(credential);grants.set(credential,held);grant=null;
   held.signal?.addEventListener('abort',held.abort,{once:true});
   if(held.signal?.aborted){permits.release(credential);deny('SEARCH_SCOPE_REVOKED');}
   reply(200,{ticket:credential});
  }catch(error){if(credential)permits.release(credential);reply(Number.isInteger(error?.status)?error.status:403,{code:SAFE.has(error?.code)?error.code:'SEARCH_SCOPE_REVOKED'});}
  finally{grant?.finish?.();}
 }
 return {handle,callback:(body,context)=>permits.callback(body,context),revokeTask:task=>permits.revokeTask(task),get busy(){return permits.busy;}};
}
