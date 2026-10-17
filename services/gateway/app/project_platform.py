"""Cloud-only project ports. No model-controlled identity, paths or endpoints.

The worker owns short-lived invocation permits. Every port rechecks IAM,
the current worker, the permit and the PostgreSQL deletion/epoch fence.
Only encrypted metadata is kept in PG; file bytes use the native owner volume.
"""
from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi.responses import JSONResponse, Response
from . import cloud_retention as retention
from . import personal_cloud as cloud
from .history_store import _connect
from .iam_oauth import IAMFailure

SID = retention.SESSION
INVOCATION = re.compile(r'^pi_[A-Za-z0-9_-]{43}$')
ARTIFACT = re.compile(r'^pjf_[A-Za-z0-9_-]{32}$')
STATE_KEY = re.compile(r'^(?:results|result-children:[a-f0-9-]{36}|cards:[A-Za-z0-9_-]{1,128}|meeting:[A-Za-z0-9_-]{1,128}|heatcost:[a-f0-9]{32}|heatcost-latest|monitor-grant|monitor-cache:(?:performance|disk))$')
KEYS = frozenset({'meeting', 'employee-cards', 'heatcost', 'infrastructure-monitor', 'service'})
SERVICE_USERS = frozenset({'M00003307','M00001069','M00096389'})
SERVICE_UNKNOWN = frozenset({'project_service_stop_unknown','project_service_operation_unknown'})
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff'}
MAX_FILE = 32 * 1024 * 1024
MAX_CARD_PRINT = 128 * 1024 * 1024
CARD_PRINT_NAME = "员工工卡.docx"
CARD_PRINT_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HUMAN_PATHS = frozenset('/__megmeet/tools/' + x for x in ('form-ticket', 'secure-form', 'invoke', 'cancel-job'))
INTERNAL_PATHS = frozenset('/internal/personal-cloud/projects/' + x for x in
    ('catalog', 'invoke', 'status', 'cancel', 'results', 'attachments', 'current-step', 'drawing-pages', 'form-ticket', 'secure-form', 'human-proof'))


def failure(code, status=403):
    return IAMFailure(status, code)


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise failure('project_value_invalid', 400) from None


