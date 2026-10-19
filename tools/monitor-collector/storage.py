"""Low-frequency directory metadata estimates; never opens regular files."""
import os
from pathlib import Path
import re
import stat
import time

from collector import MANAGED, NAMESPACE, OWNER_ID, VOLUME_FORMAT, Unavailable


def metadata_size(path, deadline, maximum_entries=200000):
    if time.monotonic() >= deadline: raise Unavailable('STORAGE_BUDGET')
    if any(p.is_symlink() for p in (path, *path.parents)): raise Unavailable('STORAGE_SYMLINK')
    first = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    device = os.fstat(first).st_dev; stack = [first]; total = entries = 0
    try:
        while stack:
            if time.monotonic() >= deadline: raise Unavailable('STORAGE_BUDGET')
            fd = stack.pop()
            try:
                with os.scandir(fd) as children:
                    for entry in children:
                        if time.monotonic() >= deadline: raise Unavailable('STORAGE_BUDGET')
                        info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False); entries += 1
                        if entries > maximum_entries: raise Unavailable('STORAGE_ENTRY_LIMIT')
                        if info.st_dev != device: raise Unavailable('STORAGE_CROSS_DEVICE')
                        if stat.S_ISREG(info.st_mode): total += info.st_size
                        elif stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            opened = os.fstat(child)
                            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                os.close(child); raise Unavailable('STORAGE_CHANGED')
                            stack.append(child)
                            if len(stack) > 512: raise Unavailable('STORAGE_FD_LIMIT')
                        # Symlinks and special files are not followed or counted as data.
            finally: os.close(fd)
        return total
    finally:
        for fd in stack: os.close(fd)


class StorageCache:
    def __init__(self, refresh_seconds=1800, stale_after_seconds=3600, catalog_seconds=60, scan_seconds=.5):
        if not (60 <= refresh_seconds <= 86400 and refresh_seconds <= stale_after_seconds <= 172800
                and 30 <= catalog_seconds <= 3600 and .05 <= scan_seconds <= 2):
            raise ValueError('STORAGE_POLICY')
        self.refresh = refresh_seconds; self.stale = stale_after_seconds; self.catalog_interval = catalog_seconds
        self.scan_seconds = scan_seconds; self.catalog_at = float('-inf'); self.pairs = {}; self.cache = {}; self.last_attempt = {}

    def catalog(self, docker, deadline):
        names = docker.rows(['volume', 'ls', '--filter', 'label=' + MANAGED + '=' + NAMESPACE,
                             '--format', '{{json .Name}}'], deadline)
        rows = docker.rows(['volume', 'inspect', '--format', VOLUME_FORMAT, *names], deadline, len(names)) if names else []
        docker_root = Path(docker.rows(['info', '--format', '{{json .DockerRootDir}}'], deadline, 1)[0])
        pairs = {}
        for row in rows:
            owner = row.get('owner'); name = row.get('name', '')
            match = re.fullmatch(re.escape(NAMESPACE) + '-([a-f0-9]{32})-(state|files)', name)
            if (not match or not isinstance(owner, str) or not OWNER_ID.fullmatch(owner) or owner != match[1]
                    or row.get('manager') != NAMESPACE or row.get('driver') != 'local' or row.get('has_options') is not False):
                raise Unavailable('STORAGE_OWNER_SHAPE')
            path = docker_root / 'volumes' / name / '_data'
            if not path.is_absolute() or str(path) != row.get('mountpoint') or not isinstance(row.get('created'), str):
                raise Unavailable('STORAGE_PATH')
            pair = pairs.setdefault(owner, {})
            if match[2] in pair: raise Unavailable('STORAGE_DUPLICATE')
            pair[match[2]] = (path, row['created'])
        if any(set(pair) != {'state', 'files'} for pair in pairs.values()): raise Unavailable('STORAGE_PAIR')
        return pairs

    def sample(self, docker, deadline):
        tick = time.monotonic(); now = time.time()
        if tick - self.catalog_at >= self.catalog_interval:
            latest = self.catalog(docker, deadline)
            for owner in list(self.cache):
                if self.pairs.get(owner) != latest.get(owner): self.cache.pop(owner, None); self.last_attempt.pop(owner, None)
            self.pairs = latest; self.catalog_at = tick
        due = sorted((self.last_attempt.get(owner, float('-inf')), owner) for owner in self.pairs
                     if tick - self.last_attempt.get(owner, float('-inf')) >= self.refresh)
        if due and time.monotonic() < deadline:
            _, owner = due[0]; self.last_attempt[owner] = tick
            scan_deadline = min(deadline, time.monotonic() + self.scan_seconds)
            try:
                size = sum(metadata_size(self.pairs[owner][kind][0], scan_deadline) for kind in ('state', 'files'))
                self.cache[owner] = {'logical_bytes': size, 'sampled_at': time.time(), 'monotonic': time.monotonic(), 'complete': True}
            except (OSError, ValueError, Unavailable):
                # Keep a last complete result as stale history, never replace it by 0.
                if owner not in self.cache:
                    self.cache[owner] = {'logical_bytes': None, 'sampled_at': None, 'monotonic': None, 'complete': False}
        output = []
        for owner in sorted(self.pairs):
            cached = self.cache.get(owner, {}); sampled = cached.get('monotonic')
            output.append({'owner_ref': owner, 'managed_namespace': NAMESPACE,
                'logical_bytes': cached.get('logical_bytes'), 'sampled_at': cached.get('sampled_at'),
                'complete': cached.get('complete', False), 'stale': sampled is None or tick - sampled > self.stale,
                'stale_after_seconds': self.stale, 'estimate': True})
        return output
