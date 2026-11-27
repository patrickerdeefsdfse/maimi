"""Postgres FIFO admission and bounded reconciliation; contains no task payloads.

The global transaction lock is shared with claim(). Unknown and deleting
generations consume their full commitments. Queue admission is driven by a fresh
authenticated open request; it never replays an enterprise action in background.
"""
from dataclasses import dataclass
import uuid
from execution_pool import PROFILES, ExecutionPool
from psycopg.errors import LockNotAvailable

LOCK = 671246335705837649


@dataclass(frozen=True)
class CapacityPolicy:
    # Conservative fallback; deployment must explicitly choose test/prod budgets.
    maximum_hosts: int = 2
    cpu_millis: int = 1000
    memory_mib: int = 1536
    ephemeral_mib: int = 1024
    total_cpu_millis: int = 2000
    total_memory_mib: int = 4096
    total_ephemeral_mib: int = 4096
    startup_parallelism: int = 2
    queue_limit: int = 128
    queue_seconds: int = 300
    pool: ExecutionPool | None = None

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for k,v in vars(self).items() if k != 'pool'):
            raise ValueError('CAPACITY_POLICY_INVALID')
        if (self.maximum_hosts > 768 or self.queue_limit > 4096 or
                not 30 <= self.queue_seconds <= 900 or self.startup_parallelism > 16 or
                self.cpu_millis > self.total_cpu_millis or self.memory_mib > self.total_memory_mib or
                self.ephemeral_mib > self.total_ephemeral_mib):
            raise ValueError('CAPACITY_POLICY_INVALID')
        if self.pool is not None and not isinstance(self.pool, ExecutionPool):
            raise ValueError('CAPACITY_POOL_INVALID')


