"""PostgreSQL owner exclusion and generation ledger for Kubernetes workers.

Hold ``async with store.operation(owner) as op`` across the entire management
operation, including Kubernetes calls. Claims commit BEFORE external creation.
No request expiry, heartbeat timeout, process restart or API 404 releases a slot.
The configured trusted verifier, not request data, authorizes release.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import math
import re
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sandbox_lifecycle import CapacityPolicy, LifecycleStore
from execution_pool import PROFILES


class ReservationError(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise ReservationError(code)


def owner_key(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value), 'INVALID_OWNER')
    return value


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ReservationError('INVALID_UUID') from None


def _hash(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value), 'INVALID_HASH')
    return value


HANDLE_FIELDS = frozenset(('namespace','instance','owner','generation','name','pod_uid','service_uid',
                           'catalog_chunks','node_name','secret_uid','state_pvc_uid','files_pvc_uid','mode','empty_storage','resource_class'))


def normalize_handle(value):
    if is_dataclass(value):
        value = asdict(value)
    require(isinstance(value, dict) and set(value) in (HANDLE_FIELDS, HANDLE_FIELDS | {'network_mode'}), 'INVALID_HANDLE_FIELDS')
    result = dict(value)
    mode = result.get('network_mode')
    require(mode in (None, 'public', 'confidential') and (mode is None or result['mode'] == 'worker'), 'INVALID_NETWORK_MODE')
    if mode is None:
        result.pop('network_mode', None)  # Preserve the digest of persisted pre-policy handles.
    owner_key(result['owner'])
    require(type(result['generation']) is int and result['generation'] > 0, 'INVALID_GENERATION')
    require(type(result['catalog_chunks']) is int and 1 <= result['catalog_chunks'] <= 9, 'INVALID_CATALOG_CHUNKS')
    require(type(result['empty_storage']) is bool, 'INVALID_EMPTY_STORAGE')
    require(result['mode'] in ('worker','card','files'), 'INVALID_PURPOSE')
    require(result['resource_class'] in ('legacy',*PROFILES), 'INVALID_RESOURCE_CLASS')
    for key in ('namespace','instance','name','node_name'):
        require(isinstance(result[key],str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',result[key]), 'INVALID_HANDLE_NAME')
    for key in ('pod_uid','service_uid','secret_uid','state_pvc_uid','files_pvc_uid'):
        result[key] = _uuid(result[key])
    return result


def handle_digest(value):
    return hashlib.sha256(json.dumps(normalize_handle(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class VerifiedTermination:
    """Return type for an injected, trusted node-evidence verifier.

    Construction alone grants nothing: release always invokes its configured
    verifier and checks that verifier's result against the locked DB handle.
    The verifier must authenticate independent node termination evidence; a
    user dictionary, API absence or a lease expiry is not such evidence.
    """
    owner: str
    generation: int
    handle_sha256: str
    audit_id: str
    evidence_sha256: str


@dataclass
class Operation:
    owner: str
    operation_id: str
    connection: object
    store: object
    active: bool = True
    shared_files_locked: bool = False


def _record(row, owner=None):
    if row is None:
        return None
    result = dict(row)
    if owner is not None:
        result['storage'] = owner['storage']
        result['storage_admitted'] = owner['storage_admitted']
    storage = result.get('storage')
    result['storage_uids'] = None if storage is None else {'state':storage['state_uid'],'files':storage['files_uid']}
    result.setdefault('storage_admitted',False)
    created = row['created_at'].timestamp()
    result.update(dict(touched=created,work_touched=0,was_busy=False,release_pending=False,closed_at=created,clients={}))
    result.update(result.pop('management',{}))
    result['status'] = {'CLAIMED':'starting','BOUND':'starting','READY':'ready','UNCERTAIN':'uncertain','DELETING':'deleting','RELEASED':'released'}[result['state']]
    for key in ('request_id','release_audit_id'):
        if result.get(key) is not None:
            result[key] = str(result[key])
    for key in ('created_at','updated_at','released_at','reconcile_after'):
        if result.get(key) is not None:
            result[key] = result[key].isoformat()
    result['owner'] = result['owner_key']
    return result


class PostgresReservations(LifecycleStore):
    def __init__(self, dsn, termination_verifier=None, max_hosts=0, capacity=None, role='chat',relocatable_chat_state=False):
        require(role in ('execution','chat'),'POOL_ROLE_INVALID')
        require(type(relocatable_chat_state) is bool and (not relocatable_chat_state or role=='chat'),'RELOCATABLE_STATE_SCOPE_REFUSED')
        self.relocatable_chat_state=relocatable_chat_state
        self.role=role
        self.schema='execution' if role=='execution' else 'platform'
        self.admission_lock=671246335705837649 if role=='execution' else 671246335705837648
        require(isinstance(dsn,str) and bool(dsn.strip()), 'DATABASE_REQUIRED')
        require(type(max_hosts) is int and 0 <= max_hosts <= 10000, 'INVALID_MAX_HOSTS')
        # Bound normal-reader waiters before opening database connections.
        self._operation_waiters = 0
        self._owner_waiters = {}
        self._dsn = dsn
        self._termination_verifier = termination_verifier
        self.max_hosts = max_hosts
        self.capacity = capacity or CapacityPolicy(maximum_hosts=max_hosts or 2)
        require(isinstance(self.capacity, CapacityPolicy), 'INVALID_CAPACITY_POLICY')
        require(not max_hosts or max_hosts == self.capacity.maximum_hosts, 'CAPACITY_POLICY_MISMATCH')

    async def _connect(self):
        return await psycopg.AsyncConnection.connect(self._dsn, autocommit=True,
            row_factory=dict_row, connect_timeout=5, options='-c statement_timeout=15000 -c lock_timeout=8000')

    @asynccontextmanager
    async def operation(self, owner, *, wait=False):
        require(type(wait) is bool, 'INVALID_OPERATION_WAIT')
        owner = owner_key(owner)
        lock = int.from_bytes(hashlib.sha256(('maimi-'+self.role+':'+owner).encode()).digest()[:8], 'big', signed=True)
        if wait:
            require(self._operation_waiters < 32 and self._owner_waiters.get(owner, 0) < 16,
                    'OWNER_OPERATION_BUSY')
            self._operation_waiters += 1
            self._owner_waiters[owner] = self._owner_waiters.get(owner, 0) + 1
        connection = None
        op = None
        try:
            connection = await self._connect()
            if wait:
                # Only normal heartbeat/activity callers opt in. A PostgreSQL
                # lock serializes them across manager replicas as well as tasks.
                # Both server lock_timeout (8s) and this deadline bound waiting.
                try:
                    await asyncio.wait_for(connection.execute(
                        'SELECT pg_advisory_lock(%s)', (lock,)), timeout=8)
                except (asyncio.TimeoutError, psycopg.errors.LockNotAvailable):
                    raise ReservationError('OWNER_OPERATION_BUSY') from None
            else:
                cur = await connection.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (lock,))
                require((await cur.fetchone())['acquired'] is True, 'OWNER_OPERATION_BUSY')
            operation_id = str(uuid.uuid4())
            async with connection.transaction():
                await connection.execute(f'INSERT INTO {self.schema}.worker_runtime_owners(owner_key) VALUES (%s) ON CONFLICT DO NOTHING',(owner,))
                await connection.execute(f'UPDATE {self.schema}.worker_runtime_owners SET operation_id=%s,operation_started_at=clock_timestamp() WHERE owner_key=%s',(operation_id,owner))
            op = Operation(owner, operation_id, connection, self)
            yield op
        finally:
            if op is not None:
                op.active = False
            # Closing this dedicated session releases its advisory lock even
            # after an interrupted operation. Reservation rows remain intact.
            try:
                if connection is not None:
                    closing = asyncio.create_task(connection.close())
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        # Do not return a waiter slot while its lock still lives.
                        await closing
                        raise
            finally:
                if wait:
                    self._operation_waiters -= 1
                    remaining = self._owner_waiters[owner] - 1
                    if remaining:
                        self._owner_waiters[owner] = remaining
                    else:
                        self._owner_waiters.pop(owner)

    async def _owner(self, op):
        require(isinstance(op,Operation) and op.store is self and op.active and not op.connection.closed,
                'OPERATION_NOT_ACTIVE')
        cur = await op.connection.execute(f'SELECT * FROM {self.schema}.worker_runtime_owners WHERE owner_key=%s FOR UPDATE',(op.owner,))
        row = await cur.fetchone()
        require(row is not None and str(row['operation_id']) == op.operation_id, 'STALE_OPERATION')
        return row

    async def assert_operation(self, op):
        """Fence a manager immediately before an external mutation."""
        async with op.connection.transaction():
            await self._owner(op)
        return op.operation_id

    async def assert_other_role_available(self,op,*,allow_workers=True):
        if not self.capacity.pool:
            return
        other_schema='platform' if self.role=='execution' else 'execution'
        cur=await op.connection.execute(f"SELECT count(*) n FROM {other_schema}.worker_runtime_reservations WHERE owner_key=%s AND state<>'RELEASED' AND (%s=false OR purpose IN ('files','card'))",(op.owner,allow_workers))
        require((await cur.fetchone())['n']==0,'SHARED_FILES_OTHER_ROLE_BUSY')

    @asynccontextmanager
    async def shared_files_exclusion(self,op):
        """Hold both role owner locks until cleanup and stop proof finish.

        Try-locking the second role avoids opposite-order deadlocks. A failed
        helper retains its own durable non-RELEASED row after this lock ends.
        Other-role creation checks that row before any external mutation.
        """
        await self.assert_operation(op)
        if not self.capacity.pool:
            yield
            return
        other_role='chat' if self.role=='execution' else 'execution'
        lock=int.from_bytes(hashlib.sha256(('maimi-'+other_role+':'+op.owner).encode()).digest()[:8],'big',signed=True)
        cur=await op.connection.execute('SELECT pg_try_advisory_lock(%s) acquired',(lock,))
        require((await cur.fetchone())['acquired'] is True,'SHARED_FILES_OTHER_OPERATION_BUSY')
        try:
            await self.assert_other_role_available(op,allow_workers=False)
            op.shared_files_locked=True
            yield
        finally:
            op.shared_files_locked=False
            await asyncio.shield(op.connection.execute('SELECT pg_advisory_unlock(%s)',(lock,)))

    async def _reservation(self, op, generation):
        require(type(generation) is int and generation > 0, 'INVALID_GENERATION')
        cur = await op.connection.execute(f'SELECT * FROM {self.schema}.worker_runtime_reservations WHERE owner_key=%s AND generation=%s FOR UPDATE',(op.owner,generation))
        row = await cur.fetchone()
        require(row is not None, 'RESERVATION_NOT_FOUND')
        return row

    async def cache_cleanup_max_generation(self,op):
        require(self.capacity.pool is not None and op.shared_files_locked,'SHARED_FILES_DUAL_FENCE_REQUIRED')
        cur=await op.connection.execute("SELECT count(*) FILTER (WHERE state<>'RELEASED' OR release_audit_id IS NULL) unsafe,COALESCE(max(generation),0) maximum FROM platform.worker_runtime_reservations WHERE owner_key=%s AND purpose='worker'",(op.owner,))
        row=await cur.fetchone();require(row['unsafe']==0,'CACHE_WRITER_STOP_UNCONFIRMED')
        return row['maximum']

    async def list_cache_cleanup(self):
        if not self.capacity.pool or self.role!='chat':return []
        async with await self._connect() as connection:
            cur=await connection.execute("""SELECT r.owner_key FROM platform.worker_runtime_reservations r
                JOIN platform.worker_runtime_owners o USING(owner_key)
                WHERE r.purpose='worker' AND r.state='RELEASED' AND r.release_audit_id IS NOT NULL
                AND o.storage_admitted AND r.released_at < clock_timestamp()-interval '300 seconds'
                AND NOT EXISTS(SELECT 1 FROM platform.worker_runtime_reservations newer WHERE newer.owner_key=r.owner_key AND newer.purpose='worker' AND newer.generation>r.generation)
                AND NOT EXISTS(SELECT 1 FROM platform.worker_runtime_reservations busy WHERE busy.owner_key=r.owner_key AND busy.state<>'RELEASED')
                AND NOT EXISTS(SELECT 1 FROM execution.worker_runtime_reservations busy WHERE busy.owner_key=r.owner_key AND busy.state<>'RELEASED')
                AND NOT EXISTS(SELECT 1 FROM platform.worker_runtime_events e WHERE e.owner_key=r.owner_key AND e.event_type='CACHE_SWEEP_COMPLETE' AND (e.details->>'chat_generation')::bigint>=r.generation)
                AND NOT EXISTS(SELECT 1 FROM platform.worker_runtime_events e WHERE e.owner_key=r.owner_key AND e.event_type='CACHE_SWEEP_ATTEMPT' AND e.created_at>clock_timestamp()-interval '300 seconds')
                ORDER BY r.released_at LIMIT 1""")
            return [row['owner_key'] for row in await cur.fetchall()]

    async def cache_cleanup_event(self,op,generation,kind,chat_generation):
        require(kind in ('CACHE_SWEEP_ATTEMPT','CACHE_SWEEP_COMPLETE'),'CACHE_EVENT_INVALID')
        await self._event(op,generation,kind,{'chat_generation':chat_generation})

    async def _event(self, op, generation, kind, details=None):
        await op.connection.execute(f'INSERT INTO {self.schema}.worker_runtime_events(owner_key,generation,operation_id,event_type,details) VALUES(%s,%s,%s,%s,%s)',
            (op.owner,generation,op.operation_id,kind,Jsonb(details or {})))

    async def require_no_placement_transfer(self,op):
        if self.relocatable_chat_state:
            from owner_transfer import require_no_transfer
            await require_no_transfer(self,op)

    async def get(self, owner):
        async with await self._connect() as connection:
            cur = await connection.execute(f"SELECT r.*,o.storage,o.storage_admitted FROM {self.schema}.worker_runtime_reservations r JOIN {self.schema}.worker_runtime_owners o USING(owner_key) WHERE owner_key=%s ORDER BY generation DESC LIMIT 1",(owner_key(owner),))
            return _record(await cur.fetchone())

    async def list_active(self, limit=1000):
        require(type(limit) is int and 1 <= limit <= 10000, 'INVALID_LIMIT')
        async with await self._connect() as connection:
            cur = await connection.execute(f"SELECT r.*,o.storage,o.storage_admitted FROM {self.schema}.worker_runtime_reservations r JOIN {self.schema}.worker_runtime_owners o USING(owner_key) WHERE state<>'RELEASED' ORDER BY owner_key LIMIT %s",(limit,))
            return [_record(row) for row in await cur.fetchall()]

    async def get_storage(self, owner):
        async with await self._connect() as connection:
            cur = await connection.execute(f'SELECT storage,storage_admitted FROM {self.schema}.worker_runtime_owners WHERE owner_key=%s',(owner_key(owner),))
            row = await cur.fetchone()
            return None if row is None or row['storage'] is None else dict(row['storage'],admitted=row['storage_admitted'])

    async def claim(self, op, *, purpose='worker', expires_at, catalog_hash, policy_hash, request_id=None, resource_class='legacy', execution_node=''):
        require(purpose in ('worker','card','files'), 'INVALID_PURPOSE')
        await self.require_no_placement_transfer(op)
        require(type(expires_at) is int and 1 <= expires_at <= 4102444800, 'INVALID_EXPIRY')
        catalog_hash,policy_hash = _hash(catalog_hash),_hash(policy_hash)
        request_id = _uuid(request_id or uuid.uuid4())
        if self.capacity.pool:
            require(purpose=='worker' or op.shared_files_locked,'SHARED_FILES_DUAL_FENCE_REQUIRED')
            await self.assert_other_role_available(op,allow_workers=purpose=='worker')
        async with op.connection.transaction():
            # All claimers take this transaction lock, including unlimited
            # configurations. Capacity is counted and committed atomically.
            await op.connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.admission_lock,))
            owner = await self._owner(op)
            if self.capacity.pool:
                if purpose != 'worker' and resource_class == 'legacy':
                    resource_class = 'light'
                execution_node = execution_node or await self.select_node(op,owner,resource_class)
                require(owner['home_node'] in (None,execution_node),'OWNER_HOME_NODE_CHANGED')
                self.capacity.pool.node(execution_node)
                require(resource_class in PROFILES,'RESOURCE_CLASS_INVALID')
            cur = await op.connection.execute(f'SELECT * FROM {self.schema}.worker_runtime_reservations WHERE owner_key=%s AND request_id=%s',(op.owner,request_id))
            prior = await cur.fetchone()
            if prior is not None:
                require(prior['state'] != 'RELEASED', 'REQUEST_ALREADY_RELEASED')
                require((prior['purpose'],prior['expires_at'],prior['catalog_hash'],prior['policy_hash']) ==
                        (purpose,expires_at,catalog_hash,policy_hash), 'REQUEST_ID_CONFLICT')
                require((prior['resource_class'],prior['execution_node'])==(resource_class,execution_node),'REQUEST_RESOURCE_CONFLICT')
                return _record(prior,owner)
            cur = await op.connection.execute(f"SELECT generation FROM {self.schema}.worker_runtime_reservations WHERE owner_key=%s AND state<>'RELEASED'",(op.owner,))
            require(await cur.fetchone() is None, 'OWNER_ALREADY_RESERVED')
            require(purpose == 'worker' or owner['storage_admitted'], 'MAINTENANCE_REQUIRES_ADMITTED_STORAGE')
            await self._claim_guard(op, request_id, resource_class, execution_node)
            if self.max_hosts:
                cur = await op.connection.execute(f"SELECT count(*) AS occupied FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED'")
                require((await cur.fetchone())['occupied'] < self.max_hosts, 'HOST_CAPACITY_REACHED')
            generation = owner['next_generation']
            r=PROFILES.get(resource_class)
            cpu,memory,ephemeral=(r.cpu,r.charged_memory,r.ephemeral) if r else (self.capacity.cpu_millis,self.capacity.memory_mib,self.capacity.ephemeral_mib)
            cur = await op.connection.execute(f"INSERT INTO {self.schema}.worker_runtime_reservations(owner_key,generation,request_id,purpose,state,expires_at,catalog_hash,policy_hash,empty_storage,cpu_millis,memory_mib,ephemeral_mib,resource_class,execution_node) VALUES(%s,%s,%s,%s,'CLAIMED',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (op.owner,generation,request_id,purpose,expires_at,catalog_hash,policy_hash,not owner['storage_admitted'],cpu,memory,ephemeral,resource_class,execution_node))
            result = _record(await cur.fetchone(),owner)
            await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_owners SET next_generation=next_generation+1 WHERE owner_key=%s',(op.owner,))
            if self.capacity.pool:
                await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_owners SET home_node=coalesce(home_node,%s) WHERE owner_key=%s',(execution_node,op.owner))
            await self._event(op,generation,'CLAIMED',{'purpose':purpose,'request_id':request_id})
        return result

    reserve = claim

    async def bind_runtime_fingerprint(self, op, generation, fingerprint):
        fingerprint=_hash(fingerprint)
        async with op.connection.transaction():
            await self._owner(op)
            row=await self._reservation(op,generation)
            require(row['purpose']=='worker' and row['state']=='CLAIMED' and row['handle'] is None,
                    'RUNTIME_FINGERPRINT_BIND_REFUSED')
            require(row['runtime_fingerprint'] in (None,fingerprint),'RUNTIME_FINGERPRINT_CHANGED')
            await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_reservations SET runtime_fingerprint=%s WHERE owner_key=%s AND generation=%s',
                                        (fingerprint,op.owner,generation))

    async def require_stopped_fingerprints(self, op, fingerprints):
        require(isinstance(fingerprints,(list,set,tuple)) and 1<=len(fingerprints)<=32,'SOURCE_FINGERPRINTS_INVALID')
        wanted={_hash(v) for v in fingerprints}
        await self._owner(op)
        cur=await op.connection.execute(f"SELECT runtime_fingerprint FROM {self.schema}.worker_runtime_reservations WHERE owner_key=%s AND runtime_fingerprint=ANY(%s) AND purpose='worker' AND state='RELEASED' AND release_audit_id IS NOT NULL",(op.owner,list(wanted)))
        observed={r['runtime_fingerprint'] for r in await cur.fetchall()}
        require(observed==wanted,'SOURCE_WRITER_STOP_UNCONFIRMED')

    async def bind_storage(self, op, generation, uids):
        require(isinstance(uids,dict) and set(uids) == {'namespace','state_name','state','files_name','files'}, 'INVALID_STORAGE_FIELDS')
        namespace,state_name,state_uid,files_name,files_uid = (uids[k] for k in ('namespace','state_name','state','files_name','files'))
        storage = dict(namespace=namespace,state_name=state_name,state_uid=_uuid(state_uid),files_name=files_name,files_uid=_uuid(files_uid))
        for key in ('namespace','state_name','files_name'):
            require(isinstance(storage[key],str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',storage[key]), 'INVALID_STORAGE_NAME')
        async with op.connection.transaction():
            row = await self._owner(op)
            reservation = await self._reservation(op,generation)
            require(reservation['state'] != 'RELEASED', 'RESERVATION_RELEASED')
            if row['storage'] is not None:
                require(row['storage'] == storage, 'OWNER_STORAGE_IDENTITY_CHANGED')
            else:
                await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_owners SET storage=%s WHERE owner_key=%s',(Jsonb(storage),op.owner))
                await self._event(op,generation,'STORAGE_BOUND',storage)
        if self.relocatable_chat_state:
            from owner_transfer import register_state_binding
            await register_state_binding(self,op,generation,storage,row['storage_admitted'])
        return dict(storage,admitted=row['storage_admitted'])

    async def bind(self, op, generation, handle):
        bound_image = getattr(handle, "bound_image", None)
        if bound_image is not None:
            from k8s_worker_provider import IMAGE_PATTERN
            require(isinstance(bound_image, str) and re.fullmatch(IMAGE_PATTERN, bound_image), "BOUND_IMAGE_REFUSED")
        handle = normalize_handle(handle)
        pin = {"image":bound_image,"handle_sha256":handle_digest(handle)} if bound_image is not None else None
        require(handle['owner'] == op.owner and handle['generation'] == generation, 'HANDLE_OWNER_GENERATION_MISMATCH')
        async with op.connection.transaction():
            owner = await self._owner(op)
            row = await self._reservation(op,generation)
            require(row['state'] in ('CLAIMED','BOUND','READY','UNCERTAIN'), 'BIND_STATE_REFUSED')
            require(handle['mode'] == row['purpose'] and handle['empty_storage'] == row['empty_storage'], 'HANDLE_POLICY_CHANGED')
            require(handle['resource_class']==row['resource_class'],'HANDLE_RESOURCE_CLASS_CHANGED')
            require(not self.capacity.pool or handle['node_name']==row['execution_node'],'HANDLE_EXECUTION_NODE_CHANGED')
            storage = owner['storage']
            require(storage is not None and storage['namespace'] == handle['namespace'] and storage['state_uid'] == handle['state_pvc_uid'] and storage['files_uid'] == handle['files_pvc_uid'], 'HANDLE_STORAGE_IDENTITY_MISMATCH')
            if row['handle'] is not None:
                require(row['handle'] == handle, 'BOUND_HANDLE_CHANGED')
                if pin is not None:
                    require(row['management'].get('generation_image') == pin, 'GENERATION_IMAGE_CHANGED')
                return _record(row,owner)
            cur = await op.connection.execute(f"UPDATE {self.schema}.worker_runtime_reservations SET handle=%s,management=management || %s,state='BOUND',revision=revision+1,updated_at=clock_timestamp() WHERE owner_key=%s AND generation=%s RETURNING *",(Jsonb(handle),Jsonb({'generation_image':pin} if pin is not None else {}),op.owner,generation))
            await self._event(op,generation,'BOUND',{'handle_sha256':handle_digest(handle)})
            return _record(await cur.fetchone(),owner)

    async def bind_existing_generation_image(self, op, record, image):
        """Called only after the original admission, endpoint and native identity are reverified."""
        from k8s_worker_provider import IMAGE_PATTERN
        require(isinstance(image, str) and re.fullmatch(IMAGE_PATTERN, image), 'BOUND_IMAGE_REFUSED')
        require(record['owner'] == op.owner and record['status'] == 'ready' and record['purpose'] == 'worker', 'GENERATION_NOT_READY')
        generation = record['generation']
        async with op.connection.transaction():
            await self._owner(op)
            row = await self._reservation(op,generation)
            require(row['state'] == 'READY' and row['purpose'] == 'worker' and row['handle'] == record['handle']
                    and row['revision'] == record['revision'], 'GENERATION_CHANGED_DURING_VERIFICATION')
            pin = {'image':image, 'handle_sha256':handle_digest(row['handle'])}
            if 'generation_image' in row['management']:
                require(row['management']['generation_image'] == pin, 'GENERATION_IMAGE_CHANGED')
                return
            await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_reservations SET management=management || %s,revision=revision+1,updated_at=clock_timestamp() WHERE owner_key=%s AND generation=%s',
                                        (Jsonb({'generation_image':pin}), op.owner, generation))
            await self._event(op, generation, 'GENERATION_IMAGE_VERIFIED', {'handle_sha256':pin['handle_sha256']})

    async def publish_ready(self, op, generation, native_workspace_id):
        require(isinstance(native_workspace_id,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',native_workspace_id), 'INVALID_NATIVE_WORKSPACE_ID')
        async with op.connection.transaction():
            owner = await self._owner(op)
            row = await self._reservation(op,generation)
            require(row['state'] in ('BOUND','READY','UNCERTAIN') and row['handle'] is not None, 'READY_STATE_REFUSED')
            require(row['native_workspace_id'] in (None,native_workspace_id), 'NATIVE_WORKSPACE_CHANGED')
            cur = await op.connection.execute(f"UPDATE {self.schema}.worker_runtime_reservations SET native_workspace_id=%s,state='READY',revision=revision+1,updated_at=clock_timestamp() WHERE owner_key=%s AND generation=%s RETURNING *",(native_workspace_id,op.owner,generation))
            await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_owners SET storage_admitted=TRUE WHERE owner_key=%s',(op.owner,))
            await self._event(op,generation,'READY')
            owner['storage_admitted'] = True
            return _record(await cur.fetchone(),owner)

    async def update(self, op, generation, fields, expected_revision=None):
        allowed = {'expires_at','catalog_hash','policy_hash','touched','work_touched','was_busy','clients','closed_at','release_pending','completed_at'}
        require(isinstance(fields,dict) and bool(fields) and set(fields) <= allowed, 'INVALID_UPDATE_FIELDS')
        values = dict(fields)
        for key in ('catalog_hash','policy_hash'):
            if key in values: _hash(values[key])
        if 'expires_at' in values:
            require(type(values['expires_at']) is int and 1 <= values['expires_at'] <= 4102444800, 'INVALID_EXPIRY')
        for key in ('touched','work_touched','closed_at','completed_at'):
            if key in values and not (key == 'closed_at' and values[key] is None):
                require(type(values[key]) in (int,float) and math.isfinite(values[key]) and 0 <= values[key] <= 4102444800, 'INVALID_ACTIVITY_TIME')
        for key in ('was_busy','release_pending'):
            if key in values: require(type(values[key]) is bool, 'INVALID_ACTIVITY_FLAG')
        if 'clients' in values:
            clients = values['clients']
            require(isinstance(clients,dict) and len(clients) <= 256, 'INVALID_PRESENCE')
            for client,until in clients.items():
                require(isinstance(client,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',client) and type(until) in (int,float) and math.isfinite(until) and 0 <= until <= 4102444800, 'INVALID_PRESENCE')
        async with op.connection.transaction():
            owner = await self._owner(op)
            row = await self._reservation(op,generation)
            require(row['state'] != 'RELEASED', 'RESERVATION_RELEASED')
            require(expected_revision is None or row['revision'] == expected_revision, 'RESERVATION_REVISION_CHANGED')
            management = dict(row['management'])
            for key,value in values.items():
                if key not in ('expires_at','catalog_hash','policy_hash'): management[key] = value
            cur = await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_reservations SET expires_at=%s,catalog_hash=%s,policy_hash=%s,management=%s,revision=revision+1,updated_at=clock_timestamp() WHERE owner_key=%s AND generation=%s RETURNING *',
                (values.get('expires_at',row['expires_at']),values.get('catalog_hash',row['catalog_hash']),values.get('policy_hash',row['policy_hash']),Jsonb(management),op.owner,generation))
            await self._event(op,generation,'MANAGEMENT_UPDATED',{'fields':sorted(values)})
            return _record(await cur.fetchone(),owner)

    async def _state(self, op, generation, target, code=None):
        require(code is None or isinstance(code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code), 'INVALID_ERROR_CODE')
        async with op.connection.transaction():
            owner = await self._owner(op)
            row = await self._reservation(op,generation)
            require(row['state'] != 'RELEASED', 'RESERVATION_RELEASED')
            cur = await op.connection.execute(f'UPDATE {self.schema}.worker_runtime_reservations SET state=%s,last_error_code=%s,revision=revision+1,updated_at=clock_timestamp() WHERE owner_key=%s AND generation=%s RETURNING *',(target,code,op.owner,generation))
            await self._event(op,generation,target,{'code':code} if code else {})
            return _record(await cur.fetchone(),owner)

    async def mark_deleting(self, op, generation):
        return await self._state(op,generation,'DELETING')

    async def mark_uncertain(self, op, generation, code='EXTERNAL_RESULT_UNKNOWN'):
        return await self._state(op,generation,'UNCERTAIN',code)

    async def release(self, op, generation, evidence):
        require(self._termination_verifier is not None, 'TRUSTED_TERMINATION_VERIFIER_REQUIRED')
        async with op.connection.transaction():
            await self._owner(op)
            before = await self._reservation(op,generation)
            require(before['state'] in ('DELETING','UNCERTAIN') and before['handle'] is not None, 'RELEASE_STATE_REFUSED')
            expected_handle = handle_digest(before['handle'])
        verifier = self._termination_verifier
        verify = verifier if callable(verifier) else verifier.verify
        verified = await verify(_record(before), evidence)
        require(type(verified) is VerifiedTermination, 'UNVERIFIED_TERMINATION_PROOF')
        require(verified.owner == op.owner and verified.generation == generation and verified.handle_sha256 == expected_handle, 'TERMINATION_IDENTITY_MISMATCH')
        audit_id,evidence_sha256 = _uuid(verified.audit_id),_hash(verified.evidence_sha256)
        async with op.connection.transaction():
            owner = await self._owner(op)
            current = await self._reservation(op,generation)
            require(current['state'] in ('DELETING','UNCERTAIN') and current['revision'] == before['revision'] and handle_digest(current['handle']) == expected_handle, 'RESERVATION_CHANGED_DURING_VERIFICATION')
            cur = await op.connection.execute(f"UPDATE {self.schema}.worker_runtime_reservations SET state='RELEASED',released_at=clock_timestamp(),release_audit_id=%s,revision=revision+1,updated_at=clock_timestamp() WHERE owner_key=%s AND generation=%s RETURNING *",(audit_id,op.owner,generation))
            await self._event(op,generation,'RELEASED',{'audit_id':audit_id,'evidence_sha256':evidence_sha256,'handle_sha256':expected_handle})
            return _record(await cur.fetchone(),owner)
