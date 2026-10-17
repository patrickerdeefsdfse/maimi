"""Structured native history projection and employee-owned 90+30 day lifecycle.

Only idle native volumes are inspected. PostgreSQL commit precedes quarantine;
native tombstones reconcile a crash between filesystem and database commits.
Project/file volumes never enter this protocol. Unknown formats fail closed.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

import httpx
from psycopg.types.json import Jsonb
from .history_store import _connect
from .iam_oauth import IAMFailure

UTC=timezone.utc
OWNER=re.compile(r'^[a-f0-9]{32}$')
DIGEST=re.compile(r'^[a-f0-9]{64}$')


async def storage_rpc(owner, action, **args):
    if not OWNER.fullmatch(owner):raise ValueError('invalid storage owner')
    base=os.getenv('PERSONAL_CLOUD_MANAGER_URL','http://personal-cloud-manager:8092')
    if not re.fullmatch(r'http://[a-z0-9-]+:8092',base):raise IAMFailure(503,'cloud_storage_configuration')
    try:
        secret=Path(os.environ['PERSONAL_CLOUD_MANAGER_SECRET_FILE']).read_text().strip()
        if len(secret)<32:raise ValueError()
        async with httpx.AsyncClient(timeout=100,trust_env=False) as client:
            async with client.stream('POST',base+'/v1/storage/'+owner,
                headers={'Authorization':'Bearer '+secret},json={'action':action,**args}) as response:
                if response.status_code!=200:
                    raise IAMFailure(response.status_code if response.status_code in (404,409) else 503,
                        'cloud_storage_busy_or_refused' if response.status_code==409 else 'cloud_storage_unavailable')
                data=bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>32*1024*1024:raise ValueError()
                return json.loads(data)['result']
    except IAMFailure:raise
    except Exception:raise IAMFailure(503,'cloud_storage_unavailable') from None


def date_ms(value):
    if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<32503680000000:
        raise ValueError('invalid history activity')
    return datetime.fromtimestamp(value/1000,UTC)


def save_inventory(owner,user,snapshot):
    if not OWNER.fullmatch(owner) or not isinstance(snapshot.get('sessions'),list):raise ValueError('invalid inventory')
    with _connect() as conn, conn.cursor() as cur:
        for item in snapshot['sessions']:
            sid=item['sessionId'];digest=item['digest'];events=item['events'];header=item['header']
            if not isinstance(sid,str) or not 1<=len(sid)<=200 or not DIGEST.fullmatch(digest) or not isinstance(events,list) or not isinstance(header,dict):
                raise ValueError('invalid native history')
            title=''
            for event in events:
                if event.get('type')=='session/title' and isinstance(event.get('data',{}).get('title'),str):
                    title=event['data']['title'][:500]
            cur.execute('''INSERT INTO platform.personal_cloud_history
              (owner_key,session_id,owner_user_no,title,native_format,source_digest,last_activity_at,header,event_count)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(owner_key,session_id) DO UPDATE SET title=EXCLUDED.title,
                native_format=EXCLUDED.native_format,source_digest=EXCLUDED.source_digest,
                last_activity_at=EXCLUDED.last_activity_at,header=EXCLUDED.header,event_count=EXCLUDED.event_count,
                restored_at=CASE WHEN platform.personal_cloud_history.status='quarantined' THEN now()
                  ELSE platform.personal_cloud_history.restored_at END,
                status='active',trash_id=NULL,restore_requested_at=NULL,quarantined_at=NULL,purge_after=NULL,synced_at=now()
              WHERE platform.personal_cloud_history.owner_user_no=EXCLUDED.owner_user_no''',
              (owner,sid,user,title,str(item.get('nativeFormat',3)),digest,
               date_ms(item['lastActivityAt']),Jsonb(header),len(events)))
            if cur.rowcount!=1:raise ValueError('history owner mismatch')
            cur.execute('DELETE FROM platform.personal_cloud_history_events WHERE owner_key=%s AND session_id=%s',(owner,sid))
            if events:cur.executemany('INSERT INTO platform.personal_cloud_history_events VALUES(%s,%s,%s,%s)',
                [(owner,sid,i,Jsonb(event)) for i,event in enumerate(events)])


def reconcile(owner,entries):
    with _connect() as conn,conn.cursor() as cur:
        for item in entries:
            if item.get('skipped'):continue
            sid=item['sessionId'];digest=item['digest'];status=item['status']
            cur.execute('SELECT * FROM platform.personal_cloud_history WHERE owner_key=%s AND session_id=%s FOR UPDATE',(owner,sid))
            row=cur.fetchone()
            if not row:continue # No projection receipt: do not purge orphaned artifacts.
            if status in ('prepared','trashed','purging') and item.get('payloadPresent') and row['source_digest']==digest:
                cur.execute('''UPDATE platform.personal_cloud_history SET status='quarantined',trash_id=%s,
                  quarantined_at=%s,purge_after=%s WHERE owner_key=%s AND session_id=%s''',
                  (item['trashId'],date_ms(item['trashedAt']),date_ms(item['purgeAfter']),owner,sid))
            elif status=='restored' and str(row['trash_id'])==item['trashId']:
                cur.execute('''UPDATE platform.personal_cloud_history SET status='active',trash_id=NULL,restore_requested_at=NULL,
                  quarantined_at=NULL,purge_after=NULL,restored_at=%s WHERE owner_key=%s AND session_id=%s''',
                  (date_ms(item.get('restoredAt',item['trashedAt'])),owner,sid))
                audit(cur,owner,sid,'restore',digest)
            elif status=='purged' and str(row['trash_id'])==item['trashId'] and row['source_digest']==digest:
                cur.execute('DELETE FROM platform.personal_cloud_history_events WHERE owner_key=%s AND session_id=%s',(owner,sid))
                cur.execute('''UPDATE platform.personal_cloud_history SET status='purged',header='{}',title='',event_count=0,
                  restore_requested_at=NULL WHERE owner_key=%s AND session_id=%s''',(owner,sid))
                if row['status']!='purged':audit(cur,owner,sid,'purge',digest)


def audit(cur,owner,sid,action,digest):
    cur.execute('INSERT INTO platform.personal_cloud_lifecycle_audit(owner_key,session_id,action,source_digest) VALUES(%s,%s,%s,%s)',(owner,sid,action,digest))


async def maintain_owner(owner,user,rpc=storage_rpc):
    """One bounded, nonoverlapping employee pass; no model or SSO credentials."""
    with _connect() as guard,guard.cursor() as gc:
        gc.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked',('cloud-history:'+owner,))
        if not gc.fetchone()['locked']:return {'status':'busy'}
        try:
            entries=await rpc(owner,'trash')
            reconcile(owner,entries)
            snapshot=await rpc(owner,'inventory')
            save_inventory(owner,user,snapshot) # Transaction committed before any deletion.
            changed=0
            for item in snapshot['sessions']:
                if not snapshot.get('complete') or not item.get('cleanupEligible') or changed>=20:continue
                with _connect() as conn,conn.cursor() as cur:
                    cur.execute('SELECT * FROM platform.personal_cloud_history WHERE owner_key=%s AND session_id=%s FOR UPDATE',(owner,item['sessionId']))
                    row=cur.fetchone();now=datetime.now(UTC)
                    if (not row or row['legal_hold'] or row['restore_requested_at'] or row['source_digest']!=item['digest']
                        or max(row['last_activity_at'],row['restored_at'] or row['last_activity_at'])>now-timedelta(days=90)):continue
                    result=await rpc(owner,'quarantine',sessionId=item['sessionId'],digest=item['digest'],archivedDigest=row['source_digest'])
                    cur.execute('''UPDATE platform.personal_cloud_history SET status='quarantined',trash_id=%s,
                      quarantined_at=%s,purge_after=%s WHERE owner_key=%s AND session_id=%s''',
                      (result['trashId'],date_ms(result['trashedAt']),date_ms(result['purgeAfter']),owner,item['sessionId']))
                    audit(cur,owner,item['sessionId'],'quarantine',item['digest']);changed+=1
            # Restoration wins over expiry. Row lock fences a simultaneous restore request.
            with _connect() as conn,conn.cursor() as cur:
                cur.execute('''SELECT * FROM platform.personal_cloud_history WHERE owner_key=%s AND status='quarantined'
                  AND (restore_requested_at IS NOT NULL OR (purge_after<=now() AND NOT legal_hold))
                  ORDER BY restore_requested_at NULLS LAST,purge_after LIMIT 20 FOR UPDATE''',(owner,))
                for row in cur.fetchall():
                    args={'trashId':str(row['trash_id']),'digest':row['source_digest']}
                    if row['restore_requested_at']:
                        await rpc(owner,'restore',**args)
                    else:await rpc(owner,'purge',sessionId=row['session_id'],archivedDigest=row['source_digest'],**args)
            reconcile(owner,await rpc(owner,'trash'))
            return {'status':'synced','sessions':len(snapshot['sessions']),'skipped':len(snapshot.get('skipped',[]))}
        finally:
            gc.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',('cloud-history:'+owner,))


async def maintenance_cycle(batch=10):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''SELECT replace(w.id::text,'-','') AS owner,w.owner_user_no AS employee
          FROM platform.hybrid_workspaces w LEFT JOIN platform.personal_cloud_storage_jobs j
          ON j.owner_key=replace(w.id::text,'-','') WHERE w.kind='personal' AND w.enabled
          ORDER BY EXISTS(SELECT 1 FROM platform.personal_cloud_history h WHERE h.owner_key=replace(w.id::text,'-','')
            AND h.status='quarantined' AND h.restore_requested_at IS NOT NULL) DESC,
            j.last_attempt_at NULLS FIRST,w.id LIMIT %s''',(max(1,min(100,batch)),))
        owners=cur.fetchall()
    counts={'synced':0,'deferred':0,'failed':0}
    for row in owners:
        owner,user=row['owner'],row['employee']
        with _connect() as conn,conn.cursor() as cur:
            cur.execute('''INSERT INTO platform.personal_cloud_storage_jobs(owner_key,owner_user_no,last_attempt_at)
              VALUES(%s,%s,now()) ON CONFLICT(owner_key) DO UPDATE SET last_attempt_at=now()''',(owner,user))
        try:
            result=await maintain_owner(owner,user)
            with _connect() as conn,conn.cursor() as cur:
                cur.execute('''UPDATE platform.personal_cloud_storage_jobs SET last_success_at=now(),last_error_code=NULL,
                  synced_sessions=%s,skipped_sessions=%s WHERE owner_key=%s''',(result.get('sessions',0),result.get('skipped',0),owner))
            counts['synced']+=1
        except Exception as exc:
            deferred=isinstance(exc,IAMFailure) and exc.status in (404,409)
            code='deferred' if deferred else 'maintenance_failed'
            counts['deferred' if deferred else 'failed']+=1
            with _connect() as conn,conn.cursor() as cur:
                cur.execute('UPDATE platform.personal_cloud_storage_jobs SET last_error_code=%s WHERE owner_key=%s',(code,owner))
    return counts


def trash_for(owner,user,page=0):
    if not isinstance(page,int) or not 0<=page<=10000:raise IAMFailure(400,'invalid_history_page')
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''SELECT session_id,title,last_activity_at,quarantined_at,purge_after,
          restore_requested_at FROM platform.personal_cloud_history WHERE owner_key=%s AND owner_user_no=%s
          AND status='quarantined' ORDER BY quarantined_at DESC,session_id LIMIT 51 OFFSET %s''',(owner,user,page*50))
        return [{k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in row.items()} for row in cur.fetchall()]


def request_restore(owner,user,sid):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE platform.personal_cloud_history SET restore_requested_at=COALESCE(restore_requested_at,now())
          WHERE owner_key=%s AND owner_user_no=%s AND session_id=%s AND status='quarantined'
          AND (purge_after>now() OR legal_hold) RETURNING session_id''',(owner,user,sid))
        if not cur.fetchone():raise IAMFailure(404,'cloud_history_not_recoverable')
    return {'status':'queued','message':'恢复已排队，将在云端工作区闲置后执行。'}
