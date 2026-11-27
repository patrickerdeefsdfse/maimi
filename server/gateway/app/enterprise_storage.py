"""Finite NAS storage for explicit desktop enterprise uploads and JSON results.

An upload is scoped to the verified employee's personal workspace plus the
client's idempotent request UUID; a later real run binds those objects. No
desktop original is accessed, deleted or mistaken for a saved download.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from . import storage_lifecycle as storage
from .history_store import _connect


def scope(actor, workspace, request_id):
    from .personal_cloud import owner_key, personal_workspace_id
    storage.require(workspace.get('kind') == 'personal' and workspace.get('owner_user_no') == actor.user_no
        and str(workspace['workspace_id']) == personal_workspace_id(actor), 'enterprise_storage_personal_scope_required', 403)
    return owner_key(actor), 'enterprise-upload:' + str(UUID(str(request_id)))


def upload(actor, workspace, body, data):
    owner, session = scope(actor, workspace, body.request_id)
    stored = storage.default_store().put(owner, session,
        request_id='desktop-upload:' + hashlib.sha256((body.name+'\0'+body.sha256).encode()).hexdigest(),
        kind='enterprise_input', job_id=str(body.request_id), name=body.name,
        media_type='text/plain', data=data)
    return {**stored, 'owner': owner, 'session': session}


def read_file(row):
    store=storage.default_store();owner=row['managed_owner_key'];session=row['managed_session_id']
    lease=store.begin_download(owner,session,str(row['managed_file_id']))
    try:return store.read_download(owner,session,lease['lease_id'])
    finally:store.abandon_download(owner,session,lease['lease_id'])


def bind_inputs(actor, workspace, request_id, run_id, rows):
    owner,session=scope(actor,workspace,request_id);store=storage.default_store()
    for row in rows:
        storage.require(row.get('managed_owner_key')==owner and row.get('managed_session_id')==session
            and row.get('managed_file_id') is not None,'enterprise_storage_reference_required',403)
        store.bind_task_file(owner,session,str(row['managed_file_id']),task_id=run_id,role='input')
    return owner,session


def result_file(row, result):
    store=storage.default_store();owner=row['managed_owner_key'];session=row['managed_session_id'];run=str(row['id'])
    data=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    storage.require(len(data)<=2*1024**2,'enterprise_result_too_large',413)
    item=store.put(owner,session,request_id='desktop-result:'+run,kind='enterprise_output',job_id=run,
        name='enterprise-result.json',media_type='application/json',data=data)
    store.bind_task_file(owner,session,item['file_id'],task_id=run,role='output')
    return item


def result_value(row):
    if row.get('content_expires_at') and row['content_expires_at']<=datetime.now(timezone.utc):return None,True
    if not row.get('managed_output_file_id'):return row['result'],False
    try:
        value=read_file({**row,'managed_file_id':row['managed_output_file_id']})
        return json.loads(value),False
    except storage.StorageError as exc:
        if exc.status in (404,410):return None,True
        raise


def cancel(row):
    if row.get('managed_owner_key'):
        storage.default_store().complete_task(row['managed_owner_key'],row['managed_session_id'],str(row['id']),cancelled=True)


def prepare_delivery(actor, value):
    from .enterprise_tasks import actor_fields
    user,_=actor_fields(actor);items=value.get('items',value.get('runs',[value]))
    if not isinstance(items,list) or len(items)>50:return []
    receipts=[];store=storage.default_store()
    with _connect() as conn,conn.cursor() as cur:
        for item in items:
            if not isinstance(item,dict) or not item.get('result') or not item.get('run_id'):continue
            cur.execute('SELECT * FROM platform.hybrid_runs WHERE id=%s AND owner_user_no=%s',(item['run_id'],user))
            row=cur.fetchone()
            if not row or not row.get('managed_output_file_id'):continue
            try:lease=store.begin_download(row['managed_owner_key'],row['managed_session_id'],str(row['managed_output_file_id']))
            except storage.StorageError as exc:
                if exc.status in (404,410):continue
                raise
            receipts.append((row,lease))
    return receipts


def complete_delivery(receipts):
    store=storage.default_store()
    for row,lease in receipts:
        store.finish_download(row['managed_owner_key'],row['managed_session_id'],lease['lease_id'],
            bytes_received=lease['expected_bytes'],sha256=lease['sha256'])
        # Input text is no longer needed once the answer was transferred. Keep
        # only the task/idempotency receipts; result bytes have the 10m NAS grace.
        with _connect() as conn,conn.cursor() as cur:
            cur.execute("UPDATE platform.hybrid_runs SET input_text='',result=NULL,document_refs='[]'::jsonb,content_expires_at=LEAST(content_expires_at,clock_timestamp()+interval '10 minutes') WHERE id=%s AND owner_user_no=%s",(row['id'],row['owner_user_no']))


async def delivered_response(actor, value):
    if not storage.configured():return value
    receipts=await asyncio.to_thread(prepare_delivery,actor,value)
    async def completed():await asyncio.to_thread(complete_delivery,receipts)
    return JSONResponse(jsonable_encoder(value),headers={'Cache-Control':'no-store','Pragma':'no-cache'},background=BackgroundTask(completed))
