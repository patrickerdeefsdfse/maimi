// Real React from the pinned DSH frontend; synthetic local RPC responses only.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {resolveRuntime,loadChromium,chromiumOptions} from './test-runtime.mjs';
const chromium=loadChromium();
const assets=pathToFileURL(path.join(resolveRuntime(),'node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/')+path.sep);
const frontend=await readFile(new URL('index-CN7NIWNw.js',assets),'utf8'),boot=frontend.indexOf('const Ms=globalThis.dshDesktopBoot');assert.ok(boot>0);
const vendor=frontend.slice(0,boot)+'\nwindow.nativeStatic=SZ();';
const client=await readFile(new URL('../../src/local-enterprise/service-ui/client.js',import.meta.url),'utf8');
const server=createServer(async(req,res)=>{
 if(req.url==='/assets/audit.js'){res.setHeader('content-type','text/javascript');res.end(vendor);return;}
 if(req.url.startsWith('/assets/')){const name=req.url.slice(8);if(name.includes('..')){res.writeHead(404).end();return;}res.setHeader('content-type','text/javascript');res.end(await readFile(new URL(name,assets)));return;}
 res.setHeader('content-type','text/html');res.end('<!doctype html><html><meta charset="utf-8"><body><main id="root"></main></body></html>');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;
try{
 browser=await chromium.launch(chromiumOptions());
 const page=await browser.newPage({viewport:{width:1000,height:700}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${server.address().port}`);await page.addScriptTag({type:'module',url:'/assets/audit.js'});await page.waitForFunction(()=>!!window.nativeStatic);
 await page.evaluate(source=>{
  const React=window.nativeStatic.react,ReactDOM=window.nativeStatic['react-dom/client'];let Component,enabled=false,connected=false;
  window.rpcCalls=[];window.secretWrites=[];
  const original=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){window.secretWrites.push([k,v]);return original.call(this,k,v);};
  window.__ModuleLoader__={load({factory}){const plugin=factory(name=>{if(name==='react')return React;throw Error(name);});plugin.apply({slots:{inject(_name,fn){fn();},register(_spec,component){Component=component;}},connection:{rpc:{async call(channel,method,payload){window.rpcCalls.push({channel,method,payload:JSON.parse(JSON.stringify(payload))});if(method==='enable')enabled=true;if(method==='connect')connected=true;if(method==='disable'){enabled=false;connected=false;}return {ok:true,value:{enabled,connected,server:connected?{label:'synthetic server'}:null}};}}}});}};
  (0,eval)(source);window.root=ReactDOM.createRoot(document.getElementById('root'));window.root.render(React.createElement(Component,{sessionId:'11111111-1111-4111-8111-111111111111'}));
 },client);
 await page.getByRole('button',{name:'service · 连接服务器'}).click();
 await page.getByLabel('IP 地址',{exact:true}).fill('10.4.240.14');await page.getByLabel('密码',{exact:true}).fill('private-ui-fixture');
 assert.equal(await page.locator('input').count(),2);
 await page.waitForTimeout(5200);assert.equal(await page.getByLabel('密码',{exact:true}).inputValue(),'private-ui-fixture','background refresh must not clear password');
 await page.getByRole('button',{name:'连接',exact:true}).click();await page.getByText('服务器已连接',{exact:true}).waitFor();
 const result=await page.evaluate(()=>({calls:window.rpcCalls,writes:window.secretWrites,body:document.body.textContent}));
 const connect=result.calls.filter(c=>c.method==='connect');assert.equal(connect.length,1);assert.deepEqual(Object.keys(connect[0].payload).sort(),['ip','password','session_id']);
 assert.ok(!result.body.includes('private-ui-fixture'));assert.ok(!JSON.stringify(result.writes).includes('private-ui-fixture'));assert.deepEqual(errors,[]);
 console.log(JSON.stringify({status:'PASSED',real_pinned_dsh_react:true,exact_inputs:2,ip_password_submission:true,background_refresh_preserves_draft:true,password_not_rendered_or_stored:true,company_requests:0}));
}finally{await browser?.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
