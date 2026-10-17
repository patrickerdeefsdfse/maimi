"""Fixed 30-day whole-workspace policy; shared by web and desktop cloud.

PG commits an admission fence before physical deletion. A partial filesystem
failure keeps that fence until retry finishes; returning users cannot create
fresh data inside an unfinished deletion. No automatic per-session expiry.
"""
from __future__ import annotations
import asyncio
import os
import re
from uuid import UUID, uuid4
from .history_store import _connect
from .iam_oauth import IAMFailure
from .cloud_history import storage_rpc

POLICY='workspace-idle-30d-v1'
OWNER=re.compile(r'^[a-f0-9]{32}$')
SESSION=re.compile(r'^(?:session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

def enabled():return os.getenv('PERSONAL_CLOUD_RETENTION_POLICY','')==POLICY

def _identity(owner,user):
    if not OWNER.fullmatch(owner) or not isinstance(user,str) or not 1<=len(user)<=100:
        raise IAMFailure(403,'cloud_retention_identity_invalid')

def _row(cur,owner,user):
    _identity(owner,user)
    cur.execute('''INSERT INTO platform.personal_cloud_retention(owner_key,workspace_id,owner_user_no)
      SELECT %s,id,owner_user_no FROM platform.hybrid_workspaces
      WHERE id=%s AND kind='personal' AND owner_user_no=%s
      ON CONFLICT(owner_key) DO NOTHING''',(owner,str(UUID(hex=owner)),user))
    cur.execute('SELECT * FROM platform.personal_cloud_retention WHERE owner_key=%s FOR UPDATE',(owner,))
    row=cur.fetchone()
    if not row or row['owner_user_no']!=user:raise IAMFailure(403,'cloud_retention_identity_invalid')
    return row

def access(owner,user,*,activity=False,session_id=None):
    if not enabled():return
    with _connect() as conn,conn.cursor() as cur:
        access_in_transaction(cur,owner,user,activity=activity,session_id=session_id)

def access_in_transaction(cur,owner,user,*,activity=False,session_id=None):
    if not enabled():return
    row=_row(cur,owner,user)
    if row['deletion_id'] is not None:raise IAMFailure(409,'cloud_workspace_cleanup_in_progress')
    if session_id is not None:
        if not isinstance(session_id,str) or not SESSION.fullmatch(session_id):raise IAMFailure(400,'cloud_session_invalid')
        cur.execute('SELECT 1 FROM platform.personal_cloud_session_deletions WHERE owner_key=%s AND session_id=%s',(owner,session_id))
        if cur.fetchone():raise IAMFailure(410,'cloud_session_permanently_deleted')
    if activity:
        cur.execute('UPDATE platform.personal_cloud_retention SET last_access_at=clock_timestamp() WHERE owner_key=%s',(owner,))

def queue_session_delete(owner,user,session_id):
    if not SESSION.fullmatch(session_id or ''):raise IAMFailure(400,'cloud_session_invalid')
    with _connect() as conn,conn.cursor() as cur:
        row=_row(cur,owner,user)
        if row['deletion_id'] is not None:raise IAMFailure(409,'cloud_workspace_cleanup_in_progress')
        cur.execute('INSERT INTO platform.personal_cloud_session_deletions(owner_key,session_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(owner,session_id))
        cur.execute('UPDATE platform.personal_cloud_retention SET last_access_at=clock_timestamp() WHERE owner_key=%s',(owner,))

def _claim(owner,user):
    with _connect() as conn,conn.cursor() as cur:
        row=_row(cur,owner,user)
        if row['deletion_id'] is not None:return str(row['deletion_id']),False
        cur.execute('''UPDATE platform.personal_cloud_retention SET deletion_id=%s,deletion_started_at=clock_timestamp(),
          last_attempt_at=clock_timestamp(),last_error_code=NULL
          WHERE owner_key=%s AND GREATEST(policy_started_at,last_access_at)<=clock_timestamp()-interval '30 days'
          RETURNING deletion_id''',(str(uuid4()),owner))
        claimed=cur.fetchone()
        return (str(claimed['deletion_id']),True) if claimed else (None,False)

def _defer_before_delete(owner,deletion_id):
    # Only used for manager preflight 409 on a NEW claim: no physical change.
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE platform.personal_cloud_retention SET deletion_id=NULL,deletion_started_at=NULL,
          last_error_code='active_workspace'
          WHERE owner_key=%s AND deletion_id=%s''',(owner,deletion_id))

def _erase_projection(cur,owner,user):
    _erase_projects(cur,owner)
    workspace=str(UUID(hex=owner))
    cur.execute('''DELETE FROM platform.enterprise_native_bindings b WHERE b.owner_user_no=%s
      AND (b.location='cloud' OR EXISTS(SELECT 1 FROM platform.hybrid_runs r
        WHERE r.id=b.run_id AND r.workspace_id=%s AND r.owner_user_no=%s))''',(user,workspace,user))
    cur.execute('DELETE FROM platform.hybrid_runs WHERE workspace_id=%s AND owner_user_no=%s',(workspace,user))
    cur.execute('DELETE FROM platform.hybrid_conversations WHERE workspace_id=%s AND owner_user_no=%s',(workspace,user))
    cur.execute('DELETE FROM platform.hybrid_attachments WHERE workspace_id=%s AND owner_user_no=%s',(workspace,user))
    cur.execute('''DELETE FROM platform.hybrid_document_versions v USING platform.hybrid_documents d
      WHERE v.document_id=d.id AND d.workspace_id=%s AND d.owner_user_no=%s''',(workspace,user))
    cur.execute('DELETE FROM platform.hybrid_documents WHERE workspace_id=%s AND owner_user_no=%s',(workspace,user))
    cur.execute('DELETE FROM platform.personal_cloud_history WHERE owner_key=%s AND owner_user_no=%s',(owner,user))
    cur.execute('DELETE FROM platform.personal_cloud_storage_jobs WHERE owner_key=%s AND owner_user_no=%s',(owner,user))
    cur.execute('DELETE FROM platform.personal_cloud_session_deletions WHERE owner_key=%s',(owner,))

def _complete(owner,user,deletion_id):
    with _connect() as conn,conn.cursor() as cur:
        row=_row(cur,owner,user)
        if str(row['deletion_id'])!=deletion_id:raise ValueError('cloud deletion generation changed')
        _erase_projection(cur,owner,user)
        cur.execute('''UPDATE platform.personal_cloud_retention SET deletion_id=NULL,deletion_started_at=NULL,
          cleared_at=clock_timestamp(),last_access_at=clock_timestamp(),archive_migrated_at=clock_timestamp(),
          last_error_code=NULL WHERE owner_key=%s''',(owner,))

def _has_objects(owner,user):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''SELECT 1 FROM platform.hybrid_attachments WHERE workspace_id=%s
          AND owner_user_no=%s AND object_key IS NOT NULL LIMIT 1''',(str(UUID(hex=owner)),user))
        return cur.fetchone() is not None

def _error(owner,code):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('UPDATE platform.personal_cloud_retention SET last_attempt_at=clock_timestamp(),last_error_code=%s WHERE owner_key=%s',(code,owner))

def _pending(owner):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''SELECT session_id FROM platform.personal_cloud_session_deletions
          WHERE owner_key=%s AND completed_at IS NULL ORDER BY requested_at LIMIT 20''',(owner,))
        return [row['session_id'] for row in cur.fetchall()]

def _archive_migration_needed(owner):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT archive_migrated_at FROM platform.personal_cloud_retention WHERE owner_key=%s',(owner,))
        row=cur.fetchone()
        return row is not None and row['archive_migrated_at'] is None

def _import_archived(owner,user,value):
    ids=value.get('sessionIds')
    if value.get('complete') is not True or not isinstance(ids,list) or len(ids)>2000 or any(not isinstance(sid,str) or not SESSION.fullmatch(sid) for sid in ids):
        raise ValueError('incomplete archived inventory')
    with _connect() as conn,conn.cursor() as cur:
        row=_row(cur,owner,user)
        if row['deletion_id'] is not None:raise IAMFailure(409,'cloud_workspace_cleanup_in_progress')
        for sid in set(ids):
            cur.execute('INSERT INTO platform.personal_cloud_session_deletions(owner_key,session_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(owner,sid))
        cur.execute('UPDATE platform.personal_cloud_retention SET archive_migrated_at=clock_timestamp() WHERE owner_key=%s',(owner,))

def _complete_session(owner,user,sid):
    with _connect() as conn,conn.cursor() as cur:
        row=_row(cur,owner,user)
        if row['deletion_id'] is not None:raise IAMFailure(409,'cloud_workspace_cleanup_in_progress')
        _erase_projects(cur,owner,sid)
        workspace=str(UUID(hex=owner))
        cur.execute('''SELECT request_id,conversation_id FROM platform.enterprise_native_bindings
          WHERE owner_user_no=%s AND location='cloud' AND native_session_id=%s''',(user,sid))
        bindings=cur.fetchall()
        requests=[item['request_id'] for item in bindings]
        conversations=[item['conversation_id'] for item in bindings if item['conversation_id']]
        if requests:
            cur.execute('''SELECT f.id,f.object_key FROM platform.hybrid_attachments f
              WHERE f.workspace_id=%s AND f.owner_user_no=%s
              AND (f.request_id=ANY(%s) OR EXISTS(SELECT 1 FROM platform.hybrid_runs target
                WHERE target.workspace_id=%s AND target.owner_user_no=%s
                  AND target.request_id=ANY(%s) AND f.id=ANY(target.attachment_ids)))
              AND NOT EXISTS(SELECT 1 FROM platform.hybrid_runs r WHERE f.id=ANY(r.attachment_ids)
                AND NOT(r.workspace_id=%s AND r.owner_user_no=%s AND r.request_id=ANY(%s)))
              AND NOT EXISTS(SELECT 1 FROM platform.hybrid_runs uploader
                WHERE uploader.workspace_id=f.workspace_id AND uploader.owner_user_no=f.owner_user_no
                  AND uploader.request_id=f.request_id AND NOT(uploader.request_id=ANY(%s)))
              AND NOT EXISTS(SELECT 1 FROM platform.enterprise_native_bindings uploader
                WHERE uploader.owner_user_no=f.owner_user_no AND uploader.request_id=f.request_id
                  AND NOT(uploader.request_id=ANY(%s)))''',
              (workspace,user,requests,workspace,user,requests,workspace,user,requests,requests,requests))
            attachments=cur.fetchall()
            attachment_ids=[item['id'] for item in attachments]
            keys={item['object_key'] for item in attachments if item['object_key'] is not None}
            from .file_store import FileStore
            for key in keys:
                cur.execute('''SELECT 1 FROM platform.hybrid_attachments WHERE object_key=%s
                  AND NOT(id=ANY(%s)) LIMIT 1''',(key,attachment_ids))
                if not cur.fetchone():FileStore().delete_object(user,workspace,key)
            cur.execute('DELETE FROM platform.hybrid_runs WHERE workspace_id=%s AND owner_user_no=%s AND request_id=ANY(%s)',(workspace,user,requests))
            cur.execute('DELETE FROM platform.hybrid_attachments WHERE workspace_id=%s AND owner_user_no=%s AND id=ANY(%s)',(workspace,user,attachment_ids))
            cur.execute('''DELETE FROM platform.hybrid_conversations c WHERE c.workspace_id=%s AND c.owner_user_no=%s
              AND c.id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM platform.hybrid_runs r WHERE r.conversation_id=c.id)''',(workspace,user,conversations))
        cur.execute('DELETE FROM platform.personal_cloud_history WHERE owner_key=%s AND session_id=%s AND owner_user_no=%s',(owner,sid,user))
        cur.execute("DELETE FROM platform.enterprise_native_bindings WHERE owner_user_no=%s AND location='cloud' AND native_session_id=%s",(user,sid))
        cur.execute('UPDATE platform.personal_cloud_session_deletions SET completed_at=clock_timestamp(),last_error_code=NULL WHERE owner_key=%s AND session_id=%s',(owner,sid))


def _erase_projects(cur,owner,session_id=None):
    # A fixed optional table permits pre-migration read-only checks. Once the
    # project schema exists, cascading metadata erasure is part of this SAME
    # retention transaction. Never leave a second recoverable state store.
    cur.execute("SELECT to_regclass('platform.project_scopes') AS table_name")
    if cur.fetchone()['table_name'] is None:return
    if session_id is None:
        cur.execute('DELETE FROM platform.project_scopes WHERE owner_key=%s',(owner,))
    else:
        cur.execute('DELETE FROM platform.project_scopes WHERE owner_key=%s AND session_id=%s',(owner,session_id))

async def _finish_thread(fn,*args):
    # Cancellation cannot interrupt a DB transaction or filesystem operation.
    # Keep the owner advisory lock until the actual thread has finished, even
    # when shutdown cancels the coroutine more than once.
    task=asyncio.create_task(asyncio.to_thread(fn,*args))
    cancelled=False
    while True:
        try:
            result=await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():raise
            cancelled=True
    if cancelled:raise asyncio.CancelledError
    return result

async def maintain_owner(owner,user,rpc=storage_rpc):
    _identity(owner,user)
    with _connect() as guard,guard.cursor() as gc:
        gc.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked',('cloud-retention:'+owner,))
        if not gc.fetchone()['locked']:return {'status':'busy'}
        try:
            deletion_id,new=await _finish_thread(_claim,owner,user)
            if deletion_id:
                try:
                    result=await rpc(owner,'wipe-workspace')
                    if result.get('status')!='deleted' or result.get('stateDeleted') is not True or result.get('filesDeleted') is not True:
                        raise ValueError('incomplete cloud deletion')
                except IAMFailure as exc:
                    if new and exc.status==409:
                        await _finish_thread(_defer_before_delete,owner,deletion_id)
                        return {'status':'deferred'}
                    raise
                from .file_store import configured,FileStore
                if not configured() and await _finish_thread(_has_objects,owner,user):
                    raise ValueError('cloud object storage unavailable')
                if configured() and not await _finish_thread(FileStore().delete_namespace,user,str(UUID(hex=owner))):
                    raise ValueError('cloud object deletion needs another pass')
                from .project_platform import purge_ephemeral
                await purge_ephemeral(owner)
                await _finish_thread(_complete,owner,user,deletion_id)
                return {'status':'cleared'}
            completed=0
            if await _finish_thread(_archive_migration_needed,owner):
                archived=await rpc(owner,'archived-sessions')
                await _finish_thread(_import_archived,owner,user,archived)
            for sid in await _finish_thread(_pending,owner):
                result=await rpc(owner,'delete-session',sessionId=sid)
                if result.get('status')!='deleted':continue
                from .project_platform import purge_ephemeral
                await purge_ephemeral(owner,sid)
                await _finish_thread(_complete_session,owner,user,sid);completed+=1
            return {'status':'maintained','deleted_sessions':completed}
        except Exception:
            await _finish_thread(_error,owner,'maintenance_incomplete')
            raise
        finally:gc.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',('cloud-retention:'+owner,))

async def maintenance_cycle(batch=10):
    if not enabled():return {'disabled':True}
    with _connect() as conn,conn.cursor() as cur:
        # Once-only bootstrap, not a fresh baseline on each sweep.
        cur.execute('''INSERT INTO platform.personal_cloud_retention(owner_key,workspace_id,owner_user_no)
          SELECT replace(id::text,'-',''),id,owner_user_no FROM platform.hybrid_workspaces WHERE kind='personal'
          ON CONFLICT(owner_key) DO NOTHING''')
        cur.execute('''SELECT owner_key,owner_user_no FROM platform.personal_cloud_retention r
          WHERE deletion_id IS NOT NULL OR archive_migrated_at IS NULL OR GREATEST(policy_started_at,last_access_at)<=clock_timestamp()-interval '30 days'
          OR EXISTS(SELECT 1 FROM platform.personal_cloud_session_deletions d WHERE d.owner_key=r.owner_key AND d.completed_at IS NULL)
          ORDER BY last_attempt_at NULLS FIRST,owner_key LIMIT %s''',(max(1,min(100,batch)),))
        rows=cur.fetchall()
    counts={'cleared':0,'maintained':0,'deferred':0,'failed':0}
    for row in rows:
        try:
            _error(row['owner_key'],'maintenance_started')
            result=await maintain_owner(row['owner_key'],row['owner_user_no'])
            counts[result['status'] if result['status'] in counts else 'deferred']+=1
        except IAMFailure as exc:counts['deferred' if exc.status in (404,409) else 'failed']+=1
        except Exception:counts['failed']+=1
    return counts
