"""Real r12 native worker with r14 idle overlays, synthetic isolated clients."""
import importlib.util
import json
from pathlib import Path
import secrets
import sys

source=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('multiclient_idle_fixture',source/'test-worker-multiclient.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
docker=fixture.docker
client=fixture.CLIENT.replace("await prompt(A);await until(()=>pending?.lease===A,'first model');", """
await prompt(A);await until(()=>pending?.lease===A,'first model');
assert.deepEqual((await request('/__cloud/activity')).value,{protocol:1,busy:true,draining:false});
assert.deepEqual((await request('/__cloud/idle-release',{})).value,{protocol:1,busy:true,draining:false});
console.log('RUNNING_NATIVE_MODEL_PREVENTS_IDLE_RELEASE: PASS');
""")
client=client.replace('gateway.closeAllConnections();',r'''
await until(async()=>!(await request('/__cloud/activity')).value.busy,'worker fully idle');
const slowUpload=http.request(base+'/api/session/uploadFileBinary?sessionId='+encodeURIComponent(sessionId)+'&name=idle-probe.txt',{
 method:'POST',headers:{Authorization:'Bearer '+process.env.CLOUD_ROUTE_TOKEN,'X-Megmeet-Client-Lease':A,'Content-Type':'application/octet-stream'}});
const uploadDone=new Promise((resolve,reject)=>{slowUpload.once('response',res=>{const chunks=[];res.on('data',c=>chunks.push(c));res.once('end',()=>resolve({status:res.statusCode,value:JSON.parse(Buffer.concat(chunks))}));});slowUpload.once('error',reject);});
slowUpload.write('SYNTHETIC_IDLE_UPLOAD_BEGIN\n');
await until(async()=>(await request('/__cloud/activity')).value.busy,'slow upload held');
assert.deepEqual((await request('/__cloud/idle-release',{})).value,{protocol:1,busy:true,draining:false});
slowUpload.end('SYNTHETIC_IDLE_UPLOAD_END\n');assert.equal((await uploadDone).status,200);
console.log('SLOW_RAW_UPLOAD_PREVENTS_IDLE_RELEASE: PASS');
await until(async()=>!(await request('/__cloud/activity')).value.busy,'upload complete idle');
assert.deepEqual((await request('/__cloud/idle-release',{})).value,{protocol:1,busy:false,draining:true});
assert.equal((await request('/api/session/create',{type:'client-request',rpcId:randomUUID(),method:'session/create',payload:{args:{request:{workspaceId:ready.native_workspace_id}}}})).status,409);
assert.equal((await request('/__cloud/idle-resume',{})).value.draining,false);
const after=(await rpc('session/list',{_request:{}})).items;assert.equal(after.length,1);assert.equal(after[0].sessionId,sessionId);
console.log('FENCED_MUTATION_DENIED_AND_RESUME_PRESERVES_SESSION: PASS');
gateway.closeAllConnections();
''')


def main():
    image=docker('image','inspect',sys.argv[1] if len(sys.argv)>1 else 'megmeet-personal-dsh:shared-20260921-r12','--format','{{.Id}}')
    cid=None
    try:
        args=['create','--name','megmeet-idle-test-'+secrets.token_hex(6),'--network','none',
            '--add-host','gateway:127.0.0.1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
            '--memory','1536m','--memory-swap','1536m','--pids-limit','192',
            '--tmpfs','/tmp:rw,nosuid,noexec,size=134217728,uid=1000,gid=1000',
            '--tmpfs','/state:rw,nosuid,size=134217728,uid=1000,gid=1000',
            '--tmpfs','/workspace:rw,nosuid,size=134217728,uid=1000,gid=1000']
        for name in ('worker.mjs','idle-control.mjs'):
            args.extend(['--mount','type=bind,source='+str(source/name)+',target=/opt/personal-cloud/'+name+',readonly'])
        args.extend(['-e','CLOUD_ROUTE_TOKEN='+secrets.token_urlsafe(32),'-e','CLOUD_MODEL_LEASE=mcl_'+'a'*43,
            '-e','CLOUD_MODEL_BASE_URL=http://gateway:8080/internal/personal-cloud/model/v1',
            '-e','CLOUD_MODEL_CATALOG_COUNT=1','-e','CLOUD_MODEL_CATALOG_0='+json.dumps({'data':[{'id':'deepseek-flash-v4.1'}],'default_model':'deepseek-flash-v4.1'}),image])
        cid=docker(*args);docker('start',cid)
        print(docker('exec','-i',cid,'node','--input-type=module',input=client,timeout=180))
    except Exception:
        if cid:print(docker('logs','--tail','40',cid))
        raise
    finally:
        if cid:docker('rm','-f',cid)


if __name__=='__main__':main()
