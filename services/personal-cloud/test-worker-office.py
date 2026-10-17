"""Native task -> private office bridge -> synthetic Gateway, wholly offline.

Real conversion formats are tested separately against the shared worker. This
probe proves the live native wrapper uses each initiating client's own lease.
"""
import importlib.util
from pathlib import Path
import sys

path = Path(__file__).with_name('test-worker-multiclient.py')
spec = importlib.util.spec_from_file_location('office_native_probe', path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

client = probe.CLIENT
client = client.replace("import {randomUUID} from 'node:crypto';", """import {randomUUID,createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {runEngine} from '/opt/dsh/office-adapter/transport.mjs';
const officeLeases=[],officeId='d'.repeat(32),officeBytes=Buffer.from('CLOUD_OFFICE_OK');
""")
anchor = " if(!req.url.endsWith('/chat/completions'))"
assert client.count(anchor) == 1
client = client.replace(anchor, """ if(req.url.startsWith('/internal/personal-cloud/office/')){
  officeLeases.push(lease);assert.equal(req.headers['x-megmeet-office-session'],undefined);
  const chunks=[];for await(const chunk of req)chunks.push(chunk);
  if(req.method==='POST'&&req.url.endsWith('/jobs')){
   const b=JSON.parse(Buffer.concat(chunks));assert.equal(b.request.output,'output.txt');
   assert.equal(b.officeSessionId,undefined);assert.equal(b.request.workspace,undefined);
  }
  if(req.url.endsWith('/outputs/0')){res.writeHead(200,{'Content-Length':String(officeBytes.length)});res.end(officeBytes);return;}
  const state=req.method==='DELETE'?'cancelled':req.url.endsWith('/run')?'queued':req.method==='GET'?'succeeded':'uploading';
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify({protocol:'megmeet-office-remote-v1',job_id:officeId,state,
   ...(state==='succeeded'?{result:{output:'output.txt'},outputs:[{id:'0',path:'output.txt',size_bytes:officeBytes.length,sha256:createHash('sha256').update(officeBytes).digest('hex')}]}:{outputs:[]})}));return;
 }
""" + anchor)
anchor = "await rpc('session/list',{_request:{}},B);"
assert client.count(anchor) == 1
client = client.replace(anchor, """const config={endpoint:'http://127.0.0.1:3086/__megmeet/office',token:process.env.CLOUD_ROUTE_TOKEN};
async function officeProof(output,expected){
 const start=officeLeases.length;
 await runEngine(config,{protocol:'megmeet-office-v1',operation:'create',workspace:'/workspace',output,options:{text:'synthetic'},officeSessionId:sessionId});
 assert.equal(await readFile('/workspace/'+output,'utf8'),'CLOUD_OFFICE_OK');
 assert.equal(officeLeases.length-start,5);assert.ok(officeLeases.slice(start).every(x=>x===expected));
}
await officeProof('client-a.txt',A);
const bad=await fetch(config.endpoint+'/jobs',{method:'POST',headers:{Authorization:'Bearer '+config.token,'X-Megmeet-Office-Session':'foreign'},body:'{}'});assert.equal(bad.status,401);
console.log('NATIVE_OFFICE_TASK_A_LEASE_AND_SESSION_BINDING: PASS');
""" + anchor)
anchor = "const second=pending;revoked.add(B);"
assert client.count(anchor) == 1
client = client.replace(anchor, "await officeProof('client-b.txt',B);console.log('NATIVE_OFFICE_NEXT_CLIENT_USES_B_LEASE: PASS');\n" + anchor)
probe.CLIENT = client
if __name__ == '__main__':
    if len(sys.argv) == 1:
        sys.argv.append('megmeet-personal-dsh:shared-20260921-candidate')
    probe.main()
