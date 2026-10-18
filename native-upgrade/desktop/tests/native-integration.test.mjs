import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fork} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {createCompanyModelBridge} from '../src/company-model-bridge.mjs';

const dir=path.dirname(fileURLToPath(import.meta.url));
const runtimeRoot=path.resolve(dir,'../../upstream/runtime-win');
function listen(server){return new Promise(resolve=>server.listen(0,'127.0.0.1',()=>resolve(`http://127.0.0.1:${server.address().port}`)));}
function stop(server){server.closeAllConnections();return new Promise(resolve=>server.close(resolve));}
const authorization=()=>({owner:'synthetic-owner',generation:1,token:'synthetic-company-access-token',expiresAt:Date.now()+60000,models:[{id:'deepseek-v4.1-flash',contextWindow:131072,maxTokens:8192}]});

test('bridge denies missing capability, browser origins, unapproved models, expired and changed IAM',async()=>{
 let auth=authorization(),calls=0;
 const remote=http.createServer((req,res)=>{calls++;res.end('{}');});
 const gatewayOrigin=await listen(remote);
 const bridge=await createCompanyModelBridge({getAuthorization:()=>auth,gatewayOrigin,testOnly:true});
 try {
  assert.equal((await fetch(bridge.origin+'/v1/models')).status,401);
  const headers={authorization:`Bearer ${bridge.capability}`,'content-type':'application/json'};
  assert.equal((await fetch(bridge.origin+'/v1/models',{headers:{...headers,origin:'https://evil.invalid'}})).status,401);
  assert.equal((await fetch(bridge.origin+'/v1/chat/completions',{method:'POST',headers,body:JSON.stringify({model:'unapproved'})})).status,403);
  auth=null;assert.equal((await fetch(bridge.origin+'/v1/models',{headers})).status,401);
  assert.equal(calls,0);
 }finally{await bridge.close();await stop(remote);}
});

test('bridge rejects an account generation change before releasing the response',async()=>{
 const auth=authorization();let release,received;
 const requestReceived=new Promise(resolve=>received=resolve);
 const remote=http.createServer(async(req,res)=>{for await(const chunk of req){}received();await new Promise(resolve=>release=resolve);res.writeHead(200,{'content-type':'application/json'});res.end('{"private_result":"must not escape"}');});
 const gatewayOrigin=await listen(remote);
 const bridge=await createCompanyModelBridge({getAuthorization:()=>auth,gatewayOrigin,testOnly:true});
 try{
  const response=fetch(bridge.origin+'/v1/chat/completions',{method:'POST',headers:{authorization:`Bearer ${bridge.capability}`,'content-type':'application/json'},body:JSON.stringify({model:'deepseek-v4.1-flash'})});
  await requestReceived;auth.generation++;release();
  const result=await response;assert.equal(result.status,401);assert.ok(!(await result.text()).includes('must not escape'));
 }finally{release?.();await bridge.close();await stop(remote);}
});

test('bridge contains IAM lookup exceptions and rejects public gateway overrides',async()=>{
 await assert.rejects(createCompanyModelBridge({getAuthorization:authorization,gatewayOrigin:'https://example.org'}),/GATEWAY_ORIGIN_NOT_ALLOWED/);
 const bridge=await createCompanyModelBridge({getAuthorization:()=>{throw Error('synthetic-private-detail');},gatewayOrigin:'http://127.0.0.1:7',testOnly:true});
 try{const response=await fetch(bridge.origin+'/v1/models',{headers:{authorization:`Bearer ${bridge.capability}`}});assert.equal(response.status,401);assert.ok(!(await response.text()).includes('synthetic-private-detail'));}
 finally{await bridge.close();await bridge.close();}
});

test('latest native desktop profile boots, serves UI and streams through isolated company bridge', {timeout:120000}, async()=>{
 assert.ok(fs.existsSync(path.join(runtimeRoot,'node_modules/@deepseek-ai/dsh/package.json')),'PINNED_RUNTIME_REQUIRED');
 const home=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-native-desktop-'));
 let calls=0,seenAuth=null,seenPath=null;
 const remote=http.createServer(async(req,res)=>{
  calls++;seenAuth=req.headers.authorization;seenPath=req.url;
  let raw='';for await(const chunk of req)raw+=chunk;
  const body=JSON.parse(raw);assert.equal(body.model,'deepseek-v4.1-flash');
  res.writeHead(200,{'content-type':'text/event-stream'});
  const chunk={id:'synthetic-completion',object:'chat.completion.chunk',created:1,model:body.model,choices:[{index:0,delta:{role:'assistant',content:'native bridge works'},finish_reason:null}]};
  res.write(`data: ${JSON.stringify(chunk)}\n\n`);
  res.write(`data: ${JSON.stringify({...chunk,choices:[{index:0,delta:{},finish_reason:'stop'}]})}\n\n`);
  res.end('data: [DONE]\n\n');
 });
 const gatewayOrigin=await listen(remote);
 const bridge=await createCompanyModelBridge({getAuthorization:authorization,gatewayOrigin,testOnly:true});
 const env={};for(const key of ['PATH','Path','SystemRoot','TEMP','TMP','APPDATA','LOCALAPPDATA','USERPROFILE'])if(process.env[key])env[key]=process.env[key];
 const child=fork(path.join(dir,'native-host-child.mjs'),[],{execArgv:['--expose-internals'],env,silent:true});
 let logs='';child.stdout.on('data',x=>{logs=(logs+x).slice(-12000);});child.stderr.on('data',x=>{logs=(logs+x).slice(-12000);});
 const messages=[];let exited=false;child.on('exit',()=>exited=true);child.on('message',m=>messages.push(m));
 const wait=async type=>{const end=Date.now()+90000;while(Date.now()<end){const error=messages.find(m=>m.type==='fixture-error');if(error)throw Error(error.message+'\n'+logs);const hit=messages.find(m=>m.type===type);if(hit)return hit;if(exited)throw Error('HOST_EARLY_EXIT\n'+logs);await new Promise(r=>setTimeout(r,50));}throw Error('HOST_TIMEOUT\n'+logs);};
 try {
  child.send({type:'synthetic-start',runtimeRoot,home,profileRoot:path.join(home,'profile'),modelBase:bridge.origin+'/v1',modelId:'deepseek-v4.1-flash',capability:bridge.capability});
  const ready=await wait('ready');
  const exchange=await fetch(ready.url,{redirect:'manual'});assert.equal(exchange.status,303);
  const cookie=exchange.headers.get('set-cookie')?.split(';')[0];assert.ok(cookie);
  const ui=await fetch(new URL(exchange.headers.get('location'),ready.url),{headers:{cookie}});assert.equal(ui.status,200);assert.match(await ui.text(),/<html/i);
  const result=await wait('model-result');assert.match(JSON.stringify(result.chunks),/native bridge works/);
  assert.equal(calls,1);assert.equal(seenAuth,'Bearer synthetic-company-access-token');assert.equal(seenPath,'/v1/personal-model/chat/completions');
  const config=fs.readFileSync(path.join(home,'profile/cordis.patch.yml'),'utf8');
  assert.ok(!config.includes('synthetic-company-access-token'));assert.ok(!config.includes(bridge.capability));
  assert.ok(!logs.includes('synthetic-company-access-token'));
  child.send({type:'shutdown'});await wait('shutdown-complete');
 }finally{
  if(!exited)child.kill();await bridge.close();await stop(remote);
  // Disposable fixture root only; no user DSH profile is touched.
  await new Promise(r=>setTimeout(r,200));fs.rmSync(home,{recursive:true,force:true,maxRetries:3});
 }
});
