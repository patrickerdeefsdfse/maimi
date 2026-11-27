"""Deliver expiry to the current worker or await verified offline cleanup."""
import asyncio
from collections import defaultdict
from .history_store import _connect

def pending():
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        cur.execute('''SELECT e.file_id,e.owner_key FROM platform.storage_events e
          LEFT JOIN platform.storage_cache_receipts c USING(file_id)
          WHERE e.action='expired' AND c.cleared_at IS NULL
          AND (c.attempted_at IS NULL OR c.attempted_at<clock_timestamp()-interval '4 minutes')
          AND NOT EXISTS (
            SELECT 1 FROM platform.personal_cloud_retention a WHERE a.owner_key=e.owner_key
            AND a.last_access_at>clock_timestamp()-interval '2 minutes'
            AND NOT EXISTS (SELECT 1 FROM platform.worker_runtime_reservations r
              WHERE r.owner_key=e.owner_key AND r.purpose='worker' AND r.state='READY'))
          ORDER BY e.event_id LIMIT 100''')
        return cur.fetchall()

def claim(ids):
    # Claim each group immediately before delivery so replicas do not launch
    # duplicate offline sweeps while another group is still waiting for proof.
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''INSERT INTO platform.storage_cache_receipts AS c(file_id,attempted_at)
          SELECT DISTINCT id,statement_timestamp() FROM unnest(%s::uuid[]) AS f(id) ORDER BY id
          ON CONFLICT(file_id) DO UPDATE SET attempted_at=EXCLUDED.attempted_at
          WHERE c.cleared_at IS NULL AND
            (c.attempted_at IS NULL OR c.attempted_at<clock_timestamp()-interval '4 minutes')
          RETURNING file_id''',(ids,))
        return [str(row['file_id']) for row in cur.fetchall()]

def ack(ids,absent):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE platform.storage_cache_receipts SET cleared_at=clock_timestamp(),acknowledgement=%s
          WHERE file_id=ANY(%s::uuid[]) AND cleared_at IS NULL''',('no_active_worker' if absent else 'native_cache_cleared',ids))

async def cycle():
    from .personal_cloud import manager
    grouped=defaultdict(list)
    for row in await asyncio.to_thread(pending):grouped[row['owner_key']].append(str(row['file_id']))
    cleared=deferred=0
    for owner,ids in grouped.items():
        try:
            ids=await asyncio.to_thread(claim,ids)
            if not ids:continue
            result=await manager('POST','/v1/hosts/'+owner+'/attachments/expire',{'file_ids':ids})
            if result.get('expired') is not True:raise ValueError()
            await asyncio.to_thread(ack,ids,result.get('worker_absent') is True);cleared+=len(ids)
        except Exception:deferred+=len(ids)
    return {'cleared':cleared,'deferred':deferred}