class LifecycleStore:
    async def _expire_queue(self, connection):
        await connection.execute(f"UPDATE {self.schema}.worker_runtime_queue SET state='EXPIRED',completed_at=clock_timestamp() WHERE state='WAITING' AND expires_at<=clock_timestamp()")

    async def _capacity(self, connection, resource_class='legacy', execution_node=''):
        cur = await connection.execute(f"""SELECT count(*) AS occupied,
            count(*) FILTER (WHERE state IN ('CLAIMED','BOUND')) AS starting,
            coalesce(sum(cpu_millis),0) AS cpu, coalesce(sum(memory_mib),0) AS memory,
            coalesce(sum(ephemeral_mib),0) AS ephemeral
            FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED'""")
        used = await cur.fetchone()
        p = self.capacity
        if p.pool:
            from worker_reservations import require
            require(resource_class in PROFILES, 'RESOURCE_CLASS_INVALID')
            node=p.pool.node(execution_node); resource=PROFILES[resource_class]
            cur=await connection.execute(f"""SELECT count(*) occupied,
                count(*) FILTER(WHERE state IN ('CLAIMED','BOUND')) starting,
                coalesce(sum(cpu_millis),0) cpu,coalesce(sum(memory_mib),0) memory,
                coalesce(sum(ephemeral_mib),0) ephemeral
                FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED' AND execution_node=%s""",(execution_node,))
            local=await cur.fetchone()
            available=(used['occupied']<p.maximum_hosts and used['starting']<p.startup_parallelism and
                local['occupied']<node.slots and local['starting']<node.starting and
                local['cpu']+resource.cpu<=node.cpu and local['memory']+resource.charged_memory<=node.memory and
                local['ephemeral']+resource.ephemeral<=node.ephemeral)
            return available,used
        available = (used['occupied'] < p.maximum_hosts and used['starting'] < p.startup_parallelism and
            used['cpu'] + p.cpu_millis <= p.total_cpu_millis and
            used['memory'] + p.memory_mib <= p.total_memory_mib and
            used['ephemeral'] + p.ephemeral_mib <= p.total_ephemeral_mib)
        return available, used

    async def _claim_guard(self, op, request_id, resource_class='legacy', execution_node=''):
        from worker_reservations import require
        await self._expire_queue(op.connection)
        cur = await op.connection.execute(f"SELECT request_id FROM {self.schema}.worker_runtime_queue WHERE state='WAITING' AND (%s='' OR execution_node=%s) ORDER BY sequence LIMIT 1",(execution_node,execution_node))
        first = await cur.fetchone()
        require(first is None or str(first['request_id']) == request_id, 'QUEUE_HAS_PRIORITY')
        available, _ = await self._capacity(op.connection,resource_class,execution_node)
        require(available, 'HOST_CAPACITY_REACHED')

    async def select_node(self, op, owner, resource_class):
        from worker_reservations import require
        if not self.capacity.pool:
            return ''
        require(resource_class in PROFILES,'RESOURCE_CLASS_INVALID')
        if owner['home_node']:
            n=self.capacity.pool.node(owner['home_node']); r=PROFILES[resource_class]
            require(n.cpu>=r.cpu and n.memory>=r.charged_memory and n.ephemeral>=r.ephemeral,'RESOURCE_CLASS_EXCEEDS_HOME_NODE')
            return owner['home_node']
        # Queue commitments participate in placement, avoiding all new users
        # selecting the same initially empty node. FIFO is preserved per node.
        cur=await op.connection.execute(f"""SELECT execution_node,count(*) n FROM
          (SELECT execution_node FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED'
           UNION ALL SELECT execution_node FROM {self.schema}.worker_runtime_queue WHERE state='WAITING') x
          GROUP BY execution_node""")
        load={r['execution_node']:r['n'] for r in await cur.fetchall()}
        r=PROFILES[resource_class]
        nodes=[n for n in self.capacity.pool.nodes if n.cpu>=r.cpu and n.memory>=r.charged_memory and n.ephemeral>=r.ephemeral]
        require(bool(nodes),'RESOURCE_CLASS_EXCEEDS_POOL')
        return min(nodes,key=lambda n:(load.get(n.name,0)/n.slots,n.name)).name

    async def admit(self, op, *, expires_at, catalog_hash, policy_hash, queue_request_id=None, resource_class='legacy'):
        try:
            return await self._admit(op,expires_at=expires_at,catalog_hash=catalog_hash,policy_hash=policy_hash,
                queue_request_id=queue_request_id,resource_class=resource_class)
        except LockNotAvailable:
            # No external action occurs in this transaction. Its rollback must
            # finish before suggesting a fresh poll, never replay an execution.
            return {'status':'starting','phase':'admission_busy','retry_after':2}

    async def _admit(self, op, *, expires_at, catalog_hash, policy_hash, queue_request_id=None, resource_class='legacy'):
        from worker_reservations import require, _uuid
        if queue_request_id is not None:
            queue_request_id = _uuid(queue_request_id)
        await self.require_no_placement_transfer(op)
        await self.assert_other_role_available(op)
        async with op.connection.transaction():
            await op.connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.admission_lock,))
            owner=await self._owner(op)
            await self._expire_queue(op.connection)
            if queue_request_id:
                cur = await op.connection.execute(f'SELECT * FROM {self.schema}.worker_runtime_queue WHERE owner_key=%s AND request_id=%s', (op.owner, queue_request_id))
                row = await cur.fetchone()
                require(row is not None, 'QUEUE_REQUEST_NOT_FOUND')
                require(row['state'] == 'WAITING', 'QUEUE_' + row['state'])
            else:
                cur = await op.connection.execute(f"SELECT * FROM {self.schema}.worker_runtime_queue WHERE owner_key=%s AND state='WAITING'", (op.owner,))
                row = await cur.fetchone()
            if row is None:
                execution_node=await self.select_node(op,owner,resource_class)
                cur = await op.connection.execute(f"SELECT count(*) AS n FROM {self.schema}.worker_runtime_queue WHERE state='WAITING'")
                require((await cur.fetchone())['n'] < self.capacity.queue_limit, 'QUEUE_FULL')
                cur = await op.connection.execute(f"""INSERT INTO {self.schema}.worker_runtime_queue(request_id,owner_key,state,expires_at,resource_class,execution_node)
                    VALUES(%s,%s,'WAITING',clock_timestamp()+(%s * interval '1 second'),%s,%s) RETURNING *""",
                    (str(uuid.uuid4()), op.owner, self.capacity.queue_seconds,resource_class,execution_node))
                row = await cur.fetchone()
                await self._event(op, None, 'QUEUE_WAITING', {'request_id': str(row['request_id'])})
            require(row['resource_class']==resource_class,'QUEUE_RESOURCE_CLASS_CHANGED')
            execution_node=row['execution_node']
            cur = await op.connection.execute(f"SELECT count(*) AS position FROM {self.schema}.worker_runtime_queue WHERE state='WAITING' AND sequence<=%s AND (%s='' OR execution_node=%s)", (row['sequence'],execution_node,execution_node))
            position = (await cur.fetchone())['position']
            available, _ = await self._capacity(op.connection,resource_class,execution_node)
            if position != 1 or not available:
                return {'status': 'queued', 'phase': 'waiting_capacity', 'request_id': str(row['request_id']),
                    'position': position, 'retry_after': 2, 'expires_at': int(row['expires_at'].timestamp()),
                    'resource_class':resource_class,'node_name':execution_node}
            record = await self.claim(op, purpose='worker', expires_at=expires_at,
                catalog_hash=catalog_hash, policy_hash=policy_hash, request_id=str(row['request_id']),
                resource_class=resource_class,execution_node=execution_node)
            await op.connection.execute(f"UPDATE {self.schema}.worker_runtime_queue SET state='ADMITTED',completed_at=clock_timestamp(),generation=%s WHERE request_id=%s", (record['generation'], row['request_id']))
            await self._event(op, record['generation'], 'QUEUE_ADMITTED', {'request_id': str(row['request_id'])})
            return record

    async def waiting_on_node(self, execution_node=''):
        async with await self._connect() as connection:
            cur=await connection.execute(f"SELECT count(*) n FROM {self.schema}.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp() AND (%s='' OR execution_node=%s)",(execution_node,execution_node))
            return (await cur.fetchone())['n']

    async def cancel_waiting(self, op, request_id):
        from worker_reservations import _uuid
        request_id = _uuid(request_id)
        async with op.connection.transaction():
            await op.connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.admission_lock,))
            await self._owner(op)
            await self._expire_queue(op.connection)
            cur = await op.connection.execute(f"UPDATE {self.schema}.worker_runtime_queue SET state='CANCELLED',completed_at=clock_timestamp() WHERE owner_key=%s AND request_id=%s AND state='WAITING' RETURNING request_id", (op.owner,request_id))
            changed = await cur.fetchone() is not None
            if changed:
                await self._event(op, None, 'QUEUE_CANCELLED', {'request_id': request_id})
            return {'status': 'cancelled' if changed else 'not_waiting'}

    async def expire_waiting(self):
        async with await self._connect() as connection:
            async with connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.admission_lock,))
                await self._expire_queue(connection)

    async def reconcile_failed(self, op, generation, code, maximum=5):
        from worker_reservations import require
        require(code in ('WORKER_RECONCILE_UNCONFIRMED','WORKER_CONTROL_UNAVAILABLE'), 'INVALID_RECONCILE_CODE')
        async with op.connection.transaction():
            await self._owner(op)
            await self._reservation(op, generation)
            await op.connection.execute(f"""UPDATE {self.schema}.worker_runtime_reservations SET
                reconcile_attempts=least(reconcile_attempts+1,%s),
                reconcile_after=clock_timestamp()+(least(300,10*power(2,least(reconcile_attempts,5))) * interval '1 second'),
                attention_required=(reconcile_attempts+1>=%s),last_error_code=%s
                WHERE owner_key=%s AND generation=%s AND state<>'RELEASED'""", (maximum,maximum,code,op.owner,generation))

    async def reconcile_succeeded(self, op, generation):
        async with op.connection.transaction():
            await self._owner(op)
            await op.connection.execute(f"UPDATE {self.schema}.worker_runtime_reservations SET reconcile_attempts=0,reconcile_after=NULL,attention_required=false,last_error_code=NULL WHERE owner_key=%s AND generation=%s AND state<>'RELEASED'", (op.owner,generation))

    async def serving_ready(self):
        # Check the actual admission connection and its required ledger privileges.
        # No worker is created and the probe cost is independent of the queue size.
        async with await self._connect() as connection:
            cursor = await connection.execute(f"""SELECT
                NOT pg_is_in_recovery() AND current_setting('transaction_read_only') = 'off'
                AND has_schema_privilege(%(schema)s, 'USAGE')
                AND has_table_privilege(%(schema)s || '.worker_runtime_events', 'INSERT')
                AND NOT EXISTS (SELECT r.state,r.attention_required,r.created_at,r.cpu_millis,r.memory_mib,
                    r.ephemeral_mib,r.execution_node,r.resource_class,q.state,q.expires_at,q.created_at,q.execution_node
                    FROM {self.schema}.worker_runtime_reservations r, {self.schema}.worker_runtime_queue q LIMIT 0)
                AND bool_and(has_table_privilege(%(schema)s || '.' || table_name, privilege)) AS ready
                FROM (VALUES ('worker_runtime_owners'), ('worker_runtime_reservations'),
                             ('worker_runtime_queue')) AS tables(table_name)
                CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE')) AS actions(privilege)""",
                {'schema': self.schema})
            return (await cursor.fetchone())['ready'] is True

    async def lifecycle_metrics(self):
        async with await self._connect() as connection:
            # Single statement provides one MVCC snapshot, including timed-out waiters.
            cur = await connection.execute(f"""SELECT
              (SELECT jsonb_object_agg(state,n) FROM (SELECT state,count(*) n FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED' GROUP BY state) r) states,
              (SELECT count(*) FROM {self.schema}.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp()) waiting,
              (SELECT coalesce(max(extract(epoch FROM clock_timestamp()-created_at)),0) FROM {self.schema}.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp()) oldest_wait_seconds,
              (SELECT jsonb_object_agg(state,n) FROM (SELECT state,count(*) n FROM {self.schema}.worker_runtime_queue GROUP BY state) q) queue_totals,
              coalesce(sum(cpu_millis),0) cpu_millis,coalesce(sum(memory_mib),0) memory_mib,coalesce(sum(ephemeral_mib),0) ephemeral_mib,
              count(*) FILTER (WHERE attention_required) attention_required,
              count(*) FILTER (WHERE state='UNCERTAIN') uncertain,
              coalesce(max(extract(epoch FROM clock_timestamp()-created_at)) FILTER (WHERE state='UNCERTAIN'),0) oldest_uncertain_seconds
              FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED'""")
            row = dict(await cur.fetchone())
            for key in ('oldest_wait_seconds','oldest_uncertain_seconds'):
                row[key] = float(row[key])
            row['states'] = row['states'] or {}
            row['queue_totals'] = row['queue_totals'] or {}
            cur=await connection.execute(f"""SELECT execution_node,resource_class,state,count(*) occupied,
                sum(cpu_millis) cpu_millis,sum(memory_mib) memory_mib,sum(ephemeral_mib) ephemeral_mib
                FROM {self.schema}.worker_runtime_reservations WHERE state<>'RELEASED'
                GROUP BY execution_node,resource_class,state""")
            row['allocations']=[dict(r) for r in await cur.fetchall()]
            cur=await connection.execute(f"""SELECT execution_node,count(*) waiting,
                max(extract(epoch FROM clock_timestamp()-created_at)) oldest_wait_seconds
                FROM {self.schema}.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp()
                GROUP BY execution_node""")
            row['node_queues']=[dict(r,oldest_wait_seconds=float(r['oldest_wait_seconds'])) for r in await cur.fetchall()]
            return row
