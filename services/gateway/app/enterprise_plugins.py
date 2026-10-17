"""Employee-authorized compact plugin execution, without a team picker."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from typing import Literal, Annotated
from uuid import UUID
from fastapi import APIRouter, Request, HTTPException
from pydantic import StringConstraints
from psycopg.types.json import Jsonb
from . import enterprise_tasks as cloud
from .history_store import _connect

NativeSession = Annotated[str, StringConstraints(pattern=r'^(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', max_length=80)]

router=APIRouter(prefix='/v1/plugins',tags=['enterprise-plugins'],route_class=cloud.CloudRoute)

class PluginUpload(cloud.UploadRequest):
    operation: Literal['plugin']='plugin'

class PluginTask(cloud.TaskRequest):
    native_session_id: NativeSession
    location: Literal['local','cloud']

def retention_guard(cur,actor,session,location):
    from .cloud_retention import access_in_transaction,enabled
    if not enabled():return
    from .personal_cloud import owner_key
    from .iam_oauth import IAMFailure
    user,_=cloud.actor_fields(actor)
    try:access_in_transaction(cur,owner_key(actor),user,activity=True,
          session_id=session if location=='cloud' else None)
    except IAMFailure as exc:raise cloud.CloudError(exc.status,exc.code) from None

def require_current_grant(cur,actor,key,version):
    user,_=cloud.actor_fields(actor)
    cur.execute('''SELECT p.* FROM platform.enterprise_plugin_catalog p
        JOIN platform.enterprise_plugin_grants g USING(plugin_key,version)
        WHERE p.plugin_key=%s AND p.version=%s AND g.user_no=%s AND p.enabled AND g.enabled''',
        (key,version,user))
    row=cur.fetchone()
    if not row:
        raise cloud.CloudError(403,'plugin_access_denied')
    manifest=cloud.RUNNERS.get(row['runner'])
    if not manifest or (manifest['id'],manifest['version'])!=(key,version):
        raise cloud.CloudError(503,'plugin_runner_unavailable')
    if manifest['simulation'] and os.getenv('HYBRID_TEST_FIXTURES','false').lower()!='true':
        raise cloud.CloudError(403,'simulation_plugin_disabled')
    return dict(row)

def sync_scope(actor,key=None,version=None):
    from .personal_cloud import ensure_personal_workspace
    w=ensure_personal_workspace(actor)
    user,_=cloud.actor_fields(actor)
    workspace=w['workspace_id']
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''INSERT INTO platform.hybrid_workspace_grants(workspace_id,user_no,can_share)
            VALUES(%s,%s,FALSE) ON CONFLICT(workspace_id,user_no) DO NOTHING''',(workspace,user))
        if key is None: return workspace
        p=require_current_grant(cur,actor,key,version)
        cur.execute('''INSERT INTO platform.hybrid_plugins(workspace_id,plugin_key,version,name,runner,model_policy)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,plugin_key,version)
            DO UPDATE SET enabled=TRUE,name=EXCLUDED.name''',
            (workspace,key,version,p['name'],p['runner'],Jsonb(p['model_policy'])))
        cur.execute('''INSERT INTO platform.hybrid_plugin_grants(workspace_id,plugin_key,version,user_no)
            VALUES(%s,%s,%s,%s) ON CONFLICT(workspace_id,plugin_key,version,user_no) DO UPDATE SET enabled=TRUE''',
            (workspace,key,version,user))
    return workspace

def catalog(actor):
    user,_=cloud.actor_fields(actor)
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''SELECT p.plugin_key,p.version,p.name,p.description,p.runner
            FROM platform.enterprise_plugin_catalog p JOIN platform.enterprise_plugin_grants g USING(plugin_key,version)
            WHERE g.user_no=%s AND g.enabled AND p.enabled ORDER BY p.name,p.version LIMIT 100''',(user,))
        items=[]
        for row in cur.fetchall():
            m=cloud.RUNNERS.get(row['runner'])
            if not m or (m['id'],m['version'])!=(row['plugin_key'],row['version']): continue
            if m['simulation'] and os.getenv('HYBRID_TEST_FIXTURES','false').lower()!='true': continue
            items.append({k:row[k] for k in ('plugin_key','version','name','description')})
    return {'items':items,'protocol':'megmeet-plugins','version':1}

def upload(actor,body):
    if body.operation!='plugin' or not body.plugin_key or not body.plugin_version:
        raise cloud.CloudError(400,'plugin_selection_required')
    return cloud.upload_attachment(actor,sync_scope(actor,body.plugin_key,body.plugin_version),body)

def bind_request(actor,body):
    user,_=cloud.actor_fields(actor)
    digest=hashlib.sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with _connect() as conn,conn.cursor() as cur:
        retention_guard(cur,actor,body.native_session_id,body.location)
        require_current_grant(cur,actor,body.plugin_key,body.plugin_version)
        if body.conversation_id:
            cur.execute('''SELECT 1 FROM platform.enterprise_native_bindings WHERE owner_user_no=%s
                AND native_session_id=%s AND location=%s AND conversation_id=%s AND plugin_key=%s AND plugin_version=%s''',
                (user,body.native_session_id,body.location,body.conversation_id,body.plugin_key,body.plugin_version))
            if not cur.fetchone(): raise cloud.CloudError(403,'plugin_conversation_scope_denied')
        cur.execute('''INSERT INTO platform.enterprise_native_bindings
            (owner_user_no,request_id,native_session_id,location,request_hash,plugin_key,plugin_version)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (user,body.request_id,body.native_session_id,body.location,digest,body.plugin_key,body.plugin_version))
        cur.execute('''SELECT request_hash FROM platform.enterprise_native_bindings WHERE owner_user_no=%s AND request_id=%s''',
            (user,body.request_id))
        if cur.fetchone()['request_hash']!=digest: raise cloud.CloudError(409,'plugin_request_scope_immutable')

def associate(actor,body,result):
    user,_=cloud.actor_fields(actor)
    with _connect() as conn,conn.cursor() as cur:
        retention_guard(cur,actor,body.native_session_id,body.location)
        require_current_grant(cur,actor,body.plugin_key,body.plugin_version)
        cur.execute('''UPDATE platform.enterprise_native_bindings SET conversation_id=%s,run_id=%s
            WHERE owner_user_no=%s AND request_id=%s''',
            (result['conversation_id'],result['run_id'],user,body.request_id))
    return {**result,'plugin_key':body.plugin_key,'plugin_version':body.plugin_version,
        'native_session_id':str(body.native_session_id),'location':body.location}

def results(actor,session,location):
    user,_=cloud.actor_fields(actor)
    with _connect() as conn,conn.cursor() as cur:
        retention_guard(cur,actor,session,location)
        cur.execute('''SELECT b.run_id,b.plugin_key,b.plugin_version,p.name
            FROM platform.enterprise_native_bindings b
            JOIN platform.enterprise_plugin_catalog p ON p.plugin_key=b.plugin_key AND p.version=b.plugin_version
            JOIN platform.enterprise_plugin_grants g ON g.plugin_key=p.plugin_key AND g.version=p.version AND g.user_no=b.owner_user_no
            WHERE b.owner_user_no=%s AND b.native_session_id=%s AND b.location=%s AND b.run_id IS NOT NULL
            AND p.enabled AND g.enabled ORDER BY b.created_at DESC,b.request_id DESC LIMIT 50''',(user,session,location))
        rows=cur.fetchall()
    items=[]
    for r in reversed(rows):
        run=cloud.read_run(actor,str(r['run_id']))
        items.append({**run,'plugin_key':r['plugin_key'],'plugin_version':r['plugin_version'],'plugin_name':r['name']})
    return {'items':items,'storage':'server','native_session_id':session,'location':location}

@router.get('/catalog')
async def get_catalog(request:Request):
    return await cloud._call(catalog,await cloud._actor(request))

@router.post('/uploads')
async def post_upload(body:PluginUpload,request:Request):
    return await cloud._call(upload,await cloud._actor(request),body)

@router.post('/tasks')
async def post_task(body:PluginTask,request:Request):
    actor=await cloud._actor(request)
    if body.document_refs:
        raise HTTPException(400,'team_document_feature_removed')
    workspace=await cloud._call(sync_scope,actor,body.plugin_key,body.plugin_version)
    await cloud._call(bind_request,actor,body)
    task=cloud.TaskRequest(**body.model_dump(exclude={'native_session_id','location'}))
    prepared=await cloud._call(cloud.prepare_run,actor,workspace,task,'plugin')
    result=prepared.get('existing') or await cloud.execute_prepared(request,actor,prepared)
    # The native-context binding is private to the verified employee and cannot
    # be reassigned by a replay. Server permissions are checked again at read.
    return await cloud._call(associate,await cloud._fresh_actor(request),body,result)

@router.get('/results')
async def get_results(request:Request,native_session_id:NativeSession,location:Literal['local','cloud']):
    return await cloud._call(results,await cloud._actor(request),str(native_session_id),location)

def admin(args):
    if args.user.strip()!=args.user or not args.user or len(args.user)>100: raise ValueError('invalid employee')
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT 1 FROM platform.enterprise_plugin_catalog WHERE plugin_key=%s AND version=%s',(args.plugin,args.version))
        if not cur.fetchone(): raise ValueError('plugin must be in release catalog')
        cur.execute('''INSERT INTO platform.enterprise_plugin_grants(plugin_key,version,user_no,enabled)
            VALUES(%s,%s,%s,%s) ON CONFLICT(plugin_key,version,user_no) DO UPDATE SET enabled=EXCLUDED.enabled''',
            (args.plugin,args.version,args.user,args.enabled))
    print('EMPLOYEE_PLUGIN_GRANT: UPDATED')

if __name__=='__main__':
    p=argparse.ArgumentParser(description='Local administrator tool; OS/database access is the authority')
    p.add_argument('--user',required=True);p.add_argument('--plugin',required=True)
    p.add_argument('--version',default='1.0.0');p.add_argument('--enabled',action=argparse.BooleanOptionalAction,default=True)
    try: admin(p.parse_args())
    except Exception as e: print('PLUGIN_ADMIN: FAILED ('+type(e).__name__+')');raise SystemExit(1)
