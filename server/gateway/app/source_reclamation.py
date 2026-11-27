"""Bounded source journal dispatch to a proven-exclusive offline files helper.

The manager returns only after helper termination is independently confirmed.
This gateway never invokes rm or accepts a caller-provided filesystem target.
"""
import asyncio
from uuid import uuid4
from psycopg.types.json import Jsonb
from .history_store import _connect
from .storage_lifecycle import Storage,StorageError,require,configured

REASONS=frozenset(('VERSION_CHANGED','CONTENT_CHANGED','UNSAFE_PATH','NOT_REGULAR','PROTECTED_PATH','IO_UNCONFIRMED'))


def validate_receipt(body,value):
    # Pure validation is also used at the manager HTTP boundary. Never let a
    # successful transport response acknowledge another operation or source.
    expected={row['source_id']:row for row in body['refs']}
    require(type(value) is dict and set(value)=={'status','operation_id','items'} and
        value['operation_id']==body['operation_id'] and type(value['items']) is list and
        len(value['items'])==len(expected),'source_receipt_invalid')
    seen=set();retained=False
    for row in value['items']:
        require(type(row) is dict and row.get('source_id') in expected and row['source_id'] not in seen,'source_receipt_invalid')
        seen.add(row['source_id']);require(type(row.get('reclaimed_bytes')) is int,'source_receipt_invalid')
        if row.get('status')=='RETAINED':
            retained=True;require(set(row)=={'source_id','status','reclaimed_bytes','reason'} and row['reclaimed_bytes']==0 and row['reason'] in REASONS,'source_receipt_invalid')
        else:
            require(set(row)=={'source_id','status','reclaimed_bytes'} and row.get('status') in ('ABSENT','DELETED') and
                row['reclaimed_bytes']==(expected[row['source_id']]['bytes'] if row['status']=='DELETED' else 0),'source_receipt_invalid')
    require(value['status']==('PARTIAL' if retained else 'COMPLETE'),'source_receipt_invalid')
    return value


class Reclamation:
    def __init__(self,connect=_connect):self.connect=connect;self.ledger=Storage(connect=connect)

    def pending_owners(self,maximum=1):
        require(type(maximum) is int and 1<=maximum<=4,'source_batch_invalid',400)
        with self.connect() as conn,conn.cursor() as cur:
            cur.execute('''SELECT owner_key FROM platform.storage_source_copies
              WHERE state='cleanup_required' AND next_attempt_at<=clock_timestamp()
              AND (cleanup_operation IS NULL OR cleanup_lease_until<=clock_timestamp())
              GROUP BY owner_key ORDER BY min(next_attempt_at),owner_key LIMIT %s''',(maximum,))
            return [row['owner_key'] for row in cur.fetchall()]

    def claim(self,owner):
        operation=str(uuid4())
        with self.ledger.transaction(owner) as cur:
            cur.execute('''SELECT * FROM platform.storage_source_copies WHERE owner_key=%s
              AND state='cleanup_required' AND next_attempt_at<=clock_timestamp()
              AND (cleanup_operation IS NULL OR cleanup_lease_until<=clock_timestamp())
              ORDER BY recorded_at,source_id LIMIT 32 FOR UPDATE''',(owner,))
            rows=[];total=0
            for row in cur.fetchall():
                if total+row['expected_bytes']>256*1024**2:break
                total+=row['expected_bytes'];rows.append(row)
            if not rows:return None
            ids=[row['source_id'] for row in rows]
            cur.execute('''UPDATE platform.storage_source_copies SET cleanup_operation=%s,
              cleanup_lease_until=clock_timestamp()+interval '5 minutes',cleanup_attempts=cleanup_attempts+1,
              cleanup_last_error=NULL WHERE source_id=ANY(%s) AND owner_key=%s''',(operation,ids,owner))
            return {'protocol':'source-retention-v1','operation_id':operation,'refs':[
                {'source_id':str(row['source_id']),'path':row['source_path'],'version_hash':row['source_version_hash'],
                 'sha256':row['expected_sha256'],'bytes':row['expected_bytes'],'runtime_fingerprint':row['runtime_fingerprint']}
                for row in rows]}

    def retry(self,owner,body,*,busy=False):
        with self.ledger.transaction(owner) as cur:
            cur.execute('''UPDATE platform.storage_source_copies SET cleanup_operation=NULL,cleanup_lease_until=NULL,
              next_attempt_at=clock_timestamp()+interval '1 minute',cleanup_last_error=%s
              WHERE owner_key=%s AND cleanup_operation=%s AND state='cleanup_required' ''',
              ('OWNER_BUSY' if busy else 'OFFLINE_CLEANUP_UNCONFIRMED',owner,body['operation_id']))

    def acknowledge(self,owner,body,value):
        # Independently validate the closed manager response before any row is
        # changed. A missing, duplicated or forged item acknowledges nothing.
        validate_receipt(body,value)
        counts={'confirmed':0,'manual_review':0,'stale_receipts':0}
        with self.ledger.transaction(owner) as cur:
            for row in value['items']:
                reason=row.get('reason');state='manual_review' if reason else 'removed_confirmed'
                cur.execute('''UPDATE platform.storage_source_copies SET state=%s,cleanup_receipt=%s,
                  cleanup_operation=NULL,cleanup_lease_until=NULL,cleanup_last_error=%s,
                  removed_at=CASE WHEN %s='removed_confirmed' THEN clock_timestamp() ELSE NULL END
                  WHERE source_id=%s AND owner_key=%s AND cleanup_operation=%s AND state='cleanup_required' ''',
                  (state,Jsonb({'operation_id':body['operation_id'],**row}),reason,state,row['source_id'],owner,body['operation_id']))
                counts[('manual_review' if reason else 'confirmed') if cur.rowcount else 'stale_receipts']+=1
        return counts


async def cycle(*,reclamation=None,manager_call=None):
    if not configured():return {'disabled':True}
    if manager_call is None:
        from .personal_cloud import manager as manager_call
    ledger=reclamation or Reclamation();totals={'confirmed':0,'manual_review':0,'stale_receipts':0,'deferred':0}
    for owner in await asyncio.to_thread(ledger.pending_owners):
        body=await asyncio.to_thread(ledger.claim,owner)
        if body is None:continue
        try:
            value=await manager_call('POST','/v1/hosts/'+owner+'/source-expire',body)
            receipt=await asyncio.to_thread(ledger.acknowledge,owner,body,value)
            for key,count in receipt.items():totals[key]+=count
        except asyncio.CancelledError:
            # Cancellation is not proof that the helper stopped. Keep the claim
            # until its bounded journal lease expires; manager retains its fence.
            raise
        except Exception as exc:
            await asyncio.to_thread(ledger.retry,owner,body,busy=getattr(exc,'status',None)==409)
            totals['deferred']+=len(body['refs'])
    return totals
