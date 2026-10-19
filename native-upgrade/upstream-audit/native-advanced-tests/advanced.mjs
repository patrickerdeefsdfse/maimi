// Actual complete native worker; fabricated prompts/model/file only, network none.
import http from 'node:http';
import {readFile,writeFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const require=createRequire('/opt/dsh/package.json'),WebSocket=require('ws');
const token=process.env.CLOUD_ROUTE_TOKEN,lease=process.env.CLOUD_MODEL_LEASE;
const base='http://127.0.0.1:3084',headers={Authorization:'Bearer '+token,'X-Megmeet-Client-Lease':lease};
const counts={},calls=[],checkpoints=[],frames=[],held=new Set();let childClosed=0,socket,delegated=false,approvalRequested=false;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const gateway=http.createServer(async(req,res)=>{
 try{
  counts[req.url]=(counts[req.url]||0)+1;assert.equal(req.headers.authorization,'Bearer '+lease);
  if(req.url==='/internal/personal-cloud/model/v1/session')return void res.end(JSON.stringify({status:'ready'}));
  assert.equal(req.url,'/internal/personal-cloud/model/v1/chat/completions');
  let raw='';for await(const b of req)raw+=b;const body=JSON.parse(raw),messages=body.messages;
  const latestUser=JSON.stringify(messages.filter(m=>m.role==='user'&&JSON.stringify(m).includes('R65_')).at(-1));
  const last=messages.at(-1),tools=body.tools?.map(t=>t.function.name)||[];
  const child=tools.length>0&&latestUser?.includes('R65_CHILD_HOLD'),delegate=tools.length>0&&latestUser?.includes('R65_PARENT_DELEGATE'),approval=tools.length>0&&latestUser?.includes('R65_APPROVAL_REJECT');
  calls.push({latestUser,lastRole:last.role,child,delegate,approval,tools});
  res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});
  const chunk=(delta,finish_reason=null)=>res.write('data: '+JSON.stringify({id:'synthetic-'+calls.length,object:'chat.completion.chunk',created:Math.floor(Date.now()/1000),model:body.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');
  chunk({role:'assistant',content:''});
  if(child){held.add(res);res.on('close',()=>{held.delete(res);childClosed++;});chunk({content:'R65_CHILD_STARTED'});return;}
  if(delegate&&!delegated){delegated=true;
   assert.ok(tools.includes('subagent'));chunk({tool_calls:[{index:0,id:'call_delegate_'+calls.length,type:'function',function:{name:'subagent',arguments:JSON.stringify({description:'Synthetic delegated task',prompt:'R65_CHILD_HOLD: wait for cancellation, no file or network actions.'})}}]});chunk({},'tool_calls');
  }else if(approval&&!approvalRequested){approvalRequested=true;
   assert.ok(tools.includes('write'));chunk({tool_calls:[{index:0,id:'call_approval_'+calls.length,type:'function',function:{name:'write',arguments:JSON.stringify({file_path:'/tmp/r65-approval-denied.txt',content:'Synthetic data only',sandbox_permissions:'danger-full-access',justification:'Synthetic approval rejection test; must not create the file.'})}}]});chunk({},'tool_calls');
  }else{chunk({content:'R65_ADVANCED_REPLY'});chunk({},'stop');}
  res.end('data: [DONE]\n\n');
 }catch(error){checkpoints.push({stage:"model-error",error:String(error)});if(res.headersSent){res.end("data: [DONE]\n\n");return;}res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:{message:String(error)}}));}
});
await new Promise((resolve,reject)=>gateway.once('error',reject).listen(8080,'127.0.0.1',resolve));
async function rpcArgs(method,args,customHeaders={}){
 const rpcId=randomUUID(),response=await fetch(base+'/api/'+method,{method:'POST',headers:{...headers,'Content-Type':'application/json',...customHeaders},body:JSON.stringify({type:'client-request',rpcId,method,payload:{args}}),signal:AbortSignal.timeout(20000)});
 const data=await response.json();assert.equal(data.rpcId,rpcId);return data.result;
}
const rpc=(method,request,h)=>rpcArgs(method,method==='session/list'?{_request:request}:{request},h);
async function good(method,request){const r=await rpc(method,request);assert.equal(r.ok,true,method+JSON.stringify(r));return r.value;}
async function events(sessionId){const p=await good('session/projections',{sessionId});return (await good('session/page',{address:{kind:'session',sessionId},throughSeq:p.asOfSeq,maxMessages:300})).records.map(x=>x.event);}
async function waitFor(fn,label,seconds=20){const t=Date.now();for(;;){if(await fn())return;if(Date.now()-t>seconds*1000)throw Error('TIMEOUT_'+label);await sleep(100);}}
const prompt=(sessionId,text)=>good('session/prompt',{sessionId,requestId:randomUUID(),mode:'queue',content:[{type:'text',text}],clientTimeZone:'Asia/Shanghai'});
const idle=async sessionId=>(await good('session/list',{})).items.find(s=>s.sessionId===sessionId)?.running===false;
const result={schema:'r65-native-advanced-worker-v1',company_requests:0,network:'none',runtime:'0.2.1-rc.2',official_packages_modified:false};
try{
 const ws=(await good('workspace/create',{path:'/workspace'})).workspace;
 const {sessionId}=await good('session/create',{workspaceId:ws.workspaceId});
 await prompt(sessionId,'R65_PARENT_DELEGATE: delegate the synthetic child task.');
 await waitFor(()=>held.size===1,'child-model-request');result.native_subagent_uses_parent_lease=true;
 const runningEvents=await events(sessionId);checkpoints.push({stage:'child-running',events:runningEvents});
 const childId=runningEvents.find(e=>e.type==='subagent/catalog').data.childId;
 const continuation={requestId:randomUUID(),parentSessionId:sessionId,childSessionId:childId,mode:'continuable',delivery:'queue',content:[{type:'text',text:'R65_CHILD_QUEUED: synthetic queued message'}],clientTimeZone:'Asia/Shanghai'};
 const badContinuation=await rpc('subagents/prompt',continuation,{'X-Megmeet-Client-Lease':'mcl_'+'C'.repeat(43)});assert.equal(badContinuation.ok,false);
 const continuationReceipt=await good('subagents/prompt',continuation);assert.equal(typeof continuationReceipt.messageId,'string');
 result.native_subagent_continuation_accepted=true;result.foreign_lease_cannot_continue_child=true;
 const cancel=await good('session/cancel',{sessionId});assert.equal(cancel.accepted,true);
 await waitFor(()=>childClosed===1,'child-model-cancel');await waitFor(()=>idle(sessionId),'parent-cancel-idle');
 result.parent_cancel_closes_child_model=true;
 await prompt(sessionId,'R65_FOLLOWUP_AFTER_CHILD_CANCEL');await waitFor(()=>idle(sessionId),'followup-idle');
 assert.ok(JSON.stringify(await events(sessionId)).includes('R65_ADVANCED_REPLY'));result.followup_after_child_cancel=true;
 checkpoints.push({stage:'after-child-cancel',events:await events(sessionId)});
 socket=new WebSocket('ws://127.0.0.1:3084/api/remote.mux',{headers});
 socket.on('message',b=>frames.push(JSON.parse(b.toString())));
 await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject);});
 socket.send(JSON.stringify({type:'open',streamId:'events',endpoint:'$events',payload:{args:{}}}));
 await waitFor(()=>frames.some(f=>f.value?.type==='ready'),'events-ready');
 const ready=frames.find(f=>f.value?.type==='ready').value;result.native_events_ready=true;
 const approvalSession=(await good('session/create',{workspaceId:ws.workspaceId})).sessionId;
 await prompt(approvalSession,'R65_APPROVAL_REJECT: request the synthetic approval test.');
 await waitFor(()=>frames.some(f=>f.value?.event==='approval/request'),'approval-event');
 const request=frames.find(f=>f.value?.event==='approval/request').value;
 const denied=await rpcArgs('$events/result',{clientId:ready.clientId,eventId:request.eventId,outcome:{kind:'result',value:'allowed-once'}},{'X-Megmeet-Client-Lease':'mcl_'+'C'.repeat(43)});
 assert.equal(denied.ok,false);result.foreign_lease_cannot_answer_approval=true;
 const reply=await rpcArgs('$events/result',{clientId:ready.clientId,eventId:request.eventId,outcome:{kind:'result',value:'rejected'}});
 checkpoints.push({stage:'approval-reply',reply});assert.equal(reply.ok,true,JSON.stringify(reply));
 await waitFor(()=>idle(approvalSession),'approval-rejected-idle');
 const ev=await events(approvalSession);checkpoints.push({stage:'approval',events:ev});
 assert.ok(ev.some(e=>e.type==='approval/decided'&&e.data?.outcome==='rejected'));
 await assert.rejects(()=>readFile('/tmp/r65-approval-denied.txt'),{code:'ENOENT'});
 result.native_approval_rejected_file_absent=true;
 approvalRequested=false;
 await prompt(approvalSession,'R65_APPROVAL_REJECT: second test, cancel while pending.');
 await waitFor(()=>frames.filter(f=>f.value?.event==='approval/request').length===2,'second-approval-event');
 const pending=frames.filter(f=>f.value?.event==='approval/request').at(-1).value;
 const cancellation=await good('session/cancel',{sessionId:approvalSession});assert.equal(cancellation.accepted,true);
 await waitFor(()=>idle(approvalSession),'pending-approval-cancel-idle');
 await waitFor(()=>frames.some(f=>f.value?.type==='cancel'&&f.value.eventId===pending.eventId),'approval-cancel-notification');
 const cancelledEvents=await events(approvalSession);assert.ok(cancelledEvents.some(e=>e.type==='approval/decided'&&e.data?.outcome==='cancelled'));
 await assert.rejects(()=>readFile('/tmp/r65-approval-denied.txt'),{code:'ENOENT'});
 checkpoints.push({stage:'approval-cancel',events:cancelledEvents});result.native_pending_approval_cancelled=true;result.status='PASSED';
}catch(error){result.status='FAILED';result.error=String(error.stack||error);}
finally{
 result.gateway_requests=counts;result.model_requests=calls.length;result.child_streams_closed=childClosed;
 await writeFile('/tmp/advanced-result.json',JSON.stringify(result,null,2));
 await writeFile('/tmp/advanced-checkpoints.json',JSON.stringify({checkpoints,frames,calls},null,2));
 socket?.terminate();for(const r of held)r.destroy();gateway.closeAllConnections();await new Promise(r=>gateway.close(r));
 console.log(JSON.stringify(result));if(result.status!=='PASSED')process.exitCode=1;
}
