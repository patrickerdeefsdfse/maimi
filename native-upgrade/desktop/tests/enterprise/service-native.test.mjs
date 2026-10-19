import test from 'node:test';
import assert from 'node:assert/strict';
import {host} from './native.test.mjs';
import {A,B} from './bridge.test.mjs';
function gateway(){
 const grants=new Map(),connections=new Set(),calls=[];let n=0;
 return {calls,handler:async(req,res,body)=>{
  if(!req.url.startsWith('/v1/connectors/local-service/'))return false;
  calls.push({path:req.url,body});const json=value=>{res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(value));return true;};
  if(req.url.endsWith('/grants')){const id=String(++n),control=id.padStart(43,'C'),tool=id.padStart(43,'T'),g={control_grant:control,tool_grant:tool,expires_at:Math.floor(Date.now()/1000)+600,native:body.native_session_id};grants.set(control,g);grants.set(tool,g);return json(g);}
  const token=req.headers['x-maimi-service-grant'],g=grants.get(token);assert.ok(g);
  if(req.url.endsWith('/connect')){assert.equal(token,g.control_grant);assert.deepEqual(Object.keys(body).sort(),['ip','password']);connections.add(g.native);}
  else if(req.url.endsWith('/invoke')){assert.equal(token,g.tool_grant);assert.ok(!JSON.stringify(body).includes('password'));if(body.action==='inspect_server')return json({host_id:'synthetic-host',checks:[{output:'synthetic Linux'}]});}
  else if(req.url.endsWith('/revoke')){assert.equal(token,g.control_grant);connections.delete(g.native);return json({revoked:true,disconnect_confirmed:true});}
  return json({protocol:'maimi-service-session-v2',connected:connections.has(g.native),server:connections.has(g.native)?{host_id:'synthetic-host',label:'synthetic server'}:null});
 }};
}
test('actual native human Peer and RPC: enable, two-field connect, then model service tool',async()=>{
 const g=gateway(),h=await host({enableServiceUi:true,handler:g.handler});try{
  assert.deepEqual(await h.human('state',{session_id:A}),{ok:true,value:{enabled:false}});
  const enabled=await h.human('enable',{session_id:A});assert.equal(enabled.ok,true);assert.equal(enabled.value.connected,false);
  const connected=await h.human('connect',{session_id:A,ip:'10.4.240.14',password:'private-fixture-value'});assert.equal(connected.ok,true);assert.equal(connected.value.connected,true);
  const output=await h.call(h.a,'maimi_service',{action:'inspect_server',arguments:{host_id:'synthetic-host'}});assert.equal(output.structuredContent.checks[0].output,'synthetic Linux');
  assert.ok(!JSON.stringify(h.a.session.snapshotEvents()).includes('private-fixture-value'));
  assert.ok(g.calls.every(c=>!c.path.includes('personal-cloud')));
  await h.human('disable',{session_id:A});await assert.rejects(h.call(h.a,'maimi_service',{action:'status',arguments:{}}),/SERVICE_NOT_SELECTED/);
 }finally{await h.close();}
});
test('native noninteractive Peer, model connect and another session cannot access connection control',async()=>{
 const g=gateway(),h=await host({enableServiceUi:true,handler:g.handler});try{
  assert.equal((await h.human('enable',{session_id:A},{'x-synthetic-none':'yes'})).ok,false);
  assert.equal((await h.human('enable',{session_id:'33333333-3333-4333-8333-333333333333'})).ok,false);
  await h.human('enable',{session_id:A});
  await assert.rejects(h.call(h.a,'maimi_service',{action:'connect',arguments:{password:'private-fixture-value'}}),/SERVICE_ARGUMENTS_INVALID/);
  await assert.rejects(h.call(h.b,'maimi_service',{action:'status',arguments:{}}),/SERVICE_NOT_SELECTED/);
  assert.ok(!g.calls.some(c=>c.path.endsWith('/connect')));
 }finally{await h.close();}
});
