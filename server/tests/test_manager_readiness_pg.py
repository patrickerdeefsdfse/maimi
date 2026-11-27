"""Exercise the manager HTTP routes against a disposable PostgreSQL ledger."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg

sys.path.insert(0, '/opt/personal-cloud')
from execution_pool import ExecutionPool, NodeBudget
from k8s_worker_provider import Handle
from manager_kubernetes import ManagerService, Settings, create_app
from sandbox_lifecycle import CapacityPolicy
from worker_reservations import PostgresReservations, ReservationError

DATABASE = os.environ['READINESS_TEST_DATABASE']
assert DATABASE.startswith('maimi_readiness_') and DATABASE.isidentifier()
ADMIN = f'postgresql://postgres@127.0.0.1:5432/{DATABASE}'
DSN = f'postgresql://maimi_qa@127.0.0.1:5432/{DATABASE}'
SECRET = 'synthetic-manager-' * 3
AUTH = {'Authorization': 'Bearer ' + SECRET}


def prepare_db():
    with psycopg.connect(ADMIN, autocommit=True) as db:
        assert db.execute("SELECT to_regnamespace('platform')").fetchone() == (None,)
        db.execute('CREATE SCHEMA platform')
        for name in ('019_worker_runtime_reservations.sql', '022_sandbox_lifecycle.sql',
                     '025_source_retention_runtime.sql', '027_execution_pool.sql'):
            db.execute((Path('/migrations') / name).read_text())
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='maimi_qa'").fetchone():
            db.execute('CREATE ROLE maimi_qa LOGIN')
        for schema in ('platform','execution'):
            db.execute(f'GRANT USAGE ON SCHEMA {schema} TO maimi_qa')
            db.execute(f'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} TO maimi_qa')
            db.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO maimi_qa')


class ProviderFixture:
    """Only the Kubernetes and bootstrap boundaries are simulated."""
    config = SimpleNamespace(namespace='maimi-test', image_admission=SimpleNamespace(runtime='0.2.2-rc.5'))

    def __init__(self):
        self.tokens = {}
        self.created = []
        self.failed = set()

    async def ensure_owner_storage(self, spec, expected):
        return expected or {'state': str(uuid4()), 'files': str(uuid4())}

    def state_claim_name(self, spec):
        return 'mmw-' + spec.owner + '-state'

    async def ensure(self, spec, token, **kwargs):
        self.tokens[spec.name] = token
        return str(uuid4())

    async def read(self, handle):
        return {'route_token': self.tokens[handle.name]}

    async def create_worker(self, spec, secret, uids):
        self.created.append(spec.owner)
        return Handle(self.config.namespace, 'fixture', spec.owner, spec.generation, spec.name,
                      str(uuid4()), str(uuid4()), spec.catalog_chunks, spec.node_name, secret,
                      uids['state'], uids['files'], spec.mode, spec.empty_storage, spec.resource_class)

    async def ready(self, handle, token):
        assert self.tokens[handle.name] == token
        if handle.owner in self.failed:
            raise RuntimeError('qa-workspace-exited')
        return {'pod_uid': handle.pod_uid, 'generation': handle.generation,
                'origin': f'http://{handle.name}:3084', 'native_workspace_id': 'qa-workspace'}


async def run(role, baseline):
    owner, other, waiting = [uuid4().hex for _ in range(3)]
    pool = ExecutionPool((NodeBudget('worker-test', 4000, 8192, 4096, 2, 2),))
    policy = CapacityPolicy(maximum_hosts=2, startup_parallelism=2, pool=pool)
    store = PostgresReservations(DSN, capacity=policy, role=role)
    provider = ProviderFixture()
    service = ManagerService(store, provider, provider, None, Settings(maximum_hosts=2))
    transport = httpx.ASGITransport(app=create_app(service, SECRET, run_reaper=False),
                                    raise_app_exceptions=False)
    checks = []
    def check(name, result):
        if not result:
            raise AssertionError(name)
        checks.append(name)

    async with httpx.AsyncClient(transport=transport, base_url='http://manager') as client:
        body = {'lease': 'mcl_' + 'l'*43, 'catalog': {'data': [], 'default_model': None},
                'expires_at': int(time.time()) + 900}
        async def open_workspace(key):
            return await client.post('/v1/hosts/' + key + '/open', json=body, headers=AUTH)
        async def ready():
            return await client.get('/v1/ready', headers=AUTH)

        check('original_workspace_opened', (await open_workspace(owner)).status_code==200)
        generation = await store.get(owner)
        provider.failed.add(owner)
        async with store.operation(owner) as op:
            await store.mark_uncertain(op, generation['generation'])
            for _ in range(5):
                await store.reconcile_failed(op, generation['generation'], 'WORKER_RECONCILE_UNCONFIRMED')
        previous = await store.get(owner)
        status = await client.get('/v1/status', headers=AUTH)
        check('operator_sees_degraded', status.status_code == 200 and status.json()['status']=='degraded')
        check('attention_visible', status.json()['lifecycle']['attention_required']==1)
        if baseline:
            check('old_probe_would_withdraw', status.json()['status']!='ready')
            check('dedicated_route_previously_absent', (await ready()).status_code==404)
            await service.client.aclose()
            return checks
        original_record = await store.get(owner)
        for admitted_version in ('0.2.2-rc.3', '0.2.2-rc.5'):
            provider.config.image_admission.runtime = admitted_version
            reported = (await client.get('/v1/status', headers=AUTH)).json()
            check('status_reports_new_workspace_version_' + admitted_version,
                  reported['worker_runtime'] == admitted_version and reported['worker_runtime_scope'] == 'new_workspaces')
            current_record = await store.get(owner)
            check('existing_workspace_identity_preserved_' + admitted_version,
                  current_record['generation'] == original_record['generation'] and current_record['handle'] == original_record['handle'])
        check('authorized_manager_stays_ready', (await ready()).json()=={'status':'ready','pool_role':role})
        check('unauthorized_denied', (await client.get('/v1/ready')).status_code==401)
        check('uncertain_owner_blocked', (await open_workspace(owner)).status_code==503)
        check('unaffected_owner_admitted', (await open_workspace(other)).status_code==200)
        check('no_replay_of_failed_worker', provider.created == [owner, other])
        check('same_owner_reuses_worker', (await open_workspace(other)).status_code==200 and provider.created==[owner,other])
        queued = await open_workspace(waiting)
        check('third_owner_queued', queued.status_code==202 and queued.json().get('status')=='queued')
        check('saturated_pool_still_serves_queue', (await ready()).status_code==200)
        try:
            async with store.operation(owner) as op:
                await store.release(op, generation['generation'], None)
        except ReservationError:
            check('missing_termination_proof_refused', True)
        else:
            raise AssertionError('missing_termination_proof_accepted')
        current = await store.get(owner)
        check('uncertain_ticket_untouched', tuple(current[k] for k in ('generation','status','reconcile_attempts','attention_required')) ==
              tuple(previous[k] for k in ('generation','status','reconcile_attempts','attention_required')))
        metrics=(await client.get('/v1/status', headers=AUTH)).json()['lifecycle']
        check('both_slots_and_memory_charged', metrics['states']=={'UNCERTAIN':1,'READY':1}
              and metrics['memory_mib']==2*previous['memory_mib'] and metrics['waiting']==1)
        with psycopg.connect(ADMIN, autocommit=True) as admin:
            admin.execute('ALTER ROLE maimi_qa SET default_transaction_read_only=on')
            try:
                check('read_only_database_refused', (await ready()).status_code==503)
            finally:
                admin.execute('ALTER ROLE maimi_qa RESET default_transaction_read_only')
            admin.execute(f'REVOKE UPDATE ON {store.schema}.worker_runtime_queue FROM maimi_qa')
            try:
                check('missing_admission_privilege_refused', (await ready()).status_code==503)
            finally:
                admin.execute(f'GRANT UPDATE ON {store.schema}.worker_runtime_queue TO maimi_qa')
            admin.execute(f'ALTER TABLE {store.schema}.worker_runtime_reservations RENAME COLUMN attention_required TO qa_renamed')
            try:
                check('incompatible_ledger_schema_refused', (await ready()).status_code==503)
            finally:
                admin.execute(f'ALTER TABLE {store.schema}.worker_runtime_reservations RENAME COLUMN qa_renamed TO attention_required')
        saved_dsn = store._dsn
        store._dsn = 'postgresql://maimi_qa@127.0.0.1:1/' + DATABASE
        try:
            check('database_outage_refused', (await ready()).status_code==503)
        finally:
            store._dsn = saved_dsn
        check('recovers_after_database_restored', (await ready()).status_code==200)
    await service.client.aclose()
    return checks


if __name__ == '__main__':
    prepare_db()
    print(json.dumps(asyncio.run(run(sys.argv[1], sys.argv[2]=='baseline'))))
