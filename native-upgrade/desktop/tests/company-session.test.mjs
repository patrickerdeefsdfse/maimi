import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {startCompanyNativeSession} from '../src/company-desktop-session.mjs';
const runtimeRoot=process.env.DSH_R65_DESKTOP_RUNTIME??process.env.DSH_R65_RUNTIME??path.resolve(import.meta.dirname,'../../upstream',process.platform==='win32'?'runtime-win':'runtime-linux');
const auth=owner=>({user:{user_no:owner},token:'synthetic-parent-only-token',expiresAt:Date.now()+120000});
const models=[{id:'deepseek-v4.1-flash'}];

test('candidate refuses an existing unmarked state and expired IAM before starting',async()=>{
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-iam-test-'));
 try{
  await assert.rejects(startCompanyNativeSession({auth:auth('M00000001'),models,runtimeRoot,stateRoot:tmp}));
  await assert.rejects(startCompanyNativeSession({auth:{...auth('M00000001'),expiresAt:0},models,runtimeRoot,stateRoot:path.join(tmp,'new')}),/VERIFIED_COMPANY_AUTH_REQUIRED/);
  assert.equal(fs.existsSync(path.join(tmp,'new')),false);
 }finally{fs.rmSync(tmp,{recursive:true,force:true});}
});

test('native startup IPC serves isolated accounts, revokes old session and retains cloud gate', {timeout:120000}, async()=>{
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-iam-test-'));
 const server=http.createServer((req,res)=>{res.writeHead(500);res.end();});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const gatewayOrigin=`http://127.0.0.1:${server.address().port}`;
 let first,second;
 try{
  const stateRoot=path.join(tmp,'state');
  first=await startCompanyNativeSession({auth:auth('M00000001'),models,runtimeRoot,stateRoot,gatewayOrigin,testOnly:true});
  assert.ok(first.hasCompanySession());assert.equal(first.cloudEntryRequired,false);assert.equal(first.companyPublishAllowed,false);
  assert.deepEqual(await first.inspectQuit(),{activeTasks:false,scheduledTasks:false});
  assert.equal(await first.updateTasks('inspect'),false);assert.equal(await first.updateTasks('lock'),false);assert.equal(await first.updateTasks('unlock'),false);
  await assert.rejects(first.updateTasks('download'),/NATIVE_UPDATE_ACTION_INVALID/);
  assert.ok(!JSON.stringify(first).includes('synthetic-parent-only-token'));
  const exchange=await fetch(first.url,{redirect:'manual'});assert.equal(exchange.status,303);
  await first.stop();assert.equal(first.hasCompanySession(),false);
  await assert.rejects(fetch(first.url));
  second=await startCompanyNativeSession({auth:auth('M00000002'),models,runtimeRoot,stateRoot,gatewayOrigin,testOnly:true});
  assert.notEqual(first.home,second.home);assert.ok(!first.home.includes('M00000001'));assert.ok(!second.home.includes('M00000002'));
  assert.ok(fs.existsSync(first.home));assert.ok(fs.existsSync(second.home));
  await second.stop();assert.equal(second.hasCompanySession(),false);
 }finally{
  await first?.stop();await second?.stop();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
  fs.rmSync(tmp,{recursive:true,force:true,maxRetries:3});
 }
});
