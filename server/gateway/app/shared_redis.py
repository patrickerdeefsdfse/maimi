"""Standalone-compatible Redis clients and authenticated TLS Sentinel transport.

Redis remains asynchronously replicated. Security state deliberately stops on a
primary process change instead of resurrecting consumed tickets or revocations.
This is AP failover support, not a claim of lossless Redis failover.
"""
import asyncio
import json
import os
import re
import ssl
import stat
from pathlib import Path

from fastapi import HTTPException


class SharedStateUnavailable(HTTPException):
    def __init__(self, detail='shared_state_unavailable'):
        super().__init__(status_code=503, detail=detail)


METHODS = frozenset({'ping', 'get', 'set', 'getdel', 'delete', 'eval', 'ttl', 'pttl',
                     'exists', 'expire', 'incr', 'sadd', 'smembers', 'scard'})


def _require(condition):
    if not condition:
        raise SharedStateUnavailable('shared_state_configuration_unavailable')


def _file(env, name, secret=False):
    value = env.get(name, '')
    _require(isinstance(value, str) and value.startswith('/') and '\x00' not in value)
    path = Path(value)
    # Kubernetes projected Secrets use symlinks; validate the opened target.
    with path.open('rb') as stream:
        metadata = os.fstat(stream.fileno())
        _require(stat.S_ISREG(metadata.st_mode) and not metadata.st_mode & 0o022)
        _require(0 < metadata.st_size <= (8192 if secret else 131072))
        data = stream.read((8192 if secret else 131072) + 1)
    if not secret:
        return value
    try:
        text = data.decode('utf-8').rstrip('\n')
    except UnicodeError:
        raise SharedStateUnavailable('shared_state_configuration_unavailable') from None
    _require(16 <= len(text) <= 4096 and not any(x in text for x in ('\r', '\n', '\x00')))
    return text


def _nodes(raw, minimum, maximum):
    try:
        nodes = json.loads(raw)
    except (TypeError, ValueError):
        raise SharedStateUnavailable('shared_state_configuration_unavailable') from None
    _require(isinstance(nodes, list) and minimum <= len(nodes) <= maximum)
    result = []
    for pair in nodes:
        _require(isinstance(pair, list) and len(pair) == 2)
        host, port = pair
        _require(isinstance(host, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host) is not None)
        _require(type(port) is int and 1024 <= port <= 65535)
        result.append((host.lower(), port))
    _require(len(set(result)) == len(result))
    return tuple(result)


