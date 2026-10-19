import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire,registerHooks} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {mkdtemp,rm} from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
import http from 'node:http';
import {fixture,A,B} from './bridge.test.mjs';
import {resolveRuntime} from './test-runtime.mjs';
const HERE=path.dirname(fileURLToPath(import.meta.url));
const runtime=resolveRuntime();
const requireRuntime=createRequire(path.join(runtime,'package.json'));
registerHooks({resolve(specifier,context,next){if(specifier==='@deepseek-ai/dsh-mcp-client'&&context.parentURL?.endsWith('/native-plugin.mjs'))return next(pathToFileURL(requireRuntime.resolve(specifier)).href,context);return next(specifier,context);}});
const [{Context,Service},{default:Agents},{default:Sessions},{default:Attachments},{default:Tools},{default:Prompt},Plugin,{HostConnectionService}]=await Promise.all(['@deepseek-ai/cordis','@deepseek-ai/dsh-agent','@deepseek-ai/dsh-session','@deepseek-ai/dsh-attachment-local','@deepseek-ai/dsh-tools','@deepseek-ai/dsh-system-prompt','../../src/local-enterprise/native-plugin.mjs','@deepseek-ai/dsh-client-connection'].map(x=>import(x.startsWith('@')?pathToFileURL(requireRuntime.resolve(x)).href:x)));
export async function host(options={}){
 const f=await fixture(options),home=await mkdtemp(path.join(tmpdir(),'maimi-local-enterprise-')),ctx=new Context(),fibers=[];
 const use=async(plugin,config)=>{const fiber=ctx.plugin(plugin,config);fibers.push(fiber);await fiber;return fiber;};
 await use(Prompt,{includeRuntimeContext:false});await use(Tools);await use(Sessions);await use(Agents);await use(Attachments,{dshHome:home});
 let connection,server,nonePeerId;
 { // Actual desktop plugin declares connection/webServer even with UI disabled.
  const routes=[];
  class Web extends Service{constructor(ctx){super(ctx,'webServer');this.host='127.0.0.1';this.protocol='http:';}register(route){routes.push(route);return()=>{const i=routes.indexOf(route);if(i>=0)routes.splice(i,1);};}}
  new Web(ctx);
  connection=new HostConnectionService(ctx,[],{isAuthenticated:req=>req.headers['x-synthetic-human']==='yes',authenticateApi:req=>req.headers['x-synthetic-none']==='yes'?nonePeerId:undefined});
  nonePeerId=connection.publishPeer('none');
  server=http.createServer((req,res)=>{const route=routes.find(r=>req.url.startsWith(r.path+'/'));if(!route){res.writeHead(404).end();return;}void route.handler(req,res);});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 }
 const policy={revision:1,selected:['logistics-query'],remoteOffice:true};
 await use(Plugin,{endpoint:f.bridge.origin,nativeCapability:f.bridge.nativeCapability,getPolicy:()=>({...policy}),enableServiceUi:options.enableServiceUi});
 async function agent(id){const session=ctx.sessions.create(id);const value={id,session,ctx,status:'idle'};await ctx.agents.register(value);return value;}
 const a=await agent(A),b=await agent(B);
 async function call(who,name,args={},signal=new AbortController().signal){const tool=ctx.tools.view(who).visible.get(name);assert.ok(tool,'tool discoverable: '+name);return ctx.agents.withInitiator(who,()=>tool.execute(args,{signal}));}
 async function human(method,payload,headers={'x-synthetic-human':'yes'}){const response=await fetch(`http://127.0.0.1:${server.address().port}/maimi-service/${method}`,{method:'POST',headers:{'content-type':'application/json',...headers},body:JSON.stringify({type:'client-request',rpcId:A,method,payload})});return response.status===200?(await response.json()).result:{ok:false,status:response.status};}
 return {f,ctx,policy,a,b,call,human,connection,close:async()=>{for(const fiber of fibers.reverse())await fiber.dispose();if(server){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await connection.browserPeer.dispose();for(const item of connection.peers.values())await item.peer.dispose();}await f.close();await rm(home,{recursive:true,force:true});}};
}
test('real latest ToolRuntime discovers enterprise tools and native AgentRegistry reaches loopback connector',async()=>{const h=await host();try{assert.equal(h.ctx.tools.view(h.a).visible.size,5);const result=await h.call(h.a,'maimi_enterprise_catalog');assert.equal(result.structuredContent.items[0].plugin_key,'logistics-query');const read=await h.call(h.a,'maimi_enterprise_call',{connector:'logistics-query',action:'query',arguments:{order_codes:['SYNTHETIC']}});assert.equal(read.structuredContent.status,'ok');}finally{await h.close();}});
test('real Session accepted attachment and real AttachmentStore Office roundtrip',async()=>{const h=await host();try{const bytes=Buffer.from('synthetic input');async function* source(){yield bytes;}
 const ref=await h.ctx.attachments.saveFileStream({name:'input.txt',data:source()});
 h.a.session.append('step/start',{step:0});h.a.session.append('user/message',{source:{kind:'user',rpcId:'33333333-3333-4333-8333-333333333333'},content:[{type:'file',attachment:ref}]},{surfaceOp:'append'});
 const listed=await h.call(h.a,'maimi_enterprise_attachments');assert.equal(listed.structuredContent.items.length,1);
 const result=await h.call(h.a,'maimi_office_remote',{operation:'convert',input_ids:[listed.structuredContent.items[0].id],output_extension:'.pdf'});
 const out=result.structuredContent.attachments[0];assert.equal(out.name,'output.pdf');const chunks=[];for await(const chunk of h.ctx.attachments.readFileStream(out))chunks.push(Buffer.from(chunk));assert.equal(Buffer.concat(chunks).toString(),'synthetic output');
}finally{await h.close();}});
test('model forged file reference and another native session cannot use accepted input',async()=>{const h=await host();try{async function* source(){yield Buffer.from('synthetic');}const ref=await h.ctx.attachments.saveFileStream({name:'input.txt',data:source()});
 h.a.session.append('step/start',{step:0});h.a.session.append('user/message',{source:{kind:'tool'},content:[{type:'file',attachment:ref}]},{surfaceOp:'append'});assert.equal((await h.call(h.a,'maimi_enterprise_attachments')).structuredContent.items.length,0);
 h.a.session.append('user/message',{source:{kind:'user',rpcId:'33333333-3333-4333-8333-333333333333'},content:[{type:'file',attachment:ref}]},{surfaceOp:'append'});
 const listed=await h.call(h.a,'maimi_enterprise_attachments');await assert.rejects(h.call(h.b,'maimi_office_remote',{operation:'inspect',input_ids:[listed.structuredContent.items[0].id]}),/FILE_NOT_ACCEPTED_IN_SESSION/);
}finally{await h.close();}});
test('missing live initiator and model-supplied session fields do not grant a native scope',async()=>{const h=await host();try{const tool=h.ctx.tools.view(h.a).visible.get('maimi_enterprise_catalog');await assert.rejects(tool.execute({session_id:A},{signal:new AbortController().signal}),/LIVE_NATIVE_AGENT_REQUIRED/);await assert.rejects(h.call({...h.a},'maimi_enterprise_catalog'),/LIVE_NATIVE_AGENT_REQUIRED/);await assert.rejects(h.call(h.a,'maimi_enterprise_catalog',{session_id:B}),/ARGUMENTS_INVALID/);}finally{await h.close();}});
test('changing human selection revokes prior input grants and connector availability',async()=>{const h=await host();try{await h.call(h.a,'maimi_enterprise_catalog');h.policy.revision++;h.policy.selected=[];const result=await h.call(h.a,'maimi_enterprise_catalog');assert.equal(result.structuredContent.items.length,0);await assert.rejects(h.call(h.a,'maimi_enterprise_call',{connector:'logistics-query',action:'query',arguments:{}}),/CONNECTOR_NOT_SELECTED/);}finally{await h.close();}});
