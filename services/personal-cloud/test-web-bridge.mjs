import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {createCloudWebBridge} from './web-bridge.mjs';
import {createToolkit} from '../../toolkit/session-tools.mjs';
import {apply, initiatingRoot} from './web-control-plugin.mjs';
const token='t'.repeat(43),lease='mcl_'+'s'.repeat(43),session='session-12345678-1234-1234-1234-123456789012';
async function setup(t,{selected=true,search=true}={}) {
  const s={task:{lease,sessionId:session},calls:[],valid:true,entered:0};
  const item={plugin_key:'web-search',name:'联网搜索',status:search?'ready':'limited',
    tools:[{name:'web_read',available:true},{name:'web_search',available:search}]};
  const toolkit=createToolkit({authorize:async c=>{assert.equal(c.clientKey,lease);if(!s.valid)throw Error('expired');},
    remote:async(path,body,c)=>{
      assert.equal(c.clientKey,lease);if(path==='/catalog')return{items:[item]};
      s.calls.push(body);s.entered++;try{
        if(s.handler)return await s.handler(body,c);
        return {status:'completed',result:body.action==='web_read'
          ?{url:'https://example.com/',text:'Synthetic public page',truncated:false,untrusted_external_content:true}
          :{sources:[{url:'https://example.com/',title:'Synthetic',snippet:body.arguments.query}],untrusted_external_content:true}};
      }finally{s.entered--;}
    }});
  if(selected)await toolkit.control('selection',{session_id:session,plugins:['web-search']},{clientKey:lease});
  const bridge=createCloudWebBridge({token,current:()=>s.task,validate:async()=>{if(!s.valid)throw Error('expired');},toolkit});
  const server=http.createServer((req,res)=>void bridge.handle(req,res));await new Promise(r=>server.listen(0,'127.0.0.1',r));
  s.send=(body={action:'web_read',arguments:{url:'https://example.com/'},session_id:session},options={})=>fetch('http://127.0.0.1:'+server.address().port+(options.path||'/__megmeet/web'),{
    method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...options.headers},body:typeof body==='string'?body:JSON.stringify(body),signal:options.signal});
  Object.assign(s,{bridge,toolkit});
  t.after(async()=>{bridge.stop();await toolkit.close();server.closeAllConnections();await new Promise(r=>server.close(r));});return s;
}
test('selected native read/search use original lease and normalized untrusted connector output',async t=>{
 const s=await setup(t);const read=await s.send();assert.equal(read.status,200);assert.deepEqual(await read.json(),{url:'https://example.com/',statusCode:200,body:{kind:'text',content:'Synthetic public page'},truncated:false});
 const search=await s.send({action:'web_search',arguments:{query:'synthetic',count:2},session_id:session});assert.equal(search.status,200);assert.equal((await search.json()).sources[0].snippet,'synthetic');
 assert.equal(s.calls[0].connector,'web-search');assert.equal(s.calls[0].action,'web_read');assert.equal(JSON.stringify(s.calls).includes(token),false);
});
test('native actions require explicit selected plugin and report missing search configuration separately',async t=>{
 const s=await setup(t,{selected:false});assert.equal((await(await s.send()).json()).code,'WEB_NOT_SELECTED');assert.equal(s.calls.length,0);
 const limited=await setup(t,{search:false});const r=await limited.send({action:'web_search',arguments:{query:'synthetic',count:2},session_id:session});assert.equal(r.status,503);assert.equal((await r.json()).code,'WEB_SEARCH_NOT_CONFIGURED');assert.equal(limited.calls.length,0);assert.equal((await limited.send()).status,200);
});
test('foreign task, revoked lease, injected endpoint and malformed action fail before Gateway',async t=>{
 const s=await setup(t);assert.equal((await s.send(undefined,{headers:{Authorization:'Bearer wrong'}})).status,401);
 assert.equal((await s.send(undefined,{path:'/__megmeet/web?url=https://evil.invalid'})).status,401);
 for(const body of [{action:'other',arguments:{url:'https://example.com/'},session_id:session},{action:'web_read',arguments:{url:'https://example.com/',headers:{Authorization:'secret'}},session_id:session},{action:'web_search',arguments:{query:'x'.repeat(401),count:1},session_id:session},'x'.repeat(9000)])assert.equal((await s.send(body)).status,400);
 assert.equal((await s.send({action:'web_read',arguments:{url:'https://example.com/'},session_id:session.replace('12345678','12345679')})).status,401);
 s.task.revoked=true;assert.equal((await s.send()).status,401);assert.equal(s.calls.length,0);
});
test('multiple native search queries serialize within connector scope',async t=>{
 const s=await setup(t);let maximum=0;s.handler=async(body)=>{maximum=Math.max(maximum,s.entered);await new Promise(r=>setTimeout(r,15));return{status:'completed',result:{sources:[],untrusted_external_content:true}};};
 const responses=await Promise.all(['one','two','three'].map(query=>s.send({action:'web_search',arguments:{query,count:2},session_id:session})));
 assert.deepEqual(responses.map(x=>x.status),[200,200,200]);assert.equal(maximum,1);assert.equal(s.calls.length,3);
});
test('task replacement suppresses old result, and stop aborts active egress',async t=>{
 const s=await setup(t);s.handler=async()=>{s.task={lease,sessionId:session};return{status:'completed',result:{sources:[],untrusted_external_content:true}};};
 assert.equal((await s.send({action:'web_search',arguments:{query:'one',count:1},session_id:session})).status,401);
 let started;const ready=new Promise(r=>started=r);let aborted=false;
 s.handler=async(_body,c)=>{started();await new Promise((resolve,reject)=>c.signal.addEventListener('abort',()=>{aborted=true;reject(Error('stopped'));},{once:true}));};
 const result=s.send();await ready;s.bridge.stop(s.task);assert.equal((await(await result).json()).code,'WEB_CANCELLED');assert.equal(aborted,true);
});
test('selection revoked while network call runs cancels result and hides raw upstream details',async t=>{
 const s=await setup(t);let entered;const ready=new Promise(r=>entered=r);
 s.handler=async(_body,c)=>{entered();await new Promise((resolve,reject)=>c.signal.addEventListener('abort',()=>reject(Error('hidden token '+token)),{once:true}));};
 const response=s.send();await ready;await s.toolkit.control('stop',{session_id:session},{clientKey:lease});const text=await(await response).text();assert.equal(text.includes(token),false);assert.equal(text.includes('Synthetic'),false);
});
test('unbounded, unmarked or noncompleted upstream results are never native pages',async t=>{
 const s=await setup(t);for(const value of [{status:'completed',result:{url:'https://example.com/',text:'x',truncated:false}}, {status:'requires_confirmation'}, {status:'completed',result:{url:'https://example.com/',text:'x'.repeat(16001),truncated:false,untrusted_external_content:true}}]){
 s.handler=async()=>value;assert.notEqual((await s.send()).status,200);}
});
test('runtime ownership includes live child but not detached or durably forked roots',()=>{
 const root={id:session,session:{id:session}}, child={id:session.replace('12345678','12345679'),session:{id:session.replace('12345678','12345679')}};
 let current=child,live=[root,child],roots=[root];const agents={currentInitiator:()=>current,list:()=>live,roots:()=>roots,isOwnedBy:(id,owner)=>id===child.id&&owner===root};
 assert.equal(initiatingRoot(agents),session);roots=[root,child];assert.equal(initiatingRoot(agents),child.id);
 live=[root];assert.throws(()=>initiatingRoot(agents),/WEB_SESSION_REQUIRED/);current=undefined;assert.throws(()=>initiatingRoot(agents),/WEB_SESSION_REQUIRED/);
});
test('provider registers exact private endpoint, normalized counts, initiating session and caller cancellation',async()=>{
 const providers={},root={id:session,session:{id:session}};const ctx={agents:{currentInitiator:()=>root,list:()=>[root],roots:()=>[root]},web:{registerSearchProvider:p=>providers.search=p,registerFetchProvider:p=>providers.fetch=p}};
 assert.throws(()=>apply(ctx,{endpoint:'https://example.com/',token}),/WEB_PRIVATE_ENDPOINT_REQUIRED/);apply(ctx,{endpoint:'http://127.0.0.1:3086/__megmeet/web',token});
 const original=globalThis.fetch;const calls=[];globalThis.fetch=async(url,opts)=>{calls.push({url:String(url),opts});return Response.json({sources:[],truncated:false});};
 try{const signal=new AbortController();await providers.search.search({query:'one',maxResults:30},signal.signal);assert.equal(calls[0].url,'http://127.0.0.1:3086/__megmeet/web');assert.deepEqual(JSON.parse(calls[0].opts.body),{action:'web_search',arguments:{query:'one',count:10},session_id:session});signal.abort();assert.equal(calls[0].opts.signal.aborted,true);assert.equal(providers.fetch.id,'megmeet-gateway');}finally{globalThis.fetch=original;}
});
