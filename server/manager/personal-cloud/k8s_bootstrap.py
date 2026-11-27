"""Generation-scoped immutable bootstrap Secrets; credential values stay in memory.

The manager's API service-account token is reread on every request, including
Kubernetes atomic projected-volume rotation. Worker route/lease credentials are
immutable for one generation and recovered from that exact Secret UID after a
manager restart. They are never rotated in-place underneath a running worker.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

from k8s_worker_provider import PREFIX, Handle, ProviderError, WorkerSpec, require, subset, uid

CHUNK_BYTES = 60000
MAX_CATALOG = 524288


class RotatingTokenFile:
    def __init__(self, path):
        require(isinstance(path, str) and os.path.isabs(path), 'TOKEN_FILE_CONFIGURATION_REFUSED')
        self.path = Path(path)
        self.root = self.path.parent.resolve(strict=True)

    def __call__(self):
        try:
            resolved = self.path.resolve(strict=True)
            require(resolved.is_relative_to(self.root), 'TOKEN_FILE_SCOPE_REFUSED')
            # Kubernetes projected Secret keys are symlinks into an atomic
            # version directory; accept only targets under the configured mount.
            fd = os.open(str(resolved), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 32769 and not info.st_mode & 0o022,
                        'TOKEN_FILE_REFUSED')
                raw = stream.read(32770)
            token = raw.decode('ascii').rstrip('\n')
            require(0 < len(token) <= 32768 and not any(c.isspace() for c in token), 'TOKEN_FILE_REFUSED')
            return token
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('TOKEN_FILE_UNAVAILABLE') from None


def catalog_parts(value):
    require(isinstance(value, dict) and set(value) == {'data', 'default_model'} and isinstance(value['data'], list) and
            len(value['data']) <= 1000, 'CATALOG_REFUSED')
    names = set()
    for model in value['data']:
        require(isinstance(model, dict) and set(model) <= {'id', 'name', 'inputModalities'} and
                isinstance(model.get('id'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', model['id']) and
                model['id'] not in names, 'CATALOG_REFUSED')
        names.add(model['id'])
        require(model.get('name') is None or isinstance(model['name'], str) and len(model['name']) <= 500, 'CATALOG_REFUSED')
        if 'inputModalities' in model:
            modes = model['inputModalities']
            require(isinstance(modes, list) and 'text' in modes and len(modes) == len(set(modes)) and
                    all(x in ('text', 'image') for x in modes), 'CATALOG_REFUSED')
    require(value['default_model'] in names if names else value['default_model'] is None, 'CATALOG_REFUSED')
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except Exception:
        raise ProviderError('CATALOG_REFUSED') from None
    require(0 < len(raw) <= MAX_CATALOG, 'CATALOG_TOO_LARGE')
    return [raw[i:i + CHUNK_BYTES] for i in range(0, len(raw), CHUNK_BYTES)]


class PrivateCredentials(dict):
    """Application-only return value with a deliberately redacted representation."""
    def __repr__(self):
        return '<PrivateCredentials redacted>'

    __str__ = __repr__


class BootstrapSecrets:
    def __init__(self, provider):
        self.provider = provider

    def payload(self, spec, route_token, lease='', catalog=None):
        require(isinstance(spec, WorkerSpec), 'BOOTSTRAP_SPEC_REFUSED')
        require(isinstance(route_token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', route_token), 'ROUTE_CREDENTIAL_REFUSED')
        values = {'CLOUD_ROUTE_TOKEN': route_token}
        if spec.mode == 'worker' and not (self.provider.config.execution_pool and self.provider.config.pool_role=='execution'):
            require(isinstance(lease, str) and re.fullmatch(r'mcl_[A-Za-z0-9_-]{43}', lease), 'MODEL_LEASE_REFUSED')
            parts = catalog_parts(catalog)
            require(len(parts) == spec.catalog_chunks, 'CATALOG_CHUNK_COUNT_REFUSED')
            values.update(CLOUD_MODEL_LEASE=lease, CLOUD_MODEL_CATALOG_COUNT=str(len(parts)))
            values.update({'CLOUD_MODEL_CATALOG_' + str(i): part for i, part in enumerate(parts)})
        elif spec.mode != 'worker':
            require(lease == '' and catalog is None, 'CARD_BOOTSTRAP_MODEL_REFUSED')
        return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'immutable': True,
                'metadata': {'name': spec.secret_name, 'namespace': self.provider.config.namespace,
                             'labels': self.provider.labels(spec)},
                'data': {key: base64.b64encode(value.encode()).decode('ascii') for key, value in values.items()}}

    def identity(self, spec, observed, expected_uid=None, terminating=False):
        require(isinstance(observed, dict), 'BOOTSTRAP_IDENTITY_REFUSED')
        meta = observed.get('metadata', {})
        require(meta.get('name') == spec.secret_name and meta.get('namespace') == self.provider.config.namespace and
                subset(self.provider.labels(spec), meta.get('labels', {})) and uid(meta.get('uid')) and
                (expected_uid is None or meta['uid'] == expected_uid) and not meta.get('ownerReferences') and
                (terminating or not meta.get('deletionTimestamp')) and observed.get('type') == 'Opaque' and
                observed.get('immutable') is True, 'BOOTSTRAP_IDENTITY_REFUSED')
        return meta['uid']

    async def ensure(self, spec, route_token, lease='', catalog=None):
        desired = self.payload(spec, route_token, lease, catalog)
        path = self.provider.base + '/secrets'
        status, observed = await self.provider.call('POST', path, desired)
        if status == 409:
            status, observed = await self.provider.call('GET', path + '/' + spec.secret_name)
        require(status in (200, 201), 'BOOTSTRAP_CREATE_REFUSED')
        secret_uid = self.identity(spec, observed)
        actual = observed.get('data')
        require(isinstance(actual, dict) and set(actual) == set(desired['data']), 'BOOTSTRAP_DATA_REFUSED')
        require(all(isinstance(actual[k], str) and hmac.compare_digest(actual[k], v) for k, v in desired['data'].items()),
                'BOOTSTRAP_DATA_REFUSED')
        return secret_uid

    async def read(self, handle):
        spec = self.provider.check_handle(handle)
        status, observed = await self.provider.call('GET', self.provider.base + '/secrets/' + spec.secret_name)
        require(status == 200, 'BOOTSTRAP_READ_REFUSED')
        self.identity(spec, observed, handle.secret_uid)
        try:
            data = observed['data']
            require(isinstance(data, dict) and len(data) <= 12, 'BOOTSTRAP_DATA_REFUSED')
            values = {key: base64.b64decode(value, validate=True).decode('ascii') for key, value in data.items()}
            route_token = values['CLOUD_ROUTE_TOKEN']
            if spec.mode == 'worker' and not (self.provider.config.execution_pool and self.provider.config.pool_role=='execution'):
                require(values['CLOUD_MODEL_CATALOG_COUNT'] == str(spec.catalog_chunks), 'BOOTSTRAP_DATA_REFUSED')
                catalog = json.loads(''.join(values['CLOUD_MODEL_CATALOG_' + str(i)] for i in range(spec.catalog_chunks)))
                lease = values['CLOUD_MODEL_LEASE']
            else:
                lease, catalog = '', None
            expected = self.payload(spec, route_token, lease, catalog)
            require(set(data) == set(expected['data']) and all(hmac.compare_digest(data[k], v) for k, v in expected['data'].items()),
                    'BOOTSTRAP_DATA_REFUSED')
            return PrivateCredentials(route_token=route_token, lease=lease, catalog=catalog)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('BOOTSTRAP_DATA_REFUSED') from None

    async def cleanup(self, handle, proof):
        from worker_reservations import VerifiedTermination, handle_digest
        spec = self.provider.check_handle(handle)
        # Only the application may pass the node verifier's typed result. A raw
        # execution_gone dictionary or an API 404 cannot authorize cleanup.
        require(isinstance(proof, VerifiedTermination) and proof.owner == handle.owner and proof.generation == handle.generation and
                proof.handle_sha256 == handle_digest(handle) and uid(proof.audit_id) and
                isinstance(proof.evidence_sha256, str) and re.fullmatch(r'[0-9a-f]{64}', proof.evidence_sha256),
                'TERMINATION_PROOF_REQUIRED')
        for plural in ('pods', 'services'):
            status, _ = await self.provider.call('GET', self.provider.base + '/' + plural + '/' + handle.name)
            require(status == 404, 'BOOTSTRAP_EXECUTION_STILL_PRESENT')
        path = self.provider.base + '/secrets/' + spec.secret_name
        status, observed = await self.provider.call('GET', path)
        if status == 404:
            return {'secret_absent': True}
        require(status == 200, 'BOOTSTRAP_READ_REFUSED')
        self.identity(spec, observed, handle.secret_uid, terminating=True)
        if not observed['metadata'].get('deletionTimestamp'):
            status, _ = await self.provider.call('DELETE', path, {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                'preconditions': {'uid': handle.secret_uid}, 'propagationPolicy': 'Background'})
            require(status in (200, 202, 404), 'BOOTSTRAP_DELETE_REFUSED')
        status, _ = await self.provider.call('GET', path)
        require(status == 404, 'BOOTSTRAP_DELETE_PENDING')
        return {'secret_absent': True}
