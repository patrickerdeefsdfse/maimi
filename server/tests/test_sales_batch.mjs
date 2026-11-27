import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { pathToFileURL } from 'node:url';

const routerModule = process.env.MAIMI_BATCH_ROUTER ? pathToFileURL(process.env.MAIMI_BATCH_ROUTER) : new URL('../worker/dsh/toolkit/project-routing.mjs', import.meta.url);
const { createProjectRouter, startProjectHeartbeat, writeProjectCompletion } = await import(routerModule);
const client = await readFile(process.env.MAIMI_BATCH_CLIENT || new URL('../worker/dsh/plugins/company-ui/client.template.js', import.meta.url), 'utf8');
const markerDefinition = client.slice(client.indexOf('  const marker = /'), client.indexOf('  const definition={',client.indexOf('  const marker = /')));
assert.ok(markerDefinition.includes('const ids ='));
const visibleJobs = runInNewContext(markerDefinition + '\nids');
const message = text => ({type:'assistant/message', surfaceOp:'append', data:{message:{content:[{type:'text',text}]}}});

function scenario(count, {connectors=['sales-orders'], orderCount=count, transportError, failed, mutate, extension, matchRequest=true}={}) {
  const task = {sessionId:randomUUID(),generation:1,lease:'synthetic'};
  const requestId = randomUUID(), calls=[], jobIds=[];
  let revision=1;
  const attachments=Array.from({length:count},(_,i)=>({
    attachment_id:'pjf_'+String(i+1).padStart(32,'0'),
    name:'order-'+(i+1)+'.'+(extension || ['pdf','png','xlsx'][i%3])
  }));
  const route=createProjectRouter({
    snapshot:()=>({ids:connectors, revision}), current:()=>task,
    currentStep:async()=>({messages:[{request_id:matchRequest?requestId:randomUUID(),attachments}]}),
    execute:async call=>{
      const index=calls.length;
      calls.push(call);
      if(index===mutate)revision++;
      if(index===transportError)throw Error('synthetic interruption');
      const job_id=randomUUID(), project_result={connector:call.connector,status:'completed'};
      if(call.action==='stage_file'){
        assert.equal(call.arguments.index,index);
        assert.equal(call.arguments.count,count);
        assert.equal(call.arguments.batch_id,requestId);
        return {job_id,project_result:{...project_result,status:index===failed?'failed':'completed'}};
      }
      if(call.action==='finish_batch'){
        const missing_indices=transportError===undefined?[]:[transportError];
        assert.ok(missing_indices.every(i=>call.arguments.skipped.includes(i)));
        const order_ids=Array.from({length:orderCount},(_,i)=>(i+1).toString(16).padStart(32,'0'));
        return {job_id,project_result,order_result:{order_ids,missing_indices}};
      }
      assert.equal(call.action,'review');
      jobIds.push(job_id);
      return {job_id,project_result:{...project_result,status:'needs_input'}};
    }
  });
  route.register(task,{sessionId:task.sessionId,requestId,content:[{type:'file'}]});
  return {route,task,calls,jobIds,attachments,respond:notify=>route.respond(task,{stream:true},notify)};
}

for(const [count,orderCount] of [[1,24],[4,1],[5,7],[20,23]]){
  test(count+' attachments return '+orderCount+' content orders, with no visible narration or write operation',async()=>{
    const s=scenario(count,{orderCount});let notified=0;
    const [a,b]=await Promise.all([s.respond(()=>notified++),s.respond()]);
    assert.equal(a,b);assert.equal(notified,1);
    assert.deepEqual(s.calls.filter(c=>c.action==='stage_file').map(c=>c.arguments.attachment_id),s.attachments.map(x=>x.attachment_id));
    assert.ok(s.calls.every(c=>c.connector==='sales-orders'&&['stage_file','finish_batch','review'].includes(c.action)));
    assert.equal(new Set(s.calls.map(c=>c.request_id)).size,count+1+orderCount);
    assert.equal(a.replace(/<!-- maimi-sales-review-v1:[0-9a-f-]+ -->/g,'').trim(),'');
    const cards=Array.from(visibleJobs(message(a)));
    assert.deepEqual(cards.map(c=>c.id),s.jobIds);assert.ok(cards.every(c=>c.salesOrder));
  });
}

test('21 files are rejected as a whole before execution',async()=>{
  const s=scenario(21);let notified=0;
  assert.match(await s.respond(()=>notified++),/1～20.*21 份/);
  assert.equal(s.calls.length,0);assert.equal(notified,0);
});

test('a failed middle file keeps other cards, filenames and selection fences',async()=>{
  const s=scenario(20,{transportError:9,failed:12,orderCount:18});
  const text=await s.respond();
  assert.equal(s.calls.length,39);
  assert.deepEqual(s.calls.find(c=>c.action==='finish_batch').arguments.skipped,[9,12]);
  assert.match(text,/order-10.pdf/);
  assert.doesNotMatch(text,/order-13/);
  assert.deepEqual(Array.from(visibleJobs(message(text)),c=>c.id),s.jobIds);
  const revoked=scenario(20,{mutate:2});
  await assert.rejects(revoked.respond(),{code:'PROJECT_ROUTE_SELECTION_CHANGED'});
  assert.equal(revoked.calls.length,3);
});

test('other skills keep their 4-file and 16-card limits',async()=>{
  const s=scenario(5,{connectors:['meeting'],extension:'xlsx'});
  let notified=0;assert.match(await s.respond(()=>notified++),/不超过 4 份/);
  assert.equal(s.calls.length,0);assert.equal(notified,0);
  const ordinary=Array.from({length:18},()=>`会议文档已完成。\n\n<!-- maimi-project-v1:${randomUUID()} -->`).join('\n\n');
  const full=await scenario(20).respond();
  const mixed=Array.from(visibleJobs(message(ordinary+'\n\n'+full)));
  assert.equal(mixed.filter(c=>!c.salesOrder).length,16);
  assert.equal(mixed.filter(c=>c.salesOrder).length,20);
  const unsupported=scenario(3,{extension:'wav'});assert.match(await unsupported.respond(),/本次有 0 份/);assert.equal(unsupported.calls.length,0);
});

test('SSE waits are invisible and end with one complete reply',()=>{
  const chunks=[];let heads=0;
  const response={writeHead(){this.headersSent=true;heads++;},write(s){chunks.push(s)},end(s){this.writableEnded=true;chunks.push(s)}};
  const stop=startProjectHeartbeat(response);
  try{writeProjectCompletion(response,{stream:true},'待复核');}finally{stop();}
  assert.equal(heads,1);assert.match(chunks[0],/^: processing\n\n$/);
  assert.match(chunks[1],/待复核/);assert.equal(chunks.at(-1),'data: [DONE]\n\n');
});

test('only accepted file requests send a silent heartbeat, revoked sessions stop',async()=>{
  let notified=0;const notify=()=>notified++;
  const unrelated=scenario(20,{matchRequest:false});
  assert.equal(await unrelated.respond(notify),null);assert.equal(unrelated.calls.length,0);
  const ordinary=scenario(20,{connectors:[]});
  assert.equal(await ordinary.respond(notify),null);assert.equal(ordinary.calls.length,0);
  assert.equal(notified,0);
  const single=scenario(1);await single.respond(notify);assert.equal(single.calls.length,3);assert.equal(notified,1);
  const aborted=scenario(20);aborted.route.revoke(aborted.task);
  await assert.rejects(aborted.respond(notify),{name:'AbortError'});
  assert.equal(aborted.calls.length,0);assert.equal(notified,1);
});
