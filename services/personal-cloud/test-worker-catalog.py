"""Offline real-worker check. Only synthetic credentials and disposable tmpfs.

Run on the local Linux Docker host; no company services or existing workers are
contacted. Pins the local image ID, mounts current wrapper source read-only, and
checks the native settings contract while one real WebSocket stays connected.
"""
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys


def docker(*args, input=None, timeout=120):
    result = subprocess.run(['docker', *args], input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Docker probe failed: ' + result.stderr[-1200:] + result.stdout[-1200:])
    return (result.stdout + (result.stderr if args and args[0]=='logs' else '')).strip()


CLIENT = r'''
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import WebSocket from '/opt/dsh/node_modules/ws/wrapper.mjs';
const base='http://127.0.0.1:3084';
const headers={Authorization:'Bearer '+process.env.CLOUD_ROUTE_TOKEN,'Content-Type':'application/json'};
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function request(path,body){
 const r=await fetch(base+path,{method:body===undefined?'GET':'POST',headers,
  ...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(40000)});
 const value=await r.json();assert.equal(r.status,200,JSON.stringify(value));return value;
}
async function rpc(method,args){
 const body=await request('/api/'+method,{type:'client-request',rpcId:randomUUID(),method,payload:{args}});
 assert.equal(body.result?.ok,true,JSON.stringify(body));return body.result.value;
}
let ready;
for(let n=0;n<80;n++){try{ready=await request('/__cloud/ready');break;}catch(e){if(n===79)throw e;await delay(1000)}}
const originalChildren=await readFile('/proc/1/task/1/children','utf8');
assert.ok(originalChildren.trim());
const startup=JSON.parse(process.env.CLOUD_MODEL_CATALOG_0);
const catalog=()=>rpc('session/modelCatalog',{});
const ids=value=>value.groups.flatMap(group=>group.models.map(model=>model.id)).sort();
assert.deepEqual(ids(await catalog()),startup.data.map(x=>x.id).sort());
console.log('INITIAL_NATIVE_CATALOG: PASS ('+(startup.data.length?'nonempty':'empty')+')');
const ws=new WebSocket('ws://127.0.0.1:3084/api/remote.mux',{headers:{Authorization:headers.Authorization}});
await once(ws,'open');let closed=false;ws.on('close',()=>{closed=true});
async function ping(){const pong=once(ws,'pong',{signal:AbortSignal.timeout(3000)});ws.ping('catalog-live');await pong;}
await ping();
const first={data:[{id:'authorized-added',name:'Added'},{id:'deepseek-flash-v4.1',name:'DS'}],default_model:'deepseek-flash-v4.1'};
for(const [name,next] of [['add',first],['revoke-all',{data:[],default_model:null}],
 ['restore',{data:[{id:'only-authorized',name:'Only'}],default_model:'only-authorized'}]]){
 const result=await request('/__cloud/catalog',{catalog:next});
 assert.equal(result.status,'updated');assert.equal(result.native_reload_required,false);
 const actual=await catalog();assert.deepEqual(ids(actual),next.data.map(x=>x.id).sort());
 if(next.data.length)assert.equal(actual.default.model,next.default_model);
 assert.equal((await request('/__cloud/ready')).native_workspace_id,ready.native_workspace_id);
 assert.equal(await readFile('/proc/1/task/1/children','utf8'),originalChildren);
 assert.equal(closed,false);await ping();
 console.log('NATIVE_CATALOG_'+name.toUpperCase().replaceAll('-','_')+'_SAME_PROCESS_AND_WS: PASS');
}
const bad=await fetch(base+'/__cloud/catalog',{method:'POST',headers,
 body:JSON.stringify({catalog:{data:[],default_model:'unauthorized-default'}})});
assert.equal(bad.status,400);
assert.deepEqual(ids(await catalog()),['only-authorized']);await ping();
console.log('INVALID_CATALOG_PRESERVES_CURRENT_NATIVE_STATE: PASS');
await new Promise(resolve=>{ws.once('close',resolve);ws.close()});
console.log('OFFLINE_REAL_WORKER_CATALOG: PASS');
'''


def main():
    source = Path(__file__).resolve().parent
    image = docker('image', 'inspect', sys.argv[1] if len(sys.argv)>1 else 'megmeet-personal-dsh:local', '--format', '{{.Id}}')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise RuntimeError('image must resolve to immutable local ID')
    print('BASE_IMAGE:', image, flush=True)
    for name in ('worker.mjs', 'catalog.mjs'):
        print('SOURCE_SHA256:', name, hashlib.sha256((source/name).read_bytes()).hexdigest(), flush=True)
    for empty in (False, True):
        name='megmeet-offline-catalog-'+secrets.token_hex(6)
        startup={'data':[] if empty else [{'id':'initial-authorized','name':'Initial'}],
                 'default_model':None if empty else 'initial-authorized'}
        cid=None
        try:
            cid=docker('create','--name',name,'--network','none','--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges:true','--memory','1536m','--memory-swap','1536m','--pids-limit','192',
                '--tmpfs','/tmp:rw,nosuid,noexec,size=67108864,uid=1000,gid=1000',
                '--tmpfs','/state:rw,nosuid,size=134217728,uid=1000,gid=1000',
                '--tmpfs','/workspace:rw,nosuid,size=134217728,uid=1000,gid=1000',
                '--mount','type=bind,source='+str(source/'worker.mjs')+',target=/opt/personal-cloud/worker.mjs,readonly',
                '--mount','type=bind,source='+str(source/'catalog.mjs')+',target=/opt/personal-cloud/catalog.mjs,readonly',
                '-e','CLOUD_ROUTE_TOKEN='+secrets.token_urlsafe(32),'-e','CLOUD_MODEL_LEASE=mcl_'+secrets.token_urlsafe(32),
                '-e','CLOUD_MODEL_BASE_URL=http://gateway:8080/internal/personal-cloud/model/v1',
                '-e','CLOUD_MODEL_CATALOG_COUNT=1','-e','CLOUD_MODEL_CATALOG_0='+json.dumps(startup),image)
            docker('start',cid)
            print(docker('exec','-i',cid,'node','--input-type=module',input=CLIENT,timeout=160), flush=True)
        except Exception:
            if cid:
                print(docker('logs','--tail','35',cid),flush=True)
                print(docker('inspect','--format','{{.State.Status}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}}',cid),flush=True)
            raise
        finally:
            if cid:
                docker('rm','-f',cid)
                print('OWNED_TEMPORARY_WORKER_CLEANUP: PASS', flush=True)


if __name__=='__main__':main()
