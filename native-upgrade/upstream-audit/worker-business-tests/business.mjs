// Synthetic local-only integration through the unchanged authenticated worker.
import http from 'node:http';
import {readFile,writeFile} from 'node:fs/promises';
import {createHash,randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';

const token=process.env.CLOUD_ROUTE_TOKEN,lease=process.env.CLOUD_MODEL_LEASE;
const base='http://127.0.0.1:3084',headers={Authorization:'Bearer '+token,'X-Megmeet-Client-Lease':lease};
const counts={},modelCalls=[],checkpoints=[];let streamingResponse=null,streamClosed=false;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const gateway=http.createServer(async(req,res)=>{
 try{
  counts[req.url]=(counts[req.url]||0)+1;
  assert.equal(req.headers.authorization,'Bearer '+lease);
  if(req.url==='/internal/personal-cloud/model/v1/session')return void res.end(JSON.stringify({status:'ready'}));
  if(req.url==='/internal/personal-cloud/model/v1/chat/completions'){
   let raw='';for await(const b of req)raw+=b;const body=JSON.parse(raw);
   assert.equal(body.model,'deepseek-v4.1-flash');assert.equal(body.stream,true);
   const content=JSON.stringify(body.messages);const holding=JSON.stringify(body.messages.filter(m=>m.role==='user').at(-1)).includes('R65_HOLD_CANCEL');
   modelCalls.push({holding,model:body.model,stream:body.stream,synthetic_marker:content.includes('R65_'),has_file_path:content.includes('synthetic-r65.txt')});
   res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});
   const chunk=(delta,finish_reason=null)=>res.write('data: '+JSON.stringify({id:'r65-synthetic-'+modelCalls.length,object:'chat.completion.chunk',created:Math.floor(Date.now()/1000),model:body.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');
   chunk({role:'assistant',content:''});
   if(holding){streamingResponse=res;res.on('close',()=>{streamClosed=true;});chunk({content:'R65_CANCEL_STARTED'});return;}
   chunk({content:'R65_SYNTHETIC_REPLY'});chunk({},'stop');
   res.write('data: '+JSON.stringify({id:'r65-synthetic',object:'chat.completion.chunk',created:Math.floor(Date.now()/1000),model:body.model,choices:[],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}})+'\n\n');
   res.end('data: [DONE]\n\n');return;
  }
  throw Error('UNEXPECTED_SYNTHETIC_ROUTE:'+req.url);
 }catch(error){res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:{message:error.message}}));}
});
await new Promise((resolve,reject)=>gateway.once('error',reject).listen(8080,'127.0.0.1',resolve));
async function rpc(method,request,customHeaders={}){
 const rpcId=randomUUID();
 const args=method==='session/list'?{_request:request}:{request};
 const res=await fetch(base+'/api/'+method,{method:'POST',headers:{...headers,'Content-Type':'application/json',...customHeaders},body:JSON.stringify({type:'client-request',rpcId,method,payload:{args}}),signal:AbortSignal.timeout(20000)});
 const raw=await res.text();let data;try{data=JSON.parse(raw);}catch{throw Error(method+' HTTP '+res.status+' '+raw.slice(0,500));}assert.equal(data.rpcId,rpcId,JSON.stringify(data));
 return data.result;
}
async function good(method,request){const result=await rpc(method,request);assert.equal(result?.ok,true,method+': '+JSON.stringify(result));return result.value;}
async function events(sessionId){const p=await good('session/projections',{sessionId});assert.equal(p.kind,'sequenced');return (await good('session/page',{address:{kind:'session',sessionId},throughSeq:p.asOfSeq,maxMessages:200})).records.map(x=>x.event);}
async function waitFor(condition,label,seconds=20){const start=Date.now();for(;;){if(await condition())return;if(Date.now()-start>seconds*1000)throw Error('TIMEOUT_'+label);await sleep(150);}}
async function idle(sessionId){return (await good('session/list',{})).items.find(x=>x.sessionId===sessionId)?.running===false;}
function prompt(sessionId,marker,extra=[]){return good('session/prompt',{sessionId,requestId:randomUUID(),mode:'queue',content:[{type:'text',text:marker},...extra],clientTimeZone:'Asia/Shanghai'});}
const result={schema:'r65-worker-business-local-v1',runtime:'0.2.1-rc.2',company_requests:0,network:'none',credentials:'SYNTHETIC_ONLY',official_packages_modified:false};
try{
 const workspaces=await good('workspace/create',{path:'/workspace'});checkpoints.push({stage:'workspace-resolve',shape:workspaces});
 const ws=workspaces.workspace;assert.ok(ws?.workspaceId,'workspace id');
 const invalid=await rpc('session/create',{workspaceId:ws.workspaceId},{'X-Megmeet-Client-Lease':'invalid'});
 assert.equal(invalid.ok,false);result.invalid_lease_rejected=true;
 const {sessionId}=await good('session/create',{workspaceId:ws.workspaceId});checkpoints.push({stage:'created',sessionId});
 await prompt(sessionId,'R65_FIRST_PROMPT');
 await waitFor(async()=>JSON.stringify(await events(sessionId)).includes('R65_SYNTHETIC_REPLY'),'first-reply');
 await waitFor(()=>idle(sessionId),'first-idle');
 result.create_prompt_model_sse_history=true;checkpoints.push({stage:'first',events:await events(sessionId)});
 const data=Buffer.from('R65 synthetic attachment\n测试文件内容\nAll data in this test is fabricated.\n','utf8');
 const name='synthetic-r65.txt',url=base+'/api/session/uploadFileBinary?'+new URLSearchParams({sessionId,name,filename:name,size:String(data.length)});
 const uploadResponse=await fetch(url,{method:'POST',headers:{...headers,'Content-Type':'application/octet-stream','Content-Length':String(data.length)},body:data});
 const uploaded=await uploadResponse.json();assert.equal(uploaded.ok,true,JSON.stringify(uploaded));
 const {file,receiptId}=uploaded.value,digest=createHash('sha256').update(data).digest('hex');
 assert.equal(file.attachmentId,'sha256:'+digest);assert.equal(file.bytes,data.length);assert.equal(file.name,name);
 assert.deepEqual(await readFile('/state/dsh/attachments/v1/files/'+digest.slice(0,2)+'/'+digest+'/'+name),data);
 await prompt(sessionId,'R65_FILE_PROMPT',[{type:'file',receiptId}]);
 await waitFor(async()=>{const es=await events(sessionId);return es.filter(e=>e.type==='assistant/message').length>=2;},'file-reply');
 await waitFor(()=>idle(sessionId),'file-idle');
 const fileEvents=await events(sessionId);assert.ok(JSON.stringify(fileEvents).includes(file.attachmentId));
 result.upload_sha256_exact=true;result.upload_bytes_and_name_preserved=true;result.file_receipt_in_accepted_history=true;
 result.file_path_reached_model=modelCalls.some(x=>x.has_file_path);assert.equal(result.file_path_reached_model,true);
 checkpoints.push({stage:'file',events:fileEvents});
 await prompt(sessionId,'R65_HOLD_CANCEL');await waitFor(()=>streamingResponse!==null,'held-stream');
 const deniedMutation=await rpc('session/cancel',{sessionId},{'X-Megmeet-Client-Lease':'mcl_'+'C'.repeat(43)});
 assert.equal(deniedMutation.ok,false);assert.equal(streamClosed,false);result.foreign_lease_cannot_cancel=true;
 const canceled=await good('session/cancel',{sessionId});assert.equal(canceled.accepted,true);
 await waitFor(()=>streamClosed,'upstream-cancel');await waitFor(()=>idle(sessionId),'canceled-idle');
 result.cancel_acknowledged_and_upstream_closed=true;checkpoints.push({stage:'cancel',events:await events(sessionId)});
 // Fresh follow-up uses a new generation after the cancellation acknowledgement.
 await prompt(sessionId,'R65_AFTER_CANCEL');await waitFor(async()=>{const es=await events(sessionId);return es.filter(e=>e.type==='assistant/message').length>=3;},'after-cancel-reply');
 await waitFor(()=>idle(sessionId),'after-cancel-idle');result.followup_after_cancel=true;
 result.status='PASSED';
}catch(error){result.status='FAILED';result.error=String(error?.stack||error);}
finally{
 result.gateway_request_counts=counts;result.model_requests=modelCalls.length;result.model_request_metadata=modelCalls;
 await writeFile('/tmp/business-result.json',JSON.stringify(result,null,2));
 await writeFile('/tmp/business-checkpoints.json',JSON.stringify(checkpoints,null,2));
 streamingResponse?.destroy();gateway.closeAllConnections();await new Promise(resolve=>gateway.close(resolve));
 console.log(JSON.stringify(result));if(result.status!=='PASSED')process.exitCode=1;
}
