"""Explicit single-node test objects volume, never a fallback from a lost NAS.

The installer records the new directory's identity before publishing the Pod.
Node UID is installer-verified; the Pod independently checks its downward-API
node name, mount source/root and held directory identity on every access.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

NODE_NAME = 'maimi-r69-test'
NODE_UID = '9e50deaf-c3a6-47ab-99fd-c9f9246f0614'
HOST_ROOT = '/data/maimi-r66-test/application-storage/objects'
CONTAINER_ROOT = '/mnt/maimi-objects'
IDENTITY_FILE = '/etc/maimi/storage-local-identity.json'
DEPLOYMENT_MODE = 'single-node-test-r78'
BACKEND = 'single-node-local-test'


class LocalObjectsError(Exception):
    pass


def need(value, code):
    if not value:
        raise LocalObjectsError(code)


def unescape(value):
    return re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), value)


def file_identity(meta):
    return (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_uid, meta.st_gid,
            meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def read_identity(path):
    """Read a root-controlled regular file, including ConfigMap subPath mounts."""
    path = Path(path)
    need(path.is_absolute(), 'storage_local_identity_path_invalid')
    for part in (*reversed(path.parents), path):
        meta = part.lstat()
        need(not stat.S_ISLNK(meta.st_mode), 'storage_local_identity_unsafe')
        if part != path:
            need(stat.S_ISDIR(meta.st_mode) and meta.st_uid == 0 and not meta.st_mode & 0o022,
                 'storage_local_identity_unsafe')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 0
             and not before.st_mode & 0o022 and 0 < before.st_size <= 8192,
             'storage_local_identity_unsafe')
        data = os.read(fd, 8193)
        need(len(data) == before.st_size and file_identity(before) == file_identity(os.fstat(fd))
             == file_identity(path.lstat()), 'storage_local_identity_changed')
        return json.loads(data)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class LocalObjectsIdentity:
    root: str
    dev: int
    inode: int
    uid: int
    gid: int
    mode: int
    mount_root: str
    fstype: str

    @classmethod
    def load(cls, path, *, root, deployment_mode, node_name):
        need(deployment_mode == DEPLOYMENT_MODE and node_name == NODE_NAME,
             'storage_local_test_environment_required')
        need(root == CONTAINER_ROOT and path == IDENTITY_FILE, 'storage_local_test_path_refused')
        value = read_identity(path)
        expected = {'schema', 'node_name', 'node_uid', 'host_root', 'container_root',
                    'dev', 'inode', 'uid', 'gid', 'mode', 'mount_root', 'fstype'}
        need(type(value) is dict and set(value) == expected, 'storage_local_identity_invalid')
        need(value['schema'] == 'maimi-local-objects-identity/v1' and value['node_name'] == NODE_NAME
             and value['node_uid'] == NODE_UID and value['host_root'] == HOST_ROOT
             and value['container_root'] == CONTAINER_ROOT, 'storage_local_identity_mismatch')
        need(all(type(value[k]) is int and value[k] > 0 for k in ('dev', 'inode'))
             and value['uid'] == value['gid'] == 1000 and type(value['mode']) is int
             and value['mode'] == 0o700, 'storage_local_identity_permissions')
        mount_root = value['mount_root']
        need(isinstance(mount_root, str) and mount_root.startswith('/')
             and all(part not in ('', '.', '..') for part in mount_root[1:].split('/'))
             and not re.search(r'[\\\x00-\x20]', mount_root)
             and value['fstype'] == 'ext4', 'storage_local_mount_identity_invalid')
        return cls(root, value['dev'], value['inode'], 1000, 1000, 0o700, mount_root, 'ext4')

    def mount(self, mountinfo=None):
        if mountinfo is None:
            with open('/proc/self/mountinfo', 'r', encoding='utf-8') as stream:
                mountinfo = stream.read(1024 * 1024 + 1)
        need(len(mountinfo) <= 1024 * 1024, 'storage_local_mount_unverified')
        covering = []
        for line in mountinfo.splitlines():
            split = line.split(' - ', 1)
            if len(split) != 2:
                continue
            left, right = split[0].split(), split[1].split()
            if len(left) < 6 or len(right) < 3:
                continue
            path = unescape(left[4])
            if self.root == path or self.root.startswith(path.rstrip('/') + '/'):
                covering.append((len(path), left, right, path))
        need(bool(covering), 'storage_local_mount_required')
        _, left, right, path = max(covering, key=lambda item: item[0])
        need(path == self.root and left[0].isdigit() and right[0] == self.fstype
             and unescape(left[3]) == self.mount_root and 'rw' in left[5].split(',')
             and 'rw' in right[2].split(',')
             and left[2] == str(os.major(self.dev)) + ':' + str(os.minor(self.dev)),
             'storage_local_mount_mismatch')
        return left[0]

    def directory(self, descriptor, mount_id):
        meta = os.fstat(descriptor)
        need(stat.S_ISDIR(meta.st_mode) and
             (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode))
             == (self.dev, self.inode, self.uid, self.gid, self.mode),
             'storage_local_root_identity_changed')
        info = Path('/proc/self/fdinfo/' + str(descriptor)).read_text()
        observed = re.search(r'^mnt_id:\s*(\d+)\s*$', info, re.MULTILINE)
        need(observed is not None and observed[1] == mount_id, 'storage_local_mount_changed')
        path = Path(self.root).lstat()
        need(stat.S_ISDIR(path.st_mode) and not stat.S_ISLNK(path.st_mode)
             and (path.st_dev, path.st_ino, path.st_uid, path.st_gid, stat.S_IMODE(path.st_mode))
             == (self.dev, self.inode, self.uid, self.gid, self.mode),
             'storage_local_root_identity_changed')
