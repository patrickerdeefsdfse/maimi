import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {createToolkit} from '../session-tools.mjs';
import {createDesktopTools} from '../desktop.mjs';
import {createToolkitHttp} from '../http.mjs';
const session='12345678-1234-1234-1234-123456789abc',other='87654321-1234-1234-1234-123456789abc';
const A={clientKey:'A'},B={clientKey:'B'};
const catalog={items:[{plugin_key:'dingtalk',name:'钉钉',status:'ready',tools:[{name:'read'},{name:'write'}]},
 {plugin_key:'web-search',name:'搜索',status:'limited',tools:[{name:'web_read',available:true},{name:'web_search',available:false}]}]};
const choose=(t,context=A,id=session)=>t.control('selection',{session_id:id,plugins:['dingtalk']},context);
const call=(action='read',id=session)=>({operation:'invoke',session_id:id,connector:'dingtalk',action,arguments:{}});
test('client/session isolation; model cannot choose or confirm; scopes cannot cross',async()=>{
 const t=createToolkit({remote:async(path)=>path==='/catalog'?catalog:{status:'completed',result:'ok'}});
 await choose(t);await assert.rejects(t.execute(call(),B),/PLUGIN_NOT_SELECTED/);
 await assert.rejects(t.execute(call('read',other),A),/PLUGIN_NOT_SELECTED/);
 await assert.rejects(t.execute({...call(),operation:'confirm'},A),/PLUGIN_NOT_SELECTED/);
 assert.equal((await t.execute(call(),A)).status,'completed');await t.close();
});
test('owner-bound human confirmation is once-only and confirmation ID is absent from model output',async()=>{
 let sent=0;const t=createToolkit({remote:async(path)=>path==='/catalog'?catalog:path==='/invoke'?{status:'requires_confirmation',confirmation_id:'c1',preview:{summary:'send'}}:(sent++,{status:'completed'})});
 await choose(t);const model=await t.execute(call('write'),A);assert.equal(model.confirmation_id,undefined);
 await assert.rejects(t.control('confirm',{session_id:session,confirmation_id:'c1',confirmed:true},B),/CONFIRMATION_EXPIRED/);
 await t.control('confirm',{session_id:session,confirmation_id:'c1',confirmed:true},A);assert.equal(sent,1);
 await assert.rejects(t.control('confirm',{session_id:session,confirmation_id:'c1',confirmed:true},A),/CONFIRMATION_EXPIRED/);await t.close();
});
test('stop aborts pending reads and no late result is accepted',async()=>{
 let entered;const started=new Promise(r=>entered=r);const t=createToolkit({remote:async(path,body,ctx)=>{
  if(path==='/catalog')return catalog;entered();await new Promise((r,j)=>ctx.signal.addEventListener('abort',()=>j(Error('aborted')),{once:true}));
 }});await choose(t);const operation=t.execute(call(),A);await started;await t.control('stop',{session_id:session},A);await assert.rejects(operation,/aborted/);await t.close();
});
test('expiry and unavailable tool on limited plugin remain denied',async()=>{
 let now=0;const t=createToolkit({now:()=>now,remote:async()=>catalog});
 await t.control('selection',{session_id:session,plugins:['web-search']},A);
 const c=await t.execute({operation:'catalog',session_id:session},A);assert.deepEqual(c.items[0].tools.map(t=>t.name),['web_read']);
 await assert.rejects(t.execute({...call(),connector:'web-search',action:'web_search'},A),/TOOL_NOT_AVAILABLE/);
 now=31*60000;await assert.rejects(t.execute({operation:'catalog',session_id:session},A),/PLUGIN_NOT_SELECTED/);await t.close();
});
test('desktop grants are per-plugin, expire, and cancellation blocks writing',async()=>{
 let now=0,yes=true,writes=0;const local=createDesktopTools({now:()=>now,approve:async()=>yes,computer:async()=>{writes++;return {ok:true}}});
 await local.enable('computer-use','scope');await local.enable('browser-use','scope');await local.disable('browser-use','scope');
 await local.invoke('computer-use','windows',{},'scope');assert.equal(writes,1);
 yes=false;await assert.rejects(local.invoke('computer-use','click',{window_id:1,x:1,y:1},'scope'),/USER_CANCELLED/);assert.equal(writes,1);
 now=11*60000;await assert.rejects(local.invoke('computer-use','windows',{},'scope'),/EXPIRED/);await local.close();
});
test('HTTP private execute requires secret; confirm is not a model operation; methods bounded',async t=>{
 const toolkit=createToolkit({remote:async()=>catalog});const handler=createToolkitHttp(toolkit,{token:'x'.repeat(43),browserContext:()=>A,modelContext:()=>A});
 const server=http.createServer((req,res)=>void handler(req,res));await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>{server.closeAllConnections();server.close();return toolkit.close();});
 const origin='http://127.0.0.1:'+server.address().port;
 assert.equal((await fetch(origin+'/__megmeet/tool-execute',{method:'POST',body:'{}'})).status,403);
 assert.equal((await fetch(origin+'/__megmeet/tools/confirm')).status,405);
 assert.equal((await fetch(origin+'/__megmeet/tools/catalog?token=bad')).status,400);
 assert.equal((await fetch(origin+'/__megmeet/tool-execute',{method:'POST',headers:{Authorization:'Bearer '+'x'.repeat(43)},body:JSON.stringify({...call(),operation:'confirm'})})).status,403);
});

