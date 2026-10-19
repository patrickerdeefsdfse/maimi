import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import {startCompanyNativeSession} from '../src/company-desktop-session.mjs';

// Keep the real two loopback bridges. Only the process creation boundary is
// faulted, and cancellation is delivered from an actual server listening event.
async function scenario(t,abortAfter) {
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-startup-cleanup-'));
 const servers=[],controller=new AbortController(),failure=Error('SYNTHETIC_STARTUP_FAILURE');
 let forks=0;
 const createServer=http.createServer;
 t.mock.method(http,'createServer',(...args)=>{
  const server=Reflect.apply(createServer,http,args);servers.push(server);
  server.once('listening',()=>{if(servers.length===abortAfter)controller.abort(failure);});
  return server;
 });
 t.mock.method(childProcess,'fork',()=>{forks++;throw failure;});
 syncBuiltinESMExports();
 try {
  await assert.rejects(startCompanyNativeSession({
   auth:{user:{user_no:'M00096389'},token:'synthetic-parent-only',expiresAt:Date.now()+60000},
   models:[{id:'deepseek-v4.1-flash'}],runtimeRoot:path.join(temp,'unused-runtime'),
   stateRoot:path.join(temp,'state'),gatewayOrigin:'http://127.0.0.1:1',testOnly:true,signal:controller.signal,
  }),error=>error===failure);
  assert.equal(forks,abortAfter===undefined?1:0,'cancelled startup must never fork a Host');
  assert.equal(servers.length,abortAfter??2,'the intended real bridge boundary was reached');
  assert.ok(servers.every(server=>!server.listening),'failed startup must close every loopback listener');
 } finally {
  t.mock.restoreAll();syncBuiltinESMExports();
  await Promise.all(servers.map(server=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve);})));
  assert.equal(path.dirname(path.resolve(temp)),path.resolve(os.tmpdir()));
  fs.rmSync(temp,{recursive:true,force:true,maxRetries:3});
 }
}

test('synchronous fork failure closes both real private bridges',{timeout:10000},t=>scenario(t));
test('abort while model bridge opens closes it without forking',{timeout:10000},t=>scenario(t,1));
test('abort while enterprise bridge opens closes both without forking',{timeout:10000},t=>scenario(t,2));
