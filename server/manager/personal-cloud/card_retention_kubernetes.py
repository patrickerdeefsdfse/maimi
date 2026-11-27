"""48-hour card-file cleanup using the same durable owner fence as workers.

Only the state PVC is mounted by an offline helper. Its fixed Python entrypoint
does not start DSH. Unknown helper creation or termination retains the owner.
"""
import hashlib
import secrets
import time
from dataclasses import asdict

from fastapi import HTTPException
from card_retention_manager import validate_result, MAX_BYTES
from k8s_worker_provider import WorkerSpec
from shared_storage_fence import shared_files_exclusion


async def request(manager, record, body):
    value = await manager.worker_request(record, '/__cloud/projects/card-expire',
                                         body, timeout=75, max_bytes=MAX_BYTES)
    try:
        return validate_result(value, body['refs'])
    except ValueError:
        raise HTTPException(503, 'card_cleanup_unavailable') from None


async def expire(manager, owner, body):
    async with manager.store.operation(owner) as op, shared_files_exclusion(manager.store,op):
        record = await manager.store.get(owner)
        if record and record['status'] != 'released':
            if getattr(getattr(manager.store,'capacity',None),'pool',None):
                # An online cleanup response can be lost while deletion is
                # still executing. New pools use a durable helper reservation.
                raise HTTPException(409,'shared_card_cleanup_requires_stopped_chat')
            if record['status'] != 'ready' or record['purpose'] != 'worker':
                raise HTTPException(409, 'personal_cloud_storage_busy')
            return {'result': await request(manager, record, body)}
        storage = await manager.store.get_storage(owner)
        if not storage:
            # A never-opened owner has no admitted state PVC or card files.
            return {'result': {'status': 'COMPLETE', 'items': [
                {'storage_id': row['storage_id'], 'status': 'ABSENT', 'reclaimed_bytes': 0}
                for row in body['refs']]}}
        if storage.get('admitted') is not True:
            raise HTTPException(503, 'card_cleanup_storage_unadmitted')
        record = await manager.store.claim(op, purpose='card',
            expires_at=int(time.time()) + 120,
            catalog_hash=hashlib.sha256(b'{}').hexdigest(), policy_hash=manager.policy_hash)
        handle = None
        try:
            spec = WorkerSpec(owner, record['generation'], mode='card', empty_storage=False,
                resource_class=record.get('resource_class','legacy'),node_name=record.get('execution_node',''))
            await manager.store.assert_operation(op)
            uids = await manager.provider.ensure_owner_storage(spec,
                {'state': storage['state_uid'], 'files': storage['files_uid']})
            await manager.store.assert_operation(op)
            secret_uid = await manager.bootstrap.ensure(spec, secrets.token_urlsafe(32))
            await manager.store.assert_operation(op)
            handle = await manager.provider.create_worker(spec, secret_uid, uids)
            await manager.store.bind(op, record['generation'], asdict(handle))
            record = await manager.store.get(owner)
            token = (await manager.credentials(record))['route_token']
            # Provider verifies the helper's exact authenticated readiness body
            # as well as Pod UID, node and ready EndpointSlice membership.
            import asyncio
            deadline = time.monotonic() + 10
            while True:
                try:
                    await manager.provider.ready(handle, token)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise HTTPException(503, 'card_cleanup_helper_unavailable') from None
                    await asyncio.sleep(.2)
            return {'result': await request(manager, record, body)}
        except BaseException:
            await manager.store.mark_uncertain(op, record['generation'], 'CARD_CLEANUP_INCOMPLETE')
            raise
        finally:
            if handle is not None:
                # Do not return success until both API removal and authenticated
                # node termination evidence authorize releasing this generation.
                try:
                    await manager.terminate(op, record)
                except BaseException:
                    # terminate persisted DELETING before its single DELETE.
                    # Reaper may only observe its completion and obtain fresh
                    # node evidence; it never replays the card expiry request.
                    raise HTTPException(503, 'card_cleanup_termination_unconfirmed') from None
