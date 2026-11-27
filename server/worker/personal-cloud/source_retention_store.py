"""Delete only an unchanged regular source under the fixed offline files mount.

The manager owns exclusion and historical worker-stop proof. This module never
starts/stops a worker and never traverses a skill, template or symlink tree.
"""
import errno
import hashlib
import os
from pathlib import Path
import stat
import time

from source_retention_contract import validate_request,validate_result

PROTECTED=frozenset(('.agents','.dsh','.git','.maimi','skills','templates'))


def native_version(info):
    # Pinned rc3 fs-local/src/fsio.ts: versionOf(BigIntStats).
    return f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'


def version_hash(info):
    return hashlib.sha256(native_version(info).encode()).hexdigest()


def identity(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode,info.st_nlink)


class SourceStore:
    def __init__(self,root='/workspace'):
        self.root=Path(root)
        if not self.root.is_absolute():raise ValueError('SOURCE_ROOT_INVALID')
        for path in (*reversed(self.root.parents),self.root):
            info=path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):raise ValueError('SOURCE_ROOT_UNSAFE')

    def remove(self,row,*,deadline):
        base={'source_id':row['source_id'],'status':'RETAINED','reclaimed_bytes':0}
        def retained(reason):return {**base,'reason':reason}
        parts=row['path'].split('/')[2:]
        if any(part.casefold() in PROTECTED for part in parts):return retained('PROTECTED_PATH')
        descriptors=[];fd=None
        try:
            directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(directory)
            for part in parts[:-1]:
                directory=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory);descriptors.append(directory)
            fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):return retained('NOT_REGULAR')
            if info.st_nlink!=1:return retained('UNSAFE_PATH')
            if info.st_size!=row['bytes']:return retained('CONTENT_CHANGED')
            if version_hash(info)!=row['version_hash']:return retained('VERSION_CHANGED')
            digest=hashlib.sha256();total=0
            while True:
                if time.monotonic()>deadline:return retained('IO_UNCONFIRMED')
                chunk=os.read(fd,1024*1024)
                if not chunk:break
                total+=len(chunk)
                if total>row['bytes']:return retained('CONTENT_CHANGED')
                digest.update(chunk)
            if total!=row['bytes'] or digest.hexdigest()!=row['sha256']:return retained('CONTENT_CHANGED')
            if identity(info)!=identity(os.fstat(fd)):return retained('VERSION_CHANGED')
            current=os.stat(parts[-1],dir_fd=directory,follow_symlinks=False)
            if identity(info)!=identity(current):return retained('VERSION_CHANGED')
            os.unlink(parts[-1],dir_fd=directory);os.fsync(directory)
            return {'source_id':row['source_id'],'status':'DELETED','reclaimed_bytes':row['bytes']}
        except FileNotFoundError:
            return {'source_id':row['source_id'],'status':'ABSENT','reclaimed_bytes':0}
        except OSError as exc:
            return retained('UNSAFE_PATH' if exc.errno in (errno.ELOOP,errno.ENOTDIR) else 'IO_UNCONFIRMED')
        finally:
            if fd is not None:os.close(fd)
            for descriptor in reversed(descriptors):os.close(descriptor)

    def execute(self,body):
        body=validate_request(body);deadline=time.monotonic()+60
        items=[self.remove(row,deadline=deadline) for row in body['refs']]
        return validate_result({'operation_id':body['operation_id'],
            'status':'PARTIAL' if any(row['status']=='RETAINED' for row in items) else 'COMPLETE','items':items},body)
