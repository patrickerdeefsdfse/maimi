import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {IdleController,holdIdleRequest} from './idle-control.mjs';

const idle=()=>({protocol:1,busy:false,activeRoots:[]});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

test('idle snapshot fences new uploads/prompts until resumed',async()=>{
 const c=new IdleController({sample:async()=>idle()});
 assert.deepEqual(await c.drain(),{protocol:1,busy:false,draining:true});
 assert.throws(()=>c.enter(),/释放/);assert.equal(c.resume().draining,false);
 c.enter()();assert.equal(c.inFlight,0);
});
test('entire in-flight upload prevents draining, even with idle native agent',async()=>{
 const c=new IdleController({sample:async()=>idle()});const done=c.enter({work:true});
 assert.equal((await c.busy()).busy,true);assert.equal((await c.drain()).draining,false);
 done();done();assert.equal(c.inFlight,0);assert.equal((await c.drain()).draining,true);
});
test('native jobs and generation/model dispatch prevent release',async()=>{
 const c=new IdleController({sample:async()=>({...idle(),busy:true})});
 assert.deepEqual(await c.drain(),{protocol:1,busy:true,draining:false});
 let busy=true;const local=new IdleController({sample:async()=>idle(),localBusy:()=>busy});
 assert.equal((await local.drain()).draining,false);busy=false;assert.equal((await local.drain()).draining,true);
});
test('background read-only POST blocks teardown but never counts as work',async()=>{
 const c=new IdleController({sample:async()=>idle()});const done=c.enter();
 assert.deepEqual(await c.busy(),{protocol:1,busy:false,draining:false});
 assert.deepEqual(await c.drain(),{protocol:1,busy:false,draining:false});
 done();assert.equal((await c.drain()).draining,true);
});
test('parsed mutating RPC upgrades its admission to real work exactly once',async()=>{
 const c=new IdleController({sample:async()=>idle()});const done=c.enter();
 assert.equal((await c.busy()).busy,false);done.work();done.work();
 assert.equal(c.workInFlight,1);assert.equal((await c.busy()).busy,true);
 done();done.work();assert.equal(c.workInFlight,0);
});
test('prompt admission cannot cross asynchronous idle snapshot',async()=>{
 const gate=deferred(),c=new IdleController({sample:()=>gate.promise});
 const result=c.drain();assert.throws(()=>c.enter());
 gate.resolve(idle());assert.equal((await result).draining,true);
});
test('busy task discovered during snapshot reopens admission',async()=>{
 const gate=deferred(),c=new IdleController({sample:()=>gate.promise});
 const result=c.drain();gate.resolve({...idle(),busy:true});
 assert.deepEqual(await result,{protocol:1,busy:true,draining:false});c.enter()();
});
test('unknown or malformed native status never authorizes deletion',async()=>{
 for(const sample of [async()=>{throw Error('unknown');},async()=>({busy:false}),async()=>({...idle(),busy:0})]){
  const c=new IdleController({sample});assert.deepEqual(await c.drain(),{protocol:1,busy:true,draining:false});c.enter()();
 }
});
test('resume invalidates an outstanding idle decision',async()=>{
 const gate=deferred(),c=new IdleController({sample:()=>gate.promise});
 const result=c.drain();c.resume();const done=c.enter();gate.resolve(idle());
 assert.deepEqual(await result,{protocol:1,busy:true,draining:false});done();
});
test('fence pauses existing streams and failed check resumes them',async()=>{
 const events=[],c=new IdleController({sample:async()=>({...idle(),busy:true}),onFence:()=>events.push('pause'),onResume:()=>events.push('resume')});
 await c.drain();assert.deepEqual(events,['pause','resume']);
});
test('concurrent drain requests share a single snapshot',async()=>{
 const gate=deferred();let calls=0;const c=new IdleController({sample:()=>{calls++;return gate.promise;}});
 const a=c.drain(),b=c.drain();gate.resolve(idle());assert.deepEqual(await a,await b);assert.equal(calls,1);
});
test('disconnected request holds the fence until its async handler completes',async()=>{
 const c=new IdleController({sample:async()=>idle()}),req=new EventEmitter(),res=new EventEmitter();
 const hold=holdIdleRequest(c,req,res,{work:true});res.emit('close');
 assert.equal((await c.busy()).busy,true);assert.equal((await c.drain()).draining,false);
 hold.handlerDone();assert.equal(c.inFlight,0);assert.equal((await c.drain()).draining,true);
});
test('completed preparation holds its slow response until finish or abort',async()=>{
 const c=new IdleController({sample:async()=>idle()}),req=new EventEmitter(),res=new EventEmitter();
 const hold=holdIdleRequest(c,req,res);hold.work();hold.handlerDone();
 assert.equal((await c.busy()).busy,true);req.emit('aborted');res.emit('close');
 assert.equal(c.workInFlight,0);assert.equal(c.inFlight,0);
});
