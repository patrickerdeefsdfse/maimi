"""Private OpenSandbox 1.1.1 adapter. Not registered in the deployed DSH profile.

The entire sandbox, including its execd process, is untrusted. A privileged node
guard verifies the outer boundary and snapshots exports; SDK readiness alone is
never evidence of isolation. Errors carry local codes, never upstream bodies.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import importlib.metadata
import logging
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.transport.retry import RetryPolicy
from opensandbox.exceptions import SandboxApiException
from opensandbox.manager import SandboxManager
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import SandboxFilter

SDK_VERSION = '1.1.1'
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
MAX_LOG = 65536
IDENT = re.compile(r'[a-zA-Z0-9_-]{1,100}\Z')
TASK = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
IMAGE = re.compile(r'[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}\Z')
REQUIRED_NODE_FACTS = (
    'runsc_registered', 'server_policy_pinned', 'internal_network_verified',
    'peer_traffic_blocked', 'host_traffic_blocked', 'rootfs_readonly_enforced',
    'bounded_tmpfs_enforced', 'no_host_business_mounts', 'api_private_and_authenticated',
    'snapshot_supported', 'capacity_reserved',
)


class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Profile:
    image: str
    cpu_millis: int = 1000
    memory_mib: int = 2048
    pids: int = 512
    max_command_seconds: int = 120
    lifetime_seconds: int = 3600

    def validate(self):
        if not IMAGE.fullmatch(self.image) or not 100 <= self.cpu_millis <= 8000:
            raise ProviderError('INVALID_ADMIN_PROFILE')
        if not 1024 <= self.memory_mib <= 16384 or self.pids != 512:
            raise ProviderError('INVALID_ADMIN_PROFILE')
        if not 1 <= self.max_command_seconds <= 600 or not self.max_command_seconds <= self.lifetime_seconds <= 3600:
            raise ProviderError('INVALID_ADMIN_PROFILE')


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, wrapped, limit):
        self.wrapped, self.limit = wrapped, limit

    async def __aiter__(self):
        count = 0
        try:
            async for chunk in self.wrapped:
                count += len(chunk)
                if count > self.limit:
                    raise ProviderError('UPSTREAM_RESPONSE_TOO_LARGE')
                yield chunk
        finally:
            await self.wrapped.aclose()

    async def aclose(self):
        await self.wrapped.aclose()


class PrivateTransport(httpx.AsyncBaseTransport):
    """No redirects, retries or environment proxy; only the configured API origin.

    Bounds a single stream as well as aggregate SDK output accumulation. Execd
    endpoints are always server-proxy URLs; API credentials never go to a sandbox.
    """
    def __init__(self, endpoint, inner=None):
        self.origin = httpx.URL(endpoint)
        self.inner = inner or httpx.AsyncHTTPTransport(trust_env=False, retries=0, verify=True)

    async def handle_async_request(self, request):
        u, o = request.url, self.origin
        if (u.scheme, u.host, u.port) != (o.scheme, o.host, o.port) or not u.path.startswith('/v1/'):
            raise ProviderError('UNTRUSTED_UPSTREAM_ENDPOINT')
        request.headers['accept-encoding'] = 'identity'
        response = await self.inner.handle_async_request(request)
        if response.is_redirect or response.headers.get('content-encoding', 'identity') not in ('', 'identity'):
            await response.aclose()
            raise ProviderError('UNSUPPORTED_UPSTREAM_RESPONSE')
        # Lifecycle JSON <= 1 MiB; streaming command output wire bytes <= 4 MiB.
        limit = 4 * 1024 * 1024 if '/proxy/' in u.path else 1024 * 1024
        response.stream = _BoundedStream(response.stream, limit)
        return response

    async def aclose(self):
        await self.inner.aclose()


class Provider:
    """Trusted node_guard ports:

    probe() -> fixed boolean facts; verify_sandbox(id, task, profile, namespace)
    snapshot_output(id, task, profile, namespace, path, max_bytes) -> bytes
    verify_task_absent(task, namespace) -> True only after local Docker absence.

    Node guard has administrative Docker authority; it is never exposed to DSH,
    user models or sandbox processes. Calls must be scoped to this controller.
    """
    def __init__(self, *, endpoint, api_key, namespace, profiles, node_guard,
                 enabled=False, transport_factory=None):
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ProviderError('INVALID_PRIVATE_ENDPOINT')
        # Same-host first phase. Remote migration requires explicit TLS/mTLS node design.
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ProviderError('PRIVATE_LOOPBACK_ENDPOINT_REQUIRED')
        if not isinstance(api_key, str) or not 32 <= len(api_key) <= 512 or any(c.isspace() for c in api_key):
            raise ProviderError('INVALID_PRIVATE_CREDENTIAL')
        if not IDENT.fullmatch(namespace) or not profiles:
            raise ProviderError('INVALID_ADMIN_CONFIGURATION')
        if importlib.metadata.version('opensandbox') != SDK_VERSION:
            raise ProviderError('SDK_VERSION_CHANGED')
        for name, profile in profiles.items():
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name):
                raise ProviderError('INVALID_ADMIN_PROFILE')
            profile.validate()
        self.endpoint, self.api_key, self.namespace = endpoint.rstrip('/'), api_key, namespace
        self.profiles, self.guard, self.enabled = dict(profiles), node_guard, enabled
        self.transport_factory = transport_factory
        # Upstream sometimes logs raw HTTP bodies and command details at error
        # level. The host process must use the local event-code audit instead.
        for name in tuple(logging.root.manager.loggerDict) + ('opensandbox', 'httpx', 'httpcore'):
            if name == 'opensandbox' or name.startswith(('opensandbox.', 'httpx', 'httpcore')):
                logging.getLogger(name).disabled = True

    def __repr__(self):
        return f'Provider(namespace={self.namespace!r}, enabled={self.enabled!r})'

    def _metadata(self, task_id, profile_name=None):
        if not isinstance(task_id, str) or not TASK.fullmatch(task_id):
            raise ProviderError('INVALID_TASK_ID')
        data = {'maimi_task_id': task_id, 'maimi_controller': self.namespace}
        if profile_name is not None:
            data['maimi_profile'] = profile_name
        return data

    @asynccontextmanager
    async def _connection(self):
        transport = PrivateTransport(self.endpoint, self.transport_factory() if self.transport_factory else None)
        cfg = ConnectionConfig(
            api_key=self.api_key, domain=self.endpoint.removeprefix('http://'), protocol='http',
            request_timeout=timedelta(seconds=30), follow_redirects=False,
            transport=transport, retry_policy=RetryPolicy.disabled(),
            use_server_proxy=True, disable_metrics=True, enable_tracing=False,
        )
        manager = None
        try:
            manager = await SandboxManager.create(cfg)
            yield cfg, manager
        finally:
            if manager is not None:
                await manager.close()
            await transport.aclose()

    async def probe(self):
        try:
            facts = await self.guard.probe()
            result = {key: facts.get(key) is True for key in REQUIRED_NODE_FACTS}
        except Exception:
            result = {key: False for key in REQUIRED_NODE_FACTS}
        result.update(sdk_version_pinned=True, automatic_retries=False, enabled=self.enabled)
        result['ready'] = self.enabled and all(result[key] for key in REQUIRED_NODE_FACTS)
        return result

    async def _require_ready(self):
        if not (await self.probe())['ready']:
            raise ProviderError('NODE_POLICY_NOT_VERIFIED')

    async def _info(self, manager, sandbox_id):
        if not isinstance(sandbox_id, str) or not IDENT.fullmatch(sandbox_id):
            raise ProviderError('INVALID_SANDBOX_ID')
        info = await manager.get_sandbox_info(sandbox_id)
        meta = info.metadata or {}
        task, name = meta.get('maimi_task_id'), meta.get('maimi_profile')
        if meta.get('maimi_controller') != self.namespace or name not in self.profiles:
            raise ProviderError('SANDBOX_OWNER_MISMATCH')
        self._metadata(task)
        profile = self.profiles[name]
        if info.id != sandbox_id or info.image is None or info.image.uri != profile.image:
            raise ProviderError('SANDBOX_IMAGE_MISMATCH')
        await self.guard.verify_sandbox(sandbox_id, task, profile, self.namespace)
        return task, profile

    async def create(self, task_id, profile_name, metadata):
        await self._require_ready()
        if profile_name not in self.profiles or metadata != {'maimi_task_id': task_id}:
            raise ProviderError('INVALID_CREATE_ARGUMENTS')
        meta, profile = self._metadata(task_id, profile_name), self.profiles[profile_name]
        sandbox = None
        try:
            async with self._connection() as (cfg, manager):
                # Never create again after a lost acknowledgement. Existing task
                # matches are an explicit uncertain outcome requiring reconcile.
                if await self._list_task(manager, task_id):
                    raise ProviderError('TASK_ALREADY_HAS_SANDBOX')
                sandbox = await Sandbox.create(
                    image=profile.image, connection_config=cfg,
                    timeout=timedelta(seconds=profile.lifetime_seconds),
                    ready_timeout=timedelta(seconds=45),
                    resource={'cpu': str(profile.cpu_millis / 1000), 'memory': f'{profile.memory_mib}Mi'},
                    metadata=meta, env={}, volumes=[], extensions={}, network_policy=None,
                    entrypoint=['/bin/sh', '-c', 'mkdir -p /workspace/inputs /workspace/outputs; exec sleep infinity'],
                )
                await self._info(manager, sandbox.id)
                return sandbox.id
        except ProviderError:
            raise
        except BaseException:
            raise ProviderError('CREATE_OUTCOME_UNKNOWN') from None
        finally:
            if sandbox is not None:
                await sandbox.close()

    @staticmethod
    def _path(path, prefix):
        if not isinstance(path, str) or len(path) > 280 or '\\' in path or any(ord(c) < 32 for c in path):
            raise ProviderError('INVALID_FILE_PATH')
        p = PurePosixPath(path)
        if p.as_posix() != path or '..' in p.parts or not path.startswith(prefix) or path == prefix:
            raise ProviderError('INVALID_FILE_PATH')
        return path

    async def write_input(self, sandbox_id, path, data):
        await self._require_ready()
        path = self._path(path, '/workspace/inputs/')
        if not isinstance(data, bytes) or len(data) > MAX_INPUT:
            raise ProviderError('INPUT_TOO_LARGE')
        sandbox = None
        try:
            async with self._connection() as (cfg, manager):
                await self._info(manager, sandbox_id)
                sandbox = await Sandbox.connect(sandbox_id, connection_config=cfg)
                await sandbox.files.write_files([WriteEntry(path=path, data=data, mode=444)])
        except ProviderError:
            raise
        except BaseException:
            raise ProviderError('INPUT_WRITE_OUTCOME_UNKNOWN') from None
        finally:
            if sandbox is not None:
                await sandbox.close()

    async def execute(self, sandbox_id, command, timeout_seconds, max_output_bytes):
        await self._require_ready()
        if not isinstance(command, str) or not command.strip() or '\x00' in command or len(command.encode()) > 32768:
            raise ProviderError('INVALID_COMMAND')
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_LOG:
            raise ProviderError('INVALID_OUTPUT_LIMIT')
        sandbox, execution_id = None, None
        logs, remaining, truncated = {'stdout': bytearray(), 'stderr': bytearray()}, max_output_bytes, False

        async def consume(key, message):
            nonlocal remaining, truncated
            raw = message.text.encode('utf-8')
            logs[key].extend(raw[:remaining])
            truncated |= len(raw) > remaining
            remaining -= min(remaining, len(raw))

        async def init(event):
            nonlocal execution_id
            execution_id = event.id

        async def reject_result(_):
            raise ProviderError('UNEXPECTED_COMMAND_RESULT_EVENT')

        try:
            async with self._connection() as (cfg, manager):
                _, profile = await self._info(manager, sandbox_id)
                if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= profile.max_command_seconds:
                    raise ProviderError('INVALID_COMMAND_TIMEOUT')
                sandbox = await Sandbox.connect(sandbox_id, connection_config=cfg)
                async with asyncio.timeout(timeout_seconds + 10):
                    result = await sandbox.commands.run(
                        command, opts=RunCommandOpts(background=False, working_directory='/workspace',
                                                    timeout=timedelta(seconds=timeout_seconds)),
                        handlers=ExecutionHandlers(skip_accumulation=True, on_init=init,
                            on_stdout=lambda m: consume('stdout', m), on_stderr=lambda m: consume('stderr', m),
                            on_result=reject_result),
                    )
                if result.error is not None or result.complete is None or type(result.exit_code) is not int:
                    raise ProviderError('COMMAND_COMPLETION_NOT_VERIFIED')
                return {'exit_code': result.exit_code, 'stdout': logs['stdout'].decode('utf-8', errors='ignore'),
                        'stderr': logs['stderr'].decode('utf-8', errors='ignore'), 'truncated': truncated}
        except BaseException:
            # Interrupt is best effort only. Caller retains the budget and must
            # destroy/reconcile the entire instance, including escaped descendants.
            if sandbox is not None and execution_id:
                try:
                    async with asyncio.timeout(5):
                        await sandbox.commands.interrupt(execution_id)
                except BaseException:
                    pass
            raise ProviderError('COMMAND_OUTCOME_UNKNOWN_NO_RETRY') from None
        finally:
            if sandbox is not None:
                await sandbox.close()

    async def read_output(self, sandbox_id, path, max_bytes):
        await self._require_ready()
        path = self._path(path, '/workspace/outputs/')
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_OUTPUT:
            raise ProviderError('INVALID_OUTPUT_LIMIT')
        try:
            async with self._connection() as (_, manager):
                task, profile = await self._info(manager, sandbox_id)
                data = await self.guard.snapshot_output(sandbox_id, task, profile, self.namespace, path, max_bytes)
                if not isinstance(data, bytes) or len(data) > max_bytes:
                    raise ProviderError('INVALID_SNAPSHOT_RESULT')
                return data
        except BaseException:
            raise ProviderError('OUTPUT_SNAPSHOT_UNCONFIRMED') from None

    async def _list_task(self, manager, task_id):
        expected, found = self._metadata(task_id), []
        for page in range(1, 5):
            result = await manager.list_sandbox_infos(SandboxFilter(metadata=expected, page=page, page_size=25))
            p = result.pagination
            if p.page != page or p.page_size != 25 or p.total_items > 100 or len(result.sandbox_infos) > 25:
                raise ProviderError('TASK_LOOKUP_UNBOUNDED')
            for info in result.sandbox_infos:
                if any((info.metadata or {}).get(k) != v for k, v in expected.items()):
                    raise ProviderError('TASK_LOOKUP_OWNER_MISMATCH')
                if not IDENT.fullmatch(info.id) or info.id in found:
                    raise ProviderError('TASK_LOOKUP_INVALID')
                found.append(info.id)
            if not p.has_next_page:
                if len(found) != p.total_items:
                    raise ProviderError('TASK_LOOKUP_CHANGED')
                return found
        raise ProviderError('TASK_LOOKUP_UNBOUNDED')

    async def destroy_task(self, task_id, sandbox_id=None):
        # Cleanup must work when activation is disabled, ownership revoked, or
        # runtime policy has drifted; it can never create or run commands.
        self._metadata(task_id)
        try:
            async with self._connection() as (_, manager):
                matches = await self._list_task(manager, task_id)
                if sandbox_id is not None:
                    if not isinstance(sandbox_id, str) or not IDENT.fullmatch(sandbox_id):
                        raise ProviderError('INVALID_SANDBOX_ID')
                    try:
                        info = await manager.get_sandbox_info(sandbox_id)
                    except SandboxApiException as e:
                        if e.status_code != 404:
                            raise
                    else:
                        if any((info.metadata or {}).get(k) != v for k, v in self._metadata(task_id).items()):
                            raise ProviderError('SANDBOX_OWNER_MISMATCH')
                        if sandbox_id not in matches:
                            matches.append(sandbox_id)
                for sid in matches:
                    # Re-check ownership immediately before destructive cleanup.
                    try:
                        info = await manager.get_sandbox_info(sid)
                    except SandboxApiException as e:
                        if e.status_code == 404:
                            continue
                        raise
                    if any((info.metadata or {}).get(k) != v for k, v in self._metadata(task_id).items()):
                        raise ProviderError('SANDBOX_OWNER_MISMATCH')
                    await manager.kill_sandbox(sid)
                if await self._list_task(manager, task_id):
                    raise ProviderError('DESTROY_NOT_CONFIRMED')
                if await self.guard.verify_task_absent(task_id, self.namespace) is not True:
                    raise ProviderError('DESTROY_NOT_CONFIRMED')
        except BaseException:
            raise ProviderError('DESTROY_OUTCOME_UNKNOWN') from None

    async def destroy(self, sandbox_id):
        try:
            async with self._connection() as (_, manager):
                info = await manager.get_sandbox_info(sandbox_id)
                task = (info.metadata or {}).get('maimi_task_id')
                if (info.metadata or {}).get('maimi_controller') != self.namespace:
                    raise ProviderError('SANDBOX_OWNER_MISMATCH')
            await self.destroy_task(task, sandbox_id)
        except BaseException:
            # Without task metadata a 404 cannot prove the unknown-create case.
            raise ProviderError('DESTROY_OUTCOME_UNKNOWN') from None
