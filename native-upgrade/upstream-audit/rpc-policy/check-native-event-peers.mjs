// Actual latest Gateway wireStream + actual Peer scopes. Only the external
// connection carrier is synthetic; no native event matching code is replaced.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {writeFile} from 'node:fs/promises';
import path from 'node:path';
const here=path.dirname(fileURLToPath(import.meta.url));
const runtime=path.resolve(process.env.DSH_R65_RUNTIME||path.resolve(here,'../../upstream/runtime-win'));
const require=createRequire(path.join(runtime,'package.json'));
const pkg=name=>import(pathToFileURL(require.resolve(name)).href);
const [{Context,Service},{Peer},{default:Gateway},{default:Registry},{default:Access}]=await Promise.all([
 '@deepseek-ai/cordis','@deepseek-ai/dsh-client-connection','@deepseek-ai/dsh-api-gateway','@deepseek-ai/dsh-typert-registry','@deepseek-ai/dsh-api-remote-access'].map(pkg));
const ctx=new Context(),fibers=[],peers=[],queue=[];let dispatch,wake,unregister,iterator,control;
const use=async plugin=>{const f=ctx.plugin(plugin);fibers.push(f);await f;};
class Carrier extends Service{
 constructor(c){super(c,'connection');this.rpc={intercept:(prefix,claims,handler)=>{assert.equal(prefix,'/api');dispatch=handler;return ()=>{};}};}
}
const result={schema:'r65-native-remote-event-peer-test-v1',company_requests:0,official_packages_modified:false};
try{
 await use(Registry);await use(Access);await use(Carrier);await use(Gateway);
 assert.equal(typeof dispatch,'function');
 unregister=ctx.typertGateway.registerRemoteEvents(signal=>(async function*(){
  while(!signal.aborted){if(!queue.length)await new Promise(resolve=>{wake=resolve;signal.addEventListener('abort',resolve,{once:true});});if(queue.length)yield queue.shift();}
 })(),{home:'/synthetic'});
 const ownerA=new Peer(ctx,{principal:{kind:'owner'},interactions:'answer'}),ownerB=new Peer(ctx,{principal:{kind:'owner'},interactions:'answer'}),noInteractions=new Peer(ctx,{principal:{kind:'owner'},interactions:'none'});peers.push(ownerA,ownerB,noInteractions);
 control=new AbortController();
 const empty=(async function*(){})();
 iterator=(await ctx.typertGateway.wireStream.open('$events',{args:{}},empty,ownerA,control.signal))[Symbol.asyncIterator]();
 const ready=(await iterator.next()).value;assert.equal(ready.type,'ready');
 let settled=false,resolveOutcome,rejectOutcome;
 const outcome=new Promise((resolve,reject)=>{resolveOutcome=resolve;rejectOutcome=reject;});
 const subject={},agentContext=ctx.extend();
 queue.push({event:'approval/request',context:{subject,agentId:'synthetic-agent',value:agentContext},request:{agent:subject,toolName:'write',reason:'Synthetic fixture'},resolve:value=>{settled=true;resolveOutcome(value);},reject:rejectOutcome});wake?.();
 const request=(await iterator.next()).value;assert.equal(request.type,'waterfall');
 const payload={args:{clientId:ready.clientId,eventId:request.eventId,outcome:{kind:'result',value:'rejected'}}};
 const denied=await dispatch('$events/result',payload,new AbortController().signal,ownerB);
 assert.equal(denied.ok,false);assert.equal(denied.error.code,'gateway/interactions-forbidden');assert.equal(settled,false);
 result.other_owner_peer_cannot_answer=true;
 const unavailable=await dispatch('$events/result',payload,new AbortController().signal,noInteractions);
 assert.equal(unavailable.ok,false);assert.equal(settled,false);result.peer_without_interactions_cannot_answer=true;
 const accepted=await dispatch('$events/result',payload,new AbortController().signal,ownerA);
 assert.equal(accepted.ok,true);assert.deepEqual(await outcome,{kind:'result',value:'rejected'});result.same_owner_peer_can_answer=true;
 assert.equal((await dispatch('$events/result',payload,new AbortController().signal,ownerA)).ok,true);result.completed_event_replay_noop=true;
 result.status='PASSED';
}finally{
 control?.abort();await iterator?.return();await unregister?.();for(const p of peers)await p.dispose();for(const f of fibers.reverse())await f.dispose();
}
await writeFile(path.join(here,'native-event-peers-result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
