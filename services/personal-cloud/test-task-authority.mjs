import test from 'node:test';
import assert from 'node:assert/strict';
import { TaskAuthority,validateNativeRpc } from './task-authority.mjs';
import { apply as installControl } from './task-control-plugin.mjs';
const A='mcl_'+'a'.repeat(43),B='mcl_'+'b'.repeat(43);
const session='session-12345678-1234-1234-1234-123456789012';
const idle={protocol:1,busy:false,activeRoots:[]};
const busy={protocol:1,busy:true,activeRoots:[session]};
test('two clients can read but only initiating client owns running task',()=>{
 const a=new TaskAuthority(),task=a.claim(A,session,idle);a.finishDispatch();
 assert.throws(()=>a.claim(B,session,busy),/另一端/);
 assert.throws(()=>a.assertMutation(B),/另一端/);
 const model=a.beginModel(busy);assert.equal(model.task.lease,A);
 a.revoke(task);assert.throws(()=>a.beginModel(busy),/authorization/);
 assert.throws(()=>a.claim(B,session,idle),/另一端/);
 model.end();a.reconcile(idle);assert.equal(a.claim(B,session,idle).lease,B);
});
test('second chat and delayed foreign root never borrow active task credential',()=>{
 const a=new TaskAuthority();a.claim(A,session,idle);a.finishDispatch();
 assert.throws(()=>a.claim(A,'session-12345678-1234-1234-1234-123456789013',busy),/另一段/);
 assert.throws(()=>a.beginModel({...busy,activeRoots:['other-root']}),/authorization/);
});
test('dispatch fence and live model connection keep generation pinned',()=>{
 const a=new TaskAuthority();const task=a.claim(A,session,idle);
 assert.equal(a.reconcile(idle),task);a.finishDispatch();
 const m=a.beginModel(busy);assert.equal(a.reconcile(idle),task);
 m.end();m.end();assert.equal(a.modelRequests,0);assert.equal(a.reconcile(idle),null);
});
test('restart does not inherit stale model authority; uncertain status fails closed',()=>{
 const a=new TaskAuthority();assert.throws(()=>a.beginModel(busy),/authorization/);
 assert.throws(()=>a.claim(A,session,busy),/结束/);
 assert.throws(()=>a.claim(A,session,{}),/unavailable/);
});
test('native identity, cloud workspace and background delegation remain bounded',()=>{
 const body=(method,request)=>({type:'client-request',rpcId:'id',method,payload:{args:{request}}});
 assert.throws(()=>validateNativeRpc('/api/session/prompt','POST',body('other/prompt',{}),'cloud'),/invalid/);
 assert.throws(()=>validateNativeRpc('/api/workspace/create','POST',body('workspace/create',{path:'/state'}),'cloud'),/工作区/);
 assert.throws(()=>validateNativeRpc('/api/session/create','POST',body('session/create',{cwd:'/etc'}),'cloud'),/工作区/);
 assert.throws(()=>validateNativeRpc('/api/goal/create','POST',body('goal/create',{}),'cloud'),/后台/);
 assert.equal(validateNativeRpc('/api/session/prompt','POST',body('session/prompt',{sessionId:session}),'cloud').request.sessionId,session);
});
test('file download reads and preview generation do not acquire a model task',()=>{
 for(const method of ['workspaceFiles/stat','workspaceFiles/readBytes','officeToPdf/generation']){
  const envelope={type:'client-request',rpcId:'id',method,payload:{args:{workspaceFileScopeId:session,path:'/workspace/synthetic.docx'}}};
  assert.equal(validateNativeRpc('/api/'+method,'POST',envelope,'cloud').readOnly,true);
 }
 const method='workspaceFiles/unexpectedMutation';
 assert.equal(validateNativeRpc('/api/'+method,'POST',{type:'client-request',rpcId:'id',method,payload:{args:{}}},'cloud').readOnly,false);
});
test('private native state observes inbox and descendant activity; revoke clears queues and cancels',async()=>{
 const events=[];let route;
 const agent={id:session,status:'idle',inbox:{nextTurn:[{id:'queued'}],nextStep:[],remove:id=>{events.push(id);agent.inbox.nextTurn=[]}},cancel:cause=>events.push(cause.kind)};
 const child={id:'child',status:'running',inbox:{nextTurn:[],nextStep:[],remove(){}},cancel:cause=>events.push('child-'+cause.kind)};
 const ctx={agents:{list:()=>[agent,child],roots:()=>[agent]},get:()=>({list:()=>[{status:'killed'}]}),
   connection:{fetch:{register:value=>{route=value;return()=>{}}}},effect:fn=>fn()};
 installControl(ctx);
 let state=await (await route.fetch(new Request('http://localhost/api/megmeet-cloud/task-state'))).json();
 assert.equal(state.busy,true);assert.deepEqual(state.activeRoots,[session]);
 await route.fetch(new Request('http://localhost/api/megmeet-cloud/task-state',{method:'POST',body:'{"cancel":true}'}));
 assert.deepEqual(events,['queued','user','child-user']);
 child.status='idle';state=await (await route.fetch(new Request('http://localhost/api/megmeet-cloud/task-state'))).json();
 assert.equal(state.busy,false);
 const post=value=>route.fetch(new Request('http://localhost/api/megmeet-cloud/task-state',{method:'POST',body:JSON.stringify(value)}));
 assert.equal((await post({claim:true,generation:1})).status,200);
 assert.equal((await post({claim:true,generation:2})).status,200);
 const count=events.length;
 assert.equal((await post({cancel:true,generation:1})).status,409);
 assert.equal((await post({claim:true,generation:1})).status,409);
 assert.equal((await post({cancel:true})).status,409);
 assert.equal(events.length,count);
 assert.equal((await post({cancel:true,generation:2})).status,200);
});
