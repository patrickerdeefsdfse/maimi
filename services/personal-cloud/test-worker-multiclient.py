"""Real native cloud worker, two synthetic clients, no network/company service."""
import importlib.util
from pathlib import Path
import json
import secrets
import sys

source=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('catalog_probe',source/'test-worker-catalog.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
docker=helper.docker
CLIENT=r'''
import http from 'node:http';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
const A='mcl_'+'a'.repeat(43),B='mcl_'+'b'.repeat(43),revoked=new Set(),models=[];
let pending;
const gateway=http.createServer(async(req,res)=>{
 const lease=(req.headers.authorization||'').slice(7);
 if(![A,B].includes(lease)||revoked.has(lease)){res.writeHead(401);res.end('{}');return;}
 if(req.url.endsWith('/session')){res.setHeader('Content-Type','application/json');res.end('{}');return;}
 if(!req.url.endsWith('/chat/completions')){res.writeHead(404);res.end('{}');return;}
 const chunks=[];for await(const c of req)chunks.push(c);const body=JSON.parse(Buffer.concat(chunks));
 if(!body.tools){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({id:'title',choices:[{message:{role:'assistant',content:'Synthetic title'},finish_reason:'stop'}]}));return;}
 models.push(lease);res.writeHead(200,{'Content-Type':'text/event-stream'});
 res.write('data: '+JSON.stringify({id:'probe',object:'chat.completion.chunk',model:body.model,choices:[{index:0,delta:{role:'assistant'},finish_reason:null}]})+'\n\n');
 const item={lease,closed:false,release(){res.write('data: '+JSON.stringify({id:'probe',object:'chat.completion.chunk',model:body.model,choices:[{index:0,delta:{content:'NATIVE_CLIENT_'+(lease===A?'A':'B')+'_OK'},finish_reason:null}]})+'\n\n');res.write('data: '+JSON.stringify({id:'probe',object:'chat.completion.chunk',model:body.model,choices:[{index:0,delta:{},finish_reason:'stop'}]})+'\n\n');res.end('data: [DONE]\n\n');}};
 pending=item;res.once('close',()=>{item.closed=true});
});
await new Promise(r=>gateway.listen(8080,'127.0.0.1',r));
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const base='http://127.0.0.1:3084';
async function request(path,body,lease=A){const response=await fetch(base+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+process.env.CLOUD_ROUTE_TOKEN,'X-Megmeet-Client-Lease':lease,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(15000)});return {status:response.status,value:await response.json()};}
async function rpc(method,args,lease=A,expected=200){const r=await request('/api/'+method,{type:'client-request',rpcId:randomUUID(),method,payload:{args}},lease);assert.equal(r.status,200,JSON.stringify(r.value));if(expected===200){assert.equal(r.value.result?.ok,true,JSON.stringify(r.value));return r.value.result.value;}assert.equal(r.value.result?.ok,false);assert.equal(r.value.result.error.code,'session/agent-busy');return r.value;}
async function until(check,label){for(let n=0;n<120;n++){if(await check())return;await delay(250);}throw Error(label+' timeout');}
let ready;await until(async()=>{try{const r=await request('/__cloud/ready');if(r.status===200)ready=r.value;return !!ready;}catch{return false;}},'ready');
const sessionId=(await rpc('session/create',{request:{workspaceId:ready.native_workspace_id}})).sessionId;
const prompt=lease=>rpc('session/prompt',{request:{sessionId,requestId:randomUUID(),mode:'queue',content:[{type:'text',text:'Reply with synthetic client proof'}]}},lease);
await prompt(A);await until(()=>pending?.lease===A,'first model');
await rpc('session/list',{_request:{}},B);
await rpc('session/prompt',{request:{sessionId,requestId:randomUUID(),mode:'queue',content:[{type:'text',text:'must not run'}]}},B,409);
await rpc('session/cancel',{request:{sessionId}},B,409);
assert.deepEqual(models,[A]);console.log('CONCURRENT_READ_ALLOWED_FOREIGN_PROMPT_AND_CANCEL_DENIED: PASS');
pending.release();await until(async()=>!(await rpc('session/list',{_request:{}})).items.find(s=>s.sessionId===sessionId)?.running,'A idle');
await prompt(B);await until(()=>pending?.lease===B,'B model');assert.deepEqual(models,[A,B]);console.log('NEXT_CLIENT_MODEL_LEASE_IS_ITS_OWN: PASS');
const second=pending;revoked.add(B);await until(()=>second.closed,'revoked stream closed');
await until(async()=>!(await rpc('session/list',{_request:{}})).items.find(s=>s.sessionId===sessionId)?.running,'B cancelled');
console.log('REVOKED_INITIATOR_CANCELS_ONLY_ITS_RUNNING_TASK: PASS');
await prompt(A);await until(()=>models.length===3,'A resumed');assert.deepEqual(models,[A,B,A]);pending.release();
await until(async()=>!(await rpc('session/list',{_request:{}})).items.find(s=>s.sessionId===sessionId)?.running,'A resumed idle');
const sessions=(await rpc('session/list',{_request:{}})).items;assert.equal(sessions.length,1);assert.equal(sessions[0].sessionId,sessionId);
console.log('SAME_NATIVE_SESSION_PERSISTS_ACROSS_CLIENT_HANDOFF: PASS');
gateway.closeAllConnections();await new Promise(r=>gateway.close(r));
'''

def main():
    image=docker('image','inspect',sys.argv[1] if len(sys.argv)>1 else 'megmeet-personal-dsh:alpha2-candidate-20260920','--format','{{.Id}}')
    name='megmeet-multiclient-test-'+secrets.token_hex(6)
    cid=None
    try:
        cid=docker('create','--name',name,'--network','none','--add-host','gateway:127.0.0.1','--read-only','--cap-drop','ALL',
          '--security-opt','no-new-privileges:true','--memory','1536m','--memory-swap','1536m','--pids-limit','192',
          '--tmpfs','/tmp:rw,nosuid,noexec,size=134217728,uid=1000,gid=1000',
          '--tmpfs','/state:rw,nosuid,size=134217728,uid=1000,gid=1000','--tmpfs','/workspace:rw,nosuid,size=134217728,uid=1000,gid=1000',
          '--mount','type=bind,source='+str(source/'worker.mjs')+',target=/opt/personal-cloud/worker.mjs,readonly',
          '-e','CLOUD_ROUTE_TOKEN='+secrets.token_urlsafe(32),'-e','CLOUD_MODEL_LEASE=mcl_'+'a'*43,
          '-e','CLOUD_MODEL_BASE_URL=http://gateway:8080/internal/personal-cloud/model/v1',
          '-e','CLOUD_MODEL_CATALOG_COUNT=1','-e','CLOUD_MODEL_CATALOG_0='+json.dumps({'data':[{'id':'deepseek-flash-v4.1'}],'default_model':'deepseek-flash-v4.1'}),image)
        docker('start',cid)
        print(docker('exec','-i',cid,'node','--input-type=module',input=CLIENT,timeout=160))
    except Exception:
        if cid:print(docker('logs','--tail','40',cid))
        raise
    finally:
        if cid:docker('rm','-f',cid)

if __name__=='__main__':main()