def exact(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise failure('project_request_invalid', 400)


def session(value):
    if not isinstance(value, str) or not SID.fullmatch(value):
        raise failure('project_session_invalid', 400)
    return value


def opaque(value):
    if not isinstance(value, str) or not INVOCATION.fullmatch(value):
        raise failure('project_invocation_invalid', 400)
    return value


def uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
        return value
    except ValueError:
        raise failure('project_request_id_invalid', 400) from None


@dataclass
class Access:
    actor: object
    owner: str
    lease: str = field(repr=False)
    session_id: str
    invocation_id: str
    permit: dict
    worker_hash: str
    epoch: str = ''
    fence: int | None = None
    job_id: str | None = None
    job_deadline: float | None = None
    parent_job_id: str | None = None
    progress: dict | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    @property
    def identity(self):
        return self.owner, self.session_id


class Database:
    """Short PG transactions plus a dedicated session advisory lock.

    The lock's connection is autocommit; external requests never keep a business
    transaction open. Monotonic fence and epoch protect late writers if that
    connection disappears, a new lock is obtained, or the workspace is erased.
    """
    def __init__(self, connector, connect=_connect):
        self.connector, self.connect = connector, connect

    def barrier(self, cur, access, *, create=False, write=False):
        if not retention.enabled():
            raise failure('project_retention_required', 503)
        retention.access_in_transaction(cur, access.owner, access.actor.user_no,
                                        activity=False, session_id=access.session_id)
        if create:
            cur.execute('SELECT count(*) AS n FROM platform.project_scopes WHERE owner_key=%s', (access.owner,))
            if cur.fetchone()['n'] >= 2000:
                cur.execute('SELECT 1 FROM platform.project_scopes WHERE owner_key=%s AND session_id=%s', access.identity)
                if not cur.fetchone():raise failure('project_session_quota',413)
            cur.execute('INSERT INTO platform.project_scopes(owner_key,session_id,epoch) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                        (*access.identity, str(uuid4())))
        cur.execute('SELECT epoch,fence FROM platform.project_scopes WHERE owner_key=%s AND session_id=%s FOR UPDATE', access.identity)
        row = cur.fetchone()
        if not row or access.epoch and str(row['epoch']) != access.epoch:
            raise failure('project_workspace_generation_changed', 409)
        if write and (access.fence is None or row['fence'] != access.fence):
            raise failure('project_state_fence_changed', 409)
        if write and access.job_id:
            cur.execute("SELECT 1 FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status='running' AND deadline>clock_timestamp()", (*access.identity, access.job_id))
            if not cur.fetchone():
                raise failure('project_task_cancelled', 409)
        if write:
            cur.execute('SELECT 1 FROM platform.project_invocation_revocations WHERE owner_key=%s AND session_id=%s AND invocation_id=%s',(*access.identity,access.invocation_id))
            if cur.fetchone():raise failure('project_task_cancelled',409)
        return row

    def transaction(self, access, operation, *, create=False, write=False):
        with self.connect() as conn, conn.cursor() as cur:
            row = self.barrier(cur, access, create=create, write=write)
            return operation(cur, row)

    async def tx(self, access, operation, **kwargs):
        # _finish_thread joins cancelled synchronous work before releasing locks.
        return await retention._finish_thread(lambda: self.transaction(access, operation, **kwargs))

    def seal(self, access, kind, value):
        return self.connector.seal(access.actor.user_no, {'epoch': access.epoch, 'session': access.session_id,
                                                        'kind': kind, 'value': value})

    def unseal(self, access, kind, value):
        record = self.connector.unseal(access.actor.user_no, value)
        if record.get('epoch') != access.epoch or record.get('session') != access.session_id or record.get('kind') != kind:
            raise failure('project_state_binding_invalid', 409)
        return record['value']

    @asynccontextmanager
    async def lock(self, access, authorize):
        guard = await retention._finish_thread(self.connect)
        guard.autocommit = True
        name = 'maimi-project:' + access.owner + ':' + access.session_id
        locked = False
        try:
            def attempt():
                with guard.cursor() as cur:
                    cur.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked', (name,))
                    return cur.fetchone()['locked']
            while not locked:
                await authorize()
                locked = await retention._finish_thread(attempt)
                if not locked:
                    await asyncio.sleep(.1)
            await authorize()
            def increment(cur, row):
                cur.execute('UPDATE platform.project_scopes SET fence=fence+1 WHERE owner_key=%s AND session_id=%s RETURNING fence', access.identity)
                return cur.fetchone()['fence']
            access.fence = await self.tx(access, increment)
            yield
        finally:
            access.fence = None
            def close():
                try:
                    if locked and not guard.closed:
                        with guard.cursor() as cur:
                            cur.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (name,))
                finally:
                    guard.close()
            await retention._finish_thread(close)


class StatePort:
    def __init__(self, runtime, access, ctx):
        self.runtime, self.access, self.ctx = runtime, access, ctx

    async def authorize(self):
        await self.runtime.authorize(self.access)

    def key(self, ctx, key):
        if ctx != self.ctx or not isinstance(key, str) or not STATE_KEY.fullmatch(key):
            raise failure('project_state_key_invalid', 400)
        return key

    def lock(self, ctx):
        if ctx != self.ctx:
            raise failure('project_identity_invalid')
        return self.runtime.db.lock(self.access, self.authorize)

    async def get(self, ctx, key):
        key = self.key(ctx, key)
        await self.authorize()
        def read(cur, _):
            cur.execute('SELECT encrypted_payload FROM platform.project_session_state WHERE owner_key=%s AND session_id=%s AND state_key=%s AND (expires_at IS NULL OR expires_at>clock_timestamp())',
                        (*self.access.identity, key))
            row = cur.fetchone()
            return self.runtime.db.unseal(self.access, 'state:' + key, row['encrypted_payload']) if row else None
        return await self.runtime.db.tx(self.access, read)

    async def put(self, ctx, key, value):
        key = self.key(ctx, key)
        await self.authorize()
        size = len(encoded(value).encode())
        if size > 4 * 1024 * 1024:
            raise failure('project_state_too_large', 413)
        expiry = None
        if key == 'monitor-grant':
            expiry = value.get('expires') if isinstance(value, dict) else None
            if type(expiry) not in (int, float) or not time.time() < expiry <= time.time() + 845:
                raise failure('project_credential_expiry_invalid', 400)
        sealed = self.runtime.db.seal(self.access, 'state:' + key, value)
        def write(cur, _):
            cur.execute('DELETE FROM platform.project_session_state WHERE owner_key=%s AND expires_at<=clock_timestamp()', (self.access.owner,))
            cur.execute('SELECT COALESCE(sum(payload_bytes),0) AS total,COALESCE(sum(payload_bytes) FILTER(WHERE session_id=%s),0) AS session FROM platform.project_session_state WHERE owner_key=%s AND NOT(session_id=%s AND state_key=%s)',
                        (self.access.session_id, self.access.owner, self.access.session_id, key))
            usage = cur.fetchone()
            if usage['total'] + size > 128 * 1024 * 1024 or usage['session'] + size > 16 * 1024 * 1024:
                raise failure('project_state_quota', 413)
            cur.execute('''INSERT INTO platform.project_session_state(owner_key,session_id,state_key,encrypted_payload,payload_bytes,fence,expires_at)
              VALUES(%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT(owner_key,session_id,state_key) DO UPDATE
              SET encrypted_payload=EXCLUDED.encrypted_payload,payload_bytes=EXCLUDED.payload_bytes,fence=EXCLUDED.fence,
              version=platform.project_session_state.version+1,expires_at=EXCLUDED.expires_at''',
              (*self.access.identity, key, sealed, size, self.access.fence, expiry))
        await self.runtime.db.tx(self.access, write, write=True)

    async def delete(self, ctx, key):
        key = self.key(ctx, key)
        await self.authorize()
        await self.runtime.db.tx(self.access, lambda cur, _: cur.execute(
            'DELETE FROM platform.project_session_state WHERE owner_key=%s AND session_id=%s AND state_key=%s',
            (*self.access.identity, key)), write=True)


def native_item(item, maximum=MAX_FILE):
    if not isinstance(item, dict) or item.get('kind') not in {'file', 'image'} or not isinstance(item.get('ref'), dict):
        raise failure('project_native_reference_invalid', 502)
    ref = item['ref']
    if (not re.fullmatch(r'sha256:[a-f0-9]{64}', str(ref.get('attachmentId', '')))
            or type(ref.get('bytes')) is not int or not 0 <= ref['bytes'] <= maximum
            or not isinstance(ref.get('name'), str) or not 1 <= len(ref['name']) <= 240
            or re.search(r'[/\\\x00-\x1f\x7f]', ref['name']) or ref['name'] in {'.', '..'}):
        raise failure('project_native_reference_invalid', 502)
    # Image shape is also validated by the pinned native plugin on every read.
    return {'kind': item['kind'], 'ref': ref}


class FilePort:
    def __init__(self, runtime, access, ctx):
        self.runtime, self.access, self.ctx = runtime, access, ctx

    async def _authorize(self, ctx):
        if ctx != self.ctx:
            raise failure('project_identity_invalid')
        await self.runtime.authorize(self.access)

    async def register(self, item, source, media, *, write=False, card_print=False):
        item = native_item(item, MAX_CARD_PRINT if card_print else MAX_FILE)
        if card_print and (source != 'project_output' or item['kind'] != 'file' or item['ref']['name'] != CARD_PRINT_NAME or media != CARD_PRINT_MEDIA):
            raise failure('project_card_print_permit_required')
        reference_hash = digest(encoded(item))
        artifact_id = 'pjf_' + secrets.token_urlsafe(24)
        value = {**item, 'media_type': media, **({'output_kind': 'card-print-v1'} if card_print else {})}
        sealed = self.runtime.db.seal(self.access, 'artifact:' + artifact_id, value)
        def commit(cur, _):
            cur.execute('SELECT artifact_id FROM platform.project_artifacts WHERE owner_key=%s AND session_id=%s AND source=%s AND reference_hash=%s',
                        (*self.access.identity, source, reference_hash))
            row = cur.fetchone()
            if row:
                return row['artifact_id']
            cur.execute('SELECT count(*) AS total,count(*) FILTER(WHERE session_id=%s) AS session FROM platform.project_artifacts WHERE owner_key=%s',
                        (self.access.session_id, self.access.owner))
            count = cur.fetchone()
            if count['total'] >= 10000 or count['session'] >= 1024:
                raise failure('project_artifact_quota', 413)
            # The database keeps the ordinary 32 MiB boundary. Only the
            # trusted card-print capability records the dedicated 128 MiB kind.
            cur.execute('INSERT INTO platform.project_artifacts(owner_key,session_id,artifact_id,source,reference_hash,encrypted_payload,file_bytes,output_kind) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                        (*self.access.identity, artifact_id, source, reference_hash, sealed, item['ref']['bytes'],
                         'card-print-v1' if card_print else 'ordinary'))
            return artifact_id
        artifact_id = await self.runtime.db.tx(self.access, commit, write=write)
        return {'attachment_id': artifact_id, 'name': item['ref']['name'], 'media_type': media}

    async def list(self):
        await self._authorize(self.ctx)
        value = await self.runtime.worker(self.access, 'files', {'operation': 'list'})
        if not isinstance(value.get('items'), list) or len(value['items']) > 1024:
            raise failure('project_native_inventory_invalid', 502)
        result = []
        for row in value['items']:
            item = native_item(row)
            await self._authorize(self.ctx)
            media = item['ref'].get('mediaType') or mimetypes.guess_type(item['ref']['name'])[0] or 'application/octet-stream'
            result.append({**await self.register(item, 'native_message', media), 'bytes': item['ref']['bytes']})
        return result

    async def current_step(self):
        await self._authorize(self.ctx)
        value = await self.runtime.worker(self.access, 'files', {'operation': 'current-step'})
        if not isinstance(value.get('messages'), list) or len(value['messages']) > 32:
            raise failure('project_native_inventory_invalid', 502)
        messages = []
        for message in value['messages']:
            if (not isinstance(message, dict) or not isinstance(message.get('request_id'), str)
                    or not 1 <= len(message['request_id']) <= 128 or not isinstance(message.get('items'), list)
                    or len(message['items']) > 1024):
                raise failure('project_native_inventory_invalid', 502)
            attachments = []
            for row in message['items']:
                item = native_item(row)
                await self._authorize(self.ctx)
                media = item['ref'].get('mediaType') or mimetypes.guess_type(item['ref']['name'])[0] or 'application/octet-stream'
                attachments.append({**await self.register(item, 'native_message', media), 'bytes': item['ref']['bytes']})
            messages.append({'request_id': message['request_id'], 'attachments': attachments})
        return {'messages': messages}

    async def record(self, artifact_id):
        if not isinstance(artifact_id, str) or not ARTIFACT.fullmatch(artifact_id):
            raise failure('project_attachment_invalid', 400)
        def read(cur, _):
            cur.execute('SELECT encrypted_payload,source FROM platform.project_artifacts WHERE owner_key=%s AND session_id=%s AND artifact_id=%s',
                        (*self.access.identity, artifact_id))
            row = cur.fetchone()
            if not row:
                raise failure('project_attachment_not_found', 404)
            return self.runtime.db.unseal(self.access, 'artifact:' + artifact_id, row['encrypted_payload']), row['source']
        return await self.runtime.db.tx(self.access, read)

    async def read(self, ctx, artifact_id, max_bytes):
        await self._authorize(ctx)
        value, source = await self.record(artifact_id)
        card_print = (source == 'project_output' and value.get('output_kind') == 'card-print-v1'
                      and value['kind'] == 'file' and value['ref']['name'] == CARD_PRINT_NAME and value['media_type'] == CARD_PRINT_MEDIA)
        maximum = min(MAX_CARD_PRINT if card_print else MAX_FILE, max_bytes)
        if value['ref']['bytes'] > maximum:
            raise failure('project_attachment_too_large', 413)
        if card_print:
            data = await self.runtime.worker_card_print(self.access, {'operation':'read-card-print',
                'kind':'file', 'ref':value['ref'], 'maximum':maximum})
            if (not isinstance(data, bytes) or len(data) != value['ref']['bytes']
                    or 'sha256:' + digest(data) != value['ref']['attachmentId']):
                raise failure('project_attachment_integrity_failed', 502)
            await self._authorize(ctx)
            return {'name':value['ref']['name'], 'data':data, 'media_type':value['media_type'], 'output_kind':'card-print-v1'}
        returned = await self.runtime.worker(self.access, 'files', {'operation': 'read' if source == 'native_message' else 'read-generated',
            'kind': value['kind'], 'ref': value['ref'], 'maximum': maximum})
        if native_item(returned, maximum) != {'kind': value['kind'], 'ref': value['ref']}:
            raise failure('project_attachment_integrity_failed', 502)
        try:
            text = returned['data_base64']
            if not isinstance(text, str) or len(text) > ((maximum + 2) // 3) * 4:
                raise ValueError()
            data = base64.b64decode(text, validate=True)
        except (KeyError, ValueError):
            raise failure('project_attachment_integrity_failed', 502) from None
        if len(data) != value['ref']['bytes'] or 'sha256:' + digest(data) != value['ref']['attachmentId']:
            raise failure('project_attachment_integrity_failed', 502)
        await self._authorize(ctx)
        return {'name': value['ref']['name'], 'data': data, 'media_type': value['media_type'], **({'output_kind': 'card-print-v1'} if card_print else {})}

    async def write_card_print(self, ctx, name, data, media_type):
        # Only the trusted card generator chooses this capability. Tool inputs
        # cannot choose a byte limit or invoke the private files callback.
        return await self._write(ctx, name, data, media_type, card_print=True)

    async def write(self, ctx, name, data, media_type):
        return await self._write(ctx, name, data, media_type, card_print=False)

    async def _write(self, ctx, name, data, media_type, *, card_print):
        await self._authorize(ctx)
        if card_print and (self.access.permit.get('connector') != 'employee-cards'
                or self.access.permit.get('action') not in {'import_workbook','generate','retry_photos','edit_employee'}
                or name != CARD_PRINT_NAME or media_type != CARD_PRINT_MEDIA):
            raise failure('project_card_print_permit_required')
        maximum = MAX_CARD_PRINT if card_print else MAX_FILE
        if self.access.permit['source'] == 'read' or not self.access.job_id or self.access.fence is None:
            raise failure('project_publish_permit_required')
        if not isinstance(data, bytes) or len(data) > maximum or not isinstance(media_type, str) or len(media_type) > 160:
            raise failure('project_artifact_invalid', 413)
        expected = native_item({'kind': 'file', 'ref': {'attachmentId': 'sha256:' + digest(data), 'name': name, 'bytes': len(data)}}, maximum)
        def quota(cur,_):
            # Recovery after a committed save but interrupted card checkpoint
            # must not consume quota again. Scope and write fence still apply.
            cur.execute('SELECT artifact_id FROM platform.project_artifacts WHERE owner_key=%s AND session_id=%s AND source=%s AND reference_hash=%s',
                        (*self.access.identity, 'project_output', digest(encoded(expected))))
            existing = cur.fetchone()
            if existing:
                return existing['artifact_id']
            cur.execute('SELECT COALESCE(sum(file_bytes),0) AS total,COALESCE(sum(file_bytes) FILTER(WHERE session_id=%s),0) AS session FROM platform.project_artifacts WHERE owner_key=%s',
                        (self.access.session_id,self.access.owner))
            row=cur.fetchone()
            if row['total']+len(data)>2*1024**3 or row['session']+len(data)>512*1024**2:
                raise failure('project_file_quota',413)
        existing = await self.runtime.db.tx(self.access,quota,write=True)
        if existing:
            await self._authorize(ctx)
            return {'attachment_id': existing, 'name': name, 'media_type': media_type}
        if card_print:
            item = await self.runtime.worker_card_print(self.access, {'operation':'save-card-print',
                'name':name, 'bytes':len(data), 'sha256':digest(data)}, data)
        else:
            item = await self.runtime.worker(self.access, 'files', {'operation':'save', 'name':name, 'data_base64':base64.b64encode(data).decode()})
        item = native_item(item, maximum)
        if item['ref']['attachmentId'] != 'sha256:' + digest(data) or item['ref']['bytes'] != len(data) or item['ref']['name'] != name:
            raise failure('project_save_integrity_failed', 502)
        await self._authorize(ctx)
        return await self.register(item, 'project_output', media_type, write=True, card_print=card_print)


def project_worker_rejection(status, endpoint):
    if status in (401, 403):
        code, outward = 'project_worker_authorization_lost', 403
    elif status == 429:
        code, outward = 'project_worker_busy', 429
    elif status == 413:
        code, outward = 'project_worker_file_limit', 413
    elif status == 400:
        code, outward = 'project_worker_request_rejected', 502
    elif status in (409, 410):
        code, outward = 'project_worker_context_lost', 409
    elif status in (408, 504):
        code, outward = 'project_worker_response_timeout', 504
    else:
        code, outward = 'project_worker_unavailable', 503
    safe_endpoint = endpoint if endpoint in {'context', 'files', 'card-print'} else 'OTHER'
    safe_status = status if type(status) is int and 100 <= status <= 599 else 0
    logging.getLogger(__name__).warning('MEGMEET_PROJECT_WORKER_REJECTED endpoint=%s status=%s code=%s',
                                      safe_endpoint, safe_status, code)
    return failure(code, outward)


class ProjectRuntime:
    def __init__(self, connector, *, connect=_connect, lease_actor=cloud.lease_actor, manager=cloud.manager, worker_call=None, options=None):
        self.connector, self.lease_actor, self.manager = connector, lease_actor, manager
        self.db = Database(connector, connect)
        self.worker_call, self.options = worker_call, options or {}
        self.tasks = {}
        self.process_id = uuid4().hex
        self.heartbeat_task = None

    async def _remote(self, host, lease, endpoint, body):
        if self.worker_call:
            return await self.worker_call(host, lease, endpoint, body)
        timeout = 60 if endpoint == 'files' else 8
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3), trust_env=False, follow_redirects=False) as client:
                async with client.stream('POST', host['origin'] + '/__cloud/projects/' + endpoint,
                    headers={'Authorization': 'Bearer ' + host['route_token'], 'X-Megmeet-Client-Lease': lease}, json=body) as response:
                    if response.status_code != 200:
                        raise project_worker_rejection(response.status_code, endpoint)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > (176 * 1024 * 1024 if endpoint == 'files' and body.get('operation') == 'read-card-print' else 45 * 1024 * 1024 if endpoint == 'files' else 8192):
                            raise failure('project_worker_response_limit', 502)
                        chunks.append(chunk)
                    value = json.loads(b''.join(chunks))
                    if not isinstance(value, dict):
                        raise ValueError()
                    return value
        except IAMFailure:
            raise
        except Exception:
            raise failure('project_worker_unavailable', 503) from None

    async def _remote_card_print(self, host, lease, metadata, data=None):
        # This authenticated private endpoint streams raw document bytes through
        # worker and native attachment storage; no large base64 JSON copies.
        saving = metadata.get('operation') == 'save-card-print'
        if not saving and metadata.get('operation') != 'read-card-print':
            raise failure('project_card_print_permit_required')
        maximum = metadata.get('bytes') if saving else metadata.get('maximum')
        expected = metadata.get('bytes') if saving else metadata.get('ref', {}).get('bytes')
        if (type(maximum) is not int or type(expected) is not int or not 0 <= expected <= maximum <= MAX_CARD_PRINT
                or saving and (not isinstance(data, bytes) or len(data) != expected)
                or not saving and data is not None):
            raise failure('project_artifact_invalid', 413)
        packed = base64.urlsafe_b64encode(encoded(metadata).encode('utf8')).decode('ascii').rstrip('=')
        if len(packed) > 16384:
            raise failure('project_card_print_permit_required')
        async def body():
            if saving:
                for offset in range(0, len(data), 1024 * 1024):
                    yield data[offset:offset + 1024 * 1024]
        try:
            async with asyncio.timeout(60):
                async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=3), trust_env=False, follow_redirects=False) as client:
                    async with client.stream('POST', host['origin'] + '/__cloud/projects/card-print',
                        headers={'Authorization':'Bearer ' + host['route_token'], 'X-Megmeet-Client-Lease':lease,
                                 'X-Megmeet-Card-Metadata':packed, 'Content-Type':'application/octet-stream',
                                 'Accept-Encoding':'identity', 'Content-Length':str(expected if saving else 0)},
                        content=body()) as response:
                        if response.status_code != 200:
                            raise project_worker_rejection(response.status_code, 'card-print')
                        if response.headers.get('content-encoding','identity').lower() != 'identity':
                            raise failure('project_attachment_integrity_failed',502)
                        if not saving:
                            if response.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/octet-stream':
                                raise failure('project_attachment_integrity_failed',502)
                            length=response.headers.get('content-length')
                            if length is not None and length != str(expected):
                                raise failure('project_attachment_integrity_failed',502)
                        limit=8192 if saving else expected
                        result=bytearray()
                        async for chunk in response.aiter_raw(1024*1024):
                            if len(result)+len(chunk)>limit:
                                raise failure('project_worker_response_limit',502)
                            result.extend(chunk)
                        if saving:
                            value=json.loads(result)
                            if not isinstance(value,dict):raise ValueError()
                            return value
                        if len(result)!=expected:raise failure('project_attachment_integrity_failed',502)
                        return bytes(result)
        except IAMFailure:
            raise
        except Exception:
            raise failure('project_worker_unavailable',503) from None

    async def worker_card_print(self, access, metadata, data=None):
        host=await self.authorize(access)
        result=await self._remote_card_print(host,access.lease,
            {**metadata,'native_session_id':access.session_id,'invocation_id':access.invocation_id},data)
        await self.authorize(access)
        return result

    async def open(self, lease, sid, invocation, *, operation='authorize'):
        sid, invocation = session(sid), opaque(invocation)
        actor = await self.lease_actor(lease)
        owner = cloud.owner_key(actor)
        await retention._finish_thread(lambda: retention.access(owner, actor.user_no, session_id=sid))
        host = await self.manager('GET', '/v1/hosts/' + owner)
        permit = await self._remote(host, lease, 'context', {'native_session_id': sid, 'invocation_id': invocation, 'operation': operation})
        self.permit(permit, sid, invocation, expired=operation in {'cancel','status'})
        access = Access(actor, owner, lease, sid, invocation, permit, digest(host['origin'] + ':' + host['route_token']))
        access.epoch = await self.db.tx(access, lambda _, row: str(row['epoch']), create=True)
        await self.authorize(access, operation=operation)
        return access

    @staticmethod
    def permit(value, sid, invocation, *, expired=False):
        if (value.get('native_session_id') != sid or value.get('invocation_id') != invocation
                or value.get('source') not in {'model', 'human', 'read'} or not isinstance(value.get('selected'), list)
                or any(not isinstance(k, str) for k in value['selected']) or len(value['selected']) > 20
                or type(value.get('deadline')) not in (int,float) or value['deadline'] > time.time() + 665
                or (not expired and value['deadline'] <= time.time())
                or type(value.get('selection_revision')) is not int or value['selection_revision'] < 0
                or not isinstance(value.get('task_generation'), (int,str))):
            raise failure('project_worker_permit_invalid')

    async def authorize(self, access, *, operation='authorize'):
        if operation not in {'cancel','status'} and (access.cancelled.is_set() or time.time() >= access.permit['deadline']):
            raise failure('project_task_cancelled', 409)
        actor = await self.lease_actor(access.lease)
        if (actor.user_no, actor.account_no, actor.auth_session_id) != (access.actor.user_no, access.actor.account_no, access.actor.auth_session_id):
            raise failure('project_identity_changed')
        host = await self.manager('GET', '/v1/hosts/' + access.owner)
        if digest(host['origin'] + ':' + host['route_token']) != access.worker_hash:
            raise failure('project_worker_generation_changed', 409)
        current = await self._remote(host, access.lease, 'context', {'native_session_id': access.session_id,
            'invocation_id': access.invocation_id, 'operation': operation})
        self.permit(current, access.session_id, access.invocation_id, expired=operation in {'cancel','status'})
        if current != access.permit:
            raise failure('project_selection_changed', 409)
        await self.db.tx(access, lambda *_: None)
        return host

    async def worker(self, access, endpoint, body):
        host = await self.authorize(access)
        result = await self._remote(host, access.lease, endpoint,
            {**body, 'native_session_id': access.session_id, 'invocation_id': access.invocation_id})
        await self.authorize(access)
        return result

    def service(self, access):
        from maimi_projects import ProjectSkillService
        from maimi_projects.service import Context
        from maimi_projects.server_operations import ServerOperations
        ctx = Context(access.actor.user_no, access.session_id, 'cloud', tuple(access.permit['selected']), digest(access.lease), source=access.permit['source'])
        async def authorize(candidate):
            if candidate != ctx:
                raise failure('project_identity_invalid')
            await self.authorize(access)
        def operation_scope():
            return {'session_id':access.session_id,'invocation_id':access.invocation_id,'job_id':access.job_id,
                    'generation':digest(encoded({'lease_hash':digest(access.lease),'worker_hash':access.worker_hash,
                        'epoch':access.epoch,'selection_revision':access.permit['selection_revision']})),
                    'expires_at':int(min(access.permit['deadline'],access.job_deadline or access.permit['deadline']))}
        server_operations=ServerOperations(context=ctx,scope=operation_scope,authorize=authorize)
        async def drawing_text(candidate, source, total_pages):
            await authorize(candidate)
            if 'heatcost' not in ctx.selected:
                raise failure('project_not_selected')
            from .drawing_office import read_drawing_text
            return await read_drawing_text(access.actor, source, total_pages, lambda: authorize(candidate))
        async def progress(phase, completed, total):
            if (phase not in {'cards_photos','cards_prepare','cards_document','cards_publish'} or
                    type(completed) is not int or type(total) is not int or not 0 <= completed <= total <= 1000):
                raise failure('project_progress_invalid', 500)
            # Counts only, bound to this existing authorized in-memory task.
            # No employee, attachment or raw exception content is exposed.
            access.progress = {'phase': phase, 'completed': completed, 'total': total}
        svc = ProjectSkillService(authorize=authorize, state=StatePort(self, access, ctx), files=FilePort(self, access, ctx),
            drawing_text=drawing_text, progress=progress, server_operations=server_operations, **self.options)
        return svc, ctx

    async def _heartbeat(self):
        try:
            while self.tasks:
                await self.connector.store.redis.set('megmeet:projects:process:' + self.process_id, 'alive', ex=30)
                await asyncio.sleep(5)
        except Exception:
            for task,access in tuple(self.tasks.values()):
                if not access.cancelled.is_set():
                    access.cancelled.set()
                    task.cancel()
        finally:
            # The liveness marker expires by itself. Never delete a fresh job's
            # marker if admission races with this loop becoming idle.
            self.heartbeat_task = None
            if any(not task.done() for task,_ in self.tasks.values()):
                self.heartbeat_task = asyncio.create_task(self._heartbeat())

    async def reap_interrupted(self):
        def running():
            with self.db.connect() as conn,conn.cursor() as cur:
                cur.execute("SELECT job_id,process_id,deadline FROM platform.project_jobs WHERE status IN ('running','cancelling') LIMIT 16")
                return cur.fetchall()
        rows=await retention._finish_thread(running)
        lost=[]
        for row in rows:
            if not await self.connector.store.redis.get('megmeet:projects:process:'+row['process_id']):
                lost.append(str(row['job_id']))
        if lost:
            def mark():
                with self.db.connect() as conn,conn.cursor() as cur:
                    cur.execute("UPDATE platform.project_jobs SET status='interrupted',error_code=CASE WHEN connector='service' THEN 'project_service_stop_unknown' ELSE 'project_task_interrupted' END,updated_at=clock_timestamp() WHERE job_id=ANY(%s::uuid[]) AND status IN ('running','cancelling')",(lost,))
            await retention._finish_thread(mark)

    async def submit(self, access, body, *, fields=None):
        await self.authorize(access)
        await self.reap_interrupted()
        connector, action = body['connector'], body['action']
        if (connector not in KEYS or connector not in access.permit['selected'] or access.permit['source'] == 'read'
                or (access.permit.get('connector'), access.permit.get('action')) != (connector, action)):
            raise failure('project_not_selected')
        if connector == 'infrastructure-monitor' and access.actor.user_no not in {'M00003307','M00001069','M00096389'}:
            raise failure('project_not_authorized')
        if connector == 'service' and access.actor.user_no not in SERVICE_USERS:
            raise failure('project_not_authorized')
        if connector == 'heatcost' and action == 'revise' and access.permit['source'] != 'human':
            raise failure('project_human_authorization_required')
        if fields is not None and (access.permit['source'] != 'human' or (connector,action) not in {('infrastructure-monitor','secure_login'),('service','secure_connect')}):
            raise failure('project_human_authorization_required')
        if fields is None and (connector,action) in {('infrastructure-monitor','secure_login'),('service','secure_connect')}:
            raise failure('project_human_authorization_required')
        request_id = uuid(body['request_id'])
        arguments = body.get('arguments', {})
        parent = body.get('parent_job_id')
        if parent is not None:
            parent = uuid(parent)
            if access.permit['source'] != 'human':
                raise failure('project_human_authorization_required')
        # Secure form data never enters a persisted argument or password digest.
        identity = {'connector': connector, 'action': action,
            'arguments': arguments if fields is None else {'ticket': body['ticket']}}
        if parent is not None:
            identity['parent_job_id'] = parent
        fingerprint = digest(encoded(identity))
        job_id = str(uuid4())
        deadline = min(access.permit['deadline'], time.time() + 650)
        def admit(cur, _):
            cur.execute('SELECT 1 FROM platform.project_invocation_revocations WHERE owner_key=%s AND session_id=%s AND invocation_id=%s',(*access.identity,access.invocation_id))
            if cur.fetchone():raise failure('project_task_cancelled',409)
            cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('maimi-project-admission',))
            if parent is not None:
                self.history_parent(cur, access, parent, connector, arguments)
            cur.execute('SELECT * FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND request_id=%s', (*access.identity, request_id))
            row = cur.fetchone()
            if row:
                if row['input_hash'] != fingerprint:
                    raise failure('project_request_id_conflict', 409)
                return row, False
            cur.execute("SELECT count(*) AS total,count(*) FILTER(WHERE owner_key=%s) AS owner,count(*) FILTER(WHERE owner_key=%s AND session_id=%s) AS session FROM platform.project_jobs WHERE status IN ('running','cancelling')", (access.owner, *access.identity))
            usage = cur.fetchone()
            if usage['total'] >= 4 or usage['owner'] >= 2 or usage['session'] >= 1:
                raise failure('project_capacity_busy', 429)
            cur.execute('SELECT count(*) AS total FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s', access.identity)
            if cur.fetchone()['total'] >= 512:
                raise failure('project_job_history_quota', 413)
            cur.execute('SELECT count(*) AS total FROM platform.project_jobs WHERE owner_key=%s',(access.owner,))
            if cur.fetchone()['total']>=4096:raise failure('project_job_history_quota',413)
            cur.execute('''INSERT INTO platform.project_jobs(owner_key,session_id,job_id,request_id,invocation_id,lease_hash,connector,action,input_hash,process_id,status,deadline)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',to_timestamp(%s)) RETURNING *''',
                (*access.identity, job_id, request_id, access.invocation_id, digest(access.lease), connector, action, fingerprint, self.process_id, deadline))
            return cur.fetchone(), True
        # A new row must never be observed as a dead process in the tiny gap
        # between admission and scheduling its local task.
        await self.connector.store.redis.set('megmeet:projects:process:' + self.process_id, 'alive', ex=30)
        row, created = await self.db.tx(access, admit)
        if not created:
            return self.job_value(access, row)
        access.job_id = job_id
        access.job_deadline = deadline
        access.parent_job_id = parent
        await self.connector.store.redis.set('megmeet:projects:process:' + self.process_id, 'alive', ex=30)
        task = asyncio.create_task(self._run(access, connector, action, arguments, fields, deadline))
        self.tasks[job_id] = (task, access)
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        if not self.heartbeat_task or self.heartbeat_task.done():
            self.heartbeat_task = asyncio.create_task(self._heartbeat())
        return {'status': 'running', 'job_id': job_id, 'poll_after_ms': 1000}

    async def _run(self, access, connector, action, arguments, fields, deadline):
        async def watch():
            while True:
                await asyncio.sleep(5)
                await self.authorize(access)
                def running(cur, _):
                    cur.execute('SELECT status FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND job_id=%s', (*access.identity, access.job_id))
                    row = cur.fetchone()
                    return row and row['status'] == 'running'
                if not await self.db.tx(access, running):
                    raise failure('project_task_cancelled', 409)
        svc, ctx = self.service(access)
        operation = asyncio.create_task((svc.secure_connect(ctx,fields) if connector=='service' else svc.secure_login(ctx,fields)) if fields is not None else svc.invoke(ctx, connector, action, arguments))
        guard = asyncio.create_task(watch())
        result, status, code = None, 'failed', 'project_operation_failed'
        try:
            done, _ = await asyncio.wait([operation, guard], timeout=max(.01, deadline-time.time()), return_when=asyncio.FIRST_COMPLETED)
            if operation not in done:
                if guard in done:
                    await guard
                raise failure('project_task_deadline', 409)
            result = await operation
            await self.authorize(access)
            status, code = 'completed', None
        except asyncio.CancelledError:
            status, code = 'cancelled', 'project_task_cancelled'
        except Exception as exc:
            status = 'failed' if getattr(exc,'remote_status_unknown',False) else 'cancelled' if access.cancelled.is_set() else 'failed'
            raw = getattr(exc, 'code', '')
            code = raw if isinstance(raw, str) and re.fullmatch(r'(?:project|meeting|monitor)_[a-z_]{1,80}', raw) else 'project_operation_failed'
            if connector == 'employee-cards':
                category = type(exc).__name__
                if category not in {'ProjectFailure','IAMFailure','TimeoutError','ReadTimeout','ConnectTimeout','OSError','FileNotFoundError','PermissionError','MemoryError','ValueError','TypeError','KeyError','AttributeError','CardValidationError','BadZipFile','OperationalError','CheckViolation','NotNullViolation','UniqueViolation','ForeignKeyViolation','DataError','ProgrammingError','SerializationFailure','DeadlockDetected'}:
                    category = 'OTHER'
                phase = (access.progress or {}).get('phase','not_started')
                logging.getLogger(__name__).warning('MEGMEET_CARD_TASK_FAILED phase=%s exception=%s code=%s',phase,category,code)
        finally:
            for task in (operation, guard):
                if not task.done():
                    task.cancel()
            joining=asyncio.gather(operation,guard,return_exceptions=True)
            # A repeated cancel/shutdown must not cancel the join Future and
            # propagate a second cancellation into a CPU/thread cleanup path.
            while not joining.done():
                try:await asyncio.shield(joining)
                except asyncio.CancelledError:continue
            # A broker operation can reject cancellation because SSH delivery
            # or remote stop is unknown. The cleanup result must survive both
            # parent cancellation and the DB's preexisting cancelling marker.
            if connector == 'service' and any(getattr(value,'remote_status_unknown',False) or getattr(value,'code',None) in SERVICE_UNKNOWN for value in joining.result()):
                result,status,code=None,'failed','project_service_stop_unknown'
            # Local task cleanup has joined synchronous work before releasing
            # its lock. A service UNKNOWN may still have remote effects; the
            # failed receipt above must never imply that SSH execution stopped.
            if isinstance(fields,dict):fields.clear()
            fields = None
        if status == 'completed' and isinstance(result, dict):
            result = {**result, 'job_id': access.job_id}
            if access.parent_job_id is not None:
                result['parent_job_id'] = access.parent_job_id
        sealed = self.db.seal(access, 'job:' + access.job_id, result) if status == 'completed' else None
        def finish(cur, scope):
            if sealed:
                cur.execute('SELECT COALESCE(sum(octet_length(encrypted_result)),0) AS total FROM platform.project_jobs WHERE owner_key=%s',(access.owner,))
                if len(sealed.encode())>2*1024*1024 or cur.fetchone()['total']+len(sealed.encode())>64*1024*1024:
                    cur.execute("UPDATE platform.project_jobs SET status='failed',error_code='project_result_quota',updated_at=clock_timestamp() WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status='running'",(*access.identity,access.job_id))
                    return
            child_index = None
            if sealed and access.parent_job_id:
                try:
                    # Recheck the root under the same scope lock that commits
                    # this result. A deleted or changed root cannot be revived.
                    self.history_parent(cur, access, access.parent_job_id, connector)
                    child_index = self.history_child_index(cur, access)
                except IAMFailure as exc:
                    cur.execute("UPDATE platform.project_jobs SET status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE 'failed' END,error_code=%s,updated_at=clock_timestamp() WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status IN ('running','cancelling')",
                                (exc.code, *access.identity, access.job_id))
                    return
            if connector == 'service' and code in SERVICE_UNKNOWN:
                cur.execute("UPDATE platform.project_jobs SET status='failed',encrypted_result=NULL,error_code=%s,updated_at=clock_timestamp() WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status IN ('running','cancelling') RETURNING status",
                            (code,*access.identity,access.job_id))
            else:
                cur.execute("UPDATE platform.project_jobs SET status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE %s END,encrypted_result=CASE WHEN status='cancelling' THEN NULL ELSE %s END,error_code=CASE WHEN status='cancelling' THEN 'project_task_cancelled' ELSE %s END,updated_at=clock_timestamp() WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status IN ('running','cancelling') RETURNING status",
                            (status, sealed, code, *access.identity, access.job_id))
            saved = cur.fetchone()
            if saved and saved['status'] == 'completed' and child_index:
                key, payload, size = child_index
                cur.execute('''INSERT INTO platform.project_session_state(owner_key,session_id,state_key,encrypted_payload,payload_bytes,fence)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_key,session_id,state_key) DO UPDATE
                    SET encrypted_payload=EXCLUDED.encrypted_payload,payload_bytes=EXCLUDED.payload_bytes,
                    fence=EXCLUDED.fence,version=platform.project_session_state.version+1''',
                    (*access.identity, key, payload, size, scope['fence']))
        try:
            await self.db.tx(access, finish)
        except Exception:
            # Erasure barriers win. Never recreate a deleted scope or result.
            pass

    def job_value(self, access, row):
        if row.get('connector') == 'service' and access.actor.user_no not in SERVICE_USERS:
            raise failure('project_not_authorized')
        result = {'status': row['status'], 'job_id': str(row['job_id'])}
        if row['status'] in {'running','cancelling'}:
            result['poll_after_ms'] = 1000
            active = self.tasks.get(str(row['job_id']))
            if active and active[1].identity == access.identity and active[1].progress:
                result['progress'] = dict(active[1].progress)
        if row['encrypted_result']:
            value = self.db.unseal(access, 'job:' + str(row['job_id']), row['encrypted_result'])
            result['result'] = {**value, 'job_id': str(row['job_id'])} if isinstance(value, dict) else value
        if row['error_code']:
            result['detail'] = row['error_code']
            if row.get('connector') == 'service' and row['error_code'] in SERVICE_UNKNOWN:
                result.update(remote_status_unknown=True,remote_stop_confirmed=False)
        if row.get('connector') == 'service' and row['status'] == 'cancelled':
            result['remote_work_may_have_completed']=True
        return result

    def history_parent(self, cur, access, parent, connector=None, arguments=None):
        cur.execute("SELECT connector,encrypted_result FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status='completed'", (*access.identity, parent))
        row = cur.fetchone()
        if not row or not row['encrypted_result'] or connector is not None and row['connector'] != connector:
            raise failure('project_parent_job_invalid', 404)
        if row['connector'] not in KEYS or (row['connector'] in {'infrastructure-monitor','service'} and access.actor.user_no not in SERVICE_USERS):
            raise failure('project_not_authorized')
        value = self.db.unseal(access, 'job:' + parent, row['encrypted_result'])
        if not isinstance(value, dict) or value.get('parent_job_id') is not None:
            raise failure('project_parent_job_invalid', 400)
        if arguments and arguments.get('job_id') is not None and value.get('project_result', {}).get('id') != arguments['job_id']:
            raise failure('project_parent_job_invalid', 400)
        return row

    def history_child_index(self, cur, access):
        # Called only inside the completion transaction, after the session's
        # scope row is locked. Store only opaque IDs, never a second result copy.
        key = 'result-children:' + access.parent_job_id
        cur.execute('SELECT encrypted_payload FROM platform.project_session_state WHERE owner_key=%s AND session_id=%s AND state_key=%s', (*access.identity, key))
        row = cur.fetchone()
        ids = self.db.unseal(access, 'state:' + key, row['encrypted_payload']) if row else []
        if not isinstance(ids, list) or len(ids) > 512 or any(uuid(x) != x for x in ids):
            raise failure('project_history_index_invalid', 409)
        if access.job_id not in ids:
            ids = ids + [access.job_id]
        if len(ids) > 512:
            raise failure('project_job_history_quota', 413)
        size = len(encoded(ids).encode())
        cur.execute('SELECT COALESCE(sum(payload_bytes),0) AS total,COALESCE(sum(payload_bytes) FILTER(WHERE session_id=%s),0) AS session FROM platform.project_session_state WHERE owner_key=%s AND NOT(session_id=%s AND state_key=%s)',
                    (access.session_id, access.owner, access.session_id, key))
        usage = cur.fetchone()
        if usage['total'] + size > 128 * 1024 * 1024 or usage['session'] + size > 16 * 1024 * 1024:
            raise failure('project_state_quota', 413)
        return key, self.db.seal(access, 'state:' + key, ids), size

    async def history(self, access, body):
        limit = body.get('history_limit', 20)
        before = body.get('history_before')
        wanted = body.get('history_job_ids')
        parent = body.get('history_children_of')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise failure('project_history_limit_invalid', 400)
        if before is not None:
            before = uuid(before)
        if parent is not None:
            parent = uuid(parent)
        if wanted is not None:
            if not isinstance(wanted, list) or len(wanted) > 16 or any(uuid(x) != x for x in wanted) or len(set(wanted)) != len(wanted):
                raise failure('project_history_ids_invalid', 400)
            if before is not None or parent is not None:
                raise failure('project_history_filter_invalid', 400)
        def read(cur, _):
            selected, parent_connector = wanted, None
            if parent is not None:
                parent_connector = self.history_parent(cur, access, parent)['connector']
                key = 'result-children:' + parent
                cur.execute('SELECT encrypted_payload FROM platform.project_session_state WHERE owner_key=%s AND session_id=%s AND state_key=%s', (*access.identity, key))
                row = cur.fetchone()
                selected = self.db.unseal(access, 'state:' + key, row['encrypted_payload']) if row else []
                if not isinstance(selected, list) or len(selected) > 512 or any(uuid(x) != x for x in selected):
                    raise failure('project_history_index_invalid', 409)
            clauses = ["owner_key=%s", "session_id=%s", "status='completed'", 'encrypted_result IS NOT NULL']
            parameters = list(access.identity)
            if access.actor.user_no not in {'M00003307','M00001069','M00096389'}:
                clauses.append("connector NOT IN ('infrastructure-monitor','service')")
            if selected is not None:
                clauses.append('job_id=ANY(%s::uuid[])'); parameters.append(selected)
            if parent_connector is not None:
                clauses.append('connector=%s'); parameters.append(parent_connector)
            if before is not None:
                if selected is not None and before not in selected:
                    raise failure('project_history_cursor_invalid', 400)
                cur.execute("SELECT created_at,job_id FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status='completed'", (*access.identity, before))
                anchor = cur.fetchone()
                if not anchor:
                    raise failure('project_history_cursor_invalid', 404)
                clauses.append('(created_at,job_id)<(%s,%s::uuid)'); parameters.extend((anchor['created_at'], str(anchor['job_id'])))
            take = len(wanted) if wanted is not None else limit
            cur.execute('SELECT job_id,request_id,invocation_id,connector,action,created_at,updated_at,encrypted_result FROM platform.project_jobs WHERE '
                        + ' AND '.join(clauses) + ' ORDER BY created_at DESC,job_id DESC LIMIT %s', (*parameters, take + 1))
            return cur.fetchall(), take
        rows, take = await self.db.tx(access, read)
        history, size = [], 0
        for row in rows[:take]:
            job = str(row['job_id'])
            value = self.db.unseal(access, 'job:' + job, row['encrypted_result'])
            if not isinstance(value, dict):
                raise failure('project_history_result_invalid', 409)
            if parent is not None and value.get('parent_job_id') != parent:
                raise failure('project_history_index_invalid', 409)
            item = {'job_id': job, 'request_id': str(row['request_id']), 'invocation_id': row['invocation_id'],
                    'connector': row['connector'], 'action': row['action'],
                    'created_at': row['created_at'].isoformat(), 'updated_at': row['updated_at'].isoformat(),
                    'parent_job_id': value.get('parent_job_id'), 'result': {**value, 'job_id': job}}
            amount = len(encoded(item).encode())
            if size + amount > 2 * 1024 * 1024 and history:
                break
            # Each saved job is already bounded to 2MiB encrypted; a single
            # projected record fits this budget without removing its artifacts.
            if amount > 2 * 1024 * 1024:
                raise failure('project_history_result_too_large', 413)
            history.append(item); size += amount
        has_more = len(rows) > len(history)
        returned = {item['job_id'] for item in history}
        return {'history': list(reversed(history)),
                'history_next_cursor': history[-1]['job_id'] if has_more and history and wanted is None else None,
                'history_remaining_job_ids': [str(row['job_id']) for row in rows if str(row['job_id']) not in returned] if wanted is not None else []}

    async def status(self, access, job_id, *, cancel=False):
        job_id = uuid(job_id) if job_id is not None else None
        if not cancel and job_id is None:raise failure('project_job_id_required',400)
        await self.authorize(access, operation='cancel' if cancel else 'status')
        if cancel:
            if access.permit['source'] not in {'human','model'}:raise failure('project_cancel_not_authorized')
            def revoke(cur,_):
                cur.execute('DELETE FROM platform.project_invocation_revocations WHERE owner_key=%s AND expires_at<=clock_timestamp()',(access.owner,))
                cur.execute('SELECT count(*) AS n FROM platform.project_invocation_revocations WHERE owner_key=%s',(access.owner,))
                if cur.fetchone()['n']>=4096:raise failure('project_cancel_quota',429)
                cur.execute('''INSERT INTO platform.project_invocation_revocations(owner_key,session_id,invocation_id,lease_hash,expires_at)
                    VALUES(%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING''',
                    (*access.identity,access.invocation_id,digest(access.lease),access.permit['deadline']+60))
                cur.execute('SELECT job_id FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND invocation_id=%s AND lease_hash=%s',
                            (*access.identity,access.invocation_id,digest(access.lease)))
                row=cur.fetchone()
                return str(row['job_id']) if row else None
            found=await self.db.tx(access,revoke)
            if job_id is not None and job_id!=found:raise failure('project_cancel_not_authorized')
            job_id=found
            if job_id is None:return {'status':'cancelled','job_id':None}
        def read(cur, _):
            cur.execute('SELECT * FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND job_id=%s', (*access.identity, job_id))
            row = cur.fetchone()
            if not row:
                raise failure('project_job_not_found', 404)
            return row
        row = await self.db.tx(access, read)
        if row.get('connector') == 'service' and access.actor.user_no not in SERVICE_USERS:
            raise failure('project_not_authorized')
        if cancel:
            if row['lease_hash'] != digest(access.lease) or row['invocation_id'] != access.invocation_id or access.permit['source'] not in {'human','model'}:
                raise failure('project_cancel_not_authorized')
            def mark(cur, _):
                cur.execute("UPDATE platform.project_jobs SET status='cancelling',error_code='project_task_cancelled',updated_at=clock_timestamp() WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status='running'", (*access.identity, job_id))
            await self.db.tx(access, mark)
            if job_id in self.tasks:
                task, active = self.tasks[job_id]
                if not active.cancelled.is_set():
                    active.cancelled.set()
                    task.cancel()
                done,_=await asyncio.wait([task],timeout=7)
                if done:await asyncio.gather(task,return_exceptions=True)
            row = await self.db.tx(access, read)
        elif row['status'] in {'running','cancelling'} and not await self.connector.store.redis.get('megmeet:projects:process:' + row['process_id']):
            def interrupt(cur, _):
                cur.execute("UPDATE platform.project_jobs SET status='interrupted',error_code=CASE WHEN connector='service' THEN 'project_service_stop_unknown' ELSE 'project_task_interrupted' END,updated_at=clock_timestamp() WHERE owner_key=%s AND session_id=%s AND job_id=%s AND status IN ('running','cancelling')", (*access.identity, job_id))
            await self.db.tx(access, interrupt)
            row = await self.db.tx(access, read)
        return self.job_value(access, row)


