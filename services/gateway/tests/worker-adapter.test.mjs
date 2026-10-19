import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire, registerHooks} from 'node:module';
import {mkdtemp, rm, readFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createServer} from 'node:http';

const HERE=path.dirname(fileURLToPath(import.meta.url));
const runtime=process.env.DSH_R65_RUNTIME || path.resolve(HERE,'../../upstream/runtime-win');
const localAdapters=path.resolve(HERE,'../worker-adapter');
const adapterRoot=process.env.DSH_R65_ADAPTER_DIR || (existsSync(localAdapters)?localAdapters:
  path.resolve(HERE,'../../../runtime-overlays/worker/opt/personal-cloud'));
const adapter=name=>import(pathToFileURL(path.join(adapterRoot,name)).href);
const requireRuntime=createRequire(path.join(runtime,'package.json'));
// Production plugins keep the same /opt/dsh boundary. Tests relocate only this
// known installation prefix; source code is imported unchanged.
registerHooks({resolve(specifier,context,next){
  if(specifier.startsWith('/opt/dsh/node_modules/'))
    return next(pathToFileURL(path.join(runtime,'node_modules',specifier.slice('/opt/dsh/node_modules/'.length))).href,context);
  return next(specifier,context);
}});
const pkg=name=>import(pathToFileURL(requireRuntime.resolve(name)).href);
const [{Context,Service},{Peer,HostConnectionService},{default:Gateway},{default:Registry},{default:RemoteAccess},
  {bindTypertRemote},{default:Agents},{default:Llm},PiAi,{LocalCredentialProvider},
  {Session},{default:LocalAttachments}]=await Promise.all([
  '@deepseek-ai/cordis','@deepseek-ai/dsh-client-connection','@deepseek-ai/dsh-api-gateway',
  '@deepseek-ai/dsh-typert-registry','@deepseek-ai/dsh-api-remote-access','@deepseek-ai/dsh-typert-protocol',
  '@deepseek-ai/dsh-agent','@deepseek-ai/dsh-llm','@deepseek-ai/dsh-llm-pi-ai',
  '@deepseek-ai/dsh-credentials-local','@deepseek-ai/dsh-session','@deepseek-ai/dsh-attachment-local'].map(pkg));
const Control=await adapter('task-control-plugin.mjs');
const Policy=await adapter('enterprise-model-policy.mjs');
const {createNativeModelScopes,scopedCredentialProvider}=await adapter('native-model-scope.mjs');
const Files=await adapter('project-files-plugin.mjs');
const {validateNativeRpc}=await adapter('task-authority.mjs');
const A='11111111-1111-4111-8111-111111111111', B='22222222-2222-4222-8222-222222222222';
const D='33333333-3333-4333-8333-333333333333', R='44444444-4444-4444-8444-444444444444';
const TOKEN='mmt_'+'S'.repeat(43);
const syntheticAgent=(id,ctx)=>({id,ctx,session:Session.create(id),status:'idle',inbox:{nextTurn:[],nextStep:[],remove(){}},cancel(){}});
async function host(){
  const ctx=new Context(),fibers=[];
  return {ctx,use:async(plugin,config)=>{const f=ctx.plugin(plugin,config);fibers.push(f);await f;return f;},
    close:async()=>{for(const f of fibers.reverse())await f.dispose();}};
}

test('native gateway rejects the old peerless invocation and respects disposed peers',async()=>{
  const h=await host();
  try{
    await h.use(Registry);await h.use(RemoteAccess);await h.use(Gateway);
    await assert.rejects(h.ctx.typertGateway.invoke({namespace:'session',method:'prompt',args:{request:{}}}),TypeError);
    const peer=new Peer(h.ctx,{principal:{kind:'owner'},interactions:'none'});
    await peer.dispose();assert.equal(peer.signal.aborted,true);
    await assert.rejects(h.ctx.typertGateway.invoke({peer,namespace:'session',method:'prompt',args:{request:{}}}),e=>e.code==='gateway/peer-disposed');
  }finally{await h.close();}
});

