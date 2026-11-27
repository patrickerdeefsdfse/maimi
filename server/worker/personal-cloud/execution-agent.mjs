// This entry point is selected only by the trusted execution-pool deployment.
import http from 'node:http';
import {timingSafeEqual,createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {ExecutionRPC} from './execution-rpc-core.mjs';
import {taskFileService} from './task-files.mjs';
import {isRead} from './execution-outcome.mjs';
import {IdleController,holdIdleRequest} from './idle-control.mjs';
import {readWorkerIdentity,workerReady} from './worker-identity.mjs';
const require=createRequire((process.env.MAIMI_DSH_RUNTIME_ROOT||'/opt/dsh')+'/package.json');
const mod=async name=>import(pathToFileURL(require.resolve(name)).href);
const [{Context},{LocalFileSystem},{LocalSubprocessRuntime}]=await Promise.all([mod('@deepseek-ai/cordis'),mod('@deepseek-ai/dsh-fs-local'),mod('@deepseek-ai/dsh-subprocess-local')]);
const token=process.env.CLOUD_ROUTE_TOKEN||'',identity=readWorkerIdentity(process.env),owner=process.env.CLOUD_OWNER_KEY;
if(!/^[A-Za-z0-9_-]{43}$/.test(token)||!/^[a-f0-9]{32}$/.test(owner)||process.env.CLOUD_MODEL_LEASE||process.env.CLOUD_MODEL_BASE_URL)throw Error('EXECUTION_BOOTSTRAP_REFUSED');
const ctx=new Context();const fibers=[ctx.plugin(LocalFileSystem,{cwd:'/workspace',diffBasisMaxBytes:1024*1024}),ctx.plugin(LocalSubprocessRuntime)];await Promise.all(fibers.map(f=>f.await()));
const rpc=new ExecutionRPC({fs:ctx.fs,subprocess:ctx.subprocess});
{
 const {executionDocumentTool}=await import('file:///opt/dsh/plugins/managed/r73/files/index.mjs');
 const documentFS={
  resolve:async(path,options)=>rpc.perform('fs.resolve',{path},''),
  stat:async(target,signal)=>ctx.fs.stat(await rpc.target(target),signal),
  readBytes:async(target,signal,maxBytes)=>{if(maxBytes>24*1024*1024)throw Error('DOCUMENT_SIZE_LIMIT');return ctx.fs.readBytes(await rpc.target(target),signal,maxBytes);}
 };
 const tool=executionDocumentTool(new Proxy(ctx,{get:(target,key)=>key==='fs'?documentFS:Reflect.get(target,key)}));
 rpc.readDocument=(args,{sessionId})=>tool.execute(args,{signal:AbortSignal.timeout(45000),agent:{session:{header:{id:sessionId,cwd:'/workspace'}}}});
}
const taskFiles=taskFileService({busy:()=>rpc.busy(),identity:identity.generation});
const idle=new IdleController({sample:async()=>({protocol:2,busy:rpc.busy()||!await taskFiles.fulfilled(),activeRoots:[]}),localBusy:()=>rpc.busy()});
const nativeId='execution-'+createHash('sha256').update(owner).digest('hex').slice(0,32);
async function json(req){const chunks=[];let length=0;for await(const b of req){length+=b.length;if(length>12*1024*1024)throw Error('EXECUTION_BODY_TOO_LARGE');chunks.push(b);}return JSON.parse(Buffer.concat(chunks));}
const server=http.createServer(async(req,res)=>{
 let hold;try{
  const auth=String(req.headers.authorization||''),expected='Bearer '+token;
  if(auth.length!==expected.length||!timingSafeEqual(Buffer.from(auth),Buffer.from(expected))||req.headers.origin||req.headers['sec-fetch-site']){res.writeHead(403);res.end();return;}
  let value;
  if(req.method==='GET'&&req.url==='/__cloud/ready')value=workerReady(nativeId,identity);
  else if(req.method==='GET'&&req.url==='/__cloud/activity')value=await idle.busy();
  else if(req.method==='POST'&&req.url==='/__cloud/idle-release')value=await idle.drain();
  else if(req.method==='POST'&&req.url==='/__cloud/idle-resume')value=idle.resume();
  else if(req.method==='POST'&&req.url==='/__cloud/task-files'){
   if(process.env.MAIMI_TEMPORARY_TURN!=='1'){res.writeHead(403);res.end();return;}
   hold=holdIdleRequest(idle,req,res,{work:true});await taskFiles.handle(req,res);return;
  }
  else if(req.method==='POST'&&req.url==='/__cloud/execute'){
   hold=holdIdleRequest(idle,req,res,{work:true});
   const body=await json(req);
   try{value=await rpc.dispatch(body);}
   catch(error){
    if(isRead(body?.operation)&&!/^[A-Z_]{3,80}$/.test(error?.code||''))throw Object.assign(Error('EXECUTION_READ_FAILED'),{code:'EXECUTION_READ_FAILED'});
    throw error;
   }
  }
  else {res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(req.url==='/__cloud/execute'?{value}:value));
 }catch(error){
  const rejectedByIdleFence=req.method==='POST'&&req.url==='/__cloud/execute'&&!hold&&error?.code==='cloud-idle-release';
  // This code is reserved for rejection before reading or dispatching an RPC.
  const workerCode=/^[A-Z_]{3,80}$/.test(error?.code||'')&&error.code!=='EXECUTION_DRAIN_BEFORE_DISPATCH'
   ?error.code:'EXECUTION_UNAVAILABLE';
  res.writeHead(409,{'Content-Type':'application/json','Cache-Control':'no-store'});
  res.end(JSON.stringify({code:rejectedByIdleFence?'EXECUTION_DRAIN_BEFORE_DISPATCH':workerCode}));
 }
 finally{hold?.handlerDone();}
});
const port=Number(process.env.MAIMI_EXECUTION_LISTEN_PORT||3084);if(!Number.isSafeInteger(port)||port<1024||port>65535)throw Error('EXECUTION_PORT_INVALID');
server.requestTimeout=30000;server.headersTimeout=10000;server.listen(port,'0.0.0.0');
async function stop(){server.close();await rpc.close();await ctx.fiber.dispose();}
process.once('SIGTERM',()=>void stop());process.once('SIGINT',()=>void stop());
