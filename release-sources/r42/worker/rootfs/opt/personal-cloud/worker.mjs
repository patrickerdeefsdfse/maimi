// Private native DSH wrapper. IAM/access tokens and actual model keys never enter.
import { spawn } from 'node:child_process';
import {FileTransferGate,withNodeTransfer} from './project-file-transfer-gate.mjs';
const projectFileTransfers=new FileTransferGate();
import http from 'node:http';
import {Readable} from 'node:stream';
import {once} from 'node:events';
import net from 'node:net';
import { timingSafeEqual, randomUUID, createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { readCatalog, normalizeCatalog, providerPatch, createCatalogUpdater, isCompanyChatModel } from './catalog.mjs';
import { TaskAuthority, validateNativeRpc } from './task-authority.mjs';
import { TaskTickets } from './task-tickets.mjs';
import {createSearchHost} from './search-host.mjs';
import { NativeCancelFence } from './native-cancel-fence.mjs';
import { NativeRequestReceipts } from './native-request-receipt.mjs';
import { createCloudOfficeBridge } from './office-bridge.mjs';
import { createCloudWebBridge } from './web-bridge.mjs';
import { IdleController, holdIdleRequest } from './idle-control.mjs';
import { createVisionAdmission, visionError, hasImage } from './vision-admission.mjs';
import { createToolkit } from '/opt/dsh/toolkit/session-tools.mjs';
import { createProjectBridge, boundedResponse } from '/opt/dsh/toolkit/project-bridge.mjs';
import { createProjectRouter, writeProjectCompletion } from '/opt/dsh/toolkit/project-routing.mjs';
// BEGIN R27 HISTORY QUERY: bounded data only; session tools recheck ownership.
function readProjectHistoryQuery(params,action){
 const fields=['history_job_ids','history_children_of','history_before','history_limit'];
 const invalid=()=>Object.assign(Error('history query invalid'),{status:400});
 const result={};const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
 for(const field of fields){
  if(!params.has(field))continue;
  if(action!=='state'||params.getAll(field).length!==1)throw invalid();
  const raw=params.get(field);
  if(field==='history_job_ids'){
   if(raw.length>700)throw invalid();let ids;try{ids=JSON.parse(raw)}catch{throw invalid()}
   if(!Array.isArray(ids)||ids.length>16||ids.some(id=>typeof id!=='string'||!uuid.test(id))||new Set(ids).size!==ids.length)throw invalid();
   result[field]=ids;
  }else if(field==='history_limit'){
   if(!/^(?:[1-9]|[1-4][0-9]|50)$/.test(raw))throw invalid();result[field]=Number(raw);
  }else{if(!uuid.test(raw))throw invalid();result[field]=raw;}
 }
 return result;
}
// END R27 HISTORY QUERY

import { extractDrawingFields } from '/opt/dsh/toolkit/drawing-extraction.mjs';
import * as yaml from '/opt/dsh/node_modules/yaml/dist/index.js';
import { prepareManagedProfile } from './managed-profile.mjs';
import { prepareCommunityProfile } from './community-profile.mjs';
import { enterpriseProfile } from './enterprise-profile.mjs';

const base='http://127.0.0.1:3085';
const routeToken=process.env.CLOUD_ROUTE_TOKEN||'';
const lease=process.env.CLOUD_MODEL_LEASE||'';
const modelBase=process.env.CLOUD_MODEL_BASE_URL||'';
const localModelBase='http://127.0.0.1:3086/v1';
const authority=new TaskAuthority();
const tickets=new TaskTickets({authority});
const nativeRequests=new NativeRequestReceipts();
const pendingVision=new Set();
const modelConnections=new Map();
const nativeSockets=new Set();
let projectBridge, toolkit, nativeCancelFence, projectRouter;
if(!/^[A-Za-z0-9_-]{43}$/.test(routeToken)||!/^mcl_[A-Za-z0-9_-]{43}$/.test(lease)
 || !/^http:\/\/[a-z0-9-]+:8080\/internal\/personal-cloud\/model\/v1$/.test(modelBase)) throw new Error('cloud bootstrap configuration invalid');
const catalog=normalizeCatalog(readCatalog(process.env));
for(const dir of ['/state/dsh','/state/home','/state/cache']) await mkdir(dir,{recursive:true});
await prepareManagedProfile({home:'/state/dsh',catalogConfig:providerPatch(catalog,localModelBase),
 defaultConfig:{provider:'deepseek-official',model:catalog.default_model||'megmeet-unavailable'},yaml});
await prepareCommunityProfile({home:'/state/dsh',runtimeRoot:'/opt/dsh',yaml});
await writeFile('/state/profile.json',JSON.stringify([
 // DSH's startup default schema requires a string, even for an empty catalog.
 // This placeholder grants nothing: provider models=[] and Gateway checks each call.
  ...enterpriseProfile(),
 {insert:[{id:'megmeet-model-policy',name:'file:///opt/personal-cloud/enterprise-model-policy.mjs',config:{modelBase:localModelBase,apiKeyEnv:'MEGMEET_MODEL_TOKEN'}}]},
 {id:'credentials',disabled:true},
 {insert:[{id:'megmeet-scoped-credentials',name:'file:///opt/personal-cloud/native-scoped-credentials.mjs',config:{dshHome:'/state/dsh',watch:false}}]},
 {insert:[{id:'megmeet-cloud-task-control',name:'file:///opt/personal-cloud/task-control-plugin.mjs',config:{endpoint:'http://127.0.0.1:3086/__megmeet/scope',token:routeToken}}]},
 {insert:[{id:'megmeet-review-skills',name:'file:///opt/dsh/review-skills/index.mjs'}]},
 {insert:[{id:'megmeet-cloud-project-files',name:'file:///opt/personal-cloud/project-files-plugin.mjs'}]},
 {insert:[{id:'megmeet-cloud-vision-control',name:'file:///opt/personal-cloud/vision-control-plugin.mjs'}]},
 {insert:[{id:'megmeet-cloud-web-control',name:'file:///opt/personal-cloud/web-control-plugin.mjs',config:{endpoint:'http://127.0.0.1:3086/__megmeet/web',token:routeToken,officialSearch:true}}]},
 {insert:[{id:'megmeet-official-first-search',name:'file:///opt/personal-cloud/official-search-plugin.mjs',config:{endpoint:'http://127.0.0.1:3086/__megmeet/search',token:routeToken}}]},
 {id:'web-search-deepseek',disabled:true},
 {id:'web',config:{searchProvider:'megmeet-gateway',fetchProvider:'megmeet-gateway'}},
 {insert:[{id:'megmeet-selected-connectors',name:'file:///opt/dsh/toolkit/adapter/index.mjs',config:{endpoint:'http://127.0.0.1:3086/__megmeet/tool-execute',token:routeToken}}]},
 {insert:[{id:'megmeet-office',name:'file:///opt/dsh/office-adapter/index.mjs',config:{
   endpoint:'http://127.0.0.1:3086/__megmeet/office',token:routeToken,timeoutMs:240000}}]}
]),{mode:0o600});
// The native loader otherwise copies its library into /tmp, which is
// deliberately noexec in employee Hosts. Load the immutable image binary.
const childEnv={PATH:process.env.PATH,HOME:'/state/home',DSH_HOME:'/state/dsh',XDG_CACHE_HOME:'/state/cache',DSH_TELEMETRY_MODE:'DISABLED',NARB_DISABLE_NATIVE_CACHE:'1',NODE_OPTIONS:'--max-old-space-size=512',MEGMEET_MODEL_TOKEN:routeToken};
let buffer='',cookie='',nativeId='',server;
const child=spawn(process.execPath,['--expose-internals','/opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js','--profile','web','--patch','/state/profile.json','--host','127.0.0.1','--port','3085','--no-open'],{cwd:'/workspace',env:childEnv,stdio:['ignore','pipe','pipe']});
child.stdout.on('data',v=>{buffer=(buffer+v).slice(-32768)});
child.stderr.on('data',v=>{buffer=(buffer+v).slice(-32768)});
child.on('exit',code=>{
 if(!server&&buffer)console.error('NATIVE_STARTUP_FAILED',buffer
  .replaceAll(routeToken,'[redacted]').replaceAll(lease,'[redacted]')
  .replace(/([?&]token=)[A-Za-z0-9_-]+/g,'$1[redacted]').replace(/Bearer\s+[A-Za-z0-9_.-]+/g,'Bearer [redacted]').slice(-6000));
 server?.close();console.log('NATIVE_CHILD_EXIT',code);process.exit(code||1);
});
for(const sig of ['SIGTERM','SIGINT'])process.on(sig,()=>{server?.close();child.kill('SIGTERM')});
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function rpcArgs(method,args){
 const r=await fetch(base+'/api/'+method,{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000),headers:{Cookie:cookie,Origin:base,'Content-Type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:randomUUID(),method,payload:{args}})});
 const body=await r.json();if(!r.ok||body.result?.ok!==true)throw new Error('cloud native bootstrap contract failed');return body.result.value;
}
const rpc=(method,request)=>rpcArgs(method,{request});
const applyCatalog=createCatalogUpdater(rpcArgs,localModelBase);
async function taskState(action=null) {
 const response=await fetch(base+'/api/megmeet-cloud/task-state',{method:action?'POST':'GET',redirect:'error',
   headers:{Cookie:cookie,Origin:base,'Content-Type':'application/json'},signal:AbortSignal.timeout(5000),
   ...(action?{body:JSON.stringify(action)}:{})});
 if(!response.ok)throw Error('cloud task state unavailable');
 return response.json();
}
async function stableTaskState(){
 // A status response sampled before another request's admission must never
 // release the newly acquired task. Retry if any local admission/model moved.
 for(let attempt=0;attempt<5;attempt++){
  const revision=authority.revision;const state=await taskState();
  if(revision===authority.revision)return state;
 }
 throw Error('cloud task state changed; retry');
}
const idleControl=new IdleController({
 sample:async()=>{const state=await stableTaskState();authority.reconcile(state);return state;},
 localBusy:()=>Boolean(authority.busy||authority.modelRequests||authority.dispatches||modelConnections.size||projectBridge?.busy||nativeCancelFence?.busy||nativeRequests.busy),
 onFence:()=>{for(const socket of nativeSockets)socket.pause();},
 onResume:()=>{for(const socket of nativeSockets)if(!socket.destroyed)socket.resume();}
});
function holdRequest(req,res,work=false){
 return holdIdleRequest(idleControl,req,res,{work});
}
nativeCancelFence=new NativeCancelFence({enter:options=>idleControl.enter(options)});
async function validateTask(task) {
 let response;
 try{response=await fetch(modelBase+'/session',{redirect:'error',headers:{Authorization:'Bearer '+task.lease},signal:AbortSignal.timeout(10000)});}
 catch{throw Object.assign(Error('云端连接暂时不可用，请稍后重试。'),{loginExpired:false,nativeCode:'session/cloud-unavailable',status:503});}
 if(!response.ok){
  const expired=[401,403].includes(response.status);
  throw Object.assign(Error(expired?'云端登录已失效，请重新进入工作区。':'云端连接暂时不可用，请稍后重试。'),
   {loginExpired:expired,nativeCode:expired?'session/cloud-login-expired':'session/cloud-unavailable',status:expired?401:503});
 }
}
const prepareVision=createVisionAdmission({
 selection:async(sessionId,signal)=>{
  const response=await fetch(base+'/api/megmeet-cloud/vision-selection?sessionId='+encodeURIComponent(sessionId),{
   redirect:'error',headers:{Cookie:cookie,Origin:base},signal:AbortSignal.any([AbortSignal.timeout(15000),signal].filter(Boolean))});
  if(!response.ok)throw visionError('VISION_SELECTION_UNAVAILABLE');
  return response.json();
 },
 negotiate:async(task,model,signal)=>{
  const response=await fetch(modelBase+'/vision-capability',{method:'POST',redirect:'error',
   headers:{Authorization:'Bearer '+task.lease,'Content-Type':'application/json'},body:JSON.stringify({model}),
   signal:AbortSignal.any([AbortSignal.timeout(65000),signal].filter(Boolean))});
  const value=await response.json().catch(()=>({}));
  if(!response.ok)throw visionError(value.detail);
  return value;
 },
 applyCatalog:value=>applyCatalog(value,{preserveDefault:true}),validate:validateTask
});
async function revokeTask(task) {
 if(!authority.has(task))return;
 if(task.cancelling)return task.cancelling;
 authority.revoke(task);tickets.revoke(task);
 projectRouter?.revoke(task);searchHost.revokeTask(task);
 // Fence admission before waiting for remote project cancellation. Otherwise
 // a native-idle sample could release this generation and cancel its successor.
 const releaseRevoke=authority.hold(task,'dispatches');
 task.cancelling=(async()=>{
  toolkit?.cancelSession(task.sessionId,{clientKey:task.lease});
  await projectBridge?.revokeTask(task);
  await officeBridge.stop(task);webBridge.stop(task);
  for(const [connection,owner] of modelConnections)if(owner===task)connection.destroy();
  // An authenticated cancellation can arrive while image admission is still
  // awaiting its first provider capability response, before native prompt
  // admission claims this generation. Claim only the fenced metadata identity;
  // this neither dispatches a prompt nor releases an older pending operation.
  await taskState({claim:true,session_id:task.sessionId,generation:task.generation});
  await taskState({cancel:true,session_id:task.sessionId,generation:task.generation});
 })().finally(()=>{releaseRevoke();task.cancelling=null;});
 return task.cancelling;
}

projectBridge=createProjectBridge({
 authorize:context=>validateTask({lease:context.clientKey}),current:session=>authority.get(session),
 selection:(session,context)=>toolkit.snapshot(session,context),enter:options=>idleControl.enter(options),
 remote:async(operation,body,context)=>{
  if(!['catalog','invoke','status','cancel','results','attachments','current-step','drawing-pages','form-ticket','secure-form','human-proof'].includes(operation))throw Error('project route invalid');
  const response=await fetch(new URL('/internal/personal-cloud/projects/'+operation,modelBase),{
   method:'POST',redirect:'error',headers:{Authorization:'Bearer '+context.clientKey,'Content-Type':'application/json'},body:JSON.stringify(body),
   signal:AbortSignal.any([AbortSignal.timeout(20000),context.signal].filter(Boolean))});
  const result=await boundedResponse(response);
  if(!response.ok)throw Object.assign(Error('PROJECT_GATEWAY_UNAVAILABLE'),{status:response.status});return result;
 },
 native:async(body,signal)=>{
  const response=await fetch(base+'/api/megmeet-cloud/project-files',{method:'POST',redirect:'error',
   headers:{Cookie:cookie,Origin:base,'Content-Type':'application/json'},body:JSON.stringify(body),
   signal:AbortSignal.any([AbortSignal.timeout(60000),signal].filter(Boolean))});
  const value=await boundedResponse(response,45*1024*1024);
  if(!response.ok)throw Object.assign(Error('PROJECT_NATIVE_FILES_UNAVAILABLE'),{status:response.status});return value;
 }
});
toolkit=createToolkit({projects:projectBridge,

 authorize:async context=>{if(!/^mcl_[A-Za-z0-9_-]{43}$/.test(context?.clientKey||''))throw Error('cloud client required');await validateTask({lease:context.clientKey});},
 remote:async(path,body,context)=>{
  if(!['/catalog','/invoke','/confirm','/stop'].includes(path))throw Error('connector route invalid');
  const response=await fetch(new URL('/internal/personal-cloud/connectors'+path,modelBase),{
   method:body===undefined?'GET':'POST',redirect:'error',
   headers:{Authorization:'Bearer '+context.clientKey,'Content-Type':'application/json',
     ...(path==='/confirm'?{'X-Megmeet-User-Confirm':context.confirmationProof||''}:{})},
   ...(body===undefined?{}:{body:JSON.stringify(body)}),
   signal:AbortSignal.any([AbortSignal.timeout(120000),context.signal].filter(Boolean))});
  if(!response.ok)throw Object.assign(Error('云端连接器暂时不可用。'),{status:response.status});
  return response.json();
 }
});
async function readJson(req,max=512*1024){const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>max)throw Error('request too large');chunks.push(chunk)}return chunks.length?JSON.parse(Buffer.concat(chunks).toString('utf8')):{};}
const officeBridge=createCloudOfficeBridge({token:routeToken,
 base:new URL('/internal/personal-cloud/office',modelBase).href,
 current:session=>authority.get(session),validate:validateTask,consume:(token,purpose)=>tickets.consume(token,'tool',purpose)});
const webBridge=createCloudWebBridge({token:routeToken,current:session=>authority.get(session),validate:validateTask,toolkit,consume:(token,purpose)=>tickets.consume(token,'tool',purpose)});
const searchHost=createSearchHost({consumeToolTicket:(token,purpose)=>tickets.consume(token,'tool',purpose),resolveScope:scope=>authority.resolveScope(scope),validateTask,routeToken,modelBase});
projectRouter=createProjectRouter({
 snapshot:(session,context)=>toolkit.snapshot(session,context),current:session=>authority.get(session),
 currentStep:(session,context)=>projectBridge.read('current-step',session,context),
 execute:(body,context)=>toolkit.execute(body,context),
 results:(session,context)=>projectBridge.read('results',session,context),
 extractDrawing:async(prepared,context)=>{
  const pages=await projectBridge.read('drawing-pages',context.task.sessionId,{clientKey:context.task.lease,signal:context.signal},
    {attachment_ids:prepared.drawing?.pages?.map(page=>page.attachment_id)});
  return extractDrawingFields({...pages,text_pages:prepared.drawing?.text_pages},{...context,endpoint:modelBase+'/chat/completions',validate:validateTask});
 }
});
const modelServer=http.createServer(async(req,res)=>{
 let admission,modelHold,modelBody=null;
 try {
  modelHold=holdRequest(req,res,true);
  if(req.url.startsWith('/__megmeet/scope/')){
   if(req.headers.origin||req.headers['sec-fetch-site']||req.headers.authorization!=='Bearer '+routeToken||req.method!=='POST')throw Error('private scope authorization required');
   const body=await readJson(req,4096);let value;
   if(req.url==='/__megmeet/scope/issue'&&Object.keys(body).sort().join(',')==='kind,scope')value={token:tickets.issue(body.scope,body.kind)};
   else if(req.url==='/__megmeet/scope/release'&&Object.keys(body).join(',')==='token'&&/^(?:mmt|mtt)_[A-Za-z0-9_-]{43}$/.test(body.token||'')){tickets.release(body.token);value={released:true};}
   else throw Error('private scope route invalid');
   res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));return;
  }
  if(req.url.startsWith('/__megmeet/search/')){await searchHost.handle(req,res);return;}
  if(req.url.startsWith('/__megmeet/web')){await webBridge.handle(req,res);return;}
  if(req.url.startsWith('/__megmeet/office')){await officeBridge.handle(req,res);return;}
  if(req.method==='POST'&&req.url==='/__megmeet/tool-execute'){
   const grant=tickets.consume(String(req.headers.authorization||'').replace(/^Bearer /,''),'tool','connector');
   res.once('close',grant.finish);req.once('aborted',grant.finish);
   const body=await readJson(req);const task=grant.task;
   if(!task||task.revoked||body.session_id!==task.sessionId)throw Error('cloud task authorization required');
   const stopped=new AbortController();const disconnect=()=>stopped.abort();
   req.once('aborted',disconnect);res.once('close',disconnect);
   let value;try{value=await toolkit.execute(body,{clientKey:task.lease,signal:AbortSignal.any([stopped.signal,grant.signal])});}
   finally{req.removeListener('aborted',disconnect);res.removeListener('close',disconnect);}
   res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));return;
  }
  // The company bridge uses inline images. An explicit Files API absence lets
  // the native adapter select its supported base64 fallback without auth errors.
  if(req.headers.authorization==='Bearer '+routeToken&&req.url.split('?')[0]==='/v1/files'){
   res.writeHead(404,{'Content-Type':'application/json'});res.end(JSON.stringify({error:{message:'Files API unavailable; use inline image content.',code:'not_found'}}));return;
  }
  if(req.method!=='POST'||req.url!=='/v1/chat/completions'){
   res.writeHead(403);res.end();return;
  }
  admission=tickets.consume(String(req.headers.authorization||'').replace(/^Bearer /,''),'model');
  admission.signal.addEventListener('abort',()=>res.destroy(),{once:true});
  await validateTask(admission.task);
  if(admission.task.revoked||req.aborted||res.destroyed)throw Error('cloud task revoked or request closed');
  modelBody=await readJson(req,32*1024*1024);
  if(!isCompanyChatModel(modelBody?.model)){
   admission.end();res.writeHead(403,{'Content-Type':'application/json','Cache-Control':'no-store'});
   res.end(JSON.stringify({error:{code:'company_chat_model_not_allowed',message:'The company chat model is DeepSeek V4.1 Flash.'}}));return;
  }
  if(projectRouter.has(admission.task)){
   const disconnect=()=>{if(!res.writableEnded)void revokeTask(admission.task).catch(()=>{});};
   res.once('close',disconnect);
   try{
    const reply=await projectRouter.respond(admission.task,modelBody);
    if(reply!==null){
     await validateTask(admission.task);
     if(admission.task.revoked||res.destroyed)throw Error('cloud project task revoked');
     if(typeof reply==='string')writeProjectCompletion(res,modelBody,reply);
     else writeProjectCompletion(res,modelBody,reply.text,reply.usage);
     admission.end();return;
    }
   }finally{res.removeListener('close',disconnect);}
  }
  const up=http.request(modelBase+'/chat/completions',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+admission.task.lease}},out=>{
   res.writeHead(out.statusCode,{'Content-Type':out.headers['content-type']||'application/json','Cache-Control':'no-store'});
   out.pipe(res);
  });
  modelConnections.set(up,admission.task);
  const finish=()=>{modelConnections.delete(up);admission.end();};
  up.once('close',finish);up.on('error',()=>{if(!res.headersSent)res.writeHead(502);res.end();});
  res.once('close',()=>up.destroy());req.once('aborted',()=>up.destroy());
  up.setTimeout(130000,()=>up.destroy());if(modelBody)up.end(JSON.stringify(modelBody));else req.pipe(up);
 }catch(error){
  if(error?.code==='PROJECT_ROUTE_SELECTION_CHANGED'&&admission&&!admission.task.revoked&&authority.has(admission.task)&&!res.destroyed){
   writeProjectCompletion(res,modelBody||{stream:true},'技能选择已变更，本次任务已停止。请按当前选择重新发送。');
   admission.end();return;
  }
  admission?.end();
  if(admission&&error.loginExpired)await revokeTask(admission.task).catch(()=>{});
  const expired=Boolean(error.loginExpired||admission?.task.revoked);
  if(!res.headersSent)res.writeHead(expired?401:503,{'Content-Type':'application/json'});
  res.end(JSON.stringify({error:{code:expired?'cloud_login_expired':'cloud_request_unavailable',message:expired?
   '发起此云端任务的登录已失效，请重新进入工作区。':'本次云端请求未完成，请稍后重试。'}}));
 }finally{modelHold?.handlerDone();}
});
await new Promise((resolve,reject)=>{modelServer.once('error',reject);modelServer.listen(3086,'127.0.0.1',resolve)});
let checking=false;
const taskTimer=setInterval(async()=>{
 if(checking)return;checking=true;
 try{
  await nativeCancelFence?.recover();
  await nativeRequests.recover();
  tickets.expire();
  for(const task of authority.list()){try{await validateTask(task);}catch(error){if(error.loginExpired)await revokeTask(task);}}
  authority.reconcile(await stableTaskState());
 }catch{/* Uncertain state retains the lock; it never authorizes another client. */}
 finally{checking=false;}
},5000);
taskTimer.unref();
child.once('exit',()=>{clearInterval(taskTimer);modelServer.close();officeBridge.stop();webBridge.stop();void toolkit.close()});
async function updateCatalog(req,res){
 res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');
 if(req.method!=='POST'){res.writeHead(405);res.end(JSON.stringify({detail:'method_not_allowed'}));return;}
 let body;
 try{
  const chunks=[];let size=0;
  for await(const chunk of req){size+=chunk.length;if(size>600*1024){res.writeHead(413);res.end(JSON.stringify({detail:'catalog_too_large'}));return;}chunks.push(chunk);}
  body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(!body||Object.keys(body).length!==1||!Object.hasOwn(body,'catalog'))throw new Error('invalid envelope');
  normalizeCatalog(body.catalog);
 }catch{res.writeHead(400);res.end(JSON.stringify({detail:'catalog_invalid'}));return;}
 try{
  await applyCatalog(body.catalog);
  res.end(JSON.stringify({status:'updated',native_reload_required:false}));
 }catch{res.writeHead(503);res.end(JSON.stringify({detail:'native_catalog_update_failed'}));}
}
let bootstrapPhase='WAITING_NATIVE';
try {
 for(let n=0;n<120;n++){
  const token=buffer.match(/http:\/\/127\.0\.0\.1:3085\/\?token=([A-Za-z0-9_-]+)/)?.[1];
  if(token){const r=await fetch(base+'/?token='+token,{redirect:'manual'});cookie=r.headers.getSetCookie().map(s=>s.split(';')[0]).join('; ');if(cookie)break;}
  await delay(500);
 }
 buffer='';if(!cookie)throw new Error('cloud native bootstrap timeout');
 // Persisted settings from an older lease must never override today's catalog.
 bootstrapPhase='MODEL_CATALOG';await applyCatalog(catalog);
 bootstrapPhase='WORKSPACE_CREATE';
 nativeId=(await rpc('workspace/create',{path:'/workspace'})).workspace.workspaceId;
 bootstrapPhase='WORKSPACE_RENAME';await rpc('workspace/rename',{workspaceId:nativeId,title:'云端工作区'});
 bootstrapPhase='PUBLIC_PROXY';
 const allowed=req=>{const got=req.headers.authorization||'';const expected='Bearer '+routeToken;return /^Bearer [A-Za-z0-9_-]{43}$/.test(got)&&timingSafeEqual(Buffer.from(got,'ascii'),Buffer.from(expected,'ascii'))};
 const headers=req=>{const h={...req.headers,host:'127.0.0.1:3085',origin:base,cookie};delete h.authorization;delete h['x-megmeet-client-lease'];delete h['x-megmeet-user-confirm'];delete h['x-megmeet-project-user'];delete h['x-forwarded-host'];delete h['x-forwarded-proto'];return h};
 const safePath=req=>req.url.startsWith('/')&&!req.url.startsWith('//')&&!/[\\\r\n]/.test(req.url);
 server=http.createServer(async(req,res)=>{
  let requestHold;
  try{
  if(!allowed(req)||!safePath(req)){res.writeHead(401);res.end();return;}
  if(req.url==='/__cloud/ready'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({status:'ready',native_workspace_id:nativeId,path:'/workspace'}));return;}
  if(['/__cloud/activity','/__cloud/idle-release','/__cloud/idle-resume'].includes(req.url)){
   if(req.method!==(req.url==='/__cloud/activity'?'GET':'POST')){res.writeHead(405);res.end();return;}
   try{
    const value=req.url==='/__cloud/activity'?await idleControl.busy():req.url==='/__cloud/idle-release'?await idleControl.drain():idleControl.resume();
    res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));
   }catch{res.writeHead(503,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:'cloud_activity_unavailable'}));}
   return;
  }
  if(req.url==='/__cloud/search/context'){
   if(req.method!=='POST'){res.writeHead(405);res.end();return;}
   try{requestHold=holdRequest(req,res);const body=await readJson(req,8192);
    const value=await searchHost.callback(body,{clientKey:req.headers['x-megmeet-client-lease']});
    res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));
   }catch{res.writeHead(403,{'Content-Type':'application/json'});res.end(JSON.stringify({code:'SEARCH_SCOPE_REVOKED'}));}return;
  }
  if(req.url==='/__cloud/projects/card-print'){
   if(req.method!=='POST'){res.writeHead(405);res.end();return;}
   try{
    requestHold=holdRequest(req,res);
    await withNodeTransfer(projectFileTransfers,req,res,async signal=>{
     const encoded=req.headers['x-megmeet-card-metadata'];
     if(typeof encoded!=='string'||encoded.length>16384||!/^[A-Za-z0-9_-]+$/.test(encoded))throw Object.assign(Error('PROJECT_CARD_PRINT_METADATA_INVALID'),{status:400});
     const metadataBytes=Buffer.from(encoded,'base64url');
     if(metadataBytes.toString('base64url')!==encoded)throw Object.assign(Error('PROJECT_CARD_PRINT_METADATA_INVALID'),{status:400});
     let metadata;try{metadata=JSON.parse(metadataBytes.toString('utf8'));}catch{throw Object.assign(Error('PROJECT_CARD_PRINT_METADATA_INVALID'),{status:400});}
     const delivery=await projectBridge.cardPrint(metadata,{clientKey:req.headers['x-megmeet-client-lease'],signal},async(cleaned,permitSignal)=>{
      const saving=cleaned.operation==='save-card-print';
      if(saving&&(!Number.isSafeInteger(cleaned.bytes)||cleaned.bytes<1||cleaned.bytes>128*1024*1024||req.headers['content-length']!==String(cleaned.bytes)))throw Object.assign(Error('PROJECT_CARD_PRINT_LENGTH_INVALID'),{status:413});
      if(!saving&&(req.headers['transfer-encoding']||(req.headers['content-length']&&req.headers['content-length']!=='0')))throw Object.assign(Error('PROJECT_CARD_PRINT_BODY_INVALID'),{status:400});
      const nativeResponse=await fetch(base+'/api/megmeet-cloud/card-print',{method:'POST',redirect:'error',
       headers:{Cookie:cookie,Origin:base,'Content-Type':'application/octet-stream','Content-Length':saving?String(cleaned.bytes):'0',
        'X-Megmeet-Card-Metadata':Buffer.from(JSON.stringify(cleaned)).toString('base64url'),'Accept-Encoding':'identity'},
       ...(saving?{body:Readable.toWeb(req),duplex:'half'}:{body:null}),signal:AbortSignal.any([permitSignal,AbortSignal.timeout(60000)])});
      if(!nativeResponse.ok){await nativeResponse.body?.cancel();throw Object.assign(Error('PROJECT_NATIVE_FILES_UNAVAILABLE'),{status:nativeResponse.status});}
      const contentType=String(nativeResponse.headers.get('content-type')||'');
      if(saving){
       const result=await boundedResponse(nativeResponse,16384);
       return {json:result};
      }
      if(!contentType.startsWith('application/octet-stream')||nativeResponse.headers.get('content-length')!==String(cleaned.ref?.bytes)){
       await nativeResponse.body?.cancel();throw Object.assign(Error('PROJECT_NATIVE_FILES_UNAVAILABLE'),{status:502});
      }
      res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Length':String(cleaned.ref.bytes),'Cache-Control':'no-store'});
      const reader=nativeResponse.body.getReader();let received=0,pending=null;
      try{
       for(;;){permitSignal.throwIfAborted();const {done,value}=await reader.read();if(done)break;
        received+=value.byteLength;if(received>cleaned.ref.bytes)throw Error('PROJECT_CARD_PRINT_LENGTH_INVALID');
        if(pending&&!res.write(pending))await once(res,'drain',{signal:permitSignal});pending=value;}
       if(received!==cleaned.ref.bytes)throw Error('PROJECT_CARD_PRINT_LENGTH_INVALID');
      }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
      return {tail:pending};
     });
     // Hold the final bytes until both native EOF integrity and the second
     // project-permit check pass; an error never delivers a complete file.
     if(delivery.json){res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(delivery.json));}
     else res.end(delivery.tail);
    });
   }catch(error){if(res.headersSent||res.destroyed){res.destroy();}else{res.writeHead([400,401,403,409,413,429].includes(error.status)?error.status:503,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:'PROJECT_CALLBACK_DENIED'}));}}
   return;
  }
  if(['/__cloud/projects/context','/__cloud/projects/files','/__cloud/projects/read-context'].includes(req.url)){
   if(req.method!=='POST'){res.writeHead(405);res.end();return;}
   try{
    requestHold=holdRequest(req,res);
    const transfer=async()=>{
    const body=await readJson(req,req.url.endsWith('/files')?45*1024*1024:32768);
    const value=await projectBridge.callback(req.url.endsWith('/files')?'files':req.url.endsWith('/read-context')?'read-context':'context',body,{clientKey:req.headers['x-megmeet-client-lease']});
    res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));
    };
    if(req.url.endsWith('/files'))await withNodeTransfer(projectFileTransfers,req,res,transfer);else await transfer();
   }catch(error){res.writeHead([400,401,403,409,413,429].includes(error.status)?error.status:503,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:'PROJECT_CALLBACK_DENIED'}));}
   return;
  }
  if(req.url.startsWith('/__cloud/')){
   if(req.url!=='/__cloud/catalog'){res.writeHead(404);res.end();return;}
   try{requestHold=holdRequest(req,res,true);}catch{res.writeHead(409);res.end();return;}
   await updateCatalog(req,res).catch(()=>res.destroy());return;
  }
  // Cover the entire upload/body-read/forwarding lifetime, not just prompt
  // dispatch. A read-only background request is not human activity.
  if(!['GET','HEAD'].includes(req.method)){
   try{requestHold=holdRequest(req,res);}catch{res.writeHead(409,{'Content-Type':'application/json'});res.end(JSON.stringify({detail:'personal_cloud_idle_release'}));return;}
  }
  const path=new URL(req.url,base).pathname;
  if(path.startsWith('/__megmeet/tools/')){
   try{
    requestHold?.work();
    const action=path.slice('/__megmeet/tools/'.length);
    if(!['catalog','state','selection','confirm','stop','invoke','cancel-job','form-ticket','secure-form'].includes(action)||!(['catalog','state'].includes(action)?req.method==='GET':req.method==='POST'))throw Error('tool control invalid');
    let rawBody=Buffer.alloc(0),body;
    if(req.method==='GET'){
     const params=new URL(req.url,base).searchParams;
     body={session_id:params.get('session_id'),...readProjectHistoryQuery(params,action)};
    }
    else{const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>512*1024)throw Error('request too large');chunks.push(chunk);}rawBody=Buffer.concat(chunks);body=JSON.parse(rawBody.toString('utf8')||'{}');}
    if(req.aborted||res.destroyed)throw Error('cloud request closed');
    const result=await toolkit.control(action,body,{clientKey:req.headers['x-megmeet-client-lease'],confirmationProof:req.headers['x-megmeet-user-confirm'],humanProof:req.headers['x-megmeet-project-user'],bodySha256:createHash('sha256').update(rawBody).digest('hex'),controlPath:path});
    res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(result));
   }catch(error){res.writeHead([400,401,403,409,429,503].includes(error.status)?error.status:503,{'Content-Type':'application/json'});res.end(JSON.stringify({detail:'云端插件暂不可用，请检查登录或重试。'}));}
   return;
  }
  if(path.startsWith('/api/megmeet-cloud/')||path.startsWith('/__megmeet/office')||path.startsWith('/__megmeet/web')||path.startsWith('/__megmeet/scope/')||path.startsWith('/__megmeet/search/')||path==='/__megmeet/tool-execute'){res.writeHead(404);res.end();return;}
  let bytes=null,dispatch=false,dispatchTask,envelope,visionPreparation;
  try{
   if(req.method==='POST'&&/^\/api\/[^/]+\/[^/]+$/.test(path)&&String(req.headers['content-type']||'').split(';')[0]==='application/json'){
    const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>32*1024*1024)throw Error('request too large');chunks.push(chunk)}
    bytes=Buffer.concat(chunks);envelope=JSON.parse(bytes.toString('utf8'));
    const command=validateNativeRpc(path,req.method,envelope,nativeId);
    if(!command.readOnly)requestHold?.work();
    const clientLease=req.headers['x-megmeet-client-lease'];
    const state=await stableTaskState();authority.reconcile(state);
    if(req.aborted||res.destroyed)throw Error('cloud request closed');
    if(command.endpoint==='session/prompt'){
     nativeCancelFence.assertAdmission(command.request?.sessionId);
     nativeRequests.assertAdmission(command.request?.sessionId);
     const task=authority.claim(clientLease,command.request?.sessionId,state);dispatchTask=task;dispatch=true;
     if(hasImage(command.request)){
      const cancelled=new AbortController();const abort=()=>cancelled.abort();
      visionPreparation={task,controller:cancelled};pendingVision.add(visionPreparation);
      req.once('aborted',abort);res.once('close',abort);
      try{
       await validateTask(task);cancelled.signal.throwIfAborted();
       await prepareVision(task,command.request,cancelled.signal);
      }
      finally{req.removeListener('aborted',abort);res.removeListener('close',abort);}
      if(req.aborted||res.destroyed||task.revoked)throw Error('cloud request closed');
      cancelled.signal.throwIfAborted();
     }else await validateTask(task);
     if(task.revoked||!authority.has(task)||req.aborted||res.destroyed)throw Error('cloud request closed');
     projectRouter.register(task,command.request);
     await taskState({claim:true,session_id:task.sessionId,generation:task.generation});
     if(task.revoked||!authority.has(task)||req.aborted||res.destroyed)throw Error('cloud request closed');
     if(visionPreparation)pendingVision.delete(visionPreparation);
     const identity={dispatchId:randomUUID(),sessionId:task.sessionId,generation:task.generation};
     const completion=nativeRequests.run(identity,async stamp=>taskState({native_prompt:true,
       dispatch_id:stamp.dispatchId,generation:stamp.generation,request:command.request}),()=>authority.finishDispatch(task));
     dispatch=false; // Exact native receipt now owns the admission hold.
     const receipt=await completion;
     if(!res.destroyed){res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});
       res.end(JSON.stringify({type:'server-response',rpcId:envelope.rpcId,result:receipt.result}));}
     return;
    }else if(command.endpoint==='session/create'){
     // Creating B must remain possible while a separately leased A runs.
     // Existing/adopted identities remain protected by their own root fences;
     // workspace validation above and the current login are still required.
     await validateTask({lease:clientLease});
     if(command.request?.sessionId){
      authority.assertMutation(clientLease,command.request.sessionId);
      nativeCancelFence.assertAdmission(command.request.sessionId);
      nativeRequests.assertAdmission(command.request.sessionId);
     }
    }else if(!command.readOnly){
     authority.assertMutation(clientLease,command.request?.sessionId);
     if(command.endpoint==='session/cancel'){
      const session=command.request?.sessionId;
      if(!/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(session||''))throw Error('cloud cancel session invalid');
      await validateTask({lease:clientLease});
      authority.assertMutation(clientLease,command.request?.sessionId);
      const cancelId=randomUUID(),cancelGeneration=authority.get(session)?.generation||authority.generations.get(session)||0;
      const reply=await nativeCancelFence.run(async()=>{
       const task=authority.get(session);if(task&&task.lease===clientLease)await revokeTask(task);
       await projectBridge.revokeScope(session,{clientKey:clientLease});
       for(const preparation of pendingVision)if(preparation.task.lease===clientLease&&preparation.task.sessionId===session)
        preparation.controller.abort(Object.assign(new Error('图片发送已取消。'),{nativeCode:'session/image-capability-failed',reason:'IMAGE_CANCELLED'}));
       // Do not attach renderer disconnect to this private request. A closed
       // browser connection is not proof that native cancellation has finished.
       const value=await taskState({native_cancel:true,cancel_id:cancelId,session_id:session,generation:cancelGeneration});
       if(value?.cancel_id!==cancelId||typeof value.result?.ok!=='boolean')throw Error('native cancel acknowledgement invalid');
       return {status:200,value:{type:'server-response',rpcId:envelope.rpcId,result:value.result}};
      },{recoverable:true,sessionId:session});
      if(!res.destroyed){res.writeHead(reply.status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(reply.value));}
      return;
     }
     nativeCancelFence.assertAdmission(command.request?.sessionId);
    }
   }else if(!['GET','HEAD'].includes(req.method)){requestHold?.work();authority.assertMutation(req.headers['x-megmeet-client-lease']);}
   if(req.aborted||res.destroyed)throw Error('cloud request closed');
   visionPreparation?.controller.signal.throwIfAborted();
   if(visionPreparation)pendingVision.delete(visionPreparation);
   const up=http.request(base+req.url,{method:req.method,headers:headers(req)},out=>{
    const h={...out.headers};delete h['set-cookie'];res.writeHead(out.statusCode,h);out.pipe(res);
    out.once('end',()=>{if(dispatch){dispatch=false;authority.finishDispatch(dispatchTask)}});
   });
   up.on('error',()=>{if(dispatch){dispatch=false;authority.finishDispatch(dispatchTask)}if(!res.headersSent)res.writeHead(502);res.end()});
   req.once('aborted',()=>up.destroy());res.once('close',()=>up.destroy());
   if(bytes)up.end(bytes);else req.pipe(up);
  }catch(error){
   if(visionPreparation)pendingVision.delete(visionPreparation);
   if(dispatch)authority.finishDispatch(dispatchTask);
   res.writeHead(envelope?.rpcId?200:409,{'Content-Type':'application/json','Cache-Control':'no-store'});
   res.end(JSON.stringify(envelope?.rpcId?{type:'server-response',rpcId:envelope.rpcId,result:{ok:false,error:{code:error.nativeCode||'session/agent-busy',message:error.message,details:error.reason?{reason:error.reason}:{}}}}:{detail:'personal_cloud_task_busy'}));
  }
  }finally{requestHold?.handlerDone();}
 });
 server.on('upgrade',(req,socket,head)=>{
  if(!allowed(req)||req.url.split('?')[0]!=='/api/remote.mux'){socket.end('HTTP/1.1 401 Unauthorized\r\n\r\n');return;}
  if(idleControl.draining){socket.end('HTTP/1.1 409 Conflict\r\n\r\n');return;}
  const finishUpgrade=idleControl.enter();
  nativeSockets.add(socket);socket.once('close',()=>nativeSockets.delete(socket));
  const up=net.connect(3085,'127.0.0.1',()=>{up.write(req.method+' '+req.url+' HTTP/1.1\r\n'+Object.entries(headers(req)).map(([k,v])=>k+': '+v).join('\r\n')+'\r\n\r\n');if(head.length)up.write(head);socket.pipe(up).pipe(socket);finishUpgrade()});
  socket.once('close',finishUpgrade);up.once('error',finishUpgrade);
  up.on('error',()=>socket.destroy());socket.on('error',()=>up.destroy());socket.on('close',()=>up.destroy());up.on('close',()=>socket.destroy());
 });
 server.listen(3084,'0.0.0.0',()=>console.log('PERSONAL_NATIVE_HOST_READY'));
} catch {console.error('PERSONAL_NATIVE_HOST_START_FAILED phase='+bootstrapPhase);child.kill('SIGTERM');process.exitCode=1;}