_runtime = None


def runtime():
    global _runtime
    if _runtime is None:
        if os.getenv('MAIMI_PROJECTS_ENABLED') != 'true':
            raise failure('project_skills_not_enabled', 503)
        from .enterprise_connectors import service
        options = {}
        if os.getenv('MAIMI_PROJECT_CARD_ASSETS_DIR'):
            options['assets_dir'] = os.environ['MAIMI_PROJECT_CARD_ASSETS_DIR']
        if os.getenv('MAIMI_PROJECT_PHOTO_TOKEN_FILE'):
            from maimi_projects.card_photos import PhotoClient
            from .enterprise_connectors import secret_file
            options['photo_client']=PhotoClient(os.getenv('MAIMI_PROJECT_PHOTO_URL','http://10.3.1.26:8098/api/person/get'),
                secret_file(os.environ['MAIMI_PROJECT_PHOTO_TOKEN_FILE']),mode='live')
        if os.getenv('MAIMI_PROJECT_MEETING_ASSETS_DIR'):
            from .enterprise_connectors import read_file
            options.update(meeting_assets_dir=os.environ['MAIMI_PROJECT_MEETING_ASSETS_DIR'],
                meeting_assets_manifest=json.loads(read_file(os.environ.get('MAIMI_PROJECT_MEETING_MANIFEST_FILE',''), 65536)))
        if os.getenv('MAIMI_PROJECT_MONITOR_ORIGIN'):
            from maimi_projects.monitor import TonggongHTTP
            options['monitor'] = TonggongHTTP(os.environ['MAIMI_PROJECT_MONITOR_ORIGIN'])
        _runtime = ProjectRuntime(service(), options=options)
    return _runtime


