"""Local-only real native A/B cloud Hosts; no company services or keys."""
import asyncio
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx
from websockets.legacy.client import connect
from desktop_auth_smoke import login,check,BASE

PREFIX='/v1/personal-cloud'


def request(grant,path,body=None):
    with httpx.Client(timeout=110,trust_env=False) as client:
        r=client.request('GET' if body is None else 'POST',BASE+path,
                         headers={'Authorization':'Bearer '+grant['access_token']},json=body)
    if r.status_code!=200:raise RuntimeError('HTTP_'+str(r.status_code)+'_'+path)
    return r.json() if 'application/json' in r.headers.get('content-type','') else r.text


def rpc(grant,method,args):
    result=request(grant,PREFIX+'/native/api/'+method,{'type':'client-request','rpcId':str(uuid4()),'method':method,'payload':{'args':args}})['result']
    if not result.get('ok'):raise RuntimeError('RPC_'+method+'_'+result.get('error',{}).get('code','unknown'))
    return result['value']


def container(opened):return 'megmeet-personal-dev-v1-'+opened['workspace_id'].replace('-','')


def journal_count(name,marker):
    # Inspect ONLY the two synthetic users' native journals; emit counts, not data.
    script="""const fs=require('node:fs'),z=require('node:zlib');let text='';function walk(p){for(const e of fs.readdirSync(p,{withFileTypes:true})){const f=p+'/'+e.name;if(e.isDirectory())walk(f);else if(f.endsWith('.jsonl'))text+=fs.readFileSync(f,'utf8');else if(f.endsWith('.jsonl.zstd')){const b=fs.readFileSync(f);let o=0;while(o<b.length){try{const r=z.zstdDecompressSync(b.subarray(o),{info:true});if(!r.engine.bytesWritten)break;text+=r.buffer.toString();o+=r.engine.bytesWritten;}catch{break;}}}}}try{walk('/state/dsh/sessions')}catch{};console.log(text.split(process.argv[1]).length-1)"""
    r=subprocess.run(['docker','exec',name,'node','-e',script,marker],capture_output=True,text=True,check=True)
    return int(r.stdout.strip())


def prompt(grant,opened,session,label):
    marker='PERSONAL_MODEL_OK[LOCAL-FIXTURE-'+label+']'
    previous=journal_count(container(opened),marker)
    text='PERSONAL_CLOUD_SMOKE_'+label+'_'+uuid4().hex
    rpc(grant,'session/prompt',{'request':{'sessionId':session,'requestId':str(uuid4()),'mode':'queue','content':[{'type':'text','text':text}]}})
    for _ in range(80):
        if journal_count(container(opened),marker)>previous:
            check(True,'ACTUAL_NATIVE_PERSONAL_MODEL_'+label);return text
        time.sleep(1)
    raise RuntimeError('NATIVE_PERSONAL_MODEL_TIMEOUT_'+label)


async def ws(grant):
    async with connect(BASE.replace('http://','ws://')+PREFIX+'/native/api/remote.mux',
         extra_headers={'Authorization':'Bearer '+grant['access_token']},open_timeout=10) as socket:
        check(socket.open,'ACTUAL_NATIVE_WEBSOCKET_UPGRADE')


def run():
    _,a=login('a');_,b=login('b')
    one=request(a,PREFIX+'/workspace');two=request(b,PREFIX+'/workspace')
    check(one['workspace_id']!=two['workspace_id'] and one['manual_entry'],'EMPLOYEE_PRIVATE_STABLE_BINDING')
    opened_a=request(a,PREFIX+'/open',{});opened_b=request(b,PREFIX+'/open',{})
    check(opened_a['native_workspace_id']!=opened_b['native_workspace_id'],'DISTINCT_NATIVE_WORKSPACES')
    check('<html' in request(a,PREFIX+'/native/').lower(),'ACTUAL_NATIVE_HTML')
    asyncio.run(ws(a))
    for method,body in [('workspace/create',{'path':'/state'}),('workspace/rename',{'workspaceId':opened_a['native_workspace_id'],'title':'renamed'}),('workspace/delete',{'workspaceId':opened_a['native_workspace_id']})]:
        try:rpc(a,method,{'request':body})
        except RuntimeError as exc:check('workspace/cloud-fixed' in str(exc),'IMMUTABLE_'+method.upper().replace('/','_'))
        else:raise RuntimeError('IMMUTABLE_POLICY_FAILED')
    sa=rpc(a,'session/create',{'request':{'workspaceId':opened_a['native_workspace_id']}})['sessionId']
    sb=rpc(b,'session/create',{'request':{'workspaceId':opened_b['native_workspace_id']}})['sessionId']
    first=prompt(a,opened_a,sa,'A');prompt(a,opened_a,sa,'A');prompt(b,opened_b,sb,'B')
    check(journal_count(container(opened_b),first)==0,'B_CANNOT_READ_A_HISTORY')
    # Exercise the real native model -> write tool -> native file reader path.
    # The local synthetic model accepts only this fixed UUID-named text fixture.
    file_id=uuid4().hex
    file='native-tool-'+file_id+'.txt'
    rpc(a,'session/prompt',{'request':{'sessionId':sa,'requestId':str(uuid4()),'mode':'queue',
        'content':[{'type':'text','text':'CLOUD_TOOL_WRITE_'+file_id}]}})
    for _ in range(80):
        try:
            read=rpc(a,'workspaceFiles/read',{'workspaceFileScopeId':sa,'path':'/workspace/'+file,'range':{}})
            if read.get('text')=='SYNTHETIC_NATIVE_TOOL_LOCAL-FIXTURE-A':break
        except RuntimeError:pass
        time.sleep(1)
    else:raise RuntimeError('NATIVE_FILE_WRITE_TOOL_TIMEOUT')
    check(True,'ACTUAL_NATIVE_WRITE_TOOL_AND_FILE_READ_PRIVATE_A')
    try:rpc(b,'workspaceFiles/read',{'workspaceFileScopeId':sb,'path':'/workspace/'+file,'range':{}})
    except RuntimeError:check(True,'B_CANNOT_READ_A_FILE')
    else:raise RuntimeError('CROSS_EMPLOYEE_FILE_LEAK')
    _,new_a=login('a')
    restored=request(new_a,PREFIX+'/open',{})
    check(restored['native_workspace_id']==opened_a['native_workspace_id'],'NATIVE_HOST_RECREATE_WORKSPACE_RESTORED')
    check(journal_count(container(restored),first)>0,'NATIVE_HOST_RECREATE_CHAT_HISTORY_RESTORED')
    check(rpc(new_a,'workspaceFiles/read',{'workspaceFileScopeId':sa,'path':'/workspace/'+file,'range':{}})['text']=='SYNTHETIC_NATIVE_TOOL_LOCAL-FIXTURE-A','NATIVE_HOST_RECREATE_FILE_RESTORED')
    prompt(new_a,restored,sa,'A')
    request(new_a,'/v1/desktop/auth/logout',{})
    with httpx.Client(trust_env=False) as client:
        r=client.get(BASE+PREFIX+'/native/',headers={'Authorization':'Bearer '+new_a['access_token']})
        check(r.status_code==401,'LOGOUT_NATIVE_ACCESS_REVOKED')
    check(rpc(b,'session/list',{'_request':{}})['items'] is not None,'A_LOGOUT_B_STILL_USABLE')
    request(a,'/v1/desktop/auth/logout',{});request(b,'/v1/desktop/auth/logout',{})
    print('PERSONAL_NATIVE_CLOUD_LOCAL_CHAIN: PASS')


if __name__=='__main__':run()
