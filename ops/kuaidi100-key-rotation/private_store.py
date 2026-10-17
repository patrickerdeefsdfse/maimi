"""Fixed-location private rotation store. Never expose secret values or hashes."""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat

ROOT = Path('/data/megmeet-kuaidi100-rotation-r53-20261014')
CREDENTIAL = Path('/etc/megmeet/logistics/kuaidi100-mcp.json')
KEY_RE = re.compile(r'[A-Za-z0-9_-]{1,512}\Z', re.ASCII)
NAMES = {'pending-key.json', 'previous-key.json', 'journal.json', 'receipt.json'}
SHARED_LOCKS = (
    Path('/data/megmeet-shared-office-20260921-r12/.apply.lock'),
    Path('/data/megmeet-logistics-eta-r52-20261014/.apply.lock'),
)

class StoreError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def require(value, code):
    if not value:
        raise StoreError(code)

def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)

def key_of(raw):
    try:
        data = strict_json(raw)
        require(isinstance(data, dict) and set(data) == {'key'} and
                isinstance(data['key'], str) and KEY_RE.fullmatch(data['key']), 'KEY_FORMAT_INVALID')
        return data['key']
    except StoreError:
        raise
    except Exception:
        raise StoreError('KEY_FORMAT_INVALID') from None

def key_bytes(key):
    require(isinstance(key, str) and KEY_RE.fullmatch(key), 'KEY_FORMAT_INVALID')
    return (json.dumps({'key': key}, separators=(',', ':')) + '\n').encode('ascii')

def same_key(a, b):
    return hmac.compare_digest(key_of(a), key_of(b))

def identity(info):
    return {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode), 'nlink': info.st_nlink}

def check_file(info, *, owner=0, maximum=128*1024, allow_empty=False):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_gid == owner
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and (0 if allow_empty else 1) <= info.st_size <= maximum, 'PRIVATE_FILE_UNSAFE')

