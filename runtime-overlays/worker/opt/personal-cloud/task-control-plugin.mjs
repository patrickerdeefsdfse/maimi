import {createNativeModelScopes} from './native-model-scope.mjs';
import {bindNativeTaskContext} from './native-task-context.mjs';
import {Peer} from '/opt/dsh/node_modules/@deepseek-ai/dsh-client-connection/lib/index.js';
export const inject=['agents','connection','typertGateway','llm','credentials'];
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const SID=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export function apply(ctx,config){
 if(config?.endpoint!=='http://127.0.0.1:3086/__megmeet/scope'||!/^[A-Za-z0-9_-]{43}$/.test(config.token||''))throw Error('CLOUD_NATIVE_CONTROL_REQUIRED');
 // DSH 0.2.1 requires a live Peer for in-process RPC. This peer belongs only
 // to the authenticated wrapper's private task route; it is never supplied by
 // browser/model JSON. Both proxies must continue denying /api/megmeet-cloud/.
 // Hanging it under this plugin context also revokes it when the plugin unloads.
 const controlPeer=new Peer(ctx,{principal:{kind:'owner'},interactions:'none'});
 const control=async(action,body)=>{
  const r=await fetch(config.endpoint+'/'+action,{method:'POST',redirect:'error',signal:AbortSignal.timeout(5000),
   headers:{Authorization:'Bearer '+config.token,'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok)throw Error('CLOUD_NATIVE_SCOPE_REJECTED');return r.json();
 };
 const scopes=createNativeModelScopes({agents:ctx.agents,
  issueCredential:async scope=>(await control('issue',{kind:'model',scope})).token,
  releaseCredential:token=>control('release',{token})});
 bindNativeTaskContext(scopes,control);
 ctx.on('llm/stream',(options,next)=>scopes.intercept(options,next),{global:true,prepend:true});
 const generations=new Map(),prompts=new Map(),cancels=new Map(),cancelled=new Set();
 const agentBusy=a=>a.status!=='idle'||a.inbox.nextTurn.length>0||a.inbox.nextStep.length>0
  ||ctx.get('jobs')?.list(a).some(j=>['running','stopping'].includes(j.status));
 const belongs=(agent,root)=>{
  const all=ctx.agents.list(),seen=new Set();let a=agent;
  while(a&&!seen.has(a)){if(a.id===root)return true;seen.add(a);
   const parents=all.filter(parent=>ctx.agents.isOwnedBy(a.id,parent));
   if(parents.length!==1)return false;a=parents[0];
  }return false;
 };
 function rootBusy(root){return ctx.agents.list().some(a=>belongs(a,root)&&agentBusy(a))
  ||[...prompts.values(),...cancels.values()].some(e=>e.session_id===root&&!e.done)
  ||scopes.metadata().some(e=>e.root===root&&e.streams>0);}
 function snapshot(){
  const roots=new Set([...ctx.agents.roots().map(a=>String(a.id)),...generations.keys()]);
  const activeRoots=[...roots].filter(rootBusy);
  return {protocol:2,busy:activeRoots.length>0||ctx.agents.list().some(agentBusy),activeRoots};
 }
 const receiptError=e=>({ok:false,error:{code:typeof e?.code==='string'&&/^[a-z][a-z0-9/-]{1,80}$/.test(e.code)?e.code:'gateway/internal',message:'本次消息未被接受，请稍后重试。',details:{}}});
 async function cancelRoot(root,generation){
  cancelled.add(generation+':'+root);scopes.revoke(root,generation);
  await Promise.all([...prompts.values()].filter(e=>e.session_id===root&&e.generation===generation).map(e=>e.promise));
  for(const a of ctx.agents.list())if(belongs(a,root)){
   for(const item of [...a.inbox.nextTurn,...a.inbox.nextStep])a.inbox.remove(item.id);
   a.cancel({kind:'user'});
  }
 }
 ctx.effect(()=>ctx.connection.fetch.register({path:'/api/megmeet-cloud/task-state',methods:['GET','POST'],requestBody:'buffered',fetch:async request=>{
  if(request.method==='GET')return Response.json(snapshot(),{headers:{'Cache-Control':'no-store'}});
  let b;try{b=await request.json();}catch{return new Response('',{status:400});}
  const root=b?.session_id||b?.request?.sessionId,gen=b?.generation;
  if(!SID.test(root||'')||!Number.isSafeInteger(gen)||gen<0)return new Response('',{status:400});
  try{
   if(b.claim===true){
    if(gen<1)return new Response('',{status:400});
    const old=generations.get(root)||0;
    if(gen<old||gen>old&&rootBusy(root))return new Response('',{status:409});
    if(gen>old){
     if(old)scopes.revoke(root,old);scopes.claim(root,gen);generations.set(root,gen);
     for(const map of [prompts,cancels])for(const [id,e]of map)if(e.session_id===root&&e.done)map.delete(id);
     for(const key of cancelled)if(key.endsWith(':'+root))cancelled.delete(key);
    }
   }else if(b.native_prompt===true){
    if(!UUID.test(b.dispatch_id||'')||!UUID.test(b.request?.requestId||''))return new Response('',{status:400});
    let e=prompts.get(b.dispatch_id);
    if(e){if(e.generation!==gen||e.session_id!==root||e.request_id!==b.request.requestId)return new Response('',{status:409});}
    else{
     if(gen!==generations.get(root)||prompts.size>=512)return new Response('',{status:409});
     e={generation:gen,session_id:root,request_id:b.request.requestId,done:false};prompts.set(b.dispatch_id,e);
     e.promise=(async()=>{
      if(cancelled.has(gen+':'+root))return {ok:false,error:{code:'gateway/cancelled',message:'该次发送已停止。',details:{}}};
      try{return {ok:true,value:await scopes.runPrompt(root,gen,b.request,()=>ctx.typertGateway.invoke({peer:controlPeer,namespace:'session',method:'prompt',args:{request:b.request}}))};}
      catch(error){return receiptError(error);}
     })().then(result=>{e.done=true;return result;});
    }
    return Response.json({dispatch_id:b.dispatch_id,session_id:root,generation:gen,state:'done',result:await e.promise});
   }else if(b.native_cancel===true){
    if(!UUID.test(b.cancel_id||''))return new Response('',{status:400});
    let e=cancels.get(b.cancel_id);
    if(e){if(e.generation!==gen||e.session_id!==root)return new Response('',{status:409});}
    else{
     if(gen!==(generations.get(root)||0)||cancels.size>=1024)return new Response('',{status:409});
     e={generation:gen,session_id:root,done:false};cancels.set(b.cancel_id,e);
     e.promise=(async()=>{
      if(gen)await cancelRoot(root,gen);
      try{return {ok:true,value:await ctx.typertGateway.invoke({peer:controlPeer,namespace:'session',method:'cancel',args:{request:{sessionId:root}}})};}
      catch(error){return receiptError(error);}
     })().then(result=>{e.done=true;return result;});
    }
    return Response.json({cancel_id:b.cancel_id,result:await e.promise});
   }else if(b.cancel===true&&gen===generations.get(root)){await cancelRoot(root,gen);}
   else return new Response('',{status:409});
   return Response.json(snapshot());
  }catch{return new Response('',{status:409});}
 }}));
}
