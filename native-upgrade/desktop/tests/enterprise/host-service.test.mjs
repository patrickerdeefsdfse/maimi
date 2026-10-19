import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import {randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {resolveRuntime,loadChromium,chromiumOptions} from './test-runtime.mjs';
const sourceRoot=process.env.MAIMI_DESKTOP_TEST_SRC??path.resolve(import.meta.dirname,'../../src');
const {startCompanyNativeSession}=await import(pathToFileURL(path.join(sourceRoot,'company-desktop-session.mjs')).href);
const runtimeRoot=resolveRuntime(true);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function connectThroughBrowser(base,cookie,sessionId,clientUrl){
 const chromium=loadChromium();
 const frontend=fs.readFileSync(path.join(runtimeRoot,'node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/index-CN7NIWNw.js'),'utf8');
 const boot=frontend.indexOf('const Ms=globalThis.dshDesktopBoot');assert.ok(boot>0);const vendor=frontend.slice(0,boot)+'\nwindow.nativeStatic=SZ();';
 const browser=await chromium.launch(chromiumOptions());
 try{
  const context=await browser.newContext();await context.addCookies(cookie.split('; ').map(v=>{const i=v.indexOf('=');return {name:v.slice(0,i),value:v.slice(i+1),url:base};}));
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{const u=new URL(route.request().url());if(u.origin!==base)return route.abort();if(u.pathname==='/local-ui-test')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><meta charset="utf-8"><body><main id="root"></main></body></html>'});if(u.pathname==='/assets/audit.js')return route.fulfill({contentType:'text/javascript',body:vendor});return route.continue();});
  await page.goto(base+'/local-ui-test');await page.addScriptTag({type:'module',url:'/assets/audit.js'});await page.waitForFunction(()=>!!window.nativeStatic);
  await page.evaluate(id=>{
   const React=window.nativeStatic.react,ReactDOM=window.nativeStatic['react-dom/client'];window.secretWrites=[];
   const original=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){window.secretWrites.push([k,v]);return original.call(this,k,v);};
   window.__ModuleLoader__={load({factory}){const plugin=factory(name=>{if(name==='react')return React;throw Error(name);});let Component;
    plugin.apply({slots:{inject(_n,f){f();},register(_s,c){Component=c;}},connection:{rpc:{async call(channel,method,payload,signal){const response=await fetch(channel+'/'+method,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:crypto.randomUUID(),method,payload}),signal});if(!response.ok)throw Error('NATIVE_RPC_HTTP');return (await response.json()).result;}}}});
    ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(Component,{sessionId:id}));}};
  },sessionId);
  // This resource is composed and served by the actual native Host profile.
  await page.addScriptTag({url:clientUrl});
  await page.getByRole('button',{name:'service · 连接服务器'}).click();
  await page.getByLabel('IP 地址',{exact:true}).fill('10.4.240.14');await page.getByLabel('密码',{exact:true}).fill('synthetic-private-password');
  assert.equal(await page.locator('input').count(),2);
  await page.waitForTimeout(5200);assert.equal(await page.getByLabel('密码',{exact:true}).inputValue(),'synthetic-private-password');
  await page.getByRole('button',{name:'连接',exact:true}).click();await page.getByText('服务器已连接',{exact:true}).waitFor();
  assert.ok(!(await page.locator('body').innerText()).includes('synthetic-private-password'));assert.ok(!(await page.evaluate(()=>JSON.stringify(window.secretWrites))).includes('synthetic-private-password'));assert.deepEqual(errors,[]);
 }finally{await browser.close();}
}