async def artifact_response(request):
    exact(dict(request.query_params), {'session_id','attachment_id','disposition'}, {'session_id','attachment_id'})
    sid = session(request.query_params['session_id'])
    disposition = request.query_params.get('disposition','attachment')
    if disposition not in {'attachment','inline'}:
        raise failure('project_disposition_invalid',400)
    actor = await cloud.actor_for(request)
    owner = cloud.owner_key(actor)
    await retention._finish_thread(lambda: retention.access(owner,actor.user_no,session_id=sid))
    lease,_ = await cloud.issue_lease(request,actor)
    r=runtime()
    host=await r.manager('GET','/v1/hosts/'+owner)
    permit=await r._remote(host,lease,'read-context',{'native_session_id':sid})
    access=await r.open(lease,sid,permit.get('invocation_id'))
    if access.permit['source']!='read':raise failure('project_read_permit_invalid')
    svc,ctx=r.service(access)
    file=await svc.files.read(ctx,request.query_params['attachment_id'],MAX_CARD_PRINT)
    await cloud.actor_for(request)
    media=file['media_type']
    if disposition=='inline' and media not in {'image/png','image/jpeg','image/webp','image/gif','application/pdf'}:
        disposition='attachment'
    filename=quote(file['name'],safe='')
    return Response(file['data'],media_type=media,headers={**HEADERS,
        **({'X-Megmeet-Artifact-Kind':'card-print-v1'} if file.get('output_kind')=='card-print-v1' else {}),
        'Content-Disposition':disposition+"; filename*=UTF-8''"+filename,
        'Content-Security-Policy':"default-src 'none'; sandbox"})


