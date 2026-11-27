import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {desktopConnectorProfile,apply,verifyDws} from '../shell/maimi/desktop-connectors.mjs';
import {startDesktopTelemetry} from '../shell/maimi/desktop-telemetry.mjs';

function clock() {
  let id=0; const jobs=new Map();
  return {jobs,setTimer(fn,delay){const handle=++id;jobs.set(handle,{fn,delay});return handle;},
    clearTimer(handle){jobs.delete(handle);},async next(){const [handle,job]=jobs.entries().next().value; jobs.delete(handle);await job.fn();}};
}
test('heartbeat sends version only, uses latest grant, and never emits activity',async()=>{
  const c=clock(),calls=[];let token='synthetic-test-grant';
  const h=startDesktopTelemetry({version:'0.14.0',getAuthorization:()=>({token,expiresAt:2000}),request:async(...a)=>{calls.push(a);return{status:200,value:{status:'recorded'}};},now:()=>1000,...c});
  await c.next(); assert.equal(calls.length,1);
  assert.equal(calls[0][0],'/v1/desktop/auth/telemetry'); assert.deepEqual(calls[0][1].body,{version:'0.14.0'});
  assert.deepEqual(Object.keys(calls[0][1]).sort(),['body','signal','token']);
  token='rotated-synthetic-grant'; await c.next();assert.equal(calls[1][1].token,token);
  h.stop();assert.equal(c.jobs.size,0);assert.equal(h.status().state,'stopped');
});
test('missing/expired auth never sends, failures back off without surfacing credentials',async()=>{
  const c=clock();let available=false,calls=0;
  const h=startDesktopTelemetry({version:'0.14.0',now:()=>1000,getAuthorization:()=>available?{token:'synthetic',expiresAt:2000}:null,
    request:async()=>{calls++;throw Error('must-not-leave-process');},...c});
  await c.next();assert.equal(calls,0);available=true;await c.next();assert.equal(calls,1);
  assert.equal([...c.jobs.values()][0].delay,120000);assert.deepEqual(h.status(),{state:'unavailable',enabled:true});h.stop();
});
test('stop aborts a pending heartbeat and prevents rescheduling',async()=>{
  const c=clock();let received,finish;
  const h=startDesktopTelemetry({version:'0.14.0',now:()=>0,getAuthorization:()=>({token:'synthetic',expiresAt:1000}),
    request:async(_,opts)=>{received=opts.signal;return new Promise(resolve=>{finish=resolve;});},...c});
  const pending=c.next();h.stop();assert.equal(received.aborted,true);finish({status:200,value:{status:'recorded'}});await pending;assert.equal(c.jobs.size,0);
});
test('invalid metadata and aggressive heartbeat intervals rejected',()=>{
  assert.throws(()=>startDesktopTelemetry({version:'token/value',getAuthorization(){},request(){}}));
  assert.throws(()=>startDesktopTelemetry({version:'0.14.0',intervalMs:100,getAuthorization(){},request(){}}));
});
test('IM profile enables exact upstream package with stable isolated home; non-Windows skips',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-connectors-test-'));
  try {
    const dir=path.join(root,'node_modules','@xmanrui','dsh-im');fs.mkdirSync(dir,{recursive:true});
    fs.writeFileSync(path.join(dir,'package.json'),JSON.stringify({name:'@xmanrui/dsh-im',version:'5.10.0'}));
    const home=path.join(root,'employee-home');
    const entry=path.join(dir,'lib','index.js');
    fs.mkdirSync(path.dirname(entry));fs.writeFileSync(entry,'// empty fixture');
    assert.deepEqual(desktopConnectorProfile({runtimeRoot:root,home,platform:'win32'}),{bundles:[],patch:[{insert:[{id:'xmanrui-dsh-im',name:pathToFileURL(entry).href,config:{dshHome:home}}]}]});
    assert.deepEqual(desktopConnectorProfile({platform:'linux'}),{bundles:[],patch:[]});
    fs.writeFileSync(path.join(dir,'package.json'),JSON.stringify({name:'@xmanrui/dsh-im',version:'changed'}));
    assert.throws(()=>desktopConnectorProfile({runtimeRoot:root,home,platform:'win32'}),/VERSION_MISMATCH/);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
test('DWS staged payload verified and skill auto-routes without starting a program', {skip:!process.env.MAIMI_TEST_DWS_ROOT},()=>{
  const root=process.env.MAIMI_TEST_DWS_ROOT;
  assert.ok(verifyDws(root).binary.endsWith('dws.exe'));
  const skills=[];apply({skills:{register:s=>skills.push(s)}},{root,platform:'win32'});
  assert.equal(skills.length,1);assert.deepEqual(skills[0].invocation,{modelInvocable:true,userInvocable:true});
  assert.ok(skills[0].content.includes('DPAPI/HKCU'));assert.ok(skills[0].content.includes('--profile'));
});
