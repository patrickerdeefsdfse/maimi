"""Bounded online cleanup of the private card-v1 namespace.

Only metadata crosses the manager RPC. The ledger survives project/session
deletion, and registry quota is released only after an exact physical result.
Legacy native CAS and one-time authorized resets use their separate reviewed
plan; this automatic loop never infers their ownership or deletes them.
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
import threading
import time
from uuid import uuid4

from .history_store import _connect
from .cloud_history import storage_rpc

POLICY = 'card-files-48h-v1'
BATCH = 64
LEASE_SECONDS = 180
RETRY_SECONDS = 60
OWNER = re.compile(r'[a-f0-9]{32}')
SID = re.compile(r'cfs_[a-f0-9]{32}')
REASONS = frozenset(('BUSY', 'NOT_EXPIRED', 'REFERENCE_CHANGED', 'FILE_CHANGED',
    'HASH_BUDGET', 'INVALID_DELETE_INTENT', 'INVALID_RECORD', 'OWNER_MISMATCH',
    'DEADLINE', 'UNSAFE_FILE', 'UNSAFE_DIRECTORY', 'TOO_MANY_LINKS', 'IO_ERROR'))
logger = logging.getLogger('megmeet.card-retention')


def enabled():
    return os.getenv('MAIMI_CARD_RETENTION_POLICY', '') == POLICY


def due_owners(connect=_connect, maximum=4):
    if type(maximum) is not int or not 1 <= maximum <= 20:
        raise ValueError('card_retention_batch_invalid')
    with connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT owner_key,min(GREATEST(expires_at,next_attempt_at)) AS first_ready
          FROM platform.card_file_refs WHERE storage_kind='card-v1'
          AND status IN ('pending','active','cleanup_pending')
          AND expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint
          AND next_attempt_at<=floor(extract(epoch FROM clock_timestamp()))::bigint
          AND (status<>'cleanup_pending' OR lease_until<=floor(extract(epoch FROM clock_timestamp()))::bigint)
          GROUP BY owner_key ORDER BY first_ready,owner_key LIMIT %s''', (maximum,))
        rows = cur.fetchall()
    owners = [row['owner_key'] for row in rows]
    if len(owners) > maximum or any(not isinstance(v, str) or not OWNER.fullmatch(v) for v in owners):
        raise ValueError('card_retention_owner_invalid')
    return owners


def claim(owner, connect=_connect):
    if not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise ValueError('card_retention_owner_invalid')
    token = str(uuid4())
    with connect() as conn, conn.cursor() as cur:
        # This is the same owner row used by gateway quota reservation and
        # activation. No request identity or employee business row is returned.
        cur.execute('SELECT owner_key FROM platform.personal_cloud_retention WHERE owner_key=%s FOR UPDATE', (owner,))
        if cur.fetchone() is None:
            return None
        cur.execute('''SELECT session_id,artifact_id,storage_id,sha256,file_bytes,expires_at
          FROM platform.card_file_refs WHERE owner_key=%s AND storage_kind='card-v1'
          AND status IN ('pending','active','cleanup_pending')
          AND expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint
          AND next_attempt_at<=floor(extract(epoch FROM clock_timestamp()))::bigint
          AND (status<>'cleanup_pending' OR lease_until<=floor(extract(epoch FROM clock_timestamp()))::bigint)
          ORDER BY GREATEST(expires_at,next_attempt_at),artifact_id FOR UPDATE SKIP LOCKED LIMIT %s''', (owner, BATCH))
        rows = cur.fetchall()
        for row in rows:
            cur.execute('''UPDATE platform.card_file_refs SET status='cleanup_pending',cleanup_token=%s,
              lease_until=floor(extract(epoch FROM clock_timestamp()))::bigint+%s,
              cleanup_attempts=cleanup_attempts+1,last_error_code=NULL
              WHERE owner_key=%s AND session_id=%s AND artifact_id=%s''',
                (token, LEASE_SECONDS, owner, row['session_id'], row['artifact_id']))
    if not rows:
        return None
    refs = [{'storage_id': row['storage_id'], 'sha256': row['sha256'],
        'bytes': row['file_bytes'], 'expires_at': row['expires_at']} for row in rows]
    validate_refs(refs)
    return {'owner': owner, 'token': token, 'rows': rows, 'refs': refs}


def validate_refs(refs):
    if not isinstance(refs, list) or not 1 <= len(refs) <= BATCH:
        raise ValueError('card_retention_refs_invalid')
    seen = set()
    for row in refs:
        if (not isinstance(row, dict) or set(row) != {'storage_id', 'sha256', 'bytes', 'expires_at'}
            or not isinstance(row['storage_id'], str) or not SID.fullmatch(row['storage_id'])
            or row['storage_id'] in seen or not isinstance(row['sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])
            or type(row['bytes']) is not int or not 1 <= row['bytes'] <= 134217728
            or type(row['expires_at']) is not int or row['expires_at'] < 172800):
            raise ValueError('card_retention_ref_invalid')
        seen.add(row['storage_id'])


def validate_result(value, refs):
    validate_refs(refs)
    expected = {r['storage_id']: r for r in refs}
    if (not isinstance(value, dict) or set(value) != {'status', 'items'}
        or value['status'] not in ('COMPLETE', 'PARTIAL') or not isinstance(value['items'], list)
        or len(value['items']) != len(refs)):
        raise ValueError('card_retention_result_invalid')
    result = {}
    for item in value['items']:
        if (not isinstance(item, dict) or set(item) not in
            ({'storage_id', 'status', 'reclaimed_bytes'},
             {'storage_id', 'status', 'reclaimed_bytes', 'reason'},
             {'storage_id', 'status', 'reclaimed_bytes', 'already_deleted'})
            or item.get('storage_id') not in expected or item['storage_id'] in result
            or item.get('status') not in ('DELETED', 'ABSENT', 'DEFERRED')
            or type(item.get('reclaimed_bytes')) is not int
            or not 0 <= item['reclaimed_bytes'] <= expected[item['storage_id']]['bytes']):
            raise ValueError('card_retention_result_invalid')
        status = item['status']
        if status == 'DEFERRED':
            if (set(item) != {'storage_id', 'status', 'reclaimed_bytes', 'reason'}
                or item['reclaimed_bytes'] != 0 or not isinstance(item.get('reason'), str)):
                raise ValueError('card_retention_result_invalid')
        elif status == 'DELETED':
            if (set(item) != {'storage_id', 'status', 'reclaimed_bytes', 'already_deleted'}
                or type(item['already_deleted']) is not bool
                or (item['already_deleted'] and item['reclaimed_bytes'] != 0)):
                raise ValueError('card_retention_result_invalid')
        elif set(item) != {'storage_id', 'status', 'reclaimed_bytes'} or item['reclaimed_bytes'] != 0:
            raise ValueError('card_retention_result_invalid')
        result[item['storage_id']] = item
    if (value['status'] == 'PARTIAL') != any(r['status'] == 'DEFERRED' for r in result.values()):
        raise ValueError('card_retention_result_invalid')
    return result


def finish(batch, result=None, *, error='RPC_UNAVAILABLE', connect=_connect):
    # Validate the entire acknowledgement before changing any registry row.
    replies = validate_result(result, batch['refs']) if result is not None else None
    if error not in ('RPC_UNAVAILABLE', 'RPC_INVALID', 'RPC_TIMEOUT'):
        error = 'RPC_UNAVAILABLE'
    counts = {'deleted': 0, 'absent': 0, 'deferred': 0, 'stale': 0, 'reclaimed_bytes': 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT owner_key FROM platform.personal_cloud_retention WHERE owner_key=%s FOR UPDATE', (batch['owner'],))
        if cur.fetchone() is None:
            return {**counts, 'stale': len(batch['rows'])}
        for row in batch['rows']:
            identity = (batch['owner'], row['session_id'], row['artifact_id'])
            cur.execute('''SELECT storage_id,sha256,file_bytes,expires_at FROM platform.card_file_refs
              WHERE owner_key=%s AND session_id=%s AND artifact_id=%s AND status='cleanup_pending'
              AND cleanup_token=%s FOR UPDATE''', (*identity, batch['token']))
            current = cur.fetchone()
            expected = {k: row[k] for k in ('storage_id', 'sha256', 'file_bytes', 'expires_at')}
            if current is None:
                counts['stale'] += 1
                continue
            if dict(current) != expected:
                raise ValueError('card_retention_claim_changed')
            reply = replies[row['storage_id']] if replies is not None else None
            if reply is not None and reply['status'] in ('DELETED', 'ABSENT'):
                cur.execute('''DELETE FROM platform.project_artifacts
                  WHERE owner_key=%s AND session_id=%s AND artifact_id=%s AND source='project_output' ''', identity)
                cur.execute('''UPDATE platform.card_file_refs SET status='deleted',cleanup_token=NULL,
                  lease_until=NULL,completed_at=floor(extract(epoch FROM clock_timestamp()))::bigint,
                  reclaimed_bytes=%s,last_error_code=NULL WHERE owner_key=%s AND session_id=%s AND artifact_id=%s''',
                    (reply['reclaimed_bytes'], *identity))
                counts[reply['status'].lower() if reply['status'] == 'ABSENT' else 'deleted'] += 1
                counts['reclaimed_bytes'] += reply['reclaimed_bytes']
            else:
                reason = reply.get('reason') if reply is not None else error
                reason = reason if reason in REASONS or reason in ('RPC_UNAVAILABLE', 'RPC_INVALID', 'RPC_TIMEOUT') else 'NATIVE_DEFERRED'
                # Keep a claimed state and its token so pending reservations
                # cannot disappear from accounting after an interrupted save.
                cur.execute('''UPDATE platform.card_file_refs SET lease_until=floor(extract(epoch FROM clock_timestamp()))::bigint,
                  next_attempt_at=floor(extract(epoch FROM clock_timestamp()))::bigint+%s,last_error_code=%s
                  WHERE owner_key=%s AND session_id=%s AND artifact_id=%s AND cleanup_token=%s''',
                    (RETRY_SECONDS, reason, *identity, batch['token']))
                counts['deferred'] += 1
    return counts


async def maintenance_cycle(*, connect=_connect, rpc=storage_rpc, maximum_owners=4):
    totals = {'owners': 0, 'deleted': 0, 'absent': 0, 'deferred': 0, 'stale': 0, 'reclaimed_bytes': 0}
    for owner in await asyncio.to_thread(due_owners, connect, maximum_owners):
        batch = await asyncio.to_thread(claim, owner, connect)
        if batch is None:
            continue
        error, result = 'RPC_UNAVAILABLE', None
        try:
            result = await asyncio.wait_for(rpc(owner, 'expire-card-files', refs=batch['refs']), timeout=90)
            validate_result(result, batch['refs'])
        except asyncio.TimeoutError:
            error, result = 'RPC_TIMEOUT', None
        except ValueError:
            error, result = 'RPC_INVALID', None
        except Exception:
            error, result = 'RPC_UNAVAILABLE', None
        counts = await asyncio.to_thread(finish, batch, result, error=error, connect=connect)
        totals['owners'] += 1
        for key, value in counts.items():
            totals[key] += value
    return totals


def start_background_worker():
    """A separate bounded loop is not starved by old 30-day maintenance."""
    if not enabled():
        return None
    def loop():
        while True:
            try:
                counts = asyncio.run(maintenance_cycle())
                logger.info('card file cleanup: owners=%s deleted=%s absent=%s deferred=%s stale=%s reclaimed_bytes=%s',
                    *(counts[k] for k in ('owners', 'deleted', 'absent', 'deferred', 'stale', 'reclaimed_bytes')))
            except Exception:
                logger.error('card file cleanup unavailable; retrying')
            time.sleep(60)
    thread = threading.Thread(target=loop, name='card-file-retention', daemon=True)
    thread.start()
    return thread