async def issue_human_proof(lease, path, raw):
    if path not in HUMAN_PATHS or len(raw) > 32768:
        raise failure('project_human_request_invalid', 400)
    try:
        body = json.loads(raw)
        sid = session(body.get('session_id'))
    except (ValueError, AttributeError):
        raise failure('project_human_request_invalid', 400) from None
    actor = await cloud.lease_actor(lease)
    await retention._finish_thread(lambda:retention.access(cloud.owner_key(actor),actor.user_no,session_id=sid))
    c = runtime().connector
    await c.store.budget(actor.user_no)
    proof = secrets.token_urlsafe(32)
    record = {'lease_hash': digest(lease), 'path': path, 'body_hash': digest(raw), 'session_id': sid, 'user_no': actor.user_no}
    await ephemeral_put(c,actor,sid,'project-proof',proof,c.seal(actor.user_no,record))
    return proof


async def ephemeral_put(c,actor,sid,category,ticket,sealed):
    owner=cloud.owner_key(actor)
    oi='megmeet:projects:ephemeral-owner:'+owner
    si='megmeet:projects:ephemeral-session:'+owner+':'+digest(sid)
    key=c.store.key(category,actor.user_no,ticket)
    script="""if not redis.call('SET',KEYS[1],ARGV[1],'EX',30,'NX') then return 0 end
redis.call('SADD',KEYS[2],KEYS[3]);redis.call('EXPIRE',KEYS[2],900)
redis.call('SADD',KEYS[3],KEYS[1]);redis.call('EXPIRE',KEYS[3],900);return 1"""
    if not await c.store.redis.eval(script,3,key,oi,si,sealed):raise failure('project_ticket_unavailable',503)


