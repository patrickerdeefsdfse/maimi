/** Synthetic image bytes only; load the unmodified DSH Connection RPC caller. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import test from 'node:test';
import vm from 'node:vm';

const source=readFileSync(new URL('../app/personal_cloud_web.py',import.meta.url),'utf8');
const extract=name=>source.split('async def '+name+'():')[1].split("return Response('''")[1].split("''',media_type='text/javascript',headers=HEADERS)")[0];
const nativeSource=readFileSync(new URL('../../../desktop-local/dist/0.8.1-company/MEGMEET%20AI-win32-x64/resources/runtime/node_modules/@deepseek-ai/dsh-client-connection/lib/client.js',import.meta.url),'utf8');
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR9sAAAAASUVORK5CYII=','base64');
const cipher=Buffer.from('%TSD-Header-###%SYNTHETIC_IMAGE_ONLY');
const name='图片 & 库龄 #+%.png';
const part=(data=cipher,filename=name)=>({type:'image',mediaType:'image/png',data:Buffer.from(data).toString('base64'),name:filename});
const request=content=>({requestId:'synthetic-request',sessionId:'synthetic-session',mode:'queue',clientTimeZone:'Asia/Shanghai',content});
const envelope=(content,method='session/prompt')=>({type:'client-request',rpcId:'synthetic-rpc',method,payload:{args:{request:request(content)}}});
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'content-type':'application/json'}});
function setup({href='https://platform-ai.megmeet.com/maimi/web/chat',reply,withIdle=true}={}){
  const calls=[],context=vm.createContext({URL,URLSearchParams,Headers,Response,Blob,Request,ReadableStream,Uint8Array,DOMException,AbortController,crypto:webcrypto,console,location:new URL(href),atob,btoa,
    document:{readyState:'loading',addEventListener(){},removeEventListener(){}},setInterval(){},clearInterval(){}});
  context.window=context;
  context.fetch=async(input,init)=>{
    const call={url:new URL(typeof input?.url==='string'?input.url:input,href),init};calls.push(call);
    if(reply){const value=await reply(call);if(value!==undefined)return value;}
    if(call.url.pathname==='/v1/personal-cloud/web/image')return json({name:call.url.searchParams.get('filename'),mediaType:'image/png',data:png.toString('base64')});
    if(call.url.pathname==='/api/mock-release')return json({detail:'personal_cloud_open_required'},409);
    if(call.url.pathname==='/api/mock-expired')return json({detail:'unauthorized'},401);
    if(call.url.pathname.endsWith('/prompt')){const body=JSON.parse(init.body);return json({type:'server-response',rpcId:body.rpcId,result:{ok:true,value:{accepted:true}}});}
    return json({status:'ready'});
  };
  if(withIdle)vm.runInContext(extract('session_script'),context);
  vm.runInContext(extract('image_script'),context);
  context.__ModuleLoader__={load(mod){const exports=mod.factory(()=>{throw Error('unexpected native dependency');});exports.apply({provide(key,value){context[key]=value;}});}};
  vm.runInContext(nativeSource,context);
  const send=(content,signal,method='session/prompt')=>context.connection.rpc.call('/api',method,{args:{request:request(content)}},signal);
  return {context,calls,send,fetch:(...args)=>context.fetch(...args)};
}
function direct(e,data,options={}){return e.fetch('https://platform-ai.megmeet.com/api/session/prompt',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data),...options});}
const uploads=e=>e.calls.filter(x=>x.url.pathname==='/v1/personal-cloud/web/image');
const prompts=e=>e.calls.filter(x=>x.url.pathname.endsWith('/prompt'));

test('boot order installs image hook after idle guard and before deferred DSH startup',()=>{
  assert.ok(source.includes('<script src="/maimi/web/upload.js"></script><script src="/maimi/web/session.js"></script><script src="/maimi/web/image.js"></script></head>'));
  assert.match(source.split('\n').find(line=>line.startsWith('PUBLIC_WEB_PATHS=')),/image\.js/);
});
test('real native RPC sees accepted original envelope after raw443 encrypted image becomes PNG',async()=>{
  const e=setup(),signal=new AbortController().signal,result=await e.send([part(),{type:'text',text:'合成测试，请描述图片。'},{type:'file',receiptId:'synthetic-file'}],signal);
  assert.equal(result.ok,true);assert.equal(e.calls.length,2);
  const raw=uploads(e)[0];assert.equal(raw.url.protocol,'https:');assert.equal(raw.url.port,'');assert.equal(raw.url.searchParams.get('filename'),name);assert.equal(raw.url.searchParams.get('size'),String(cipher.length));
  assert.equal(raw.init.credentials,'same-origin');assert.equal(raw.init.mode,'same-origin');assert.equal(raw.init.redirect,'error');assert.equal(raw.init.cache,'no-store');assert.equal(raw.init.signal,signal);assert.equal(raw.init.headers['Content-Type'],'application/octet-stream');
  assert.deepEqual(Buffer.from(await raw.init.body.arrayBuffer()),cipher);
  const sent=JSON.parse(prompts(e)[0].init.body);assert.equal(sent.method,'session/prompt');assert.equal(sent.payload.args.request.sessionId,'synthetic-session');assert.equal(sent.payload.args.request.requestId,'synthetic-request');assert.equal(sent.payload.args.request.mode,'queue');assert.equal(sent.payload.args.request.clientTimeZone,'Asia/Shanghai');
  assert.equal(sent.payload.args.request.content[0].data,png.toString('base64'));assert.deepEqual(sent.payload.args.request.content.slice(1),[{type:'text',text:'合成测试，请描述图片。'},{type:'file',receiptId:'synthetic-file'}]);
  assert.equal(prompts(e)[0].init.signal,signal);
});
test('plain text goes through byte-identical without image processing',async()=>{
  const e=setup(),body=JSON.stringify(envelope([{type:'text',text:'你好'}]));await direct(e,JSON.parse(body));assert.equal(uploads(e).length,0);assert.equal(prompts(e)[0].init.body,body);
});
test('subagent native image continuation also uses raw gateway upload',async()=>{
  const e=setup();await e.send([part()],undefined,'subagents/prompt');assert.equal(uploads(e).length,1);assert.equal(prompts(e)[0].url.pathname,'/api/subagents/prompt');
});
test('ordered multiple images and MIME changes retain native inline-image form',async()=>{
  const gif=Buffer.from('GIF89aSYNTHETIC');let count=0;
  const e=setup({reply:call=>{if(call.url.pathname!=='/v1/personal-cloud/web/image')return;count++;return json({name:call.url.searchParams.get('filename'),mediaType:count===1?'image/png':'image/gif',data:(count===1?png:gif).toString('base64')});}});
  await e.send([part(),{type:'text',text:'compare'},part(cipher,'second.gif')]);const content=JSON.parse(prompts(e)[0].init.body).payload.args.request.content;assert.equal(content[0].mediaType,'image/png');assert.equal(content[1].text,'compare');assert.equal(content[2].mediaType,'image/gif');
});
test('invalid base64, unsupported MIME, too many, oversized and aggregate inputs never upload or prompt',async()=>{
  const e=setup();const oversized=Buffer.alloc(8*1024*1024+1);const eight=Buffer.alloc(8*1024*1024);
  const cases=[[{...part(),data:'@@=='}],[{...part(),data:'AB=='}],[{...part(),mediaType:'image/svg+xml'}],Array.from({length:9},()=>part()),[part(oversized)],[part(eight),part(eight),part(Buffer.from([1]))],[{...part(),name:'x'.repeat(256)}]];
  for(const content of cases){const result=await e.send(content);assert.equal(result.ok,false);assert.equal(result.error.code,'session/image-upload-failed');}
  assert.equal(e.calls.length,0);
});
test('source image with no name receives an appropriate filename marker',async()=>{
  const e=setup(),image=part();delete image.name;await e.send([image]);assert.equal(uploads(e)[0].url.searchParams.get('filename'),'image.png');
});
test('preaborted prompt does not upload or send',async()=>{
  const e=setup(),controller=new AbortController();controller.abort();await assert.rejects(e.send([part()],controller.signal),{name:'AbortError'});assert.equal(e.calls.length,0);
});
test('aborting raw network upload stops before native prompt',async()=>{
  const controller=new AbortController();let entered;
  const started=new Promise(resolve=>entered=resolve),e=setup({reply:call=>{if(call.url.pathname!== '/v1/personal-cloud/web/image')return;entered();return new Promise((resolve,reject)=>call.init.signal.addEventListener('abort',()=>reject(call.init.signal.reason),{once:true}));}});
  const pending=e.send([part()],controller.signal);await started;controller.abort();await assert.rejects(pending,{name:'AbortError'});assert.equal(prompts(e).length,0);
});
test('abort after a complete raw response still prevents native prompt',async()=>{
  const controller=new AbortController();const e=setup({reply:call=>{if(call.url.pathname!=='/v1/personal-cloud/web/image')return;controller.abort();return json({data:png.toString('base64'),name,mediaType:'image/png'});}});
  await assert.rejects(e.send([part()],controller.signal),{name:'AbortError'});assert.equal(prompts(e).length,0);
});
test('fixed backend decrypt error is a native Remote failure and never sends ciphertext',async()=>{
  const e=setup({reply:call=>call.url.pathname=== '/v1/personal-cloud/web/image'?json({detail:'personal_cloud_image_still_encrypted'},422):undefined});const result=await e.send([part()]);assert.equal(result.ok,false);assert.match(result.error.message,/安全网关尚未完成解密/);assert.equal(prompts(e).length,0);
});
test('invalid MIME/magic, bad JSON and huge server response fail closed',async()=>{
  const cases=[()=>json({mediaType:'image/png',data:cipher.toString('base64'),name}),()=>json({mediaType:'image/gif',data:png.toString('base64'),name}),()=>new Response('not JSON'),()=>new Response('x'.repeat(12*1024*1024))];
  for(const response of cases){const e=setup({reply:call=>call.url.pathname==='/v1/personal-cloud/web/image'?response():undefined});assert.equal((await e.send([part()])).ok,false);assert.equal(prompts(e).length,0);}
});
test('unknown server/network diagnostics never reach the UI or prompt',async()=>{
  for(const reply of [()=>json({detail:'PRIVATE_SYNTHETIC_DETAILS'},500),()=>{throw Error('PRIVATE_SYNTHETIC_DIAGNOSTICS');}]){
    const e=setup({reply:call=>call.url.pathname==='/v1/personal-cloud/web/image'?reply():undefined});const result=await e.send([part()]);assert.equal(result.ok,false);assert.ok(!JSON.stringify(result).includes('PRIVATE'));assert.equal(prompts(e).length,0);
  }
});
test('r15 released and expired guards block raw upload and preserve native error response',async()=>{
  for(const cause of ['release','expired']){const e=setup();await e.fetch('/api/mock-'+cause);const count=e.calls.length;const result=await e.send([part()]);assert.equal(result.ok,false);assert.equal(e.calls.length,count);assert.equal(uploads(e).length,0);assert.equal(prompts(e).length,0);assert.match(result.error.message,cause==='release'?/工作区已释放/:/登录已失效/);}
});
test('r15 idle guard is rechecked after image preprocessing and before final prompt',async()=>{
  let e;e=setup({reply:async call=>{if(call.url.pathname!=='/v1/personal-cloud/web/image')return;await e.fetch('/api/mock-release');return json({data:png.toString('base64'),mediaType:'image/png',name});}});
  await assert.rejects(e.send([part()]),/HTTP 409/);assert.equal(uploads(e).length,1);assert.equal(prompts(e).length,0);
});
test('HTTP or non443 same-origin prompt cannot route images to a weaker endpoint',async()=>{
  for(const href of ['http://platform-ai.megmeet.com/maimi/web/chat','https://platform-ai.megmeet.com:444/maimi/web/chat']){const e=setup({href});const result=await e.send([part()]);assert.equal(result.ok,false);assert.match(result.error.message,/HTTPS 443/);assert.equal(e.calls.length,0);}
});
test('hook installation is idempotent and ordinary binary upload remains untouched',async()=>{
  const e=setup();const once=e.context.fetch;vm.runInContext(extract('image_script'),e.context);assert.equal(e.context.fetch,once);
  const body=new Blob([cipher]);await e.fetch('/api/session/uploadFileBinary',{method:'POST',headers:{'content-type':'application/octet-stream'},body});assert.equal(e.calls.length,1);assert.equal(e.calls[0].init.body,body);
});
test('all four supported image signatures returned by the gateway remain image content',async()=>{
  for(const [mediaType,bytes] of [['image/png',png],['image/jpeg',Buffer.from([255,216,255,1])],['image/gif',Buffer.from('GIF87aSYNTHETIC')],['image/webp',Buffer.from('RIFF0000WEBPSYNTHETIC')]]){
    const e=setup({reply:call=>call.url.pathname==='/v1/personal-cloud/web/image'?json({mediaType,data:bytes.toString('base64'),name}):undefined});assert.equal((await e.send([part()])).ok,true);assert.equal(JSON.parse(prompts(e)[0].init.body).payload.args.request.content[0].mediaType,mediaType);
  }
});
