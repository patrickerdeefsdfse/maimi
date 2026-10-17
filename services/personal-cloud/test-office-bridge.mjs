import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {createCloudOfficeBridge,officeRoute} from './office-bridge.mjs';
const token='t'.repeat(43),id='a'.repeat(32),lease='mcl_'+'s'.repeat(43),session='session-12345678-1234-1234-1234-123456789012';
async function setup(t){
 const state={task:{lease,revoked:false,sessionId:session},calls:[],valid:true};
 const upstream=http.createServer(async(req,res)=>{
  const chunks=[];for await(const b of req)chunks.push(b);
  state.calls.push({path:req.url,headers:req.headers,body:Buffer.concat(chunks)});
  if(state.handler)return state.handler(req,res);
  if(req.url.endsWith('/outputs/0')){res.writeHead(200,{'Content-Length':'3','Set-Cookie':'private=forbidden'});res.end('out');return;}
  res.writeHead(200,{'Content-Type':'application/json','Set-Cookie':'private=forbidden'});
  res.end(JSON.stringify({protocol:'megmeet-office-remote-v1',job_id:id,state:'uploading',outputs:[]}));
 });
 await new Promise(r=>upstream.listen(0,'127.0.0.1',r));
 const office=createCloudOfficeBridge({token,base:'http://gateway:8080/internal/personal-cloud/office',current:()=>state.task,
  validate:async()=>{if(!state.valid)throw Error('expired');},request:(url,opts,cb)=>http.request('http://127.0.0.1:'+upstream.address().port+new URL(url).pathname,opts,cb)});
 const server=http.createServer((req,res)=>void office.handle(req,res));await new Promise(r=>server.listen(0,'127.0.0.1',r));
 t.after(async()=>{office.stop();server.closeAllConnections();upstream.closeAllConnections();await Promise.all([new Promise(r=>server.close(r)),new Promise(r=>upstream.close(r))]);});
 state.send=(suffix,method='GET',body,headers={})=>fetch('http://127.0.0.1:'+server.address().port+'/__megmeet/office'+suffix,
  {method,headers:{Authorization:'Bearer '+token,'X-Megmeet-Office-Session':session,...headers},...(body===undefined?{}:{body})});return state;
}
test('office fixed routes reject escaped paths, queries and foreign endpoints',()=>{
 for(const value of ['/__megmeet/office/jobs?owner=other','/__megmeet/office/jobs/'+id+'/inputs/20','/__megmeet/office/jobs/%2e%2e','//evil'])assert.equal(officeRoute('PUT',value),null);
 assert.equal(officeRoute('PUT','/__megmeet/office/jobs/'+id+'/inputs/19'),'/jobs/'+id+'/inputs/19');
});
test('loopback credentials exchanged for initiating lease, bytes streamed and headers stripped',async t=>{
 const s=await setup(t);assert.equal((await s.send('/jobs','POST','{}',{'Cookie':'never-forward','X-IAM-Access-Token':'never-forward'})).status,200);
 assert.equal((await s.send('/jobs/'+id+'/inputs/0','PUT','input')).status,200);
 const out=await s.send('/jobs/'+id+'/outputs/0');assert.equal(await out.text(),'out');assert.equal(out.headers.get('set-cookie'),null);
 assert.equal(s.calls[0].headers.authorization,'Bearer '+lease);assert.equal(s.calls[0].headers.cookie,undefined);assert.equal(s.calls[0].headers['x-iam-access-token'],undefined);
 assert.equal(s.calls[0].headers['x-megmeet-office-session'],undefined);
 assert.equal(s.calls[1].body.toString(),'input');
});
test('unknown jobs and a following task cannot borrow previous job authority',async t=>{
 const s=await setup(t);assert.equal((await s.send('/jobs/'+id)).status,401);assert.equal(s.calls.length,0);
 await s.send('/jobs','POST','{}');s.task={lease,revoked:false,sessionId:session};assert.equal((await s.send('/jobs/'+id)).status,401);assert.equal(s.calls.length,1);
});
test('expired, revoked or wrong loopback lease never reaches Gateway',async t=>{
 const s=await setup(t);assert.equal((await s.send('/jobs','POST','{}',{Authorization:'Bearer wrong'})).status,401);
 assert.equal((await s.send('/jobs','POST','{}',{'X-Megmeet-Office-Session':'other'})).status,401);
 s.valid=false;assert.equal((await s.send('/jobs','POST','{}')).status,401);s.valid=true;s.task.revoked=true;
 assert.equal((await s.send('/jobs','POST','{}')).status,401);assert.equal(s.calls.length,0);
});
test('task switch during upstream request suppresses response and foreign redirects',async t=>{
 const s=await setup(t);s.handler=(_req,res)=>{s.task={lease,revoked:false,sessionId:session};res.end(JSON.stringify({protocol:'megmeet-office-remote-v1',job_id:id}));};
 assert.equal((await s.send('/jobs','POST','{}')).status,502);
 s.handler=(_req,res)=>{res.writeHead(302,{Location:'https://foreign.invalid/'});res.end();};
 const r=await s.send('/jobs','POST','{}');assert.equal(r.status,503);assert.equal(r.headers.get('location'),null);
});