async def purge_ephemeral(owner,sid=None,*,redis=None):
    if not retention.OWNER.fullmatch(owner):raise failure('project_identity_invalid')
    owned=redis is None
    if owned:
        from redis.asyncio import Redis
        redis=Redis.from_url(os.environ['REDIS_URL'],decode_responses=True,socket_timeout=3)
    oi='megmeet:projects:ephemeral-owner:'+owner
    si='megmeet:projects:ephemeral-session:'+owner+':'+digest(sid) if sid else ''
    script="""local groups=ARGV[1]~='' and {ARGV[1]} or redis.call('SMEMBERS',KEYS[1])
local a='megmeet:connectors:v1:project-proof:';local b='megmeet:connectors:v1:project-form:'
for _,g in ipairs(groups) do
 if string.sub(g,1,string.len(ARGV[2]))==ARGV[2] then
  for _,k in ipairs(redis.call('SMEMBERS',g)) do
   if string.sub(k,1,string.len(a))==a or string.sub(k,1,string.len(b))==b then redis.call('DEL',k) end
  end
  redis.call('DEL',g);redis.call('SREM',KEYS[1],g)
 end
end
if redis.call('SCARD',KEYS[1])==0 then redis.call('DEL',KEYS[1]) end;return 1"""
    try:await redis.eval(script,1,oi,si,'megmeet:projects:ephemeral-session:'+owner+':')
    finally:
        if owned:await redis.aclose()


