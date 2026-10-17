"""Single-controller durable pilot; gateway owns authentication and attachment authorization.

No model-controlled identity, runtime, image, environment, host path or URL is accepted.
An unknown provider outcome retains its resource reservation until absence is verified.
This module is not registered in the production DSH profile.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import sqlite3
import time
from uuid import uuid4
from .controller_lock import ControllerLock


class Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Scope:
    owner: str
    workspace: str
    session: str

    def validate(self):
        if any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', x)
               for x in (self.owner, self.workspace, self.session)):
            raise Failure('INVALID_TRUSTED_SCOPE')


@dataclass(frozen=True)
class Profile:
    cpu_millis: int
    memory_mib: int
    pids: int = 512
    max_command_seconds: int = 120
    max_lifetime_seconds: int = 3600
    idle_seconds: int = 600


@dataclass(frozen=True)
class Budget:
    cpu_millis: int
    memory_mib: int
    max_active: int
    per_owner_active: int = 1
    per_owner_queued: int = 4
    max_queued: int = 64


RESERVED = ('PROVISIONING', 'READY', 'BUSY', 'STOPPING', 'UNCERTAIN')
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 65536


class Broker:
    """Private, single active controller. All ports are trusted injected dependencies.

    authorize(scope) rechecks access for every user operation and queued dispatch.
    files.read_input(scope, opaque_ref, max_bytes) -> bytes, from authorized native files.
    files.publish_output(scope, name, bytes, idempotency_key) -> opaque artifact reference.
    provider methods implement the pinned OpenSandbox contract; no shell runs here.
    SQLite persists metadata only. Deploy with one exclusive controller; do not run replicas.
    """
    def __init__(self, database, *, authorize, files, provider, profiles, budget,
                 enabled=False, clock=time.time):
        if not profiles or any(not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', k) for k in profiles):
            raise ValueError('invalid administrator profiles')
        if any(p.cpu_millis <= 0 or p.memory_mib <= 0 or p.pids <= 0
               or p.max_command_seconds <= 0 or p.idle_seconds <= 0
               or p.max_lifetime_seconds < p.max_command_seconds for p in profiles.values()):
            raise ValueError('invalid profile limits')
        if min(budget.cpu_millis, budget.memory_mib, budget.max_active,
               budget.per_owner_active, budget.per_owner_queued, budget.max_queued) <= 0:
            raise ValueError('invalid administrator budget')
        self.authorize, self.files, self.provider = authorize, files, provider
        self.profiles, self.budget, self.enabled, self.clock = dict(profiles), budget, enabled, clock
        try:
            self._controller_lock = ControllerLock(database)
        except OSError:
            raise Failure('CONTROLLER_LOCK_UNAVAILABLE') from None
        try:
            self.db = sqlite3.connect(database, isolation_level=None)
        except BaseException:
            self._controller_lock.close()
            raise
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, workspace TEXT NOT NULL,
            session TEXT NOT NULL, request_key TEXT NOT NULL, profile TEXT NOT NULL,
            state TEXT NOT NULL, sandbox_id TEXT, inputs TEXT NOT NULL,
            created REAL NOT NULL, touched REAL NOT NULL, admitted REAL,
            cpu_millis INTEGER NOT NULL, memory_mib INTEGER NOT NULL,
            UNIQUE(owner,workspace,session,request_key));
          CREATE TABLE IF NOT EXISTS commands(
            task_id TEXT NOT NULL REFERENCES tasks(id), request_key TEXT NOT NULL,
            digest TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(task_id,request_key));
          CREATE TABLE IF NOT EXISTS fairness(owner TEXT PRIMARY KEY, dispatched REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(
            seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL);
        ''')
        # Call recover() once before dispatching after process startup. No blind re-execution.
        self.recovered = False
        self._dispatch_lock = asyncio.Lock()

    def close_store(self):
        """Call only after draining local handlers; provider tasks are reconciled at next startup."""
        try:
            self.db.close()
        finally:
            self._controller_lock.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _record(self, task_id, event):
        self.db.execute('INSERT INTO audit(task_id,event,at) VALUES(?,?,?)',
                        (task_id, event, self.clock()))

    async def _auth(self, scope):
        scope.validate()
        if not self.enabled:
            raise Failure('SANDBOX_DISABLED')
        await self.authorize(scope)

    def _owned(self, scope, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=? AND owner=? AND workspace=? AND session=?',
                              (task_id, scope.owner, scope.workspace, scope.session)).fetchone()
        if row is None:
            raise Failure('TASK_NOT_FOUND')
        return row

    def _public(self, row):
        return {'task_id': row['id'], 'state': row['state'], 'profile': row['profile']}

    @staticmethod
    def _key(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', value):
            raise Failure('INVALID_REQUEST_KEY')
        return value

    def recover(self):
        """Must run with exclusive controller ownership before serving any request."""
        with self.transaction():
            for row in self.db.execute("SELECT id FROM tasks WHERE state IN ('PROVISIONING','READY','BUSY','STOPPING')").fetchall():
                self.db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=?", (row['id'],))
                self._record(row['id'], 'RECOVERY_REQUIRES_VERIFIED_DESTROY')
        self.recovered = True

    async def submit(self, scope, *, profile, inputs, request_key):
        await self._auth(scope)
        self._key(request_key)
        if profile not in self.profiles or not isinstance(inputs, list) or len(inputs) > 16:
            raise Failure('INVALID_TASK')
        if any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', x) for x in inputs):
            raise Failure('INVALID_INPUT_REFERENCE')
        if len(set(inputs)) != len(inputs):
            raise Failure('INVALID_INPUT_REFERENCE')
        p = self.profiles[profile]
        if p.cpu_millis > self.budget.cpu_millis or p.memory_mib > self.budget.memory_mib:
            raise Failure('PROFILE_EXCEEDS_NODE_BUDGET')
        with self.transaction():
            old = self.db.execute('SELECT * FROM tasks WHERE owner=? AND workspace=? AND session=? AND request_key=?',
                                 (scope.owner, scope.workspace, scope.session, request_key)).fetchone()
            encoded = json.dumps(inputs, separators=(',', ':'))
            if old:
                if old['profile'] != profile or old['inputs'] != encoded:
                    raise Failure('REQUEST_KEY_CONFLICT')
                return self._public(old)
            queued = self.db.execute("SELECT count(*) FROM tasks WHERE owner=? AND state='QUEUED'", (scope.owner,)).fetchone()[0]
            if queued >= self.budget.per_owner_queued:
                raise Failure('OWNER_QUEUE_FULL')
            if self.db.execute("SELECT count(*) FROM tasks WHERE state='QUEUED'").fetchone()[0] >= self.budget.max_queued:
                raise Failure('NODE_QUEUE_FULL')
            task_id, now = str(uuid4()), self.clock()
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,NULL,?,?,?,NULL,?,?)',
                            (task_id, scope.owner, scope.workspace, scope.session, request_key, profile,
                             'QUEUED', encoded, now, now, p.cpu_millis, p.memory_mib))
            self._record(task_id, 'QUEUED')
            return {'task_id': task_id, 'state': 'QUEUED', 'profile': profile}

    async def status(self, scope, task_id):
        await self._auth(scope)
        return self._public(self._owned(scope, task_id))

    async def dispatch_one(self):
        if not self.enabled or not self.recovered:
            raise Failure('CONTROLLER_NOT_READY')
        async with self._dispatch_lock:
            with self.transaction():
                active = self.db.execute('SELECT owner,cpu_millis,memory_mib FROM tasks WHERE state IN (?,?,?,?,?)', RESERVED).fetchall()
                cpu = sum(x['cpu_millis'] for x in active)
                memory = sum(x['memory_mib'] for x in active)
                candidates = self.db.execute("SELECT t.* FROM tasks t LEFT JOIN fairness f ON f.owner=t.owner WHERE t.state='QUEUED' ORDER BY coalesce(f.dispatched,0),t.created,t.id").fetchall()
                row = next((r for r in candidates if len(active) < self.budget.max_active
                    and sum(x['owner'] == r['owner'] for x in active) < self.budget.per_owner_active
                    and cpu + r['cpu_millis'] <= self.budget.cpu_millis
                    and memory + r['memory_mib'] <= self.budget.memory_mib), None)
                if row is None:
                    return None
                self.db.execute("UPDATE tasks SET state='PROVISIONING',admitted=? WHERE id=?", (self.clock(), row['id']))
                self.db.execute('INSERT INTO fairness VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET dispatched=excluded.dispatched', (row['owner'], self.clock()))
                self._record(row['id'], 'PROVISIONING')
            scope = Scope(row['owner'], row['workspace'], row['session'])
            task_id, sandbox_id = row['id'], None
            try:
                await self._auth(scope)
                sandbox_id = await self.provider.create(task_id, row['profile'],
                    {'maimi_task_id': task_id})
                self.db.execute('UPDATE tasks SET sandbox_id=? WHERE id=?', (sandbox_id, task_id))
                if self._owned(scope, task_id)['state'] != 'PROVISIONING':
                    await self._destroy(task_id, sandbox_id)
                    return self._public(self._owned(scope, task_id))
                total = 0
                for index, reference in enumerate(json.loads(row['inputs'])):
                    await self._auth(scope)
                    data = await self.files.read_input(scope, reference, MAX_INPUT_BYTES)
                    if not isinstance(data, bytes) or len(data) > MAX_INPUT_BYTES:
                        raise Failure('INPUT_TOO_LARGE')
                    total += len(data)
                    if total > MAX_TOTAL_INPUT_BYTES:
                        raise Failure('INPUT_TOTAL_TOO_LARGE')
                    await self.provider.write_input(sandbox_id, f'/workspace/inputs/input-{index:03d}', data)
                await self._auth(scope)
                current = self._owned(scope, task_id)
                if current['state'] != 'PROVISIONING':
                    await self._destroy(task_id, sandbox_id)
                else:
                    self.db.execute("UPDATE tasks SET state='READY',touched=? WHERE id=?", (self.clock(), task_id))
                    self._record(task_id, 'READY')
            except BaseException:
                self.db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=? AND state!='CLOSED'", (task_id,))
                self._record(task_id, 'PROVISIONING_UNCERTAIN')
                # Includes cancellation: preserve reservation; reconcile explicitly before reuse.
                raise Failure('PROVISIONING_UNCERTAIN') from None
            return self._public(self._owned(scope, task_id))

    async def execute(self, scope, task_id, *, command, request_key, timeout_seconds=120):
        await self._auth(scope)
        self._key(request_key)
        if not isinstance(command, str) or not command.strip() or len(command.encode('utf-8')) > 32768 or '\x00' in command:
            raise Failure('INVALID_COMMAND')
        with self.transaction():
            row = self._owned(scope, task_id)
            if row['state'] != 'READY':
                raise Failure('TASK_NOT_READY')
            p = self.profiles[row['profile']]
            remaining = p.max_lifetime_seconds - (self.clock() - row['admitted'])
            if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= p.max_command_seconds or timeout_seconds > remaining:
                raise Failure('INVALID_COMMAND_TIMEOUT')
            if self.db.execute('SELECT 1 FROM commands WHERE task_id=? AND request_key=?', (task_id, request_key)).fetchone():
                raise Failure('COMMAND_ALREADY_ATTEMPTED')
            self.db.execute('INSERT INTO commands VALUES(?,?,?,?)',
                            (task_id, request_key, hashlib.sha256(command.encode()).hexdigest(), 'STARTED'))
            self.db.execute("UPDATE tasks SET state='BUSY',touched=? WHERE id=?", (self.clock(), task_id))
            self._record(task_id, 'COMMAND_STARTED')
        try:
            result = await self.provider.execute(row['sandbox_id'], command, timeout_seconds, MAX_LOG_BYTES)
            await self._auth(scope)
            if self._owned(scope, task_id)['state'] != 'BUSY':
                raise Failure('TASK_STOPPED_DURING_EXECUTION')
            if not isinstance(result, dict) or type(result.get('exit_code')) is not int:
                raise Failure('INVALID_PROVIDER_RESULT')
            clean = {'exit_code': result['exit_code'], 'truncated': bool(result.get('truncated', False))}
            remaining_bytes = MAX_LOG_BYTES
            for key in ('stdout', 'stderr'):
                value = result.get(key, '')
                if not isinstance(value, str):
                    raise Failure('INVALID_PROVIDER_RESULT')
                data = value.encode('utf-8')
                clean['truncated'] |= len(data) > remaining_bytes
                clean[key] = data[:remaining_bytes].decode('utf-8', errors='ignore')
                remaining_bytes -= min(len(data), remaining_bytes)
            with self.transaction():
                self.db.execute("UPDATE tasks SET state='READY',touched=? WHERE id=? AND state='BUSY'", (self.clock(), task_id))
                self.db.execute("UPDATE commands SET state='FINISHED' WHERE task_id=? AND request_key=?", (task_id, request_key))
                self._record(task_id, 'COMMAND_FINISHED')
            return clean
        except BaseException:
            self.db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=? AND state='BUSY'", (task_id,))
            self._record(task_id, 'COMMAND_OUTCOME_NOT_DELIVERED')
            raise Failure('COMMAND_OUTCOME_UNKNOWN_NO_AUTOMATIC_RETRY') from None

    @staticmethod
    def output_path(path):
        if not isinstance(path, str) or len(path) > 240 or '\\' in path or '\x00' in path:
            raise Failure('INVALID_OUTPUT_PATH')
        p = PurePosixPath(path)
        if p.is_absolute() or '..' in p.parts or not p.parts or path != p.as_posix():
            raise Failure('INVALID_OUTPUT_PATH')
        return '/workspace/outputs/' + path

    async def export(self, scope, task_id, *, path):
        await self._auth(scope)
        row = self._owned(scope, task_id)
        if row['state'] != 'READY':
            raise Failure('TASK_NOT_READY')
        remote = self.output_path(path)
        # Serializes export against execution; sandbox descendants must be quiesced by provider.
        self.db.execute("UPDATE tasks SET state='BUSY' WHERE id=?", (task_id,))
        try:
            data = await self.provider.read_output(row['sandbox_id'], remote, MAX_OUTPUT_BYTES)
            if not isinstance(data, bytes) or len(data) > MAX_OUTPUT_BYTES:
                raise Failure('OUTPUT_TOO_LARGE')
            await self._auth(scope)
            if self._owned(scope, task_id)['state'] != 'BUSY':
                raise Failure('TASK_STOPPED_DURING_EXPORT')
            key = 'sandbox-' + task_id + '-' + hashlib.sha256(path.encode() + b'\x00' + data).hexdigest()
            artifact = await self.files.publish_output(scope, PurePosixPath(path).name, data, key)
            await self._auth(scope)
            if self._owned(scope, task_id)['state'] != 'BUSY':
                raise Failure('TASK_STOPPED_DURING_EXPORT')
            self._record(task_id, 'ARTIFACT_PUBLISHED')
            self.db.execute("UPDATE tasks SET state='READY',touched=? WHERE id=? AND state='BUSY'", (self.clock(), task_id))
            return artifact
        except BaseException:
            # A snapshot may have paused the instance and lost its resume ACK.
            # Keep its reservation; never execute again without reconciliation.
            self.db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=? AND state='BUSY'", (task_id,))
            self._record(task_id, 'EXPORT_OUTCOME_NOT_DELIVERED')
            raise Failure('EXPORT_OUTCOME_UNKNOWN_NO_AUTOMATIC_RETRY') from None

    async def _destroy(self, task_id, sandbox_id):
        try:
            await self.provider.destroy_task(task_id, sandbox_id)
        except BaseException:
            self.db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=?", (task_id,))
            self._record(task_id, 'STOP_UNCONFIRMED_RESERVATION_RETAINED')
            raise Failure('STOP_UNCONFIRMED') from None
        self.db.execute("UPDATE tasks SET state='CLOSED',touched=? WHERE id=?", (self.clock(), task_id))
        self._record(task_id, 'DESTROY_CONFIRMED')

    async def close(self, scope, task_id):
        await self._auth(scope)
        row = self._owned(scope, task_id)
        if row['state'] == 'CLOSED':
            return self._public(row)
        if row['state'] == 'QUEUED':
            self.db.execute("UPDATE tasks SET state='CLOSED' WHERE id=?", (task_id,))
            self._record(task_id, 'QUEUE_CANCELLED')
        elif row['state'] == 'PROVISIONING':
            self.db.execute("UPDATE tasks SET state='STOPPING' WHERE id=?", (task_id,))
            # Provisioning completion will destroy the returned instance; don't free its budget.
        else:
            self.db.execute("UPDATE tasks SET state='STOPPING' WHERE id=?", (task_id,))
            await self._destroy(task_id, row['sandbox_id'])
        return self._public(self._owned(scope, task_id))

    async def reap(self):
        """Administrative cleanup; access revocation must never prevent resource cleanup."""
        for row in self.db.execute("SELECT * FROM tasks WHERE state='READY'").fetchall():
            p = self.profiles[row['profile']]
            if self.clock() - row['touched'] >= p.idle_seconds or self.clock() - row['admitted'] >= p.max_lifetime_seconds:
                self.db.execute("UPDATE tasks SET state='STOPPING' WHERE id=? AND state='READY'", (row['id'],))
                await self._destroy(row['id'], row['sandbox_id'])