test('worker media-type gate agrees with actual native parser, including mixed case and binary upload',async()=>{
  const source=await readFile(path.join(adapterRoot,'worker.mjs'),'utf8');
  const gateLine=source.split('\n').find(line=>line.includes(".test(path)&&String(req.headers['content-type']"));
  assert.ok(gateLine);
  const condition=gateLine.trim().slice(3,-2);
  const gated=new Function('req','path','return ('+condition+');');
  const ctx=new Context(),connection=new HostConnectionService(ctx,[],{});
  const remove=connection.rpc.intercept('/api',()=>true,async()=>({ok:true,value:'synthetic accepted'}));
  const handler=connection.createSharedFetchHandler('/api');
  try{
    const endpoint='/api/workspace/create',body={type:'client-request',rpcId:R,method:'workspace/create',payload:{args:{request:{path:'/outside-workspace'}}}};
    for(const type of ['application/json','Application/JSON','APPLICATION/JSON; charset=utf-8',' application/json ; charset=utf-8']){
      const response=await handler.fetch(new Request('http://127.0.0.1'+endpoint,{method:'POST',headers:{'content-type':type},body:JSON.stringify(body)}),connection.browserPeer);
      assert.equal(response.status,200,'native parser accepts '+type);
      assert.equal(gated({method:'POST',headers:{'content-type':type}},endpoint),true,'wrapper gates '+type);
      assert.throws(()=>validateNativeRpc(endpoint,'POST',body,A),/本人的云端工作区/);
    }
    const oldGate=String('Application/JSON').split(';')[0]==='application/json';
    assert.equal(oldGate,false,'old case-sensitive condition skipped an accepted native request');
    connection.fetch.register({path:'/api/session/uploadFileBinary',methods:['POST'],requestBody:'streaming',fetch:async()=>new Response('synthetic',{status:200})});
    const binaryType='application/octet-stream';
    assert.equal(gated({method:'POST',headers:{'content-type':binaryType}},'/api/session/uploadFileBinary'),false);
    assert.equal((await handler.fetch(new Request('http://127.0.0.1/api/session/uploadFileBinary',{method:'POST',headers:{'content-type':binaryType},body:'synthetic'}),connection.browserPeer)).status,200);
  }finally{await remove();await connection.browserPeer.dispose();}
});

test('private task controller uses actual native Peer and Gateway for prompt/cancel, with generation fencing',async()=>{
  const h=await host(),calls=[],routes=[];
  class Connection extends Service {
    constructor(ctx){super(ctx,'connection');this.fetch={register:route=>{routes.push(route);return()=>{};}};}
  }
  class Credentials extends Service { constructor(ctx){super(ctx,'credentials');} }
  class SessionBoundary extends Service {
    constructor(ctx){super(ctx,'testSessionBoundary');this.typertRemote=bindTypertRemote(this,'testSessionBoundary',{namespace:'session'});}
    prompt(request){calls.push({method:'prompt',peer:this.ctx.invocation.peer,request});return {status:'accepted'};}
    cancel(request){calls.push({method:'cancel',peer:this.ctx.invocation.peer,request});return {status:'cancelled'};}
  }
  try{
    await h.use(Registry);await h.use(RemoteAccess);await h.use(Gateway);await h.use(Agents);
    await h.use(Llm);await h.use(Connection);await h.use(SessionBoundary);
    await h.use(Credentials);
    h.ctx.typert.register({package:'@fixture/r65-controller',face:'host',schemas:[],model:{services:[],events:[],objects:[]},
      invocations:['prompt','cancel'].map(method=>({id:'@fixture/r65-controller#session/'+method,
        service:'testSessionBoundary',namespace:'session',method,invocation:{kind:'direct'},
        parameters:[{name:'request',wire:'request',source:'json',codec:{mode:'src-json'}}],result:{mode:'src-json'}}))});
    const fiber=await h.use(Control,{endpoint:'http://127.0.0.1:3086/__megmeet/scope',token:'T'.repeat(43)});
    assert.equal(routes.length,1);assert.equal(routes[0].path,'/api/megmeet-cloud/task-state');
    const send=body=>routes[0].fetch(new Request('http://127.0.0.1/api/megmeet-cloud/task-state',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}));
    assert.equal((await send({session_id:A,generation:1,claim:true})).status,200);
    const body={session_id:A,generation:1,native_prompt:true,dispatch_id:D,request:{sessionId:A,requestId:R,content:[]},
      peer:{principal:{kind:'untrusted-from-browser'}}};
    assert.equal((await (await send(body)).json()).result.ok,true);
    assert.equal(calls.length,1);assert.equal(calls[0].peer instanceof Peer,true);
    assert.equal(calls[0].peer.principal.kind,'owner');assert.equal(calls[0].peer.interactions,'none');
    assert.equal((await (await send(body)).json()).result.ok,true);assert.equal(calls.length,1);
    assert.equal((await send({...body,generation:2})).status,409);
    assert.equal((await send({...body,session_id:B})).status,409);
    const cancelled=await send({session_id:A,generation:1,native_cancel:true,cancel_id:D});
    assert.equal((await cancelled.json()).result.ok,true);assert.equal(calls.length,2);
    assert.equal(calls[1].peer,calls[0].peer);
    await fiber.dispose();assert.equal(calls[0].peer.signal.aborted,true);
  }finally{await h.close();}
});

