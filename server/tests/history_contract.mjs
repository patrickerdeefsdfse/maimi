// Actual rc1 services, synthetic transport; no network or fixture credentials.
import assert from 'node:assert/strict';
import {Context} from '/opt/dsh/node_modules/@deepseek-ai/cordis/lib/index.js';
import {SessionStore} from '/opt/dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js';
import {PgSessionPersistence} from '/opt/personal-cloud/native-history-plugin.mjs';
const header={version:4,id:'synthetic-history-contract',createdAt:1,cwd:'/workspace',isSeeded:false,delegationDepth:0};
const title=(seq)=>({type:'session/title',seq,time:seq+1,data:{title:'synthetic '+seq}});
async function fixture(events,headerOverride={}){
 const ctx=new Context(),calls=[];const meta={header:{...header,...headerOverride},eventCount:events.length,inheritedEventCount:0,revision:'1'};
 const backend=new PgSessionPersistence(ctx,async(op,body)=>{calls.push(op);if(op==='stat')return structuredClone(meta);if(op==='read')return structuredClone({...meta,events:events.slice(body.offset,body.offset+body.length)});throw Error('UNEXPECTED_WRITE:'+op);});
 const handle=await backend.open(meta.header.id,'read');return {ctx,backend,handle,calls};
}
const checks={};
{
 const f=await fixture([]);const data=await f.handle.read();assert.equal(data.eventState,'detached');assert.deepEqual(data.events,[]);new SessionStore(f.ctx).prepare(header.id,{seed:data.events,meta:header,inheritedEventCount:0,eventState:data.eventState});await f.backend.close();checks.empty_restore=true;
}
{
 const original=Array.from({length:300},(_,i)=>title(i));const serialized=JSON.stringify(original);const f=await fixture(original);const data=await f.handle.read();assert.equal(data.eventState,'detached');assert.equal(data.events.length,300);const s=new SessionStore(f.ctx).prepare(header.id,{seed:data.events,meta:header,inheritedEventCount:0,eventState:data.eventState});assert.deepEqual(s.snapshotEvents().slice(0,300),data.events);assert.equal(JSON.stringify(original),serialized);assert.equal(f.calls.filter(x=>x==='read').length,2);data.events[0].data.title='changed';assert.equal(original[0].data.title,'synthetic 0');assert.equal((await f.handle.read(0,1)).events[0].data.title,'synthetic 0');const again=await f.handle.read(255,3);assert.deepEqual(again.events.map(x=>x.seq),[255,256,257]);await f.backend.close();checks.paged_detached_restoration_no_transport_aliases=true;
}
{
 const f=await fixture([{...title(0),type:'synthetic/future-required'}]);await assert.rejects(f.handle.read(),/unknown.*not marked ignorable/);await f.backend.close();checks.unknown_required_refused=true;
}
{
 const f=await fixture([{...title(0),type:'synthetic/future-optional',ignorable:true}]);const v=await f.handle.read();assert.equal(v.events[0].type,'synthetic/future-optional');assert.equal(v.events[0].ignorable,true);await f.backend.close();checks.unknown_optional_retained=true;
}
{
 const f=await fixture([title(1)]);await assert.rejects(f.handle.read(),/seq mismatch/);await f.backend.close();checks.sequence_gap_refused=true;
}
{
 const f=await fixture([], {version:5}).then(()=>null,e=>e);assert(f instanceof Error&&/newer harness/.test(f.message));checks.future_format_refused=true;
}
console.log(JSON.stringify({status:'PASSED_ACTUAL_RC1_ADAPTER_CONTRACT',checks,network_requests:0,storage_rewrites:0}));

