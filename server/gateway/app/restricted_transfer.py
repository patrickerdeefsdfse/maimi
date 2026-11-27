"""Transfer exactly the native turn's selected attachments via private, inbound C RPC."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import re
from uuid import UUID, uuid5

import httpx
from . import personal_cloud as cloud
from .iam_oauth import IAMFailure
from .storage_lifecycle import default_store, managed_output_path

MAX_FILE=32*1024**2
MAX_TOTAL=128*1024**2
SLOTS=asyncio.Semaphore(3)
NAMESPACE=UUID('a16ab88f-81aa-478b-b565-8c4bfdad119d')
def key(path):
    return str(uuid5(NAMESPACE,path))
def need(ok):
    if not ok: raise IAMFailure(409,'task_file_transfer_unavailable')

def published(store,owner,sid,path):
    """Look up the most recently published version of this conversation's output."""
    managed_output_path(sid,path)
    with store.transaction(owner) as cur:
        cur.execute('''SELECT f.file_id, f.status, f.expires_at > clock_timestamp() AS live
          FROM (
            SELECT t.file_id, t.created_at AS published_at
            FROM platform.storage_task_files t
            WHERE t.owner_key=%s AND t.session_id=%s AND t.task_id=%s AND t.role='output'
            UNION ALL
            SELECT s.file_id, s.recorded_at AS published_at
            FROM platform.storage_source_copies s
            WHERE s.owner_key=%s AND s.session_id=%s AND s.source_path=%s
          ) publication JOIN platform.storage_files f USING (file_id)
          WHERE f.owner_key=%s AND f.session_id=%s AND f.kind='ordinary'
          ORDER BY publication.published_at DESC,f.created_at DESC,f.file_id DESC LIMIT 1''',
          (owner,sid,key(path),owner,sid,path,owner,sid))
        row=cur.fetchone()
        if row is None: return None
        if row['status']!='active' or not row['live']:
            from .storage_lifecycle import StorageError
            raise StorageError('storage_file_expired',410)
        return str(row['file_id'])

@dataclass
class Handoff:
    owner:str
    session:str
    lease:str
    host:dict
    guard:tuple
    out:str|None

async def check(state):
    actor=await cloud.lease_actor(state.lease)
    need(cloud.owner_key(actor)==state.owner and actor.task_network_mode=='restricted' and actor.task_session_id==state.session)

async def exchange(client,state,op,body,*,binary=False,maximum=131072,index=None):
    await check(state)
    headers={'Authorization':'Bearer '+state.host['route_token'],'X-Maimi-File-Operation':op,
             'X-Maimi-File-Session':state.session,'X-Maimi-File-Generation':str(state.host['generation'])}
    if index is not None: headers['X-Maimi-File-Index']=str(index)
    options={'content':body} if isinstance(body,bytes) else {'json':body}
    async with client.stream('POST',state.host['origin']+'/__cloud/task-files',headers=headers,**options) as r:
        need(r.status_code==200)
        data=bytearray()
        async for chunk in r.aiter_bytes():
            need(len(data)+len(chunk)<=maximum);data.extend(chunk)
    await check(state)
    return bytes(data) if binary else json.loads(data)