def _configuration(purpose):
    env = os.environ
    _require(purpose in ('security', 'cache'))
    name = env.get('MAIMI_REDIS_MASTER_NAME', '')
    _require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name) is not None)
    sentinels = _nodes(env.get('MAIMI_REDIS_SENTINELS'), 3, 5)
    allowed = _nodes(env.get('MAIMI_REDIS_ALLOWED_DATA_NODES'), 2, 5)
    username = env.get('MAIMI_REDIS_USERNAME', '')
    sentinel_username = env.get('MAIMI_REDIS_SENTINEL_USERNAME', '')
    _require(all(re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', u) for u in (username, sentinel_username)))
    _require(username != 'default' and sentinel_username != 'default')
    db = env.get('MAIMI_REDIS_DB', '0')
    _require(re.fullmatch(r'[0-9]{1,2}', db) is not None and 0 <= int(db) <= 15)
    expected_run_id = env.get('MAIMI_REDIS_ACCEPTED_PRIMARY_RUN_ID', '')
    if purpose == 'security':
        _require(re.fullmatch(r'[0-9a-f]{40}', expected_run_id) is not None)
    expected_epoch = env.get('MAIMI_REDIS_ACCEPTED_CONFIG_EPOCH', '')
    if purpose == 'security':
        _require(re.fullmatch(r'[0-9]{1,16}', expected_epoch) is not None)
    tls = {'ssl': True, 'ssl_ca_certs': _file(env, 'MAIMI_REDIS_CA_FILE'),
           'ssl_cert_reqs': 'required', 'ssl_check_hostname': True,
           'ssl_min_version': ssl.TLSVersion.TLSv1_2}
    cert, key = env.get('MAIMI_REDIS_CLIENT_CERT_FILE'), env.get('MAIMI_REDIS_CLIENT_KEY_FILE')
    _require(bool(cert) == bool(key))
    if cert:
        tls.update(ssl_certfile=_file(env, 'MAIMI_REDIS_CLIENT_CERT_FILE'),
                   ssl_keyfile=_file(env, 'MAIMI_REDIS_CLIENT_KEY_FILE'))
    return {'name': name, 'sentinels': sentinels, 'allowed': allowed,
            'username': username, 'password': _file(env, 'MAIMI_REDIS_PASSWORD_FILE', True),
            'sentinel_username': sentinel_username,
            'sentinel_password': _file(env, 'MAIMI_REDIS_SENTINEL_PASSWORD_FILE', True),
            'db': int(db), 'tls': tls, 'run_id': expected_run_id, 'purpose': purpose,
            'epoch': int(expected_epoch) if expected_epoch else None}


def _options(config, asynchronous, kwargs):
    from redis.backoff import NoBackoff
    if asynchronous:
        from redis.asyncio.retry import Retry
    else:
        from redis.retry import Retry
    _require(not set(kwargs) - {'decode_responses', 'socket_connect_timeout', 'socket_timeout'})
    timeout = kwargs.get('socket_timeout', 3)
    connect = kwargs.get('socket_connect_timeout', 3)
    _require(type(timeout) in (int, float) and 0 < timeout <= 5)
    _require(type(connect) in (int, float) and 0 < connect <= 5)
    common = dict(config['tls'], decode_responses=True, socket_timeout=timeout,
                  socket_connect_timeout=connect, socket_keepalive=True,
                  retry=Retry(NoBackoff(), 0), retry_on_error=[], retry_on_timeout=False,
                  max_connections=32)
    data = dict(common, username=config['username'], password=config['password'], db=config['db'])
    sentinel = dict(common, username=config['sentinel_username'], password=config['sentinel_password'],
                    socket_connect_timeout=min(connect, .75), socket_timeout=min(timeout, .75))
    return data, sentinel


def _address(address, allowed):
    if not isinstance(address, (tuple, list)) or len(address) != 2 or (str(address[0]).lower(), address[1]) not in allowed:
        raise SharedStateUnavailable('shared_state_discovery_refused')
    return tuple(address)


def _identity(info, role, accepted):
    if not isinstance(info, dict) or info.get('run_id') != accepted or not isinstance(role, (list, tuple)) or not role or role[0] not in ('master', b'master'):
        raise SharedStateUnavailable('shared_state_primary_changed_recovery_required')


def _consensus(states, config):
    """Quorum observations are an additional guard, not a linearizable lock.

    A fixed config epoch also refuses an original process promoted back after
    resynchronization. Never automatically learn a new epoch from live Redis.
    """
    votes = {}
    for state in states:
        if not isinstance(state, dict):
            continue
        if state.get('config-epoch') != config['epoch']:
            raise SharedStateUnavailable('shared_state_primary_changed_recovery_required')
        if state.get('is_sdown') or state.get('is_odown') or not state.get('is_master'):
            continue
        address = _address((state.get('ip'), state.get('port')), config['allowed'])
        votes[address] = votes.get(address, 0) + 1
    winners = [a for a, n in votes.items() if n >= len(config['sentinels']) // 2 + 1]
    if len(winners) != 1:
        raise SharedStateUnavailable('shared_state_discovery_quorum_unavailable')
    return winners[0]


class _AsyncClient:
    def __init__(self, raw, sentinel, config):
        self._raw, self._sentinel, self._config = raw, sentinel, config

    async def _invoke(self, name, *args, **kwargs):
        from redis.exceptions import RedisError
        try:
            if self._config['purpose'] == 'cache':
                return await getattr(self._raw, name)(*args, **kwargs)
            # Refresh master discovery before borrowing a single connection.
            # Identity checks and the business command use that same socket.
            await self._raw.connection_pool.get_master_address()
            async with self._raw.client() as connected:
                _identity(await connected.info('server'), await connected.role(), self._config['run_id'])
                result = await getattr(connected, name)(*args, **kwargs)
                _identity(await connected.info('server'), await connected.role(), self._config['run_id'])
                await self._sentinel.discover_master(self._config['name'])
                return result
        except SharedStateUnavailable:
            raise
        except (RedisError, OSError):
            raise SharedStateUnavailable() from None

    def __getattr__(self, name):
        if name not in METHODS:
            raise AttributeError(name)
        async def call(*args, **kwargs):
            return await self._invoke(name, *args, **kwargs)
        return call

    async def aclose(self):
        try:
            await self._raw.aclose(close_connection_pool=True)
        finally:
            for client in self._sentinel.sentinels:
                await client.aclose()


class _SyncClient:
    def __init__(self, raw, sentinel, config):
        self._raw, self._sentinel, self._config = raw, sentinel, config

    def _invoke(self, name, *args, **kwargs):
        from redis.exceptions import RedisError
        try:
            if self._config['purpose'] == 'cache':
                return getattr(self._raw, name)(*args, **kwargs)
            self._raw.connection_pool.get_master_address()
            with self._raw.client() as connected:
                _identity(connected.info('server'), connected.role(), self._config['run_id'])
                result = getattr(connected, name)(*args, **kwargs)
                _identity(connected.info('server'), connected.role(), self._config['run_id'])
                self._sentinel.discover_master(self._config['name'])
                return result
        except SharedStateUnavailable:
            raise
        except (RedisError, OSError):
            raise SharedStateUnavailable() from None

    def __getattr__(self, name):
        if name not in METHODS:
            raise AttributeError(name)
        def call(*args, **kwargs):
            return self._invoke(name, *args, **kwargs)
        return call

    def close(self):
        try:
            self._raw.close()
            self._raw.connection_pool.disconnect()
        finally:
            for client in self._sentinel.sentinels:
                client.close()


def async_client(redis_url='', *, purpose='security', **kwargs):
    from redis.asyncio import Redis
    mode = os.getenv('MAIMI_REDIS_MODE', 'standalone')
    if mode == 'standalone':
        return Redis.from_url(redis_url, **kwargs)
    _require(mode == 'sentinel')
    from redis.asyncio.sentinel import Sentinel
    config = _configuration(purpose)
    class AllowedSentinel(Sentinel):
        async def discover_master(self, service_name):
            if config['purpose'] == 'security':
                # A failed AP also removes its Sentinel. Two healthy observers
                # suffice; do not hold every login behind the third timeout.
                from redis.exceptions import RedisError
                async def observe(client):
                    try:
                        return client, await client.sentinel_master(service_name)
                    except (RedisError, OSError):
                        return client, None
                tasks = [asyncio.create_task(observe(s)) for s in self.sentinels]
                states, good = [], []
                try:
                    for pending in asyncio.as_completed(tasks):
                        client, state = await pending
                        states.append(state)
                        if isinstance(state, dict):
                            good.append(client)
                        try:
                            address = _consensus(states, config)
                        except SharedStateUnavailable as error:
                            if error.detail != 'shared_state_discovery_quorum_unavailable':
                                raise
                        else:
                            self.sentinels = good + [s for s in self.sentinels if s not in good]
                            return address
                    return _consensus(states, config)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            return _address(await super().discover_master(service_name), config['allowed'])
    data, sentinel_options = _options(config, True, kwargs)
    sentinel = AllowedSentinel(config['sentinels'], min_other_sentinels=1,
                               sentinel_kwargs=sentinel_options, **data)
    raw = sentinel.master_for(config['name'])
    return _AsyncClient(raw, sentinel, config)


def sync_client(redis_url='', *, purpose='security', **kwargs):
    import redis
    mode = os.getenv('MAIMI_REDIS_MODE', 'standalone')
    if mode == 'standalone':
        return redis.from_url(redis_url, **kwargs)
    _require(mode == 'sentinel')
    from redis.sentinel import Sentinel
    config = _configuration(purpose)
    class AllowedSentinel(Sentinel):
        def discover_master(self, service_name):
            if config['purpose'] == 'security':
                from redis.exceptions import RedisError
                states, good = [], []
                for sentinel in self.sentinels:
                    try:
                        states.append(sentinel.sentinel_master(service_name)); good.append(sentinel)
                    except (RedisError, OSError):
                        states.append(None)
                    try:
                        address = _consensus(states, config)
                    except SharedStateUnavailable as error:
                        if error.detail != 'shared_state_discovery_quorum_unavailable':
                            raise
                    else:
                        self.sentinels = good + [s for s in self.sentinels if s not in good]
                        return address
                return _consensus(states, config)
            return _address(super().discover_master(service_name), config['allowed'])
    data, sentinel_options = _options(config, False, kwargs)
    sentinel = AllowedSentinel(config['sentinels'], min_other_sentinels=1,
                               sentinel_kwargs=sentinel_options, **data)
    raw = sentinel.master_for(config['name'])
    return _SyncClient(raw, sentinel, config)
