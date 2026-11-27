"""Offline exact source deletion, only after every source writer is proven stopped.

The helper occupies the original owner's durable slot and mounts only files PVC.
An uncertain create/delete never releases it, and no cleanup request is replayed
by the reaper. Precise file/version/hash validation is shared with the helper.
"""
import asyncio
import hashlib
import secrets
import time
import uuid
from dataclasses import asdict

from fastapi import HTTPException
from k8s_worker_provider import WorkerSpec
from source_retention_contract import validate_request, validate_result, MAX_RESULT_BYTES
from shared_storage_fence import shared_files_exclusion
STOP_OBSERVATION_SECONDS=45


async def finish_helper(manager,op,record):
    try:
        await manager.terminate(op,record)
        return
    except Exception:
        # A successful DELETE is normally asynchronous. Losing this HTTP
        # result cannot authorize another DELETE or another cleanup request.
        pass
    deadline=time.monotonic()+STOP_OBSERVATION_SECONDS
    while time.monotonic()<deadline:
        current=await manager.store.get(record['owner'])
        if (not current or current['generation']!=record['generation'] or
                current['status']!='deleting' or current.get('handle')!=record['handle']):
            raise HTTPException(503,'source_cleanup_termination_unconfirmed')
        try:
            observed=await manager.provider.observe_deleted(manager.handle(record))
            if observed.get('api_absent'):
                await manager.finish_termination(op,record)
                return
        except Exception:
            pass
        await asyncio.sleep(.2)
    raise HTTPException(503,'source_cleanup_termination_unconfirmed')


async def expire_cache(manager,owner):
    if not manager.store.capacity.pool or manager.store.role!='chat':
        raise HTTPException(503,'cache_cleanup_role_unadmitted')
    return await expire(manager,owner,None,cache=True)


async def expire(manager,owner,body,*,cache=False):
    try: body=validate_request(body) if not cache else None
    except (ValueError,TypeError):
        raise HTTPException(400,'source_cleanup_request_invalid') from None
    async with manager.store.operation(owner) as op, shared_files_exclusion(manager.store,op):
        record=await manager.store.get(owner)
        if record and record['status']!='released':
            raise HTTPException(409,'personal_cloud_storage_busy')
        storage=await manager.store.get_storage(owner)
        if not storage or storage.get('admitted') is not True:
            raise HTTPException(503,'source_cleanup_storage_unadmitted')
        # The route token itself was destroyed with the stopped worker Secret.
        # The one-way hash survives in its audited generation ledger.
        if cache:
            if not record:raise HTTPException(503,'cache_cleanup_history_unavailable')
            maximum=await manager.store.cache_cleanup_max_generation(op)
            body={'protocol':'cache-retention-v1','operation_id':str(uuid.uuid4()),'owner':owner,'maximum_chat_generation':maximum}
            await manager.store.cache_cleanup_event(op,record['generation'],'CACHE_SWEEP_ATTEMPT',maximum)
        else:
            await manager.store.require_stopped_fingerprints(op,
                [ref['runtime_fingerprint'] for ref in body['refs']])
        if not manager.provider.config.image_admission.files_image:
            raise HTTPException(503,'source_cleanup_image_unadmitted')
        record=await manager.store.claim(op,purpose='files',expires_at=int(time.time())+120,
            catalog_hash=hashlib.sha256(b'{}').hexdigest(),policy_hash=manager.policy_hash)
        handle=None;cache_result=None
        try:
            spec=WorkerSpec(owner,record['generation'],mode='files',empty_storage=False,
                resource_class=record.get('resource_class','legacy'),node_name=record.get('execution_node',''))
            await manager.store.assert_operation(op)
            uids=await manager.provider.ensure_owner_storage(spec,
                {'state':storage['state_uid'],'files':storage['files_uid']})
            await manager.store.assert_operation(op)
            secret_uid=await manager.bootstrap.ensure(spec,secrets.token_urlsafe(32))
            await manager.store.assert_operation(op)
            handle=await manager.provider.create_worker(spec,secret_uid,uids)
            await manager.store.bind(op,record['generation'],asdict(handle))
            record=await manager.store.get(owner)
            token=(await manager.credentials(record))['route_token']
            deadline=time.monotonic()+10
            while True:
                try:
                    await manager.provider.ready(handle,token)
                    break
                except Exception:
                    if time.monotonic()>=deadline:
                        raise HTTPException(503,'source_cleanup_helper_unavailable') from None
                    await asyncio.sleep(.2)
            value=await manager.worker_request(record,'/__cloud/files/cache-expire' if cache else '/__cloud/files/expire',body,
                                                timeout=75,max_bytes=MAX_RESULT_BYTES)
            try:
                if cache:
                    from attachment_cache_retention import result
                    cache_result=result(value,body)
                    return cache_result
                return validate_result(value,body)
            except (ValueError,TypeError):
                raise HTTPException(503,'source_cleanup_acknowledgement_invalid') from None
        except BaseException:
            await manager.store.mark_uncertain(op,record['generation'],'SOURCE_CLEANUP_INCOMPLETE')
            raise
        finally:
            if handle is not None:
                # Python finally runs before delivering the return. A helper
                # result without verified node termination is not completion.
                try: await finish_helper(manager,op,record)
                except BaseException:
                    raise HTTPException(503,'source_cleanup_termination_unconfirmed') from None
                if cache_result and cache_result['status']=='COMPLETE':
                    await manager.store.cache_cleanup_event(op,record['generation'],'CACHE_SWEEP_COMPLETE',maximum)
