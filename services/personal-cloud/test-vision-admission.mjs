import test from 'node:test';
import assert from 'node:assert/strict';
import {createVisionAdmission, visionError} from './vision-admission.mjs';
import {apply as installSelection} from './vision-control-plugin.mjs';
const selection = {protocol:1,provider:'deepseek-official',model:'synthetic-vision'};
const request = {sessionId:'session-12345678-1234-1234-1234-123456789012',content:[{type:'image',data:'synthetic'}]};
const catalog = {data:[{id:selection.model,name:'Synthetic',inputModalities:['text','image']}],default_model:selection.model};
function harness(overrides={}) {
  const calls=[];
  const prepare=createVisionAdmission({
    selection:async()=>{calls.push('selection');return selection;},
    negotiate:async(task,model)=>{calls.push('negotiate');assert.equal(model,selection.model);return catalog;},
    applyCatalog:async value=>{calls.push('catalog');assert.deepEqual(value,catalog);},
    validate:async()=>{calls.push('validate');},...overrides});
  return {prepare,calls};
}
test('plain prompts incur no capability requests',async()=>{
  const h=harness();await h.prepare({}, {content:[{type:'text',text:'synthetic'}]});assert.deepEqual(h.calls,[]);
});
test('actual selection is checked around verified catalog application',async()=>{
  const h=harness();await h.prepare({},request);assert.deepEqual(h.calls,['selection','validate','negotiate','validate','catalog','selection']);
});
test('unsupported catalog does not silently allow image',async()=>{
  const h=harness({negotiate:async()=>({data:[{id:selection.model,inputModalities:['text']}]})});
  await assert.rejects(h.prepare({},request),error=>error.reason==='personal_model_vision_unverified');
  assert.ok(!h.calls.includes('catalog'));
});
test('switched model is not overwritten or sent the prepared image',async()=>{
  let n=0;const h=harness({selection:async()=>({...selection,model:++n===1?selection.model:'changed'})});
  await assert.rejects(h.prepare({},request),error=>error.reason==='MODEL_SELECTION_CHANGED');
});
test('revocation and abort during verification stop prompt admission',async()=>{
  const task={};let h=harness({negotiate:async()=>{task.revoked=true;return catalog;}});
  await assert.rejects(h.prepare(task,request),error=>error.reason==='personal_key_reauthorization_required');
  assert.ok(!h.calls.includes('catalog'));
  const ac=new AbortController();h=harness({negotiate:async()=>{ac.abort();return catalog;}});
  await assert.rejects(h.prepare({},request,ac.signal),error=>error.name==='AbortError');
  assert.ok(!h.calls.includes('catalog'));
});
test('bounded image count and fixed safe errors',async()=>{
  const h=harness();await assert.rejects(h.prepare({}, {content:Array(9).fill({type:'image'})}),e=>e.reason==='IMAGE_LIMIT');
  const e=visionError('secret upstream text');assert.equal(e.reason,'VISION_UNAVAILABLE');assert.ok(!e.message.includes('secret'));
});
test('private native selection contains no message content and validates identity',async()=>{
  let route;
  const ctx={effect:f=>f(),connection:{fetch:{register:value=>{route=value;return()=>{};}}},
    sessionController:{resolveAgent:async id=>({agent:{id}}),agents:{selectionFor:()=>({current:selection})}}};
  installSelection(ctx);
  assert.equal(route.requestBody, 'buffered');
  assert.equal((await route.fetch(new Request('http://local/api/megmeet-cloud/vision-selection?sessionId=wrong'))).status,400);
  const r=await route.fetch(new Request('http://local/api/megmeet-cloud/vision-selection?sessionId='+request.sessionId));
  assert.equal(r.status,200);assert.deepEqual(await r.json(),selection);
});
