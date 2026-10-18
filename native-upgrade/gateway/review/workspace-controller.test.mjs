import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire,registerHooks} from 'node:module';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const HERE=path.dirname(fileURLToPath(import.meta.url));
const runtime=process.env.DSH_R65_RUNTIME||path.resolve(HERE,'../../upstream/runtime-win');
const require=createRequire(path.join(runtime,'package.json'));
const pkg=name=>import(pathToFileURL(require.resolve(name)).href);
registerHooks({resolve(specifier,context,next){
 if(specifier.startsWith('/opt/dsh/node_modules/'))return next(pathToFileURL(path.join(runtime,'node_modules',specifier.slice('/opt/dsh/node_modules/'.length))).href,context);
 return next(specifier,context);
}});
const [{Context,Service},{Peer},{default:Gateway},{default:Registry},{default:RemoteAccess},{TYPERT},
 {default:CompanyWorkspace}]=await Promise.all([
 pkg('@deepseek-ai/cordis'),pkg('@deepseek-ai/dsh-client-connection'),pkg('@deepseek-ai/dsh-api-gateway'),
 pkg('@deepseek-ai/dsh-typert-registry'),pkg('@deepseek-ai/dsh-api-remote-access'),
 pkg('@deepseek-ai/dsh-api-workspace-controller/typert'),
 import(pathToFileURL(path.resolve(HERE,'../../web/company-workspace-controller.mjs')).href)]);

test('real generated workspace Remote dispatch retains subclass path guard, fixed title and default disable',async()=>{
 const ctx=new Context(),fibers=[],calls=[];
 const workspace={id:'11111111-1111-4111-8111-111111111111',path:'/workspace',title:'Original',sessionIds:[],
  createdAt:'2026-10-18T00:00:00.000Z',updatedAt:'2026-10-18T00:00:00.000Z',
  async setTitle(title){calls.push(['rename',title]);this.title=title;}};
 class Storage extends Service{
  constructor(ctx){super(ctx,'workspaceRegistry');this.archivedSessionIds=[];this.pinnedSessionIds=[];}
  list(){return [workspace];}
  get(id){return id===workspace.id?workspace:undefined;}
  resolveByPath(dir){calls.push(['resolve',dir]);return undefined;}
  create(dir){calls.push(['create',dir]);return workspace;}
  delete(){calls.push(['delete']);return true;}
  initializeDefault(){calls.push(['initializeDefault']);return workspace;}
 }
 const use=async(plugin,config)=>{const f=ctx.plugin(plugin,config);fibers.push(f);await f;};
 try{
  await use(Registry);await use(RemoteAccess);await use(Gateway);await use(Storage);
  ctx.typert.register(TYPERT);
  await use(CompanyWorkspace);
  const peer=new Peer(ctx,{principal:{kind:'owner'},interactions:'none'});
  const invoke=(method,request)=>ctx.typertGateway.invoke({peer,namespace:'workspace',method,args:request===undefined?{}:{request}});
  for(const dir of ['/tmp','/workspace/..','/workspace/child','C:\\outside'])
   await assert.rejects(invoke('create',{path:dir}),e=>e.code==='workspace/cloud-fixed');
  assert.deepEqual(calls,[],'forbidden paths never reach registry even without HTTP wrapper');
  assert.equal((await invoke('create',{path:'/workspace'})).created,true);
  assert.deepEqual(calls,[['resolve','/workspace'],['create','/workspace']]);
  await assert.rejects(invoke('rename',{workspaceId:workspace.id,title:'Other'}),e=>e.code==='workspace/cloud-fixed');
  assert.equal((await invoke('rename',{workspaceId:workspace.id,title:'云端工作区'})).workspace.title,'云端工作区');
  await assert.rejects(invoke('delete',{workspaceId:workspace.id}),e=>e.code==='workspace/cloud-fixed');
  assert.equal(await invoke('initializeDefault'),undefined);
  assert.equal(calls.some(call=>['delete','initializeDefault'].includes(call[0])),false);
  const outsider=new Peer(ctx,{principal:{kind:'fixture-outsider'},interactions:'none'});
  await assert.rejects(ctx.typertGateway.invoke({peer:outsider,namespace:'workspace',method:'create',args:{request:{path:'/workspace'}}}),e=>e.code==='remote-access/denied');
  await outsider.dispose();await peer.dispose();
 }finally{for(const fiber of fibers.reverse())await fiber.dispose();}
});
