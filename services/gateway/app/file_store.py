"""Private Linux object store. Database authorisation precedes every read.

Only opaque namespace/digest keys are accepted. No public URLs or user paths.
Atomic, fsynced writes precede metadata commit; an interrupted transaction can
leave an unreferenced object, never a row pointing to a partially-written file.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

KEY = re.compile(r'^[a-f0-9]{64}/[a-f0-9]{64}$')


def configured() -> bool:
    return bool(os.getenv('PLATFORM_FILE_ROOT','').strip())


class FileStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ['PLATFORM_FILE_ROOT'])
        if not self.root.is_absolute(): raise ValueError('file_store_absolute_root_required')
        # Root is a dedicated mounted volume, never a workspace accessible to DSH.
        for parent in (*reversed(self.root.parents),self.root):
            if parent.is_symlink(): raise ValueError('file_store_symlink_denied')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)

    def directory(self, namespace):
        if not re.fullmatch(r'[a-f0-9]{64}',namespace):raise ValueError('file_store_key_invalid')
        flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd=os.open(self.root,flags)
        try:
            try:os.mkdir(namespace,mode=0o700,dir_fd=root_fd)
            except FileExistsError:pass
            # Persist the namespace entry before an object can be committed to
            # PostgreSQL. Also covers a concurrent creator of this directory.
            os.fsync(root_fd)
            return os.open(namespace,flags,dir_fd=root_fd)
        finally:os.close(root_fd)

    def put(self,owner: str,scope: str,data: bytes) -> str:
        if not owner or not scope:raise ValueError('file_store_scope_required')
        namespace=hashlib.sha256((owner+'\0'+scope).encode()).hexdigest()
        digest=hashlib.sha256(data).hexdigest()
        directory=self.directory(namespace)
        tmp='.pending-'+secrets.token_hex(16)
        try:
            fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(fd,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,digest,src_dir_fd=directory,dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:os.unlink(tmp,dir_fd=directory)
            except FileNotFoundError:pass
            os.close(directory)
        return namespace+'/'+digest

    def read(self,key: str,*,expected_size: int,expected_sha256: str) -> bytes:
        if not isinstance(key,str) or not KEY.fullmatch(key):raise ValueError('file_store_key_invalid')
        namespace,digest=key.split('/')
        if digest!=expected_sha256 or not 0<=expected_size<=64*1024*1024:
            raise ValueError('file_store_metadata_invalid')
        directory=self.directory(namespace)
        try:fd=os.open(digest,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        finally:os.close(directory)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size!=expected_size:
                raise ValueError('file_store_content_invalid')
            data=stream.read(expected_size+1)
        if len(data)!=expected_size or hashlib.sha256(data).hexdigest()!=digest:
            raise ValueError('file_store_content_invalid')
        return data

    def delete_object(self,owner: str,scope: str,key: str):
        """Caller holds owner row lock and has established zero retained refs."""
        namespace=hashlib.sha256((owner+'\0'+scope).encode()).hexdigest()
        if not KEY.fullmatch(key or '') or key.split('/')[0]!=namespace:raise ValueError('file_store_scope_invalid')
        directory=self.directory(namespace)
        try:
            name=key.split('/')[1]
            try:info=os.stat(name,dir_fd=directory,follow_symlinks=False)
            except FileNotFoundError:return
            if not stat.S_ISREG(info.st_mode):raise ValueError('file_store_unsafe_object')
            os.unlink(name,dir_fd=directory);os.fsync(directory)
        finally:os.close(directory)

    def delete_namespace(self,owner: str,scope: str) -> bool:
        """Only under the durable whole-workspace admission fence.

        Includes interrupted-upload objects. Never follows a link or accepts a
        caller-supplied path. A bounded partial pass is retried under the fence.
        """
        if not owner or not scope:raise ValueError('file_store_scope_required')
        namespace=hashlib.sha256((owner+'\0'+scope).encode()).hexdigest()
        flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd=os.open(self.root,flags)
        directory=None
        try:
            try:directory=os.open(namespace,flags,dir_fd=root_fd)
            except FileNotFoundError:return True
            names=[]
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(names)>=10000:break
                    if not re.fullmatch(r'(?:[a-f0-9]{64}|\.pending-[a-f0-9]{32})',entry.name):
                        raise ValueError('file_store_unknown_object')
                    if not stat.S_ISREG(os.stat(entry.name,dir_fd=directory,follow_symlinks=False).st_mode):
                        raise ValueError('file_store_unsafe_object')
                    names.append(entry.name)
            for name in names:os.unlink(name,dir_fd=directory)
            os.fsync(directory)
            with os.scandir(directory) as entries:
                if next(entries,None) is not None:return False
            os.rmdir(namespace,dir_fd=root_fd);os.fsync(root_fd)
            return True
        finally:
            if directory is not None:os.close(directory)
            os.close(root_fd)


def attachment_bytes(row) -> bytes:
    if row.get('object_key'):
        return FileStore().read(row['object_key'],expected_size=row['size_bytes'],expected_sha256=row['sha256'])
    if row.get('content') is None:raise ValueError('attachment_content_missing')
    return bytes(row['content'])
