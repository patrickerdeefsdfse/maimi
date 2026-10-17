"""Private shared Office queue. Gateway owns IAM; this worker never receives it.

Run exactly one ASGI process. Each bounded job starts a separate engine process
group with a private workspace, sanitized environment and no client executables.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import hashlib
import hmac
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import time
from typing import Any
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

PROTOCOL = 'megmeet-office-remote-v1'
ENGINE_PROTOCOL = 'megmeet-office-v1'
INPUT_LIMIT = 64 * 1024**2
INPUT_TOTAL = OUTPUT_LIMIT = 128 * 1024**2
RESPONSE_LIMIT = 2 * 1024**2
MUTATING = {'create', 'edit', 'convert', 'pdf', 'image'}
OPERATIONS = MUTATING | {'read', 'inspect', 'ocr'}
TERMINAL = {'succeeded', 'failed', 'cancelled'}
OPAQUE = re.compile(r'^[a-f0-9]{64}$')
JOB_ID = re.compile(r'^[a-f0-9]{32}$')
RESERVED = {'workspace', 'limits', 'protocol', 'operation', 'input', 'output',
            'engineRoot', 'pythonExecutable', 'command', 'env', 'overwrite'}


def refused(code, status=400):
    raise HTTPException(status, code)


def relative(value):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024 or re.search(r'[\x00-\x1f\x7f:\\]', value):
        refused('OFFICE_PATH_INVALID')
    parts = value.split('/')
    if any(not p or len(p.encode('utf-8')) > 255 or p in {'.', '..'} or p.startswith('.office-') or p.endswith((' ', '.'))
           or re.match(r'(?i)^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)', p) for p in parts):
        refused('OFFICE_PATH_INVALID')
    return value


def validate(body):
    if not isinstance(body, dict) or set(body) != {'protocol', 'request', 'inputs'} or body['protocol'] != PROTOCOL:
        refused('OFFICE_REQUEST_INVALID')
    request, inputs = body['request'], body['inputs']
    if (not isinstance(request, dict) or set(request) - {'operation', 'input', 'output', 'options'}
            or request.get('operation') not in OPERATIONS or not isinstance(inputs, list) or len(inputs) > 20):
        refused('OFFICE_REQUEST_INVALID')
    request = dict(request)
    options = request.setdefault('options', {})
    if not isinstance(options, dict) or RESERVED.intersection(options):
        refused('OFFICE_OPTIONS_INVALID')
    try:
        encoded_options = json.dumps(options, allow_nan=False).encode()
    except (TypeError, ValueError):
        refused('OFFICE_OPTIONS_INVALID')
    if len(encoded_options) > 512 * 1024:
        refused('OFFICE_OPTIONS_INVALID')
    entries = []
    for item in inputs:
        if (not isinstance(item, dict) or set(item) != {'path', 'size_bytes'}
                or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= INPUT_LIMIT):
            refused('OFFICE_INPUT_INVALID')
        entries.append({'path': relative(item['path']), 'size_bytes': item['size_bytes']})
    names = [row['path'] for row in entries]
    if len(set(names)) != len(names) or sum(row['size_bytes'] for row in entries) > INPUT_TOTAL:
        refused('OFFICE_INPUT_LIMIT', 413)
    operation = request['operation']
    merge = operation == 'pdf' and options.get('action') == 'merge'
    selected = []
    if 'input' in request:
        selected.append(relative(request['input']))
    if merge:
        if 'input' in request or not isinstance(options.get('inputs'), list) or not 1 <= len(options['inputs']) <= 20:
            refused('OFFICE_INPUT_INVALID')
        selected += [relative(p) for p in options['inputs']]
    elif 'inputs' in options:
        refused('OFFICE_OPTIONS_INVALID')
    if operation == 'create':
        if selected:
            refused('OFFICE_INPUT_INVALID')
    elif not selected:
        refused('OFFICE_INPUT_REQUIRED')
    if set(selected) != set(names):
        refused('OFFICE_SELECTED_INPUTS_REQUIRED')
    if operation in MUTATING:
        output = relative(request.get('output'))
        if output in names:
            refused('OFFICE_NEW_OUTPUT_REQUIRED')
        all_names = names + [output]
    else:
        if 'output' in request:
            refused('OFFICE_READ_OUTPUT_DENIED')
        all_names = names
    if any(a != b and b.startswith(a + '/') for a in all_names for b in all_names):
        refused('OFFICE_PATH_CONFLICT')
    return request, entries


@dataclass(frozen=True)
class Config:
    root: Path = Path('/var/lib/megmeet-office/jobs')
    engine: Path = Path('/opt/office/engine.py')
    python: Path = Path('/opt/office/venv/bin/python')
    secret_file: Path = Path('/run/secrets/office-service')
    concurrency: int = 2
    queue_limit: int = 16
    owner_pending: int = 4
    max_jobs: int = 64
    spool_bytes: int = 2 * 1024**3
    ttl_seconds: int = 900
    timeout_seconds: int = 120
    child_memory_bytes: int = 3 * 1024**3

    def __post_init__(self):
        for path in (self.root, self.engine, self.python, self.secret_file):
            if not path.is_absolute():
                raise ValueError('OFFICE_CONFIG_PATH_INVALID')
        for value, low, high in ((self.concurrency, 1, 4), (self.queue_limit, 1, 64),
            (self.owner_pending, 1, 16), (self.max_jobs, 1, 256),
            (self.spool_bytes, 128 * 1024**2, 16 * 1024**3),
            (self.ttl_seconds, 30, 3600), (self.timeout_seconds, 1, 300),
            (self.child_memory_bytes, 256 * 1024**2, 8 * 1024**3)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('OFFICE_RESOURCE_CONFIG_INVALID')

    @classmethod
    def environment(cls):
        fields = {'root': 'OFFICE_JOB_ROOT', 'engine': 'OFFICE_ENGINE_PATH',
                  'python': 'OFFICE_PYTHON', 'secret_file': 'OFFICE_SERVICE_SECRET_FILE'}
        values = {key: Path(os.environ[name]) for key, name in fields.items() if name in os.environ}
        for key in ('concurrency', 'queue_limit', 'owner_pending', 'max_jobs', 'spool_bytes',
                    'ttl_seconds', 'timeout_seconds', 'child_memory_bytes'):
            name = 'OFFICE_' + key.upper()
            if name in os.environ:
                values[key] = int(os.environ[name])
        return cls(**values)


@dataclass
class Job:
    id: str
    owner: str
    scope: str
    request: dict
    inputs: list
    root: Path
    reservation: int
    state: str = 'uploading'
    uploaded: set = field(default_factory=set)
    uploading: set = field(default_factory=set)
    result: Any = None
    outputs: list = field(default_factory=list)
    error: str | None = None
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    finished: float | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: Any = None


class EngineFailure(Exception):
    def __init__(self, code):
        self.code = code


async def terminate(process):
    # The group remains relevant even if a converter's parent exited early.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 1.5)
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def run_engine(config, request, cancel, temporary):
    if cancel.is_set():
        raise EngineFailure('OFFICE_CANCELLED')
    env = {'PATH': str(config.python.parent) + ':/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1',
           'HOME': str(temporary), 'TMPDIR': str(temporary),
           'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
           'OFFICE_CHILD_MEMORY_BYTES': str(config.child_memory_bytes),
           'OFFICE_CHILD_CPU_SECONDS': str(config.timeout_seconds + 5)}
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(str(config.python), '-I', '-B',
        str(Path(__file__).with_name('runner.py')), str(config.engine), cwd=temporary,
        env=env, start_new_session=True, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
    process = None
    tasks = []
    try:
        process = await asyncio.shield(spawn)

        async def collect():
            chunks, total = [], 0
            while chunk := await process.stdout.read(65536):
                total += len(chunk)
                if total > RESPONSE_LIMIT:
                    raise EngineFailure('OFFICE_RESPONSE_LIMIT')
                chunks.append(chunk)
            return b''.join(chunks)

        async def discard():
            while await process.stderr.read(65536):
                pass

        async def exchange():
            reader, diagnostics = asyncio.create_task(collect()), asyncio.create_task(discard())
            tasks.extend([reader, diagnostics])
            process.stdin.write(json.dumps(request, allow_nan=False).encode() + b'\n')
            await process.stdin.drain()
            process.stdin.close()
            raw = await reader
            await process.wait()
            await diagnostics
            if process.returncode != 0:
                raise EngineFailure('OFFICE_ENGINE_FAILED')
            return raw

        operation = asyncio.create_task(exchange())
        cancelled = asyncio.create_task(cancel.wait())
        tasks.extend([operation, cancelled])
        complete, _ = await asyncio.wait([operation, cancelled], timeout=config.timeout_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise EngineFailure('OFFICE_CANCELLED')
        if operation not in complete:
            raise EngineFailure('OFFICE_TIMEOUT')
        raw = await operation
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('protocol') != ENGINE_PROTOCOL or type(value.get('ok')) is not bool:
                raise ValueError()
            if not value['ok']:
                code = (value.get('error') or {}).get('code', '')
                if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code):
                    code = 'OFFICE_ENGINE_REFUSED'
                raise EngineFailure(code)
            if 'result' not in value:
                raise ValueError()
            return value['result']
        except (ValueError, TypeError, AttributeError):
            raise EngineFailure('OFFICE_RESPONSE_INVALID') from None
    finally:
        if process is None:
            with suppress(BaseException):
                process = await spawn
        if process is not None:
            await terminate(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class OfficeService:
    def __init__(self, config, secret, runner=run_engine):
        self.config, self.secret, self.runner = config, secret, runner
        self.jobs, self.queue, self.running = {}, [], {}
        self.lock = asyncio.Lock()
        self.reaper = None
        self.spool_lock = None
        self.closed = False
        self.capability_cache = None

    async def start(self):
        root = self.config.root
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError('OFFICE_SPOOL_SYMLINK')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        if not self.config.engine.is_file() or self.config.engine.is_symlink() or not self.config.python.is_file():
            raise ValueError('OFFICE_ENGINE_UNAVAILABLE')
        lock_path = root / '.office-lock'
        if lock_path.is_symlink():
            raise ValueError('OFFICE_SPOOL_SYMLINK')
        self.spool_lock = lock_path.open('a')
        try:
            fcntl.flock(self.spool_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.spool_lock.close()
            raise ValueError('OFFICE_SINGLE_WORKER_REQUIRED') from None
        marker = root / '.office-spool-v1'
        ownership = b'megmeet-shared-office-ephemeral-spool-v1\n'
        if not marker.exists():
            if any(path != lock_path for path in root.iterdir()):
                self.spool_lock.close()
                raise ValueError('OFFICE_SPOOL_NOT_PRIVATE')
            with marker.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(ownership)
        elif marker.is_symlink() or not marker.is_file() or marker.read_bytes() != ownership:
            self.spool_lock.close()
            raise ValueError('OFFICE_SPOOL_NOT_PRIVATE')
        # This dedicated ephemeral mount contains only service-owned UUID jobs.
        # Never accept other filenames and never recursively follow a link.
        stale = [path for path in root.iterdir() if path not in {lock_path, marker}]
        for path in stale:
            if not JOB_ID.fullmatch(path.name) or not path.is_dir() or path.is_symlink():
                self.spool_lock.close()
                raise ValueError('OFFICE_SPOOL_NOT_PRIVATE')
        for path in stale:
            shutil.rmtree(path)
        self.reaper = asyncio.create_task(self.reap_loop())

    async def close(self):
        self.closed = True
        if self.reaper:
            self.reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self.reaper
        for job in list(self.jobs.values()):
            await self.cancel(job)
        if self.spool_lock:
            self.spool_lock.close()

    def identity(self, request):
        if not hmac.compare_digest(request.headers.get('x-office-service-key', '').encode(), self.secret):
            refused('OFFICE_SERVICE_AUTH_REQUIRED', 401)
        owner, scope = request.headers.get('x-office-owner', ''), request.headers.get('x-office-scope', '')
        if not OPAQUE.fullmatch(owner) or not OPAQUE.fullmatch(scope):
            refused('OFFICE_IDENTITY_REQUIRED', 401)
        return owner, scope

    def owned(self, job_id, identity):
        job = self.jobs.get(job_id)
        if not job or (job.owner, job.scope) != identity:
            refused('OFFICE_JOB_NOT_FOUND', 404)
        return job

    def status(self, job):
        value = {'protocol': PROTOCOL, 'job_id': job.id, 'state': job.state, 'outputs': job.outputs}
        if job.state == 'queued':
            value['queue_position'] = self.queue.index(job.id) + 1
        if job.state == 'succeeded':
            value['result'] = job.result
        if job.error:
            value['error'] = {'code': job.error}
        return value

    async def create(self, identity, body):
        request, inputs = validate(body)
        reservation = sum(row['size_bytes'] for row in inputs) + (OUTPUT_LIMIT if request['operation'] in MUTATING else 0)
        async with self.lock:
            if self.closed:
                refused('OFFICE_UNAVAILABLE', 503)
            # Bounded cancellation tombstones must not exhaust useful capacity.
            for old in list(self.jobs.values()):
                if len(self.jobs) < self.config.max_jobs:
                    break
                if old.state == 'cancelled' and old.done.is_set() and not old.uploading:
                    self.jobs.pop(old.id)
            if (len(self.jobs) >= self.config.max_jobs
                    or sum(j.reservation for j in self.jobs.values()) + reservation > self.config.spool_bytes):
                refused('OFFICE_STORAGE_BUSY', 429)
            if sum(j.owner == identity[0] and j.state not in TERMINAL for j in self.jobs.values()) >= self.config.owner_pending:
                refused('OFFICE_OWNER_BUSY', 429)
            job_id = uuid.uuid4().hex
            root = self.config.root / job_id
            root.mkdir(mode=0o700)
            try:
                (root / '.office-tmp').mkdir(mode=0o700)
                for relative_path in [row['path'] for row in inputs] + ([request['output']] if 'output' in request else []):
                    (root / relative_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:
                shutil.rmtree(root)
                refused('OFFICE_STORAGE_UNAVAILABLE', 503)
            job = Job(job_id, *identity, request, inputs, root, reservation)
            self.jobs[job_id] = job
            return job

    async def upload(self, job, index, stream):
        async with self.lock:
            if job.state != 'uploading' or not 0 <= index < len(job.inputs) or index in job.uploaded or index in job.uploading:
                refused('OFFICE_UPLOAD_STATE_INVALID', 409)
            job.uploading.add(index)
            job.touched = time.monotonic()
        entry = job.inputs[index]
        target = job.root / entry['path']
        partial = job.root / '.office-tmp' / ('upload-' + str(index))

        async def receive():
            size = 0
            with partial.open('xb') as output:
                os.chmod(partial, 0o600)
                async for chunk in stream:
                    if job.cancel.is_set():
                        refused('OFFICE_JOB_CANCELLED', 409)
                    size += len(chunk)
                    if size > entry['size_bytes']:
                        refused('OFFICE_INPUT_SIZE_MISMATCH', 413)
                    output.write(chunk)
                    job.touched = time.monotonic()
            if size != entry['size_bytes']:
                refused('OFFICE_INPUT_SIZE_MISMATCH', 400)
            async with self.lock:
                if job.state != 'uploading':
                    refused('OFFICE_JOB_CANCELLED', 409)
                os.link(partial, target)
                partial.unlink()
                job.uploaded.add(index)

        try:
            await asyncio.wait_for(receive(), self.config.timeout_seconds)
        except asyncio.TimeoutError:
            refused('OFFICE_UPLOAD_TIMEOUT', 408)
        except OSError:
            refused('OFFICE_STORAGE_UNAVAILABLE', 503)
        finally:
            with suppress(FileNotFoundError):
                partial.unlink()
            async with self.lock:
                job.uploading.discard(index)
                if job.state == 'cancelled':
                    self.cleanup(job)

    async def submit(self, job):
        async with self.lock:
            if job.state != 'uploading' or job.uploading or len(job.uploaded) != len(job.inputs):
                refused('OFFICE_UPLOADS_INCOMPLETE', 409)
            if len(self.queue) >= self.config.queue_limit:
                refused('OFFICE_QUEUE_FULL', 429)
            job.state = 'queued'
            self.queue.append(job.id)
            self.schedule()

    def schedule(self):
        if self.closed:
            return
        for job_id in list(self.queue):
            if len(self.running) >= self.config.concurrency:
                break
            job = self.jobs[job_id]
            if job.owner in self.running.values():
                continue
            self.queue.remove(job_id)
            self.running[job_id] = job.owner
            job.state = 'running'
            job.task = asyncio.create_task(self.execute(job))

    def cleanup(self, job):
        if job.root.parent != self.config.root or not JOB_ID.fullmatch(job.root.name) or job.root.is_symlink():
            raise RuntimeError('OFFICE_CLEANUP_OWNERSHIP_FAILED')
        if job.root.exists():
            shutil.rmtree(job.root)
        job.reservation = 0

    async def execute(self, job):
        try:
            request = {'protocol': ENGINE_PROTOCOL, **job.request, 'workspace': str(job.root)}
            result = await self.runner(self.config, request, job.cancel, job.root / '.office-tmp')
            if not isinstance(result, dict) or len(json.dumps(result, allow_nan=False).encode()) > RESPONSE_LIMIT:
                raise EngineFailure('OFFICE_RESULT_INVALID')
            outputs = []
            if 'output' in job.request:
                output = job.root / job.request['output']
                info = output.lstat()
                if (any(p.is_symlink() for p in (output, *output.parents)) or not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1 or not 0 < info.st_size <= OUTPUT_LIMIT):
                    raise EngineFailure('OFFICE_OUTPUT_INVALID')
                digest = await asyncio.to_thread(file_hash, output)
                if result.get('output') != job.request['output'] or result.get('bytes') != info.st_size or result.get('sha256') != digest:
                    raise EngineFailure('OFFICE_OUTPUT_INTEGRITY_FAILED')
                outputs = [{'id': '0', 'path': job.request['output'], 'size_bytes': info.st_size, 'sha256': digest}]
            async with self.lock:
                if job.cancel.is_set():
                    raise EngineFailure('OFFICE_CANCELLED')
                job.result, job.outputs, job.state = result, outputs, 'succeeded'
                for item in job.inputs:
                    (job.root / item['path']).unlink()
                shutil.rmtree(job.root / '.office-tmp')
                job.reservation = sum(row['size_bytes'] for row in outputs)
        except BaseException as error:
            async with self.lock:
                job.state = 'cancelled' if job.cancel.is_set() else 'failed'
                job.error = None if job.state == 'cancelled' else (error.code if isinstance(error, EngineFailure) else 'OFFICE_PROCESSING_FAILED')
                job.result, job.outputs = None, []
                self.cleanup(job)
        finally:
            async with self.lock:
                job.finished = time.monotonic()
                self.running.pop(job.id, None)
                job.done.set()
                self.schedule()

    async def cancel(self, job):
        async with self.lock:
            job.cancel.set()
            running = job.id in self.running
            job.state, job.result, job.outputs, job.error = 'cancelled', None, [], None
            if job.id in self.queue:
                self.queue.remove(job.id)
            if not running and not job.uploading:
                self.cleanup(job)
            if not running:
                job.done.set()
                job.finished = time.monotonic()
            self.schedule()
        if running:
            await job.done.wait()

    async def reap(self, now=None):
        now = time.monotonic() if now is None else now
        for job in list(self.jobs.values()):
            start = job.finished if job.finished is not None else job.touched
            if job.state != 'running' and not job.uploading and now - start >= self.config.ttl_seconds:
                await self.cancel(job)
                async with self.lock:
                    self.jobs.pop(job.id, None)

    async def reap_loop(self):
        while True:
            await asyncio.sleep(5)
            await self.reap()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def create_app(config=None, *, secret=None, runner=run_engine):
    config = config or Config.environment()

    @asynccontextmanager
    async def lifespan(app):
        if secret is None:
            path = config.secret_file
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError('OFFICE_SERVICE_KEY_INVALID')
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 32 <= info.st_size <= 257:
                raise ValueError('OFFICE_SERVICE_KEY_INVALID')
            key = path.read_bytes().strip()
        else:
            key = secret
        if not isinstance(key, bytes) or not 32 <= len(key) <= 256:
            raise ValueError('OFFICE_SERVICE_KEY_INVALID')
        service = OfficeService(config, key, runner)
        await service.start()
        app.state.office = service
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(request, call_next):
        if request.url.path != '/health':
            try:
                request.state.identity = app.state.office.identity(request)
            except HTTPException as error:
                return JSONResponse({'detail': error.detail}, error.status_code, headers={'Cache-Control': 'no-store'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/health')
    async def health():
        return {'status': 'healthy', 'service': 'office-service'}

    @app.get('/v1/office/capabilities')
    async def capabilities(request: Request):
        # Capability jobs use the same admission and concurrency limits as work.
        service = app.state.office
        if service.capability_cache is not None:
            return service.capability_cache
        job = await service.create(request.state.identity, {'protocol': PROTOCOL,
            'request': {'operation': 'create', 'output': 'capability.txt', 'options': {'text': 'capability'}}, 'inputs': []})
        job.request = {'operation': 'capabilities', 'options': {}}
        try:
            await service.submit(job)
            await asyncio.wait_for(job.done.wait(), config.timeout_seconds + config.ttl_seconds)
            if job.state != 'succeeded':
                refused(job.error or 'OFFICE_UNAVAILABLE', 503)
            service.capability_cache = {**job.result, 'remote_limits': {'concurrency': config.concurrency,
                'queue_limit': config.queue_limit, 'owner_pending': config.owner_pending,
                'input_bytes': INPUT_LIMIT, 'total_input_bytes': INPUT_TOTAL,
                'output_bytes': OUTPUT_LIMIT, 'retention_seconds': config.ttl_seconds}}
            return service.capability_cache
        finally:
            await service.cancel(job)
            async with service.lock:
                service.jobs.pop(job.id, None)

    @app.post('/v1/office/jobs', status_code=201)
    async def create(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                refused('OFFICE_REQUEST_LIMIT', 413)
        try:
            body = json.loads(raw)
            job = await app.state.office.create(request.state.identity, body)
        except (ValueError, TypeError):
            refused('OFFICE_REQUEST_INVALID')
        return app.state.office.status(job)

    @app.get('/v1/office/jobs/{job_id}')
    async def status(job_id: str, request: Request):
        service = app.state.office
        return service.status(service.owned(job_id, request.state.identity))

    @app.put('/v1/office/jobs/{job_id}/inputs/{index}')
    async def upload(job_id: str, index: int, request: Request):
        service = app.state.office
        job = service.owned(job_id, request.state.identity)
        await service.upload(job, index, request.stream())
        return service.status(job)

    @app.post('/v1/office/jobs/{job_id}/run', status_code=202)
    async def submit(job_id: str, request: Request):
        service = app.state.office
        job = service.owned(job_id, request.state.identity)
        await service.submit(job)
        return service.status(job)

    @app.delete('/v1/office/jobs/{job_id}')
    async def cancel(job_id: str, request: Request):
        service = app.state.office
        job = service.owned(job_id, request.state.identity)
        await service.cancel(job)
        return service.status(job)

    @app.get('/v1/office/jobs/{job_id}/outputs/{output_id}')
    async def download(job_id: str, output_id: str, request: Request):
        service = app.state.office
        job = service.owned(job_id, request.state.identity)
        artifact = next((o for o in job.outputs if o['id'] == output_id), None)
        if job.state != 'succeeded' or artifact is None:
            refused('OFFICE_OUTPUT_NOT_FOUND', 404)
        source = (job.root / artifact['path']).open('rb')

        async def chunks():
            try:
                while not job.cancel.is_set():
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    yield chunk
                    await asyncio.sleep(0)
            finally:
                source.close()
        return StreamingResponse(chunks(), media_type='application/octet-stream',
            headers={'Content-Length': str(artifact['size_bytes']), 'X-Office-SHA256': artifact['sha256']})

    return app


app = create_app()