test('desktop dispatch refuses injected actions, extra fields and malformed arguments before approval',async()=>{
 let approvals=0,calls=0;
 const local=createDesktopTools({approve:async()=>{approvals++;return true;},computer:async()=>{calls++;return {};}});
 await local.enable('computer-use','scope');
 for(const [action,args] of [['windows',{action:'type',text:'injected'}],['click',{window_id:1,x:'1',y:1}],['type',{window_id:1}],['key',{window_id:1,key:'ENTER',unexpected:true}]])
   await assert.rejects(local.invoke('computer-use',action,args,'scope'),/ARGUMENTS_INVALID/);
 assert.equal(approvals,1);assert.equal(calls,0);await local.close();
});

test('stop invalidates an in-flight enable approval and cannot resurrect a plugin',async()=>{
 let release,entered;const gate=new Promise(r=>entered=r);
 const local=createDesktopTools({approve:()=>{entered();return new Promise(r=>release=r);},computer:async()=>({})});
 const toolkit=createToolkit({local,remote:async()=>({items:[]})});
 const enabling=toolkit.control('selection',{session_id:session,plugins:['computer-use']},A);
 await gate;await toolkit.control('stop',{session_id:session},A);release(true);
 await assert.rejects(enabling,/USER_CANCELLED|SELECTION_CHANGED/);
 assert.deepEqual((await toolkit.control('state',{session_id:session},A)).selected,[]);
 await assert.rejects(local.invoke('computer-use','windows',{},'A:'+session),/EXPIRED/);
 await toolkit.close();
});

test('stop during catalog lookup prevents a late selection from being installed',async()=>{
 let release,entered;const gate=new Promise(r=>entered=r);
 const toolkit=createToolkit({remote:async()=>{entered();return new Promise(r=>release=r);}});
 const selecting=choose(toolkit);await gate;
 await toolkit.control('stop',{session_id:session},A);release(catalog);
 await assert.rejects(selecting,/SELECTION_CHANGED/);
 assert.deepEqual((await toolkit.control('state',{session_id:session},A)).selected,[]);await toolkit.close();
});

test('stop during invocation catalog lookup prevents the tool from starting',async()=>{
 let release,entered,hold=false,invocations=0;const gate=new Promise(r=>entered=r);
 const toolkit=createToolkit({remote:async path=>{
  if(path!=='/catalog'){invocations++;return {status:'completed'};}
  if(!hold)return catalog;entered();return new Promise(r=>release=r);
 }});
 await choose(toolkit);hold=true;const invocation=toolkit.execute(call(),A);await gate;
 await toolkit.control('stop',{session_id:session},A);release(catalog);
 await assert.rejects(invocation,/SELECTION_CHANGED/);assert.equal(invocations,0);await toolkit.close();
});

test('cancelled multi-plugin selection rolls back earlier local permission',async()=>{
 let approvals=0;
 const local=createDesktopTools({approve:async()=>++approvals===1,computer:async()=>({})});
 const toolkit=createToolkit({local,remote:async()=>({items:[]})});
 await assert.rejects(toolkit.control('selection',{session_id:session,plugins:['computer-use','browser-use']},A),/USER_CANCELLED/);
 await assert.rejects(local.invoke('computer-use','windows',{},'A:'+session),/EXPIRED/);
 assert.deepEqual((await toolkit.control('state',{session_id:session},A)).selected,[]);await toolkit.close();
});