async function modelHost(){
  const h=await host(),home=await mkdtemp(path.join(tmpdir(),'maimi-r65-model-'));
  const received=[],issued=[],released=[];
  const server=createServer(async(req,res)=>{
    let text='';for await(const chunk of req)text+=chunk;
    received.push({authorization:req.headers.authorization,body:JSON.parse(text)});
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.end('data: '+JSON.stringify({choices:[{delta:{role:'assistant',content:'synthetic'},index:0,finish_reason:null}]})+'\n\n'+
      'data: '+JSON.stringify({choices:[{delta:{},index:0,finish_reason:'stop'}],usage:{prompt_tokens:1,completion_tokens:1}})+'\n\ndata: [DONE]\n\n');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port+'/v1';
  await h.use(Agents);
  const roots=[syntheticAgent(A,h.ctx),syntheticAgent(B,h.ctx)];
  const detach=roots.map(agent=>h.ctx.agents.enter(agent,undefined));
  const scopes=createNativeModelScopes({agents:h.ctx.agents,issueCredential:scope=>{issued.push(scope);return TOKEN;},releaseCredential:token=>released.push(token)});
  await h.use(scopedCredentialProvider(LocalCredentialProvider,scopes),{dshHome:home,watch:false});
  await h.use(Llm);
  h.ctx.on('llm/stream',(options,next)=>scopes.intercept(options,next),{global:true,prepend:true});
  await h.use(PiAi,{providers:{'deepseek-official':{api:'openai-completions',baseURL:base,apiKeyEnv:'MEGMEET_MODEL_TOKEN',
    models:[{id:'deepseek-v4.1-flash',contextWindow:32000,maxTokens:1024,input:['text']}]}}});
  await h.use(Policy,{apiKeyEnv:'MEGMEET_MODEL_TOKEN',modelBase:base});
  const close=h.close;
  return {...h,scopes,issued,released,received,base,
    close:async()=>{detach.reverse().forEach(fn=>fn());await close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
      await rm(home,{recursive:true,force:true});}};
}

test('native LLM prepareCall preserves configure callback and managed model selection',async()=>{
  const h=await modelHost();
  try{
    let called=0;
    const result=await h.ctx.llm.prepareCall({provider:'legacy',model:'old'},undefined,(controls,info)=>{
      called++;assert.equal(info.id,'deepseek-v4.1-flash');return {...controls,maxTokens:37};});
    assert.equal(called,1);assert.equal(result.config.maxTokens,37);
    assert.equal(result.config.provider,'deepseek-official');assert.equal(result.config.model,'deepseek-v4.1-flash');
    const abort=new AbortController();abort.abort(Error('test-abort'));
    await assert.rejects(h.ctx.llm.prepareCall({provider:'legacy',model:'old'},abort.signal,()=>{throw Error('unreachable');}),/test-abort/);
  }finally{await h.close();}
});

test('native latest credentials use task-scoped key, reject writes and cross-session/revoked calls',async()=>{
  const h=await modelHost();
  try{
    const request={provider:'deepseek-official',model:'deepseek-v4.1-flash',sessionId:A,
      messages:[{role:'user',content:[{type:'text',text:'synthetic'}]}]};
    const drain=async iterable=>{for await(const ignored of iterable)void ignored;};
    await assert.rejects(h.ctx.credentials.resolve('MEGMEET_MODEL_TOKEN'),/CLOUD_MODEL_STREAM_SCOPE_REQUIRED/);
    assert.throws(()=>h.ctx.credentials.set('MEGMEET_MODEL_TOKEN','synthetic'),/CLOUD_MODEL_CREDENTIAL_READ_ONLY/);
    assert.throws(()=>h.ctx.credentials.unset('MEGMEET_MODEL_TOKEN'),/CLOUD_MODEL_CREDENTIAL_READ_ONLY/);
    assert.equal(typeof h.ctx.credentials.readRecord,'function');assert.equal(typeof h.ctx.credentials.modifyRecord,'function');
    h.scopes.claim(A,1);
    await h.scopes.runPrompt(A,1,{sessionId:A},()=>drain(h.ctx.llm.stream(request)));
    assert.equal(h.received.length,1);assert.equal(h.received[0].authorization,'Bearer '+TOKEN);
    assert.equal(h.issued[0].rootSessionId,A);assert.equal(h.released.length,1);assert.equal(h.scopes.busy(),false);
    assert.throws(()=>h.scopes.runPrompt(A,1,{sessionId:A},()=>h.ctx.llm.stream({...request,sessionId:B})),/CLOUD_MODEL_CROSS_SESSION/);
    h.scopes.revoke(A,1);
    assert.throws(()=>h.scopes.runPrompt(A,1,{sessionId:A},()=>h.ctx.llm.stream(request)),/CLOUD_MODEL_GENERATION_REVOKED/);
    assert.equal(h.received.length,1);
  }finally{await h.close();}
});

test('latest native AttachmentStore and Session preserve explicit user input proof and generated file roundtrip',async()=>{
  const h=await host(),home=await mkdtemp(path.join(tmpdir(),'maimi-r65-files-'));
  try{
    await h.use(LocalAttachments,{dshHome:home});
    const data=Buffer.from('synthetic,office\n1,2\n');
    async function* chunks(){yield data;}
    const ref=await h.ctx.attachments.saveFileStream({name:'synthetic.csv',data:chunks()});
    const session=Session.create(A);
    session.append('step/start',{step:0});
    session.append('user/message',{source:{kind:'user',rpcId:R},content:[{type:'file',attachment:ref}]},{surfaceOp:'append'});
    const ctx={sessions:{get:id=>id===A?session:undefined},attachments:h.ctx.attachments};
    const found=await Files.dispatch(ctx,{operation:'list',session_id:A});
    assert.equal(found.items.length,1);assert.equal(found.items[0].ref.attachmentId,ref.attachmentId);
    const step=await Files.dispatch(ctx,{operation:'current-step',session_id:A});
    assert.equal(step.messages[0].request_id,R);assert.equal(step.messages[0].items.length,1);
    const read=await Files.dispatch(ctx,{operation:'read',session_id:A,kind:'file',ref,maximum:1024});
    assert.deepEqual(Buffer.from(read.data_base64,'base64'),data);
    await assert.rejects(Files.dispatch(ctx,{operation:'read',session_id:A,kind:'file',ref:{...ref,name:'not-user-selected.csv'},maximum:1024}),e=>e.code==='project_attachment_not_in_session');
    const saved=await Files.dispatch(ctx,{operation:'save',session_id:A,name:'result.csv',data_base64:data.toString('base64')});
    assert.equal(saved.ref.attachmentId,ref.attachmentId);
    session.append('user/message',{source:{kind:'tool'},content:[{type:'file',attachment:{...ref,name:'forged.csv'}}]},{surfaceOp:'append'});
    assert.equal((await Files.dispatch(ctx,{operation:'list',session_id:A})).items.length,1);
  }finally{await h.close();await rm(home,{recursive:true,force:true});}
});
