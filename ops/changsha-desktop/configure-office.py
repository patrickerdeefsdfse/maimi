#!/usr/bin/env python3
"""Create/check only the optional office configuration; never rotate IAM secrets.

This is a candidate-release entry point. It does not start containers, build
images, contact providers or change the main .env. Existing office keys survive
repeat runs. Compose is parsed quietly with all selected overlays before commit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile

LIMITS = {
    'OFFICE_CONCURRENCY': (2, 1, 4),
    'OFFICE_QUEUE_LIMIT': (16, 1, 64),
    'OFFICE_OWNER_PENDING': (4, 1, 16),
    'OFFICE_MAX_JOBS': (64, 1, 256),
    'OFFICE_SPOOL_BYTES': (2147483648, 134217728, 8589934592),
    'OFFICE_TTL_SECONDS': (900, 30, 3600),
    'OFFICE_TIMEOUT_SECONDS': (120, 1, 300),
    'OFFICE_CHILD_MEMORY_BYTES': (3221225472, 268435456, 8589934592),
}
SOURCE = 'OFFICE_SERVICE_SECRET_SOURCE'
FIELDS = frozenset((SOURCE, *LIMITS, 'OFFICE_MEMORY_LIMIT', 'OFFICE_CPU_LIMIT'))


def safe(path, *, exists=False):
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('OFFICE_PATH_UNSAFE')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('OFFICE_FILE_NOT_REGULAR')
    elif exists:
        raise ValueError('OFFICE_REQUIRED_FILE_MISSING')


def read_config(path):
    safe(path, exists=True)
    info = path.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384:
        raise ValueError('OFFICE_CONFIG_NOT_PRIVATE')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if sep != '=' or key not in FIELDS or key in values:
            raise ValueError('OFFICE_CONFIG_UNKNOWN_OR_DUPLICATE_FIELD')
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if value != value.strip() or any(c in value for c in "\"'$`\r\n\x00"):
            raise ValueError('OFFICE_CONFIG_VALUE_INVALID')
        values[key] = value
    validate(values)
    return values


def validate(values):
    if set(values) != FIELDS:
        raise ValueError('OFFICE_CONFIG_FIELDS_INCOMPLETE')
    path = Path(values[SOURCE])
    safe(path)
    if path.name != 'office-service-secret' or any(c in values[SOURCE] for c in "\"'$`\r\n\x00"):
        raise ValueError('OFFICE_SECRET_MUST_USE_INDEPENDENT_FILE')
    for name, (_, minimum, maximum) in LIMITS.items():
        value = values[name]
        if not re.fullmatch(r'[0-9]{1,11}', value) or not minimum <= int(value) <= maximum:
            raise ValueError('OFFICE_RESOURCE_VALUE_INVALID')
    memory = re.fullmatch(r'([2-9]|1[0-9]|2[0-4])g', values['OFFICE_MEMORY_LIMIT'])
    if not memory or not re.fullmatch(r'(?:[1-7](?:\.[0-9])?|8(?:\.0)?)', values['OFFICE_CPU_LIMIT']):
        raise ValueError('OFFICE_CONTAINER_LIMIT_INVALID')
    if int(values['OFFICE_SPOOL_BYTES']) >= int(memory[1]) * 1024**3:
        raise ValueError('OFFICE_TMPFS_MUST_FIT_CONTAINER_MEMORY')


def secret_check(path):
    safe(path, exists=True)
    info = path.stat()
    if (info.st_uid != 0 or info.st_gid != 10001 or stat.S_IMODE(info.st_mode) != 0o440
            or not 32 <= info.st_size <= 257):
        raise ValueError('OFFICE_SECRET_METADATA_INVALID')
    value = path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value):
        raise ValueError('OFFICE_SECRET_FORMAT_INVALID')


def quiet_compose(directory, config, *, nginx_network=False, connectors=False, base_directory=None, compose_files=()):
    # Whitelisted process environment prevents shell exports overriding checked files.
    env = {'PATH': os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'),
           'LANG': 'C.UTF-8', 'DOCKER_HOST': 'unix:///var/run/docker.sock'}
    base = base_directory or directory
    safe(base / '.env', exists=True)  # No direct read of IAM or database values.
    args = ['docker', 'compose', '-p', 'megmeet-desktop-server', '--env-file', str(base / '.env')]
    if connectors:
        args += ['--env-file', str(base / '.connectors.env')]
    args += ['--env-file', str(config)]
    names = ['compose.yaml', 'personal-cloud.compose.yaml']
    if nginx_network:
        names.append('compose.nginx-network.yaml')
    if connectors:
        names.append('connectors.compose.yaml')
    for name in names:
        safe(base / name, exists=True)
        args += ['-f', str(base / name)]
    for path in (directory / 'office.compose.yaml', *compose_files):
        safe(path, exists=True)
        args += ['-f', str(path)]
    result = subprocess.run(args + ['config', '--quiet'], env=env, capture_output=True, timeout=45)
    if result.returncode:
        raise ValueError('OFFICE_COMPOSE_CHECK_FAILED_NO_SERVICES_STARTED')


def check(directory, **options):
    if (directory / '.office-configuration-in-progress').exists():
        raise ValueError('OFFICE_CONFIGURATION_INCOMPLETE')
    config = directory / '.office.env'
    values = read_config(config)
    secret_check(Path(values[SOURCE]))
    quiet_compose(directory, config, **options)
    return values


def write_new(path, body, *, mode=0o600, gid=0):
    safe(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        identity = os.fstat(stream.fileno())
        try:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            if path.exists() and not path.is_symlink() and path.stat().st_ino == identity.st_ino:
                path.unlink()
            raise
    return path.stat().st_ino


@contextmanager
def locked(directory):
    path = directory / '.office-configuration.lock'
    safe(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('OFFICE_LOCK_NOT_PRIVATE')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('OFFICE_CONFIGURATION_ALREADY_RUNNING') from None
        yield


def configure(directory, secrets_directory, **options):
    if (directory / '.office.env').exists():
        check(directory, **options)
        return 'EXISTING_KEY_AND_SETTINGS_PRESERVED'
    marker = directory / '.office-configuration-in-progress'
    safe(marker)
    if marker.exists():
        raise ValueError('OFFICE_CONFIGURATION_INCOMPLETE')
    secret = secrets_directory / 'office-service-secret'
    safe(secret)
    if not secrets_directory.exists():
        secrets_directory.mkdir(mode=0o750, parents=True)
        os.chown(secrets_directory, 0, 10001)
        os.chmod(secrets_directory, 0o750)
    values = {name: str(bounds[0]) for name, bounds in LIMITS.items()}
    values.update({SOURCE: str(secret), 'OFFICE_MEMORY_LIMIT': '8g', 'OFFICE_CPU_LIMIT': '2'})
    validate(values)
    content = '# Independent generated Office settings; never contains IAM credentials.\n'
    content += ''.join(f"{key}='{values[key]}'\n" for key in sorted(values))
    created = []
    temporary = None
    marker_id = write_new(marker, b'Office configuration in progress; preserve existing credentials.\n')
    try:
        if not secret.exists():
            created.append((secret, write_new(secret, (secrets.token_urlsafe(48) + '\n').encode(), mode=0o440, gid=10001)))
        secret_check(secret)
        fd, name = tempfile.mkstemp(prefix='.office-check-', dir=directory)
        temporary = Path(name)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        quiet_compose(directory, temporary, **options)
        config = directory / '.office.env'
        created.append((config, write_new(config, content.encode())))
        if read_config(config) != values:
            raise ValueError('OFFICE_CONFIG_COMMIT_MISMATCH')
    except BaseException:
        for path, inode in reversed(created):
            if path.is_symlink() or not path.exists() or path.stat().st_ino != inode:
                raise ValueError('OFFICE_ROLLBACK_STOP_CONCURRENT_CHANGE') from None
            path.unlink()
        if marker.stat().st_ino == marker_id:
            marker.unlink()
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if marker.stat().st_ino != marker_id:
        raise ValueError('OFFICE_MARKER_CHANGED')
    marker.unlink()
    return 'INDEPENDENT_KEY_CREATED_OR_PRESERVED'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--directory', type=Path, default=Path(__file__).absolute().parent)
    parser.add_argument('--secrets-directory', type=Path, default=Path('/data/megmeet-desktop-secrets'))
    parser.add_argument('--nginx-network', action='store_true')
    parser.add_argument('--connectors', action='store_true')
    parser.add_argument('--base-directory', type=Path, help='Use existing release settings and base Compose files without copying credentials')
    parser.add_argument('--compose-file', type=Path, action='append', default=[], help='Append a verified public overlay after office.compose.yaml')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0 or sys.version_info < (3, 10):
        raise ValueError('OFFICE_USE_ROOT_LINUX_PYTHON_3_10')
    os.umask(0o077)
    directory = args.directory.absolute()
    options = {'nginx_network': args.nginx_network, 'connectors': args.connectors}
    if args.base_directory is not None:
        options['base_directory'] = args.base_directory.absolute()
    if args.compose_file:
        options['compose_files'] = tuple(path.absolute() for path in args.compose_file)
    with locked(directory):
        outcome = 'CHECKED' if args.check else configure(directory, args.secrets_directory.absolute(), **options)
        if args.check:
            check(directory, **options)
    print('SHARED_OFFICE_CONFIGURATION: ' + outcome)
    print('IAM_AND_TOKENKEY_CONFIGURATION: UNCHANGED')
    print('SERVICES_STARTED: NO; COMPANY_NETWORK: NOT_ACCESSED')


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'OFFICE_[A-Z_]+', str(exc)) else type(exc).__name__
        print('SHARED_OFFICE_CONFIGURATION: STOP ' + code, file=sys.stderr)
        raise SystemExit(1)