test('real desktop parent/Host/Peer: private IPC service selection, two-field connect, native model tool and logout',{timeout:150000},async t=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-desktop-service-'));let session,outsider;
 const calls=[],grants=new Map(),connections=new Set(),errors=[];let modelCalled=false,modelToolObserved=false,held,heldClosed=false,modelCount=0;
 const token='synthetic-private-parent-access';
 const gateway=http.createServer(async(req,res)=>{
  try{
   assert.equal(req.headers.authorization,'Bearer '+token);let raw='';for await(const b of req)raw+=b;const body=raw?JSON.parse(raw):{};
   calls.push(req.url);const json=value=>{res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(value));};
   if(req.url==='/v1/personal-model/chat/completions'){
    modelCalled=true;modelCount++;const tools=body.tools?.map(x=>x.function.name)??[];
    const last=body.messages.at(-1),user=JSON.stringify(body.messages.filter(x=>x.role==='user').at(-1));
    res.writeHead(200,{'content-type':'text/event-stream'});
    const chunk=(delta,finish_reason=null)=>res.write('data: '+JSON.stringify({id:'synthetic-'+modelCount,object:'chat.completion.chunk',created:Math.floor(Date.now()/1000),model:body.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');
    chunk({role:'assistant',content:''});
    if(user?.includes('SYNTHETIC_HOLD')){held=res;res.on('close',()=>{heldClosed=true;});chunk({content:'holding'});return;}
    if(last.role!=='tool'&&tools.includes('maimi_service')){
     modelToolObserved=true;
     assert.ok(!tools.some(x=>['maimi_enterprise_catalog','maimi_office_remote','maimi_enterprise_call'].includes(x)));
     chunk({tool_calls:[{index:0,id:'call_service_synthetic',type:'function',function:{name:'maimi_service',arguments:JSON.stringify({action:'inspect_server',arguments:{host_id:'synthetic-host'}})}}]});chunk({},'tool_calls');
    }else {chunk({content:'SYNTHETIC_SERVICE_COMPLETE'});chunk({},'stop');}
    res.end('data: [DONE]\n\n');return;
   }
   assert.ok(req.url.startsWith('/v1/connectors/local-service/'),'cloud host and unrelated route forbidden');
   if(req.url.endsWith('/grants')){
    const n=String(grants.size+1),g={control_grant:n.padStart(43,'C'),tool_grant:n.padStart(43,'T'),expires_at:Math.floor(Date.now()/1000)+600,native:body.native_session_id};
    grants.set(g.control_grant,g);grants.set(g.tool_grant,g);json(g);return;
   }
   const g=grants.get(req.headers['x-maimi-service-grant']);assert.ok(g);
   if(req.url.endsWith('/connect')){
    assert.equal(req.headers['x-maimi-service-grant'],g.control_grant);assert.deepEqual(Object.keys(body).sort(),['ip','password']);assert.equal(body.password,'synthetic-private-password');connections.add(g.native);
   }else if(req.url.endsWith('/revoke')){
    connections.delete(g.native);json({revoked:true,disconnect_confirmed:true});return;
   }else if(req.url.endsWith('/invoke')){
    assert.equal(req.headers['x-maimi-service-grant'],g.tool_grant);
    if(body.action==='inspect_server'){assert.ok(connections.has(g.native));json({host_id:'synthetic-host',checks:[{output:'SYNTHETIC_LINUX_RESULT'}]});return;}
   }
   json({protocol:'maimi-service-session-v2',connected:connections.has(g.native),server:connections.has(g.native)?{host_id:'synthetic-host',label:'synthetic server'}:null});
  }catch(e){errors.push(e.message);res.writeHead(500).end();}
 });
 await new Promise(r=>gateway.listen(0,'127.0.0.1',r));
 try{
  session=await startCompanyNativeSession({auth:{user:{user_no:'M00096389'},token,expiresAt:Date.now()+180000},models:[{id:'deepseek-v4.1-flash'}],runtimeRoot,stateRoot:path.join(temp,'state'),gatewayOrigin:`http://127.0.0.1:${gateway.address().port}`,testOnly:true,signal:t.signal});
  t.diagnostic('actual native desktop child ready');assert.equal(session.cloudEntryRequired,false);
  const boot=session.injections.find(x=>x.name==='__DSH_BOOT__');
  assert.ok(boot,'native client boot graph');
  const serviceEntry=boot.value.entries.find(x=>x.id==='maimi-local-service-ui');assert.ok(serviceEntry,'public service client entry composed');
  const exchange=await fetch(session.url,{redirect:'manual'});assert.equal(exchange.status,303);
  const cookie=exchange.headers.getSetCookie().map(x=>x.split(';')[0]).join('; '),base=new URL(session.url).origin;
  assert.ok(cookie);
  async function rpc(channel,method,payload){const response=await fetch(base+channel+'/'+method,{method:'POST',headers:{cookie,'content-type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:randomUUID(),method,payload})});assert.equal(response.status,200);return (await response.json()).result;}
  async function good(method,request){const result=await rpc('/api',method,{args:method==='session/list'?{_request:request}:{request}});assert.equal(result.ok,true,method+': '+JSON.stringify(result));return result.value;}
  const work=path.join(temp,'workspace');fs.mkdirSync(work);
  const ws=(await good('workspace/create',{path:work})).workspace;
  const {sessionId}=await good('session/create',{workspaceId:ws.workspaceId});
  const human=(method,more={})=>rpc('/maimi-service',method,{session_id:sessionId,...more});
  assert.deepEqual(await human('state'),{ok:true,value:{enabled:false}});assert.equal(calls.length,0);
  await connectThroughBrowser(base,cookie,sessionId,'/'+serviceEntry.url);
  assert.equal((await human('state')).value.connected,true);
  await good('session/prompt',{sessionId,requestId:randomUUID(),mode:'queue',content:[{type:'text',text:'SYNTHETIC_INSPECT'}],clientTimeZone:'Asia/Shanghai'});
  async function wait(fn,label){for(let n=0;n<200;n++){if(await fn())return;await sleep(100);}throw Error('TIMEOUT_'+label);}
  await wait(async()=>modelCalled&&(await good('session/list',{})).items.find(x=>x.sessionId===sessionId)?.running===false,'model');
  const p=await good('session/projections',{sessionId});
  const history=JSON.stringify(await good('session/page',{address:{kind:'session',sessionId},throughSeq:p.asOfSeq,maxMessages:100}));
  assert.ok(modelToolObserved,'enterprise tool discoverable by real model');assert.ok(history.includes('SYNTHETIC_LINUX_RESULT'));assert.ok(!history.includes('synthetic-private-password'));assert.ok(!history.includes(token));
  await good('session/prompt',{sessionId,requestId:randomUUID(),mode:'queue',content:[{type:'text',text:'SYNTHETIC_HOLD'}],clientTimeZone:'Asia/Shanghai'});
  await wait(()=>held!==undefined,'held model');await session.stop();
  assert.equal(session.hasCompanySession(),false);assert.equal(connections.size,0);await wait(()=>heldClosed,'model cancelled');
  assert.ok(calls.some(x=>x.endsWith('/revoke')));assert.ok(!calls.some(x=>x.includes('cloud')));assert.deepEqual(errors,[]);
  const before=calls.length;
  outsider=await startCompanyNativeSession({auth:{user:{user_no:'M00003308'},token,expiresAt:Date.now()+120000},models:[{id:'deepseek-v4.1-flash'}],runtimeRoot,stateRoot:path.join(temp,'other-state'),gatewayOrigin:`http://127.0.0.1:${gateway.address().port}`,testOnly:true,signal:t.signal});
  const outsiderGraph=outsider.injections.find(x=>x.name==='__DSH_BOOT__').value;
  assert.ok(!outsiderGraph.entries.some(x=>x.id==='maimi-local-service-ui'));assert.equal(calls.length,before);await outsider.stop();
  const patches=fs.readdirSync(path.join(session.home,'native-profiles')).map(x=>fs.readFileSync(path.join(session.home,'native-profiles',x,'cordis.patch.yml'),'utf8'));
  for(const patch of patches){assert.ok(!patch.includes('nativeCapability'));assert.ok(!patch.includes(token));assert.ok(!patch.includes('synthetic-private-password'));assert.ok(!patch.includes('maimi-native-desktop-ui'));}
 }finally{held?.destroy();await session?.stop();await outsider?.stop();gateway.closeAllConnections();await new Promise(r=>gateway.close(r));fs.rmSync(temp,{recursive:true,force:true,maxRetries:5});}
});