def input_bytes(store,state,row):
    options={'sha256':row['sha256'],'name':row['name'],'size':row['bytes']}
    if row['media']:
        options.update(name=None,image_media_type=row['media'])
    record=store.resolve_native(state.owner,state.session,**options)
    lease=store.begin_download(state.owner,state.session,record['file_id'])
    try:
        data=store.read_download(state.owner,state.session,lease['lease_id'])
        need(len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256'])
        return data
    finally: store.abandon_download(state.owner,state.session,lease['lease_id'])

def validate(manifest,sid):
    need(isinstance(manifest,dict) and set(manifest)=={'inputs','output_directory'})
    rows=manifest['inputs'];need(isinstance(rows,list) and len(rows)<=32)
    total=0
    for row in rows:
        need(isinstance(row,dict) and set(row)=={'sha256','name','bytes','media','aliases'} and
             isinstance(row['sha256'],str) and re.fullmatch('[a-f0-9]{64}',row['sha256']) and
             isinstance(row['name'],str) and 1<=len(row['name'])<=255 and row['name'] not in ('.','..') and
             not re.search(r'[/\\\x00-\x1f\x7f]',row['name']) and type(row['bytes']) is int and 0<=row['bytes']<=MAX_FILE and
             row['media'] in (None,'image/png','image/jpeg','image/webp','image/gif') and isinstance(row['aliases'],list) and len(row['aliases'])<=4)
        sha=row['sha256'];tail='/attachments/v1/files/'+sha[:2]+'/'+sha+'/'+row['name']
        need(all(isinstance(a,str) and re.fullmatch(r'/workspace/\.maimi-cache/cache-[A-Za-z0-9]{6}'+re.escape(tail),a) for a in row['aliases']))
        total+=row['bytes']
    need(total<=MAX_TOTAL)
    if manifest['output_directory'] is not None:
        need(isinstance(manifest['output_directory'],str))
        managed_output_path(sid,manifest['output_directory']+'/fixture')
        need(manifest['output_directory'].count('/')==3)
    return rows

async def prepare(actor,lease,host,manifest,rpc,*,chat_guard):
    sid=actor.task_session_id
    need(rpc['session_id']==sid and actor.task_network_mode=='restricted')
    rows=validate(manifest,sid)
    state=Handoff(cloud.owner_key(actor),sid,lease,host,chat_guard,manifest['output_directory'])
    async with asyncio.timeout(40),SLOTS,httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
        reply=await exchange(client,state,'prepare',manifest)
        pending=reply.get('missing')
        need(isinstance(pending,list) and len(pending)<=len(rows) and all(type(i) is int and 0<=i<len(rows) for i in pending) and len(set(pending))==len(pending))
        store=default_store() if pending else None
        for index in pending:
            data=await asyncio.to_thread(input_bytes,store,state,rows[index])
            need(await exchange(client,state,'put',data,index=index)=={'saved':True})
    return state

def publish(store,state,row,data):
    sha=hashlib.sha256(data).hexdigest()
    need(len(data)==row['bytes'] and sha==row['sha256'])
    record=store.reserve(state.owner,state.session,request_id='private-output:'+hashlib.sha256(
        json.dumps([row['path'],row['version'],sha],separators=(',',':')).encode()).hexdigest(),
        kind='ordinary',name=row['name'],media_type=mimetypes.guess_type(row['name'])[0] or 'application/octet-stream',
        size=len(data),sha256=sha,native_guard=state.guard)
    result=store.commit(state.owner,state.session,record['file_id'],data,native_guard=state.guard)
    need(result['status']=='active' and result['sha256']==sha)
    store.bind_task_file(state.owner,state.session,result['file_id'],task_id=key(row['path']),role='output')

async def collect(state,rpc,result):
    op=rpc.get('operation')
    ended=op=='process.poll' and isinstance(result,dict) and result.get('empty') and result.get('finished')
    if not state.out or not (ended or op in ('fs.writeText','fs.editText','process.forget')):return
    async with asyncio.timeout(42),SLOTS,httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
        reply=await exchange(client,state,'list',{})
        if reply.get('running') is True:return
        rows=reply.get('files');need(isinstance(rows,list) and len(rows)<=32)
        total=0
        for row in rows:
            need(isinstance(row,dict) and set(row)=={'path','name','version','sha256','bytes'} and
                 isinstance(row['path'],str) and row['path'].startswith(state.out+'/') and
                 row['name']==row['path'].rsplit('/',1)[-1] and type(row['bytes']) is int and 0<=row['bytes']<=MAX_FILE and
                 re.fullmatch(r'[a-f0-9]{64}',row.get('sha256','')) and isinstance(row['version'],str) and
                 re.fullmatch(r'[0-9]+:[0-9]+:[0-9]+:-?[0-9]+:-?[0-9]+',row['version']))
            managed_output_path(state.session,row['path']);total+=row['bytes'];need(total<=MAX_TOTAL)
            data=await exchange(client,state,'read',row,binary=True,maximum=row['bytes'])
            await asyncio.to_thread(publish,default_store(),state,row,data)
            need(await exchange(client,state,'ack',row)=={'saved':True})