async def consume_human_proof(lease, body):
    exact(body, {'proof','control_path','body_sha256','native_session_id'}, {'proof','control_path','body_sha256','native_session_id'})
    actor = await cloud.lease_actor(lease)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(body['proof'])):
        raise failure('project_human_authorization_required')
    c = runtime().connector
    raw = await c.store.pop('project-proof', actor.user_no, body['proof'])
    expected = {'lease_hash': digest(lease), 'path': body['control_path'], 'body_hash': body['body_sha256'],
                'session_id': body['native_session_id'], 'user_no': actor.user_no}
    if not raw or c.unseal(actor.user_no, raw) != expected:
        raise failure('project_human_authorization_required')
    return {'verified': True}


async def dispatch(lease, operation, body):
    if operation == 'human-proof':
        return await consume_human_proof(lease, body)
    common = {'native_session_id', 'invocation_id'}
    history_fields = {'history_job_ids','history_children_of','history_before','history_limit'}
    extra = {'invoke': {'connector','action','arguments','request_id','parent_job_id'}, 'status': {'job_id'}, 'cancel': {'job_id'},
             'secure-form': {'ticket','fields','request_id','parent_job_id','form'}, 'form-ticket': {'form'}, 'catalog': set(), 'results': history_fields, 'attachments': set(), 'current-step': set(), 'drawing-pages': {'attachment_ids'}}
    if operation not in extra:
        raise failure('project_route_not_found', 404)
    optional = {'job_id'} if operation == 'cancel' else {'arguments','parent_job_id'} if operation == 'invoke' else {'parent_job_id','form'} if operation == 'secure-form' else history_fields if operation == 'results' else set()
    exact(body, common | extra[operation], common | (extra[operation] - optional))
    r = runtime()
    access = await r.open(lease, body['native_session_id'], body['invocation_id'], operation=operation if operation in {'cancel','status'} else 'authorize')
    svc, ctx = r.service(access)
    if operation == 'catalog':
        return {'items': await svc.catalog(ctx)}
    if operation == 'attachments':
        return {'attachments': await svc.files.list()}
    if operation == 'current-step':
        return await svc.files.current_step()
    if operation == 'drawing-pages':
        ids = body['attachment_ids']
        if ('heatcost' not in access.permit['selected'] or not isinstance(ids, list)
                or not 1 <= len(ids) <= 3 or any(not isinstance(i, str) or not ARTIFACT.fullmatch(i) for i in ids)):
            raise failure('project_drawing_pages_invalid', 400)
        images = []
        for artifact_id in ids:
            record, source = await svc.files.record(artifact_id)
            if source != 'project_output' or record['media_type'] not in {'image/jpeg', 'image/png'}:
                raise failure('project_drawing_pages_invalid', 400)
            value = await svc.files.read(ctx, artifact_id, 700 * 1024)
            images.append({'media_type': record['media_type'], 'data_base64': base64.b64encode(value['data']).decode('ascii')})
        return {'images': images}
    if operation == 'results':
        def jobs(cur, _):
            # An exact history request also needs the exact job's terminal
            # status. The former global recent-error list omitted cancelled
            # jobs and could hide a failure behind a newer successful action.
            # History validates these IDs before this transaction is called.
            if body.get('history_job_ids') is not None:
                allowed = sorted(KEYS - ({'infrastructure-monitor','service'} if access.actor.user_no not in
                    {'M00003307','M00001069','M00096389'} else set()))
                cur.execute("SELECT * FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND job_id=ANY(%s::uuid[]) AND connector=ANY(%s) ORDER BY created_at,job_id",
                            (*access.identity, body['history_job_ids'], allowed))
                return cur.fetchall()
            cur.execute("SELECT * FROM platform.project_jobs WHERE owner_key=%s AND session_id=%s AND status IN ('running','cancelling') ORDER BY created_at LIMIT 4", access.identity)
            active = cur.fetchall()
            cur.execute("""SELECT failed.* FROM platform.project_jobs failed WHERE owner_key=%s AND session_id=%s
                AND status IN ('failed','interrupted') AND NOT EXISTS (
                  SELECT 1 FROM platform.project_jobs done WHERE done.owner_key=failed.owner_key
                  AND done.session_id=failed.session_id AND done.connector=failed.connector
                  AND done.action=failed.action AND done.status='completed' AND done.created_at>failed.created_at)
                ORDER BY created_at DESC LIMIT 12""", access.identity)
            return active + cur.fetchall()
        history_requested = bool(history_fields.intersection(body))
        history = await r.history(access, body) if history_requested else {
            'history': [], 'history_next_cursor': None, 'history_remaining_job_ids': []}
        # A precise history fetch must not drag unrelated live forms into an
        # older message, nor duplicate their payload within the worker budget.
        return {'results': [] if history_requested else await svc.results(ctx), 'jobs': [{**r.job_value(access, row), 'connector': row['connector']}
                for row in await r.db.tx(access, jobs)], **history}
    if operation in {'status', 'cancel'}:
        return await r.status(access, body.get('job_id'), cancel=operation == 'cancel')
    if operation == 'invoke':
        return await r.submit(access, body)
    connector,action=access.permit.get('connector'),access.permit.get('action')
    form={('infrastructure-monitor','secure_login'):'monitor-login',('service','secure_connect'):'service-login'}.get((connector,action))
    if (access.permit['source'] != 'human' or access.actor.user_no not in SERVICE_USERS
            or connector not in access.permit['selected'] or form is None):
        raise failure('project_human_authorization_required')
    # Old monitor clients omitted form; that narrow existing route remains.
    # Service clients must identify the form explicitly and cannot consume a
    # monitor ticket, or a ticket from another worker or selection generation.
    if body.get('form', 'monitor-login')!=form:raise failure('project_form_invalid',400)
    binding = {'lease_hash': digest(lease), 'session_id': access.session_id,
               'selection_revision': access.permit['selection_revision'], 'epoch': access.epoch,
               'worker_hash':access.worker_hash,'form':form,'connector':connector,'action':action}
    c = r.connector
    if operation == 'form-ticket':
        ticket = secrets.token_urlsafe(32)
        await ephemeral_put(c,access.actor,access.session_id,'project-form',ticket,c.seal(access.actor.user_no,binding))
        return {'ticket': ticket, 'expires_in': 30, 'form': form}
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(body['ticket'])):
        raise failure('project_form_ticket_invalid')
    raw = await c.store.pop('project-form', access.actor.user_no, body['ticket'])
    if not raw or c.unseal(access.actor.user_no, raw) != binding:
        raise failure('project_form_ticket_invalid')
    return await r.submit(access, {**body, 'connector': connector, 'action': action}, fields=body['fields'])


async def internal(request, operation):
    authorization = request.headers.get('authorization', '')
    lease = authorization[7:] if authorization.startswith('Bearer ') else ''
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise failure('project_request_too_large', 413)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise failure('project_request_invalid', 400) from None
    try:
        value = await dispatch(lease, operation, body)
    except IAMFailure:
        raise
    except Exception as exc:
        from maimi_projects import ProjectFailure
        if isinstance(exc, ProjectFailure):
            raise failure(exc.code, exc.status) from None
        raise failure('project_service_unavailable', 503) from None
    return JSONResponse(value, status_code=202 if operation in {'invoke','secure-form'} and value['status']=='running' else 200, headers=HEADERS)
