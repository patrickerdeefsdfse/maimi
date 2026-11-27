"""Gateway-facing Kubernetes manager. No Docker imports or import-time IO.

An owner operation is a PostgreSQL session lock, held across each lifecycle
decision. Unknown Kubernetes outcomes retain the durable generation. API
absence never proves that a worker has stopped executing on its node.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from card_retention_manager import validate_refs, validate_result, MAX_BYTES
from k8s_worker_provider import Handle, WorkerSpec, temporary_turn_owner
from terminal_execution import terminal_execution
from worker_reservations import ReservationError
from execution_pool import RANK

CLOSED_SECONDS = 7200
PRESENCE_SECONDS = 180
CATALOG_MAX_BYTES = 524288
CATALOG_CHUNK_BYTES = 60000
POLICY_FIELDS = ('expires_at', 'catalog_hash', 'touched', 'work_touched',
                 'was_busy', 'clients', 'closed_at', 'release_pending','completed_at')


@dataclass(frozen=True)
class Settings:
    idle_seconds: int = 300
    maximum_hosts: int = 2
    startup_seconds: float = 80
    startup_parallelism: int = 2
    pressure_idle_seconds: int = 60
    completed_idle_seconds: int = 30

    def __post_init__(self):
        if (not 60 <= self.idle_seconds <= 1800
                or not 1 <= self.maximum_hosts <= 768 or not 1 <= self.startup_parallelism <= 16
                or not 30 <= self.pressure_idle_seconds <= self.idle_seconds
                or not 10 <= self.completed_idle_seconds <= self.pressure_idle_seconds
                or not 1 <= self.startup_seconds <= 85):
            raise ValueError('MANAGER_POLICY_INVALID')


class OpenBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lease: str = Field(pattern=r'^mcl_[A-Za-z0-9_-]{43}$')
    catalog: dict
    expires_at: int = Field(strict=True)
    queue_request_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    network_mode: Literal['public', 'confidential'] = 'public'
    temporary_turn: bool = Field(default=False,strict=True)
    resource_class: str = Field(default='standard',pattern=r'^(light|standard|office)$')


class ReleaseHintBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    generation: int = Field(strict=True,gt=0)


class CancelQueueBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')


class HistoryAuthorizeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    generation: int = Field(strict=True, gt=0)
    route_token: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')


class ExpireAttachmentsBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file_ids: list[UUID] = Field(min_length=1, max_length=100)


class HeartbeatBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lease: str = Field(pattern=r'^mcl_[A-Za-z0-9_-]{43}$')
    expires_at: int = Field(strict=True)
    activity: bool = Field(default=False, strict=True)
    presence_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    present: bool = Field(default=True, strict=True)


class StorageBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['inventory', 'trash', 'quarantine', 'restore', 'purge',
        'wipe-workspace', 'delete-session', 'archived-sessions', 'migration-check',
        'migration-apply', 'migration-rollback', 'expire-card-files']
    refs: list[dict] | None = Field(default=None, max_length=64)
    sessionId: str | None = Field(default=None, max_length=200)
    digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    archivedDigest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    trashId: str | None = Field(default=None, pattern=r'^[a-f0-9-]{36}$')


def owner_key(owner):
    if not isinstance(owner, str) or not re.fullmatch('[a-f0-9]{32}', owner):
        raise HTTPException(400, 'invalid_owner')
    return owner


def check_lease(body):
    if not time.time() < body.expires_at <= time.time() + 905:
        raise HTTPException(400, 'invalid_host_lease')


def catalog_bytes(catalog):
    try:
        value = json.dumps(catalog, ensure_ascii=True, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('ascii')
    except (TypeError, ValueError):
        raise HTTPException(400, 'personal_cloud_catalog_invalid') from None
    if len(value) > CATALOG_MAX_BYTES:
        raise HTTPException(400, 'personal_cloud_catalog_too_large')
    return value


def prune_presence(record, now):
    clients = record.setdefault('clients', {})
    expired = [key for key, until in clients.items() if until <= now]
    last = max((clients[key] for key in expired), default=0)
    for key in expired:
        clients.pop(key)
    if expired and not clients:
        record['closed_at'] = max(record.get('closed_at') or 0, last)
    return clients


def update_presence(record, client_id, present, now):
    clients = prune_presence(record, now)
    if present:
        clients[client_id] = now + PRESENCE_SECONDS
        record['closed_at'] = None
    elif client_id in clients:
        clients.pop(client_id)
        if not clients:
            record['closed_at'] = now


def observed_activity(record, busy, now):
    if busy or record.get('was_busy', False):
        record['work_touched'] = now
    record['was_busy'] = busy


def retained(record, now, idle_seconds=1200):
    # Browser visibility/presence is useful UI metadata, never work activity.
    # Persistent PVCs/history are retained independently from compute lifetime.
    prune_presence(record, now)
    return now - max(record.get('work_touched', 0), record['touched']) < idle_seconds


class ManagerService:
    def __init__(self, store, provider, bootstrap, termination, settings=None,
                 *, http_client=None):
        self.store, self.provider, self.bootstrap = store, provider, bootstrap
        self.termination = termination
        self.settings = settings or Settings()
        self.client = http_client or httpx.AsyncClient(trust_env=False,
            follow_redirects=False, timeout=12)
        self.owns_client = http_client is None
        self.startup_slots = asyncio.Semaphore(self.settings.startup_parallelism)
        self.jobs = set()
        self.open_jobs = {}
        self.policy_hash = hashlib.sha256(json.dumps({
            'closed_seconds': CLOSED_SECONDS, 'presence_seconds': PRESENCE_SECONDS,
            'runtime': '0.2.1-rc.3', 'settings': asdict(self.settings)},
            sort_keys=True).encode()).hexdigest()

    async def owned(self, awaitable):
        # Client cancellation does not abandon an in-flight create/delete or
        # release its database operation. Shutdown still leaves a durable row.
        task = asyncio.create_task(awaitable)
        self.jobs.add(task)
        def finished(done):
            self.jobs.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    @staticmethod
    def handle(record):
        if not isinstance(record.get('handle'), dict):
            raise HTTPException(503, 'personal_cloud_worker_state_unavailable')
        pin = record.get('generation_image')
        if 'generation_image' not in record:
            return Handle(**record['handle'])
        from worker_reservations import handle_digest
        if not isinstance(pin, dict) or set(pin) != {'image','handle_sha256'} or not isinstance(pin['image'], str) or pin['handle_sha256'] != handle_digest(record['handle']):
            raise HTTPException(503, 'personal_cloud_generation_image_unconfirmed')
        return Handle(**record['handle'], image_pin=pin['image'])

    async def bind_existing_generation_image(self, op, record, expected_current_image):
        if record['purpose'] != 'worker' or record['status'] != 'ready' or record['owner'] != op.owner or record.get('attention_required'):
            raise HTTPException(409, 'personal_cloud_generation_not_stable')
        handle = self.handle(record)
        spec = self.provider.check_handle(handle)
        if self.provider.admitted_image(spec) != expected_current_image or handle.bound_image not in (None, expected_current_image):
            raise HTTPException(409, 'personal_cloud_image_admission_changed')
        await self.ready(record)
        await self.store.bind_existing_generation_image(op, record, expected_current_image)

    async def save(self, op, record):
        return await self.store.update(op, record['generation'],
            {key: record[key] for key in POLICY_FIELDS if key in record})

    async def credentials(self, record):
        value = await self.bootstrap.read(self.handle(record))
        if not isinstance(value, dict) or not re.fullmatch('[A-Za-z0-9_-]{43}', value.get('route_token', '')):
            raise HTTPException(503, 'personal_cloud_worker_state_unavailable')
        return value

    async def ready(self, record):
        credentials = await self.credentials(record)
        value = await self.provider.ready(self.handle(record), credentials['route_token'])
        if (not isinstance(value, dict) or value.get('pod_uid') != record['handle']['pod_uid']
                or value.get('generation') != record['generation']
                or not re.fullmatch(r'http://[a-z0-9-]+:3084', value.get('origin', ''))
                or not isinstance(value.get('native_workspace_id'), str)):
            raise HTTPException(503, 'personal_cloud_worker_unavailable')
        if record.get('native_workspace_id') and value['native_workspace_id'] != record['native_workspace_id']:
            raise HTTPException(503, 'personal_cloud_worker_unavailable')
        return value

    async def public(self, record):
        credentials = await self.credentials(record)
        handle = self.handle(record)
        execution=self.store.capacity.pool and self.store.role=='execution'
        origin='http://' + handle.name + ('.'+handle.namespace+'.svc.cluster.local' if execution else '') + ':3084'
        result={'origin': origin,
                'route_token': credentials['route_token'],
                'native_workspace_id': record['native_workspace_id'],
                'network_mode': handle.network_mode or 'public',
                'expires_at': record['expires_at']}
        if execution:
            result.update(status='ready',generation=record['generation'],resource_class=record['resource_class'],node_name=handle.node_name)
        return result

    async def worker_request(self, record, path, body=None, *, timeout=12, max_bytes=65536):
        # Validate current Pod + EndpointSlice identity before sending a token.
        info = await self.provider.inspect_worker(self.handle(record))
        if not info or not info.get('ready'):
            raise HTTPException(503, 'personal_cloud_worker_unavailable')
        credentials = await self.credentials(record)
        origin = 'http://' + self.handle(record).name + ':3084'
        async with self.client.stream('GET' if body is None else 'POST', origin + path,
                headers={'Authorization': 'Bearer ' + credentials['route_token']},
                json=body, timeout=timeout) as response:
            raw = bytearray()
            async for part in response.aiter_bytes():
                raw.extend(part)
                if len(raw) > max_bytes:
                    raise HTTPException(503, 'personal_cloud_worker_unavailable')
            if response.status_code != 200:
                raise HTTPException(503, 'personal_cloud_worker_unavailable')
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(503, 'personal_cloud_worker_unavailable') from None

    async def control(self, record, action):
        if action not in ('activity', 'idle-release', 'idle-resume'):
            raise ValueError('WORKER_ACTION_INVALID')
        value = await self.worker_request(record, '/__cloud/' + action,
                                           None if action == 'activity' else {})
        if (not isinstance(value, dict) or set(value) != {'protocol', 'busy', 'draining'}
                or type(value['protocol']) is not int or value['protocol'] != 1
                or type(value['busy']) is not bool or type(value['draining']) is not bool):
            raise HTTPException(503, 'personal_cloud_worker_unavailable')
        return value

    async def resume_draining(self, record):
        if record.get('release_pending'):
            value = await self.control(record, 'idle-resume')
            if value['draining']:
                raise HTTPException(503, 'personal_cloud_worker_resume_failed')
            record['release_pending'] = False

    async def require_record(self, owner, *, expired=False):
        record = await self.store.get(owner)
        if (not record or record['status'] != 'ready' or record['purpose'] != 'worker'
                or (not expired and record['expires_at'] <= time.time())):
            raise HTTPException(409, 'personal_cloud_open_required')
        return record

    async def acknowledge_catalog(self, op, record, body):
        digest = hashlib.sha256(catalog_bytes(body.catalog)).hexdigest()
        execution=self.store.capacity.pool and self.store.role=='execution'
        if digest != record['catalog_hash'] and not execution:
            value = await self.worker_request(record, '/__cloud/catalog',
                                              {'catalog': body.catalog}, timeout=35)
            if not isinstance(value, dict) or value.get('status') != 'updated':
                raise HTTPException(503, 'personal_cloud_catalog_update_failed')
            record['catalog_hash'] = digest
        if execution:
            record['catalog_hash'] = digest
        record['completed_at'] = 0
        record['expires_at'] = max(record['expires_at'], body.expires_at)
        await self.save(op, record)

    async def open(self, owner, body):
        owner_key(owner); check_lease(body); catalog_bytes(body.catalog)
        # Polls share a single task in this replica. PostgreSQL also excludes
        # concurrent creates from other manager replicas.
        existing = self.open_jobs.get(owner)
        if existing and not existing.done():
            return JSONResponse({'status': 'starting', 'phase': 'preparing', 'retry_after': 1}, status_code=202)
        task = asyncio.create_task(self._open(owner, body))
        self.open_jobs[owner] = task
        self.jobs.add(task)
        def finished(done):
            self.jobs.discard(done)
            if self.open_jobs.get(owner) is done:
                self.open_jobs.pop(owner, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except asyncio.TimeoutError:
            return JSONResponse({'status': 'starting', 'phase': 'preparing', 'retry_after': 1}, status_code=202)
        except ReservationError as exc:
            if str(exc) == 'OWNER_OPERATION_BUSY':
                return JSONResponse({'status': 'starting', 'phase': 'preparing', 'retry_after': 1}, status_code=202)
            code = str(exc)
            if code in ('QUEUE_EXPIRED','QUEUE_CANCELLED','QUEUE_REQUEST_NOT_FOUND','QUEUE_ADMITTED'):
                raise HTTPException(409, 'personal_cloud_' + code.lower()) from None
            if code == 'QUEUE_FULL':
                raise HTTPException(429, 'personal_cloud_queue_full', headers={'Retry-After': '10'}) from None
            raise

    async def _open(self, owner, body):
        temporary=temporary_turn_owner(owner)
        if (body.temporary_turn!=temporary or
            temporary and (self.store.role!='execution' or body.network_mode!='confidential' or body.resource_class!='office')):
            raise HTTPException(403,'temporary_turn_scope_invalid')
        async with self.store.operation(owner) as op:
            await self.store.require_no_placement_transfer(op)
            record = await self.store.get(owner)
            if temporary and record and (record['status']=='released' or record.get('storage_uids')):
                raise HTTPException(409,'temporary_turn_closed')
            if record and record['status'] != 'released':
                if record['purpose'] != 'worker':
                    raise HTTPException(409, 'personal_cloud_storage_busy')
                bound_mode = (record.get('handle') or {}).get('network_mode') or 'public'
                if record.get('handle') and bound_mode != body.network_mode:
                    raise HTTPException(409, 'personal_cloud_network_mode_mismatch')
                # Recovery observes the already bound generation; it never
                # recreates a missing API object or changes a persisted UID.
                if record['status'] in ('starting', 'uncertain') and record.get('handle'):
                    value = await self.ready(record)
                    await self.store.publish_ready(op, record['generation'], value['native_workspace_id'])
                    record = await self.store.get(owner)
                if record['status'] != 'ready':
                    if record['status'] == 'deleting' and self.store.capacity.pool:
                        return JSONResponse({'status':'starting','phase':'releasing_previous','retry_after':2},status_code=202)
                    raise HTTPException(503, 'personal_cloud_worker_state_unavailable')
                await self.ready(record)
                if self.store.capacity.pool and self.store.role=='execution' and RANK[body.resource_class] > RANK[record['resource_class']]:
                    value=await self.control(record,'idle-release')
                    if value['busy'] or not value['draining']:
                        raise HTTPException(409,'resource_class_busy')
                    record['release_pending']=True
                    await self.save(op,record)
                    # Never rerun the caller's operation. It polls and obtains a
                    # new generation only after independent termination proof.
                    await self.store.mark_deleting(op,record['generation'])
                    await self.provider.delete_worker(self.handle(record))
                    return JSONResponse({'status':'starting','phase':'releasing_for_resource_upgrade','retry_after':2},status_code=202)
                await self.store.reconcile_succeeded(op, record['generation'])
                await self.resume_draining(record)
                await self.acknowledge_catalog(op, record, body)
                return await self.public(record)
            encoded = catalog_bytes(body.catalog)
            record = await self.store.admit(op, expires_at=body.expires_at,
                catalog_hash=hashlib.sha256(encoded).hexdigest(), policy_hash=self.policy_hash,
                queue_request_id=body.queue_request_id,
                resource_class=('chat' if self.store.role=='chat' else body.resource_class) if self.store.capacity.pool else 'legacy')
            if record.get('status') == 'queued' or record.get('phase')=='admission_busy':
                return JSONResponse(record, status_code=202)
            try:
                spec = WorkerSpec(owner, record['generation'],
                    max(1, (len(encoded) + CATALOG_CHUNK_BYTES - 1) // CATALOG_CHUNK_BYTES),
                    mode='worker', empty_storage=not record.get('storage_admitted', False), network_mode=body.network_mode,
                    resource_class=record.get('resource_class','legacy'),node_name=record.get('execution_node',''))
                async with self.startup_slots:
                    await self.store.assert_operation(op)
                    uids = await self.provider.ensure_owner_storage(spec, record.get('storage_uids') or None)
                    if not temporary:
                        await self.store.bind_storage(op, record['generation'], dict(uids,
                            namespace=self.provider.config.namespace,
                            state_name=self.provider.state_claim_name(spec), files_name='mmw-' + owner + '-files'))
                    token = secrets.token_urlsafe(32)
                    # Preserve only a one-way identity before any external
                    # creation. Released Secret deletion must not erase proof
                    # of which generation wrote a temporary workspace source.
                    await self.store.bind_runtime_fingerprint(op,record['generation'],hashlib.sha256(token.encode('ascii')).hexdigest())
                    await self.store.assert_operation(op)
                    secret_uid = await self.bootstrap.ensure(spec, token, lease=body.lease, catalog=body.catalog)
                    await self.store.assert_operation(op)
                    handle = await self.provider.create_worker(spec, secret_uid, uids)
                    await self.store.bind(op, record['generation'], handle)
                    record = await self.store.get(owner)
                    deadline = time.monotonic() + self.settings.startup_seconds
                    while True:
                        try:
                            value = await self.ready(record)
                            break
                        except Exception:
                            if time.monotonic() >= deadline:
                                raise HTTPException(503, 'personal_cloud_start_timeout') from None
                            await asyncio.sleep(0.25)
                    await self.store.publish_ready(op, record['generation'], value['native_workspace_id'])
                    record = await self.store.get(owner)
                    record.update(touched=time.time(), clients={}, closed_at=None,
                                  work_touched=0, was_busy=False, release_pending=False)
                    await self.save(op, record)
                    return await self.public(record)
            except BaseException:
                # Includes cancelled manager shutdown and lost create response.
                # No automatic release, delete retry, or replacement generation.
                await self.store.mark_uncertain(op, record['generation'], 'WORKER_START_INCOMPLETE')
                raise

    async def cancel_queue(self, owner, body):
        owner_key(owner)
        async with self.store.operation(owner) as op:
            return await self.store.cancel_waiting(op, body.request_id)

    async def release_hint(self, owner, body):
        owner_key(owner)
        async with self.store.operation(owner) as op:
            record=await self.store.get(owner)
            if not record or record['generation']!=body.generation or record['status']!='ready':
                raise HTTPException(409,'execution_generation_changed')
            record['completed_at']=time.time()
            record['work_touched']=record['completed_at']
            await self.save(op,record)
            return {'status':'release_scheduled','generation':body.generation,
                    'idle_grace_seconds':self.settings.completed_idle_seconds}

    async def history_authorize(self, owner, body):
        owner_key(owner)
        # No owner-operation lock: startup holds that lock while the worker
        # restores history. Requiring it here would deadlock cold startup.
        record = await self.store.get(owner)
        if (not record or record['generation'] != body.generation or
                record['status'] not in ('starting','ready','deleting','uncertain') or
                not record.get('handle') or record['purpose'] != 'worker'):
            raise HTTPException(403, 'personal_cloud_history_authority_refused')
        credentials = await self.credentials(record)
        if not hmac.compare_digest(credentials['route_token'].encode(), body.route_token.encode()):
            raise HTTPException(403, 'personal_cloud_history_authority_refused')
        # This authenticates a generation, not an atomic PG write. The history
        # transaction must fence against the live ledger generation itself.
        return {'authorized': True, 'generation': body.generation}

    async def reconcile_requested(self, owner):
        """Operator-only single observation after attention; never create/replay."""
        owner_key(owner)
        async with self.store.operation(owner) as op:
            record = await self.store.get(owner)
            if not record or record['status'] == 'released':
                return {'status': 'not_active'}
            # This endpoint is deliberately absent from employee gateway routes.
            # An operator can retry after repairing node/API connectivity.
            await self.store.reconcile_succeeded(op, record['generation'])
            try:
                await self.reconcile_one(op, record)
            except Exception:
                await self.store.reconcile_failed(op, record['generation'], 'WORKER_RECONCILE_UNCONFIRMED')
                return {'status': 'unconfirmed', 'generation': record['generation']}
            current = await self.store.get(owner)
            return {'status': current['status'], 'generation': record['generation']}

    async def expire_attachments(self, owner, body):
        owner_key(owner)
        async with self.store.operation(owner) as op:
            record=await self.store.get(owner)
            if not record or record['status']=='released':
                if not record or not self.store.capacity.pool:
                    return {'expired': True, 'worker_absent': True}
            else:
                if record['purpose']!='worker' or not record.get('handle'):
                    raise HTTPException(503,'personal_cloud_worker_state_unavailable')
                await self.store.assert_operation(op)
                result=await self.worker_request(record,'/__cloud/attachments/expire',body.model_dump(mode='json'))
                if not isinstance(result,dict) or result.get('expired') is not True:
                    raise HTTPException(503,'personal_cloud_attachment_expiry_unconfirmed')
                return {'expired': True}
        # An absent A process no longer proves its shared NAS cache was removed.
        # The offline helper reacquires both role locks and verifies stopped A/C.
        from source_retention_kubernetes import expire_cache
        result=await expire_cache(self,owner)
        if result['status']!='COMPLETE':raise HTTPException(503,'personal_cloud_attachment_expiry_unconfirmed')
        return {'expired':True,'offline_cache_verified':True}

    async def host(self, owner, touch=False):
        owner_key(owner)
        async with self.store.operation(owner) as op:
            record = await self.require_record(owner)
            if touch:
                await self.resume_draining(record)
                record['touched'] = time.time()
            await self.save(op, record)
            return await self.public(record)

    async def renew(self, owner, body, verify=False):
        owner_key(owner); check_lease(body)
        async with self.store.operation(owner, wait=True) as op:
            record = await self.require_record(owner, expired=True)
            if verify or record['expires_at'] <= time.time():
                await self.ready(record)
            record['expires_at'] = max(record['expires_at'], body.expires_at)
            if body.activity:
                await self.resume_draining(record)
                record['touched'] = time.time()
            if body.presence_id:
                update_presence(record, body.presence_id, body.present, time.time())
            # body.lease is client authority, never the running task lease.
            await self.save(op, record)
            return await self.public(record)

    async def catalog(self, owner, body):
        owner_key(owner); check_lease(body); catalog_bytes(body.catalog)
        async with self.store.operation(owner) as op:
            record = await self.require_record(owner)
            await self.resume_draining(record)
            await self.acknowledge_catalog(op, record, body)
            return await self.public(record)

    async def activity(self, owner):
        owner_key(owner)
        async with self.store.operation(owner, wait=True) as op:
            record = await self.require_record(owner)
            busy = None
            try:
                busy = (await self.control(record, 'activity'))['busy']
            except Exception:
                pass
            now = time.time()
            if busy is not None:
                observed_activity(record, busy, now)
                await self.save(op, record)
            return {'status': 'ready', 'idle_seconds': self.settings.idle_seconds,
                'idle_for_seconds': max(0, int(now - max(record['touched'], record.get('work_touched', 0)))),
                'busy': busy}

    async def terminate(self, op, record):
        if temporary_turn_owner(record['owner']):
            # The execution agent holds its write lock and verifies that all
            # published results were acknowledged before reporting draining.
            # A broken control channel cannot authorize discarding task files.
            drain=await self.control(record,'idle-release')
            if drain.get('busy') is not False or drain.get('draining') is not True:
                raise ReservationError('TEMPORARY_TURN_HANDOFF_PENDING')
        await self.store.mark_deleting(op, record['generation'])
        handle = self.handle(record)
        await self.store.assert_operation(op)
        result = await self.provider.delete_worker(handle)
        if not result.get('api_absent'):
            raise HTTPException(503, 'personal_cloud_termination_unconfirmed')
        await self.finish_termination(op, record)

    async def finish_termination(self, op, record):
        handle = self.handle(record)
        proof = await self.termination(handle)
        verified = await self.termination.verify(record, proof)
        await self.store.assert_operation(op)
        await self.bootstrap.cleanup(handle, verified)
        await self.store.release(op, record['generation'], proof)

    async def reap_once(self):
        await self.store.expire_waiting()
        for candidate in await self.store.list_active():
            try:
                async with self.store.operation(candidate['owner']) as op:
                    record = await self.store.get(candidate['owner'])
                    if not record or record['generation'] != candidate['generation']:
                        continue
                    after = record.get('reconcile_after')
                    if after and datetime.fromisoformat(after) > datetime.now(timezone.utc):
                        continue
                    attention = record.get('attention_required')
                    if attention and not (getattr(self.store,'role',None)=='execution'
                            and record['purpose']=='worker' and record.get('handle')
                            and record['status'] in ('ready','deleting')):
                        continue
                    try:
                        if attention and record['status']=='ready':
                            if not await terminal_execution(self, record):
                                raise ReservationError('EXECUTION_TERMINATION_UNCONFIRMED')
                            await self.terminate(op, record)
                        else:
                            await self.reconcile_one(op, record)
                    except Exception:
                        await self.store.reconcile_failed(op, record['generation'], 'WORKER_RECONCILE_UNCONFIRMED')
            except Exception:
                # A failure is not a termination proof. The durable row stays.
                continue
        if self.store.capacity.pool and getattr(self.store,'role',None)=='chat':
            from source_retention_kubernetes import expire_cache
            for owner in await self.store.list_cache_cleanup():
                try:await expire_cache(self,owner)
                except Exception:pass  # Durable attempt backoff/unknown slot remain.

    async def reconcile_one(self, op, record):
        if record['status'] == 'deleting' and record.get('handle'):
            result = await self.provider.observe_deleted(self.handle(record))
            if not result.get('api_absent'):
                if await terminal_execution(self, record):
                    await self.terminate(op, record)
                    return
                raise ReservationError('TERMINATION_NOT_OBSERVED')
            await self.finish_termination(op, record)
            return
        if record['status'] in ('starting', 'uncertain'):
            if not record.get('handle'):
                # Lost create response has no trustworthy UID. Never adopt by
                # name or invent a stop proof; bounded retries raise an alert.
                raise ReservationError('UNBOUND_GENERATION_UNCONFIRMED')
            if record['purpose'] != 'worker':
                # A crashed maintenance controller must not replay deletions or
                # create another helper. Once its exact API objects are gone,
                # original node evidence can still complete owner release.
                result=await self.provider.observe_deleted(self.handle(record))
                if not result.get('api_absent'):
                    raise ReservationError('MAINTENANCE_TERMINATION_UNCONFIRMED')
                await self.store.mark_uncertain(op,record['generation'],'MAINTENANCE_STOP_OBSERVED')
                await self.finish_termination(op,record)
                return
            try:
                value = await self.ready(record)
            except Exception:
                result = await self.provider.observe_deleted(self.handle(record))
                if not result.get('api_absent'):
                    raise
                await self.store.mark_uncertain(op, record['generation'])
                await self.finish_termination(op, record)
                return
            await self.store.publish_ready(op, record['generation'], value['native_workspace_id'])
            await self.store.reconcile_succeeded(op, record['generation'])
            return
        if record['purpose'] != 'worker' or record['status'] != 'ready':
            raise ReservationError('WORKER_RECONCILE_STATE_UNCONFIRMED')
        try:
            value = await self.control(record, 'activity')
        except Exception:
            if not await terminal_execution(self, record):
                raise
            await self.terminate(op, record)
            return
        observed_activity(record, value['busy'], time.time())
        idle_seconds=self.settings.idle_seconds
        if self.store.capacity.pool and await self.store.waiting_on_node(record.get('execution_node','')):
            idle_seconds=min(idle_seconds,self.settings.pressure_idle_seconds)
        if record.get('completed_at',0)>0:
            # The hint is advisory. A busy execution is never stopped; activity
            # after the hint also receives the ordinary idle grace.
            idle_seconds=min(idle_seconds,self.settings.completed_idle_seconds)
        if temporary_turn_owner(record['owner']):
            # Individual tool completions are not end-of-turn receipts.
            idle_seconds=max(idle_seconds,300)
        keep = value['busy'] or retained(record, time.time(), idle_seconds)
        await self.save(op, record)
        if keep:
            await self.store.reconcile_succeeded(op, record['generation'])
            return
        # The worker atomically blocks new execution before acknowledging drain.
        # This second observation closes the activity-check/start-task race.
        value = await self.control(record, 'idle-release')
        record['release_pending'] = value['draining']
        if value['busy']:
            observed_activity(record, True, time.time())
        await self.save(op, record)
        if value['busy'] or not value['draining']:
            return
        await self.terminate(op, record)

    async def status(self):
        metrics = await self.store.lifecycle_metrics()
        capacity = self.store.capacity
        return {'status': 'degraded' if metrics['attention_required'] else 'ready',
            'pool_role':getattr(self.store,'role','chat'),
            'node_budgets':capacity.pool.summary() if capacity.pool else [],
            'active_hosts': sum(metrics['states'].values()),
            'maximum_hosts': capacity.maximum_hosts, 'waiting_hosts': metrics['waiting'],
            'queue_limit': capacity.queue_limit, 'host_memory_limit_mib': capacity.memory_mib,
            'idle_release_seconds': self.settings.idle_seconds,
            'closed_retention_seconds': self.settings.idle_seconds, 'presence_seconds': PRESENCE_SECONDS,
            'general_maximum_hosts': self.settings.maximum_hosts,
            'host_slot_limit_enabled': True,
            'reserved_slots': 0, 'memory_guard_enabled': True,
            'minimum_available_mib': 0, 'worker_runtime': self.provider.config.image_admission.runtime,
            'worker_runtime_scope': 'new_workspaces',
            'runtime_admission_enabled': True, 'lifecycle': metrics,
            'capacity': asdict(capacity), 'queue_requires_authenticated_poll': True,
            'process_limit_enforcement': 'requires_qualified_node_kubelet_podPidsLimit',
            'ephemeral_storage_enforcement': 'requires_qualified_node_filesystem_accounting'}

    async def storage(self, owner, body):
        from card_retention_kubernetes import expire
        owner_key(owner)
        if body.action != 'expire-card-files':
            # Preserve the existing native-0.2.1 maintenance boundary.
            raise HTTPException(503, 'personal_cloud_native021_maintenance_unreviewed')
        if any(value is not None for key, value in body.model_dump().items()
               if key not in ('action', 'refs')):
            raise HTTPException(400, 'card_cleanup_arguments')
        try:
            value = validate_refs(body.refs)
        except ValueError:
            raise HTTPException(400, 'card_cleanup_arguments') from None
        return await expire(self, owner, value)


def create_app(service, token, *, run_reaper=True, metrics_token=None):
    if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError('MANAGER_SECRET_INVALID')
    if metrics_token is not None and (not isinstance(metrics_token,str) or len(metrics_token)<32 or
            any(c.isspace() for c in metrics_token) or hmac.compare_digest(metrics_token,token)):
        raise ValueError('METRICS_SECRET_INVALID')

    async def reaper():
        while True:
            await asyncio.sleep(10)
            try:
                await service.reap_once()
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(reaper()) if run_reaper else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for job in list(service.jobs):
                job.cancel()
            await asyncio.gather(*list(service.jobs), return_exceptions=True)
            if service.owns_client:
                await service.client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None)
    app.state.manager = service

    @app.middleware('http')
    async def authenticated_bounded(request, call_next):
        supplied = request.headers.get('authorization', '')
        expected = metrics_token if request.url.path == '/metrics' and metrics_token else token
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + expected).encode()):
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 600 * 1024:
                return JSONResponse({'detail': 'request_too_large'}, status_code=413)
        request._body = bytes(raw)
        return await call_next(request)

    @app.exception_handler(ReservationError)
    async def reservation_failure(request,exc):
        if str(exc) == 'OWNER_OPERATION_BUSY':
            return JSONResponse({'detail':'personal_cloud_owner_busy'},status_code=503,headers={'Retry-After':'1'})
        if str(exc) in ('SHARED_FILES_OTHER_ROLE_BUSY','SHARED_FILES_OTHER_OPERATION_BUSY'):
            return JSONResponse({'detail':'shared_files_busy'},status_code=409)
        return JSONResponse({'detail':'personal_cloud_worker_unavailable'},status_code=503)

    @app.exception_handler(Exception)
    async def safe_failure(request, exc):
        return JSONResponse({'detail': 'personal_cloud_worker_unavailable'}, status_code=503)

    @app.get('/v1/status')
    async def status():
        return await service.status()

    @app.get('/v1/ready')
    async def readiness():
        # A reservation requiring attention still consumes capacity and remains
        # visible in /v1/status. Probe the shared admission ledger separately.
        if not await service.store.serving_ready():
            raise HTTPException(503, 'personal_cloud_worker_unavailable')
        return {'status': 'ready', 'pool_role': service.store.role}

    @app.get('/metrics', response_class=PlainTextResponse)
    async def metrics():
        value = await service.store.lifecycle_metrics()
        lines = []
        for state in ('CLAIMED','BOUND','READY','UNCERTAIN','DELETING'):
            lines.append('maimi_worker_generations{state="' + state.lower() + '"} ' + str(value['states'].get(state, 0)))
        for key in ('waiting','oldest_wait_seconds','cpu_millis','memory_mib','ephemeral_mib','attention_required','oldest_uncertain_seconds'):
            lines.append('maimi_worker_' + key + ' ' + str(value[key]))
        for state in ('ADMITTED','CANCELLED','EXPIRED'):
            lines.append('maimi_worker_queue_outcomes{state="' + state.lower() + '"} ' + str(value['queue_totals'].get(state, 0)))
        role=getattr(service.store,'role','chat')
        for key in ('waiting','oldest_wait_seconds','oldest_uncertain_seconds','attention_required','cpu_millis','memory_mib','ephemeral_mib'):
            lines.append('maimi_pool_'+key+'{pool="'+role+'"} '+str(value[key]))
        for allocation in value.get('allocations',[]):
            labels='pool="'+role+'",node="'+allocation['execution_node']+'",resource_class="'+allocation['resource_class']+'",state="'+allocation['state'].lower()+'"'
            for key in ('occupied','cpu_millis','memory_mib','ephemeral_mib'):
                lines.append('maimi_pool_allocation_'+key+'{'+labels+'} '+str(allocation[key]))
        for queue in value.get('node_queues',[]):
            for key in ('waiting','oldest_wait_seconds'):
                lines.append('maimi_pool_node_'+key+'{pool="'+role+'",node="'+queue['execution_node']+'"} '+str(queue[key]))
        if service.store.capacity.pool:
            for node in service.store.capacity.pool.nodes:
                for key in ('cpu','memory','ephemeral','slots'):
                    lines.append('maimi_pool_budget_'+key+'{pool="'+role+'",node="'+node.name+'"} '+str(getattr(node,key)))
        return PlainTextResponse('\n'.join(lines) + '\n', media_type='text/plain; version=0.0.4')

    @app.post('/v1/hosts/{owner}/open')
    async def open_host(owner: str, body: OpenBody):
        return await service.open(owner, body)

    @app.post('/v1/hosts/{owner}/queue/cancel')
    async def cancel_queue(owner: str, body: CancelQueueBody):
        return await service.owned(service.cancel_queue(owner, body))

    @app.post('/v1/hosts/{owner}/release-hint')
    async def release_hint(owner: str, body: ReleaseHintBody):
        return await service.owned(service.release_hint(owner,body))

    @app.post('/v1/hosts/{owner}/history-authorize')
    async def history_authorize(owner: str, body: HistoryAuthorizeBody):
        return await service.history_authorize(owner, body)

    @app.post('/v1/hosts/{owner}/reconcile-once')
    async def reconcile_requested(owner: str):
        return await service.owned(service.reconcile_requested(owner))

    @app.post('/v1/hosts/{owner}/attachments/expire')
    async def expire_attachments(owner: str, body: ExpireAttachmentsBody):
        return await service.owned(service.expire_attachments(owner, body))

    @app.post('/v1/hosts/{owner}/source-expire')
    async def expire_sources(owner: str, request: Request):
        from source_retention_kubernetes import expire
        owner_key(owner)
        raw=await request.body()
        try:
            if len(raw)>160*1024: raise ValueError()
            body=json.loads(raw)
        except (ValueError,UnicodeError):
            raise HTTPException(400,'source_cleanup_request_invalid') from None
        return await service.owned(expire(service,owner,body))

    @app.post('/v1/hosts/{owner}/cache-expire')
    async def expire_cache(owner: str):
        from source_retention_kubernetes import expire_cache
        owner_key(owner)
        return await service.owned(expire_cache(service,owner))

    @app.get('/v1/hosts/{owner}')
    async def host(owner: str, touch: bool = False):
        return await service.owned(service.host(owner, touch))

    @app.post('/v1/hosts/{owner}/resume')
    async def resume(owner: str, body: HeartbeatBody):
        return await service.owned(service.renew(owner, body, True))

    @app.post('/v1/hosts/{owner}/heartbeat')
    async def heartbeat(owner: str, body: HeartbeatBody):
        return await service.owned(service.renew(owner, body))

    @app.post('/v1/hosts/{owner}/catalog')
    async def catalog(owner: str, body: OpenBody):
        return await service.owned(service.catalog(owner, body))

    @app.get('/v1/hosts/{owner}/activity')
    async def activity(owner: str):
        return await service.owned(service.activity(owner))

    @app.post('/v1/storage/{owner}')
    async def storage(owner: str, body: StorageBody):
        return await service.owned(service.storage(owner, body))

    return app


def build_from_env():
    """Concrete production wiring, selected only by manager_entry's K8s branch."""
    from k8s_bootstrap import BootstrapSecrets, RotatingTokenFile
    from k8s_worker_provider import Config, ImageAdmission, KubernetesHTTPS, KubernetesWorkerProvider
    from node_execution_observer import ObserverClient,read_key
    from execution_pool import ExecutionPool,PoolObserver
    from worker_reservations import PostgresReservations
    from sandbox_lifecycle import CapacityPolicy

    def required(name):
        value = os.environ.get(name, '').strip()
        if not value:
            raise ValueError('MANAGER_CONFIGURATION_REQUIRED:' + name)
        return value

    prefix = 'PERSONAL_CLOUD_K8S_'
    role=required('MAIMI_POOL_ROLE')
    if role not in ('chat','execution'):
        raise ValueError('POOL_ROLE_INVALID')
    pool=ExecutionPool.load(required(prefix+'POOL_FILE'))
    if int(os.environ.get('PERSONAL_CLOUD_CLOSED_SECONDS', '7200')) != CLOSED_SECONDS:
        raise ValueError('CLOSED_RETENTION_MUST_BE_7200')
    # Reserved employees need a distributed admission policy before enabling;
    # do not silently ignore an old Docker-only reservation configuration.
    if os.environ.get('PERSONAL_CLOUD_RESERVED_OWNERS', '').strip():
        raise ValueError('K8S_RESERVED_OWNER_POLICY_NOT_CONFIGURED')
    settings = Settings(idle_seconds=int(os.environ.get('PERSONAL_CLOUD_IDLE_SECONDS', '300')),
        maximum_hosts=int(required('PERSONAL_CLOUD_MAX_ACTIVE_HOSTS')),
        startup_parallelism=int(os.environ.get(prefix + 'STARTUP_PARALLELISM', '3')),
        pressure_idle_seconds=int(os.environ.get(prefix+'PRESSURE_IDLE_SECONDS','60')),
        completed_idle_seconds=int(os.environ.get(prefix+'COMPLETED_IDLE_SECONDS','30')))
    namespace = required(prefix + 'NAMESPACE')
    admission = ImageAdmission.load(required(prefix + 'IMAGE_ADMISSION_FILE'),
                                    required(prefix + 'IMAGE_ADMISSION_SHA256'))
    config = Config(namespace=namespace,
        instance=os.environ.get('PERSONAL_CLOUD_MANAGER_NAMESPACE', 'megmeet-personal-v1'),
        image=admission.worker_image, image_admission=admission,
        state_storage_class=required(prefix + 'STATE_STORAGE_CLASS'),
        files_storage_class=required(prefix + 'FILES_STORAGE_CLASS'),
        worker_node=required(prefix + 'WORKER_NODE'),
        model_base_url=os.environ.get('PERSONAL_CLOUD_MODEL_BASE_URL',
            'http://gateway:8080/internal/personal-cloud/model/v1'),
        runtime_class=required(prefix + 'RUNTIME_CLASS'),
        state_access_mode=os.environ.get(prefix + 'STATE_ACCESS_MODE', 'ReadWriteOnce'),
        files_access_mode=os.environ.get(prefix + 'FILES_ACCESS_MODE', 'ReadWriteMany'),
        state_gib=int(os.environ.get(prefix + 'STATE_GIB', '2')),
        files_gib=int(os.environ.get(prefix + 'FILES_GIB', '5')),
        cpu_millis=int(os.environ.get(prefix + 'CPU_MILLIS', '1000')),
        memory_mib=int(os.environ.get(prefix + 'MEMORY_MIB', '1536')),
        ephemeral_mib=int(os.environ.get(prefix + 'EPHEMERAL_MIB', '1024')),
        execution_pool=pool,pool_role=role,
        shared_files_prefix=os.environ.get(prefix+'SHARED_FILES_PREFIX',''),
        shared_nfs_server=os.environ.get(prefix+'SHARED_NFS_SERVER',''),
        shared_nfs_export=os.environ.get(prefix+'SHARED_NFS_EXPORT',''),
        shared_files_backend=os.environ.get(prefix+'SHARED_FILES_BACKEND','nfs'),
        shared_local_node_uid=os.environ.get(prefix+'SHARED_LOCAL_NODE_UID',''),
        workspace_state_backend=os.environ.get(prefix+'WORKSPACE_STATE_BACKEND',''),
        relocatable_chat_state=os.environ.get(prefix+'RELOCATABLE_CHAT_STATE','0')=='1',
        local_state_root=os.environ.get(prefix+'LOCAL_STATE_ROOT',''))
    api = KubernetesHTTPS(required(prefix + 'API'), required(prefix + 'CA_FILE'),
        RotatingTokenFile(required(prefix + 'TOKEN_FILE')), namespace=namespace)
    provider = KubernetesWorkerProvider(config, api)
    observers={}
    for node in pool.nodes:
        if not node.observer_url or not node.observer_key_file:
            raise ValueError('POOL_NODE_OBSERVER_REQUIRED')
        observers[node.name]=ObserverClient(node.observer_url,lambda p=node.observer_key_file:read_key(p),node.name)
    observer=PoolObserver(observers)
    capacity = CapacityPolicy(maximum_hosts=settings.maximum_hosts,
        cpu_millis=config.cpu_millis, memory_mib=config.memory_mib, ephemeral_mib=config.ephemeral_mib,
        total_cpu_millis=int(required(prefix + 'TOTAL_CPU_MILLIS')),
        total_memory_mib=int(required(prefix + 'TOTAL_MEMORY_MIB')),
        total_ephemeral_mib=int(required(prefix + 'TOTAL_EPHEMERAL_MIB')),
        startup_parallelism=settings.startup_parallelism,
        queue_limit=int(os.environ.get(prefix + 'QUEUE_LIMIT', '128')),
        queue_seconds=int(os.environ.get(prefix + 'QUEUE_SECONDS', '180')),pool=pool)
    store = PostgresReservations(required('DATABASE_URL'),
        termination_verifier=observer.verify, max_hosts=settings.maximum_hosts, capacity=capacity,role=role,relocatable_chat_state=config.relocatable_chat_state)
    secret_file = os.environ.get('PERSONAL_CLOUD_MANAGER_SECRET_FILE',
                                 '/run/secrets/personal-cloud-manager')
    token = RotatingTokenFile(secret_file)()
    service = ManagerService(store, provider, BootstrapSecrets(provider), observer, settings)
    metrics_token = RotatingTokenFile(required('PERSONAL_CLOUD_METRICS_SECRET_FILE'))()
    return create_app(service, token, metrics_token=metrics_token)