@contextmanager
def directory(path, *, create=False):
    """Walk all ancestors without symlinks; only final task directory may be made."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'PRIVATE_DIRECTORY_UNSAFE')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = []
    links = []
    try:
        fd = os.open('/', flags)
        fds.append(fd)
        for index, part in enumerate(path.parts[1:]):
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                require(create and index == len(path.parts)-2, 'PRIVATE_DIRECTORY_MISSING')
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            info = os.fstat(child)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_gid == 0
                    and stat.S_IMODE(info.st_mode) in (0o700, 0o755), 'PRIVATE_DIRECTORY_UNSAFE')
            links.append((fd, part, child))
            fds.append(child)
            fd = child
        def stable():
            for parent, part, child in links:
                current = os.stat(part, dir_fd=parent, follow_symlinks=False)
                original = os.fstat(child)
                require(identity(current) == identity(original) and stat.S_ISDIR(current.st_mode),
                        'PRIVATE_DIRECTORY_CHANGED')
        stable()
        yield fd, stable
    except StoreError:
        raise
    except OSError:
        raise StoreError('PRIVATE_FILESYSTEM_ERROR') from None
    finally:
        for fd in reversed(fds):
            os.close(fd)

class PrivateStore:
    """Paths injectable by local tests only. Production CLI never accepts paths."""
    def __init__(self, root=ROOT, credential=CREDENTIAL, shared_locks=SHARED_LOCKS):
        self.root = Path(root)
        self.credential = Path(credential)
        self.shared_locks = tuple(Path(path) for path in shared_locks)

    @contextmanager
    def shared_locked(self):
        import fcntl
        with ExitStack() as stack:
            for path in self.shared_locks:
                parent, stable = stack.enter_context(directory(path.parent))
                fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                        and not stat.S_IMODE(info.st_mode) & 0o022, 'SHARED_LOCK_UNSAFE')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise StoreError('RELEASE_MAINTENANCE_BUSY') from None
                stable()
                require(identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) == identity(info),
                        'SHARED_LOCK_CHANGED')
            yield

    @contextmanager
    def locked(self):
        import fcntl
        with self.shared_locked(), directory(self.root, create=True) as (parent, stable):
            require(stat.S_IMODE(os.fstat(parent).st_mode) == 0o700, 'PRIVATE_DIRECTORY_UNSAFE')
            fd = os.open('operation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            try:
                check_file(os.fstat(fd), allow_empty=True)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise StoreError('ROTATION_BUSY') from None
                stable()
                require(identity(os.stat('operation.lock', dir_fd=parent, follow_symlinks=False)) == identity(os.fstat(fd)),
                        'PRIVATE_FILE_CHANGED')
                yield self
            finally:
                os.close(fd)

    def read(self, name, *, optional=False):
        require(name in NAMES, 'PRIVATE_FILENAME_INVALID')
        with directory(self.root) as (parent, stable):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                if optional:
                    return None
                raise StoreError('PRIVATE_FILE_MISSING') from None
            try:
                before = os.fstat(fd)
                check_file(before)
                raw = os.read(fd, 128*1024+1)
                after = os.fstat(fd)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                require(identity(before) == identity(after) == identity(current) and
                        (before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                        (after.st_size, after.st_mtime_ns, after.st_ctime_ns) and len(raw) == before.st_size,
                        'PRIVATE_FILE_CHANGED')
                stable()
                return raw
            finally:
                os.close(fd)

    def write(self, name, raw):
        require(name in NAMES and isinstance(raw, bytes) and 1 <= len(raw) <= 128*1024, 'PRIVATE_WRITE_INVALID')
        with directory(self.root) as (parent, stable):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                check_file(info)
            except FileNotFoundError:
                pass
            temp = '.write-' + secrets.token_hex(16)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(fd)
                stable()
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def read_json(self, name, optional=False):
        raw = self.read(name, optional=optional)
        if raw is None:
            return None
        try:
            return strict_json(raw)
        except Exception:
            raise StoreError('JOURNAL_INVALID') from None

    def write_json(self, name, value):
        self.write(name, (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))+'\n').encode())

    @contextmanager
    def credential_fd(self, *, writable=False, expected=None, allow_incomplete=False):
        with directory(self.credential.parent) as (parent, stable):
            fd = os.open(self.credential.name,
                         (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=parent)
            try:
                info = os.fstat(fd)
                check_file(info, owner=10001, maximum=2048, allow_empty=allow_incomplete)
                current = os.stat(self.credential.name, dir_fd=parent, follow_symlinks=False)
                require(identity(info) == identity(current), 'CREDENTIAL_IDENTITY_CHANGED')
                if expected is not None:
                    require(identity(info) == expected, 'CREDENTIAL_IDENTITY_CHANGED')
                def stable_file():
                    stable()
                    require(identity(os.stat(self.credential.name, dir_fd=parent, follow_symlinks=False)) == identity(info)
                            == identity(os.fstat(fd)), 'CREDENTIAL_IDENTITY_CHANGED')
                yield fd, info, stable_file
            finally:
                os.close(fd)

    def read_credential(self, expected=None):
        with self.credential_fd(expected=expected) as (fd, before, stable):
            raw = os.read(fd, 2049)
            after = os.fstat(fd)
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns) and len(raw) == before.st_size,
                    'CREDENTIAL_CHANGED_DURING_READ')
            key_of(raw)
            stable()
            return raw, identity(before)

    def write_credential_in_place(self, raw, expected):
        """Caller MUST have journal+backup and verified stopped gateway first."""
        key_of(raw)
        with self.credential_fd(writable=True, expected=expected, allow_incomplete=True) as (fd, _, stable):
            stable()
            os.ftruncate(fd, 0)
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                require(written > 0, 'CREDENTIAL_WRITE_FAILED')
                remaining = remaining[written:]
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            require(hmac.compare_digest(os.read(fd, 2049), raw), 'CREDENTIAL_WRITE_VERIFY_FAILED')
            stable()
