"""Narrow policy hook for the pinned OpenSandbox 1.1.1 Docker server.

This is an unpublished local candidate, not an upstream feature. Its actual
runsc/tmpfs/bootstrap compatibility remains an explicit activation gate.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path

POLICY = 'maimi-osb-1.1.1-r64-v1'
NETWORK = 'maimi-sandbox-r64'
TMPFS = {
    '/workspace': 'rw,nosuid,nodev,size=536870912,mode=1777',
    '/tmp': 'rw,nosuid,nodev,size=134217728,mode=1777',
    '/opt/opensandbox': 'rw,nosuid,nodev,size=134217728,mode=0755',
    '/run': 'rw,nosuid,nodev,noexec,size=16777216,mode=0755',
}


def validate_config(config):
    if (config.runtime.type != 'docker' or not config.runtime.execd_run_as_init
        or config.secure_runtime.type != 'gvisor' or config.secure_runtime.docker_runtime != 'runsc'
        or config.docker.network_mode != NETWORK or config.docker.publish_host != '127.0.0.1'
        or config.docker.drop_capabilities != ['ALL'] or not config.docker.no_new_privileges
        or config.docker.pids_limit != 512 or config.docker.sandbox_env or config.docker.sandbox_binds
        or config.docker.apparmor_profile or config.docker.seccomp_profile
        or not config.server.api_key or len(config.server.api_key) < 32):
        raise RuntimeError('MAIMI_SERVER_POLICY_REJECTED')


def enforce_host_config(value):
    if (value.get('network_mode') != NETWORK or value.get('runtime') != 'runsc'
        or value.get('cap_drop') != ['ALL'] or value.get('cap_add')
        or value.get('security_opt') != ['no-new-privileges=true']
        or value.get('pids_limit') != 512
        or not 1024 * 1024 * 1024 <= value.get('mem_limit', 0) <= 16384 * 1024 * 1024
        or not 100000000 <= value.get('nano_cpus', 0) <= 8000000000
        or value.get('binds') or value.get('devices') or value.get('device_requests')
        or value.get('privileged') or value.get('pid_mode') or value.get('ipc_mode') == 'host'):
        raise RuntimeError('MAIMI_CONTAINER_POLICY_REJECTED')
    result = dict(value)
    result.update(read_only=True, tmpfs=dict(TMPFS), shm_size=67108864, memswap_limit=value['mem_limit'])
    # Bound Docker logs too. Commands normally return via SSE, but arbitrary
    # task code must not fill the host by writing to PID 1's log descriptors.
    result['log_config'] = {'Type': 'local', 'Config': {'max-size': '10m', 'max-file': '2'}}
    return result


def install(config):
    if importlib.metadata.version('opensandbox-server') != '1.1.1':
        raise RuntimeError('MAIMI_SERVER_VERSION_CHANGED')
    from opensandbox_server.services.docker.container_ops import DockerContainerOpsMixin
    from opensandbox_server.services.docker.docker_service import DockerSandboxService

    pins = json.loads((Path(__file__).parent / 'upstream-pins.json').read_text())
    for cls in (DockerContainerOpsMixin, DockerSandboxService):
        source_path = Path(inspect.getfile(cls))
        key = source_path.name
        digest = hashlib.sha256(source_path.read_text(encoding='utf-8').replace('\r\n', '\n').encode()).hexdigest()
        if digest != pins['server_normalized_sha256'][key]:
            raise RuntimeError('MAIMI_UPSTREAM_SOURCE_CHANGED')
    validate_config(config)
    if getattr(DockerContainerOpsMixin, '_maimi_policy', None):
        raise RuntimeError('MAIMI_POLICY_ALREADY_INSTALLED')
    original = DockerContainerOpsMixin._create_and_start_container
    signature = inspect.signature(original)

    def guarded(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        validate_config(self.app_config)
        if bound.arguments.get('unconfine_system_paths'):
            raise RuntimeError('MAIMI_UNCONFINED_REQUEST_REJECTED')
        bound.arguments['host_config_kwargs'] = enforce_host_config(bound.arguments['host_config_kwargs'])
        return original(*bound.args, **bound.kwargs)

    DockerContainerOpsMixin._create_and_start_container = guarded
    DockerContainerOpsMixin._maimi_policy = POLICY
    return {'policy': POLICY, 'verified': True}


def build_app():
    # Install before main initializes services, then validate the exact config
    # loaded by the actual app and expose only fixed facts behind its auth gate.
    from opensandbox_server.config import load_config
    config = load_config()
    install(config)
    from opensandbox_server.main import app, app_config
    validate_config(app_config)

    @app.get('/v1/maimi-policy', include_in_schema=False)
    async def policy():
        validate_config(app_config)
        return {'policy': POLICY, 'verified': True}

    return app
