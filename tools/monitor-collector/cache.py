"""Atomic monitor metadata cache only; does not write business or service files."""
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from collector import SCHEMA, Unavailable

GROUP = 10001
NAME = 'latest.json'


def directory_fd(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts: raise Unavailable('CACHE_DIRECTORY')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        value = os.fstat(fd)
        if value.st_uid != 0 or value.st_gid != GROUP or stat.S_IMODE(value.st_mode) != 0o750:
            raise Unavailable('CACHE_DIRECTORY_MODE')
        return fd
    except BaseException:
        os.close(fd); raise


def cache_file_shape(value):
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != 0 or value.st_gid != GROUP
            or stat.S_IMODE(value.st_mode) != 0o640 or value.st_nlink != 1):
        raise Unavailable('CACHE_FILE_MODE')


def publish_cache(snapshot, directory):
    """Operator-provisioned root:10001/0750 dir, atomic root:10001/0640 JSON."""
    if os.geteuid() != 0 or snapshot.get('schema') != SCHEMA: raise Unavailable('CACHE_PRODUCER')
    raw = json.dumps(snapshot, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > 2 * 1024**2: raise Unavailable('CACHE_SIZE')
    parent = directory_fd(directory); pending = '.pending-' + uuid.uuid4().hex; fd = None
    try:
        try: cache_file_shape(os.stat(NAME, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError: pass
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        os.fchown(fd, 0, GROUP); os.fchmod(fd, 0o640)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0: raise Unavailable('CACHE_WRITE')
            view = view[written:]
        os.fsync(fd); os.close(fd); fd = None
        os.replace(pending, NAME, src_dir_fd=parent, dst_dir_fd=parent); os.fsync(parent)
    finally:
        if fd is not None: os.close(fd)
        try: os.unlink(pending, dir_fd=parent)
        except FileNotFoundError: pass
        os.close(parent)


def collector_lock(directory):
    """Caller retains the returned descriptor for the lifetime of the collector."""
    parent = directory_fd(directory)
    try:
        fd = os.open('.collector.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise Unavailable('CACHE_LOCK_MODE')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        if 'fd' in locals(): os.close(fd)
        raise
    finally: os.close(parent)
