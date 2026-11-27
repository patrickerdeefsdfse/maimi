"""r72 cloud-file ledger, bounded leases and physically acknowledged reclamation.

This store owns only its dedicated mounted root. Desktop originals, platform
skills/templates and arbitrary native workspace paths are never traversed.
All publication requires a quota reservation before bytes are accepted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from .history_store import _connect

ORDINARY_SECONDS = 86400
ENTERPRISE_SECONDS = 3600
RETRY_DOWNLOAD_SECONDS = 600
DOWNLOAD_LEASE_SECONDS = 300
RESERVATION_SECONDS = 900
MAX_BYTES = 128 * 1024 * 1024
DOWNLOAD_BLOCK_BYTES = 1024 * 1024
KINDS = frozenset(('ordinary', 'enterprise_input', 'enterprise_intermediate', 'enterprise_output'))
OWNER = re.compile(r'^[a-f0-9]{32}$')
SESSION = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
SHA = re.compile(r'^[a-f0-9]{64}$')


class StorageError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def require(value, code, status=409):
    if not value:
        raise StorageError(code, status)


def identity(owner, session):
    require(isinstance(owner, str) and OWNER.fullmatch(owner) and
            isinstance(session, str) and SESSION.fullmatch(session), 'storage_identity_invalid', 400)


def file_uuid(value):
    try:
        require(str(UUID(str(value))) == str(value), 'storage_file_invalid', 400)
        return str(value)
    except (ValueError, TypeError, AttributeError):
        raise StorageError('storage_file_invalid', 400) from None


def workspace_request_id(path,version,fingerprint):
    return 'workspace-delivery:'+hashlib.sha256(json.dumps({'path':path,'version':version,'runtime':fingerprint},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def managed_output_path(session,path):
    session_uuid=session.removeprefix('session-')
    file_uuid(session_uuid)
    prefix='/workspace/maimi-outputs/'+session_uuid+'-'
    require(isinstance(path,str) and path.startswith(prefix) and len(path)<=4096 and
            not re.search(r'[\\\x00-\x1f\x7f]',path),'storage_output_path_invalid',400)
    pieces=path[len(prefix):].split('/')
    require(len(pieces)>=2,'storage_output_path_invalid',400);file_uuid(pieces[0])
    require(all(part and part not in ('.','..') and part.casefold() not in
        ('.agents','.dsh','.git','.maimi','skills','templates') for part in pieces[1:]),'storage_output_path_invalid',400)
    return path


@dataclass(frozen=True)
class Limits:
    owner_bytes: int = 2 * 1024**3
    session_bytes: int = 512 * 1024**2
    owner_files: int = 10000
    session_files: int = 1024
    global_bytes: int = 120 * 1024**3
    # Explicit rollout option; legacy callers keep their strict byte limits.
    shared_burst: bool = False

    def __post_init__(self):
        require(all(type(x) is int and x > 0 for x in
                    (self.owner_bytes, self.session_bytes, self.owner_files, self.session_files, self.global_bytes)), 'storage_limits_invalid')
        require(self.session_bytes <= self.owner_bytes and self.session_files <= self.owner_files, 'storage_limits_invalid')
        require(type(self.shared_burst) is bool, 'storage_limits_invalid')


def alert_transitions(previous, current):
    """Collector-owned previous/current snapshots; no delivery or persisted secrets.

    A collector must retain the previous *available* snapshot on a failed poll.
    It must not treat missing/unavailable telemetry as recovery.
    """
    require(isinstance(previous, list) and isinstance(current, list), 'storage_alert_snapshot_invalid')
    def indexed(items):
        result = {}
        for item in items:
            require(isinstance(item, dict) and all(isinstance(item.get(k), str) for k in
                    ('code', 'scope', 'severity')), 'storage_alert_snapshot_invalid')
            result[(item['code'], item['scope'])] = {k: item[k] for k in ('code', 'scope', 'severity')}
        return result
    before, after = indexed(previous), indexed(current)
    return ([dict(item, state='firing') for key, item in after.items() if before.get(key) != item] +
            [dict(item, state='recovered') for key, item in before.items() if key not in after])


def nfs_mount(root,expected_source,mountinfo=None):
    """Return the covering NFS mount id, refusing a missing/wrong NAS mount."""
    require(isinstance(expected_source,str) and re.fullmatch(r'[A-Za-z0-9.-]+:/[^\\\x00-\x20]+',expected_source) and
            all(part not in ('','.','..') for part in expected_source.split(':/',1)[1].split('/')),
            'storage_nfs_source_invalid',503)
    if mountinfo is None:
        with open('/proc/self/mountinfo','r',encoding='utf-8') as stream:mountinfo=stream.read(1024*1024+1)
    require(len(mountinfo)<=1024*1024,'storage_mount_unverified',503)
    def unescape(value):return re.sub(r'\\(040|011|012|134)',lambda m:chr(int(m[1],8)),value)
    covering=[];root=str(root)
    for line in mountinfo.splitlines():
        fields=line.split(' - ',1)
        if len(fields)!=2:continue
        left,right=fields[0].split(),fields[1].split()
        if len(left)<6 or len(right)<3:continue
        mount=unescape(left[4]);source=unescape(right[1])
        if root==mount or root.startswith(mount.rstrip('/')+'/'):
            covering.append((len(mount),left[0],right[0],source,left[5]))
    require(bool(covering),'storage_mount_unverified',503)
    _,mount_id,fstype,source,options=max(covering)
    require(fstype in ('nfs','nfs4') and (source==expected_source or source.startswith(expected_source+'/')) and
            'rw' in options.split(',') and mount_id.isdigit(),'storage_nfs_mount_required',503)
    return mount_id


class BlobStore:
    """One physical object per reservation; no cross-user deduplication or aliases.

    root must be an administrator-mounted private volume. Linux dir_fd and
    O_NOFOLLOW protect both path traversal and replacement by symbolic links.
    """
    def __init__(self, root, *, expected_nfs_source=None, local_test_identity=None, minimum_free_bytes=0):
        self.root = Path(root)
        self.expected_nfs_source=expected_nfs_source
        self.local_test_identity=local_test_identity
        require(local_test_identity is None or expected_nfs_source is None,'storage_backend_ambiguous',503)
        require(type(minimum_free_bytes) is int and minimum_free_bytes>=0,'storage_limits_invalid',503)
        self.minimum_free_bytes=minimum_free_bytes
        require(self.root.is_absolute(), 'storage_root_invalid')
        for part in (*reversed(self.root.parents), self.root):
            info = part.lstat()
            require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), 'storage_root_unsafe')
        with self.root_directory():pass

    @contextmanager
    def root_directory(self):
        from .storage_local_test import LocalObjectsError
        try:
            mount_id=nfs_mount(self.root,self.expected_nfs_source) if self.expected_nfs_source is not None else None
            if self.local_test_identity is not None:
                mount_id=self.local_test_identity.mount()
            descriptor=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        except OSError:raise StorageError('storage_root_unavailable',503) from None
        except LocalObjectsError as error:raise StorageError(str(error),503) from None
        try:
            if mount_id is not None:
                info=Path('/proc/self/fdinfo/'+str(descriptor)).read_text()
                observed=re.search(r'^mnt_id:\s*(\d+)\s*$',info,re.MULTILINE)
                require(observed is not None and observed[1]==mount_id,'storage_mount_changed',503)
            if self.local_test_identity is not None:
                self.local_test_identity.directory(descriptor,mount_id)
            yield descriptor
            if self.local_test_identity is not None:
                self.local_test_identity.directory(descriptor,mount_id)
                require(self.local_test_identity.mount()==mount_id,'storage_local_mount_changed',503)
        except LocalObjectsError as error:raise StorageError(str(error),503) from None
        finally:os.close(descriptor)

    def capacity(self,unwritten_bytes):
        require(type(unwritten_bytes) is int and unwritten_bytes>=0,'storage_limits_invalid',503)
        with self.root_directory() as descriptor:
            space=os.fstatvfs(descriptor)
            require(space.f_bavail*space.f_frsize>=self.minimum_free_bytes+unwritten_bytes,'storage_backend_capacity_low',507)

    def capacity_snapshot(self):
        with self.root_directory() as descriptor:
            space = os.fstatvfs(descriptor)
            blocks = getattr(space, 'f_blocks', None)
            return {'available_bytes': int(space.f_bavail * space.f_frsize),
                    'total_bytes': int(blocks * space.f_frsize) if blocks is not None else None,
                    'minimum_free_bytes': self.minimum_free_bytes}

    @contextmanager
    def directory(self, owner, create=False):
        identity(owner, 'storage')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = None
        with self.root_directory() as root:
            try:
                if create:
                    try:
                        os.mkdir(owner, 0o700, dir_fd=root)
                        os.fsync(root)
                    except FileExistsError:
                        pass
                directory = os.open(owner, flags, dir_fd=root)
                yield directory
            finally:
                if directory is not None:os.close(directory)

    def stage(self, owner, file_id, data, expected_sha):
        name = file_uuid(file_id) + '.part'
        require(isinstance(data, bytes) and len(data) <= MAX_BYTES and hashlib.sha256(data).hexdigest() == expected_sha,
                'storage_content_mismatch', 400)
        with self.directory(owner, True) as directory:
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            except FileExistsError:
                # A complete staged retry is reusable. Never truncate a writer.
                self._read(directory, name, len(data), expected_sha, verify_only=True)
                return
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)

    def publish(self, owner, file_id, size, sha):
        name = file_uuid(file_id)
        with self.directory(owner) as directory:
            try:
                self._read(directory, name, size, sha, verify_only=True)
                return
            except FileNotFoundError:
                pass
            self._read(directory, name + '.part', size, sha, verify_only=True)
            # Caller holds the same account lock used by cleanup admission.
            os.rename(name + '.part', name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)

    @staticmethod
    def _read(directory, name, size, sha, *, verify_only=False, block_hashes=None):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == size, 'storage_blob_changed')
            if verify_only:
                digest = hashlib.sha256()
                remaining = size
                block_size = DOWNLOAD_BLOCK_BYTES if block_hashes is not None else 256 * 1024
                while remaining:
                    length = min(remaining, block_size)
                    chunk = stream.read(length)
                    require(len(chunk) == length, 'storage_blob_changed')
                    digest.update(chunk)
                    if block_hashes is not None:
                        block_hashes.append(hashlib.sha256(chunk).digest())
                    remaining -= len(chunk)
                require(not stream.read(1) and digest.hexdigest() == sha, 'storage_blob_changed')
                return
            data = stream.read(size + 1)
            require(len(data) == size and hashlib.sha256(data).hexdigest() == sha, 'storage_blob_changed')
            return data

    def read(self, owner, file_id, size, sha):
        with self.directory(owner) as directory:
            return self._read(directory, file_uuid(file_id), size, sha)

    def download_hashes(self, owner, file_id, size, sha):
        hashes = []
        with self.directory(owner) as directory:
            self._read(directory, file_uuid(file_id), size, sha, verify_only=True, block_hashes=hashes)
        return hashes

    def download_block(self, owner, file_id, size, index, expected_hash):
        offset = index * DOWNLOAD_BLOCK_BYTES
        length = min(DOWNLOAD_BLOCK_BYTES, size - offset)
        require(length > 0 and offset >= 0, 'storage_blob_changed')
        with self.directory(owner) as directory:
            fd = os.open(file_uuid(file_id), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == size, 'storage_blob_changed')
                stream.seek(offset)
                data = stream.read(length)
                require(len(data) == length and hashlib.sha256(data).digest() == expected_hash, 'storage_blob_changed')
                return data

    def remove(self, owner, file_id, expected_size, expected_sha):
        """Missing is an exact-object success; unknown files/symlinks are refused."""
        name = file_uuid(file_id)
        removed = 0
        try:
            with self.directory(owner) as directory:
                for candidate in (name, name + '.part'):
                    try:
                        info = os.stat(candidate, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= expected_size,
                            'storage_blob_unsafe')
                    # Published objects must match their immutable descriptor.
                    try:
                        if candidate == name:
                            self._read(directory, candidate, expected_size, expected_sha, verify_only=True)
                        os.unlink(candidate, dir_fd=directory)
                    except FileNotFoundError:
                        # A concurrent disappearance must not skip the sibling
                        # staging file and falsely release its charge.
                        continue
                    removed += info.st_size
                os.fsync(directory)
        except FileNotFoundError:
            pass
        return removed


class Storage:
    def __init__(self, *, connect=_connect, blobs=None, limits=None):
        self.connect = connect
        self.blobs = blobs
        self.limits = limits or Limits()

    @contextmanager
    def transaction(self, owner):
        identity(owner, 'storage')
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO platform.storage_accounts(owner_key) VALUES(%s) ON CONFLICT DO NOTHING', (owner,))
            cur.execute('SELECT owner_key FROM platform.storage_accounts WHERE owner_key=%s FOR UPDATE', (owner,))
            require(cur.fetchone() is not None, 'storage_account_missing')
            yield cur

    @staticmethod
    def now(cur):
        cur.execute('SELECT clock_timestamp() AS now')
        return cur.fetchone()['now']

    @staticmethod
    def public(row):
        return {key: str(row[key]) if key == 'file_id' else row[key]
                for key in ('file_id', 'name', 'media_type', 'sha256', 'expected_bytes', 'kind', 'status', 'expires_at')}

    @staticmethod
    def output_guard(cur,owner,session,guard):
        if guard is None:return
        generation,fingerprint=guard
        require(type(generation) is int and generation>0 and SHA.fullmatch(fingerprint or ''),'storage_generation_invalid',400)
        # The database fence remains held through reserve/commit, so a stale
        # manager authorization cannot write after runtime release or purge.
        cur.execute("SELECT generation FROM platform.worker_runtime_reservations WHERE owner_key=%s AND generation=%s AND state<>'RELEASED' AND purpose='worker' AND handle IS NOT NULL AND runtime_fingerprint=%s FOR SHARE",(owner,generation,fingerprint))
        require(cur.fetchone() is not None,'storage_generation_lost',403)
        cur.execute('SELECT purged_at FROM platform.native_sessions WHERE owner_key=%s AND session_id=%s FOR SHARE',(owner,session))
        row=cur.fetchone();require(row is not None and row['purged_at'] is None,'storage_output_session_unavailable',410)

    def reserve(self, owner, session, *, request_id, kind, name, media_type, size, sha256, job_id=None,native_guard=None):
        identity(owner, session)
        require(kind in KINDS and type(size) is int and 0 <= size <= MAX_BYTES and SHA.fullmatch(sha256 or ''), 'storage_descriptor_invalid', 400)
        require(isinstance(name, str) and 1 <= len(name) <= 255 and name not in ('.', '..') and
                not re.search(r'[/\\\x00-\x1f\x7f]', name), 'storage_filename_invalid', 400)
        require(isinstance(media_type, str) and 1 <= len(media_type) <= 160 and '\r' not in media_type and '\n' not in media_type,
                'storage_media_invalid', 400)
        require(isinstance(request_id, str) and 1 <= len(request_id) <= 256, 'storage_request_invalid', 400)
        require((kind == 'ordinary' and job_id is None) or
                (kind != 'ordinary' and isinstance(job_id, str) and SESSION.fullmatch(job_id)), 'storage_job_invalid', 400)
        values = dict(session=session, kind=kind, name=name, media_type=media_type, size=size, sha256=sha256, job_id=job_id)
        request_key = hashlib.sha256((session + '\0' + request_id).encode()).hexdigest()
        request_digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.transaction(owner) as cur:
            self.output_guard(cur,owner,session,native_guard)
            cur.execute('SELECT * FROM platform.storage_files WHERE owner_key=%s AND request_key=%s', (owner, request_key))
            old = cur.fetchone()
            if old:
                require(old['request_digest'] == request_digest, 'storage_idempotency_conflict')
                require(old['status'] in ('reserved', 'active') and old['expires_at'] > self.now(cur), 'storage_file_expired', 410)
                return self.public(old)
            cur.execute('''SELECT COALESCE(sum(expected_bytes),0) AS total,
              COALESCE(sum(expected_bytes) FILTER(WHERE session_id=%s),0) AS session,
              count(*) AS total_count,count(*) FILTER(WHERE session_id=%s) AS session_count
              FROM platform.storage_files WHERE owner_key=%s AND status<>'deleted' ''', (session, session, owner))
            usage = cur.fetchone()
            require((self.limits.shared_burst or
                     (usage['total'] + size <= self.limits.owner_bytes and usage['session'] + size <= self.limits.session_bytes)) and
                    usage['total_count'] < self.limits.owner_files and usage['session_count'] < self.limits.session_files,
                    'storage_quota_exceeded', 413)
            # The same fixed lock serializes all owners' global reservations.
            # Deleted rows leave the charge only after physical cleanup ACK.
            cur.execute('SELECT pg_advisory_xact_lock(728125,1)')
            cur.execute("SELECT COALESCE(sum(expected_bytes),0) AS total,COALESCE(sum(expected_bytes) FILTER(WHERE status='reserved'),0) AS unwritten FROM platform.storage_files WHERE status<>'deleted'")
            global_usage=cur.fetchone()
            require(global_usage['total']+size<=self.limits.global_bytes,'storage_global_quota_exceeded',413)
            if self.blobs is not None:self.blobs.capacity(int(global_usage['unwritten'])+size)
            cur.execute('''INSERT INTO platform.storage_files(file_id,owner_key,session_id,request_key,request_digest,
              kind,job_id,name,media_type,sha256,expected_bytes,status,expires_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved',clock_timestamp()+interval '15 minutes') RETURNING *''',
                (str(uuid4()), owner, session, request_key, request_digest, kind, job_id, name, media_type, sha256, size))
            return self.public(cur.fetchone())

    def commit(self, owner, session, file_id, data, *, native_guard=None):
        identity(owner, session)
        file_id = file_uuid(file_id)
        require(self.blobs is not None, 'storage_backend_unavailable', 503)
        with self.transaction(owner) as cur:
            self.output_guard(cur,owner,session,native_guard)
            row = self._row(cur, owner, session, file_id)
            require(row['status'] in ('reserved', 'active') and row['expires_at'] > self.now(cur), 'storage_file_expired', 410)
            require(isinstance(data, bytes) and len(data) == row['expected_bytes'] and hashlib.sha256(data).hexdigest() == row['sha256'],
                    'storage_content_mismatch', 400)
            if row['status'] == 'active':
                self.blobs.read(owner, file_id, len(data), row['sha256'])
                return self.public(row)
            # Holding admission through disk commit makes reserve/commit/cleanup
            # serializable per owner. A failed transaction leaves charged intent.
            cur.execute("SELECT COALESCE(sum(expected_bytes),0) AS unwritten FROM platform.storage_files WHERE status='reserved'")
            self.blobs.capacity(int(cur.fetchone()['unwritten']))
            self.blobs.stage(owner, file_id, data, row['sha256'])
            self.blobs.publish(owner, file_id, len(data), row['sha256'])
            ttl = ORDINARY_SECONDS if row['kind'] == 'ordinary' else ENTERPRISE_SECONDS
            cur.execute('''UPDATE platform.storage_files SET status='active',physical_bytes=expected_bytes,
              expires_at=created_at+(%s*interval '1 second') WHERE file_id=%s RETURNING *''', (ttl, file_id))
            return self.public(cur.fetchone())

    def put(self, owner, session, *, request_id, kind, name, media_type, data, job_id=None):
        require(isinstance(data, bytes), 'storage_content_invalid', 400)
        reserved = self.reserve(owner, session, request_id=request_id, kind=kind, name=name,
            media_type=media_type, size=len(data), sha256=hashlib.sha256(data).hexdigest(), job_id=job_id)
        return self.commit(owner, session, reserved['file_id'], data)

    @staticmethod
    def _row(cur, owner, session, file_id):
        cur.execute('SELECT * FROM platform.storage_files WHERE owner_key=%s AND session_id=%s AND file_id=%s', (owner, session, file_id))
        row = cur.fetchone()
        require(row is not None, 'storage_file_not_found', 404)
        return row

    def inspect(self, owner, session, file_id):
        identity(owner, session)
        with self.transaction(owner) as cur:
            row = self._row(cur, owner, session, file_uuid(file_id))
            require(row['status'] == 'active' and row['expires_at'] > self.now(cur), 'storage_file_expired', 410)
            return self.public(row)

    def resolve_native(self, owner, session, *, sha256, name, size, image_media_type=None):
        identity(owner, session)
        require(isinstance(sha256, str) and SHA.fullmatch(sha256) and type(size) is int and
                0 <= size <= MAX_BYTES, 'storage_reference_invalid', 400)
        image_types = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')
        if image_media_type is None:
            require(isinstance(name, str) and 1 <= len(name) <= 255, 'storage_reference_invalid', 400)
        else:
            require(name is None and image_media_type in image_types, 'storage_reference_invalid', 400)
        with self.transaction(owner) as cur:
            if image_media_type is not None:
                # Native image references can omit filenames. Their bytes, media type,
                # owner and session still identify the exact content.
                cur.execute('''SELECT * FROM platform.storage_files WHERE owner_key=%s AND session_id=%s
                  AND sha256=%s AND expected_bytes=%s AND media_type=%s AND status='active'
                  AND expires_at>clock_timestamp() ORDER BY created_at,file_id LIMIT 1''',
                  (owner, session, sha256, size, image_media_type))
            else:
                cur.execute('''SELECT * FROM platform.storage_files WHERE owner_key=%s AND session_id=%s AND sha256=%s
                  AND name=%s AND expected_bytes=%s AND status='active' AND expires_at>clock_timestamp()
                  ORDER BY created_at,file_id LIMIT 1''', (owner, session, sha256, name, size))
            row = cur.fetchone()
            if row is None and name == 'image' and image_media_type is None:
                # Older workers use 'image' for an omitted name. Require a matching
                # nameless image reference in this session's persisted tool result.
                cur.execute('''SELECT f.* FROM platform.storage_files f
                    WHERE f.owner_key=%s AND f.session_id=%s AND f.sha256=%s AND f.expected_bytes=%s
                      AND f.media_type=ANY(%s) AND f.status='active' AND f.expires_at>clock_timestamp()
                      AND EXISTS (
                        SELECT 1 FROM platform.native_session_events e
                        JOIN platform.native_sessions s USING (owner_key,session_id)
                        CROSS JOIN LATERAL jsonb_path_query(e.event->'data',
                            '$.** ? (@.attachmentId.type() == "string")') ref
                        WHERE e.owner_key=f.owner_key AND e.session_id=f.session_id AND s.purged_at IS NULL
                          AND e.event->>'type'='tool/result' AND NOT (ref ? 'name')
                          AND ref->>'attachmentId'=('sha256:' || f.sha256)
                          AND ref->'bytes'=to_jsonb(f.expected_bytes) AND ref->>'mediaType'=f.media_type)
                    ORDER BY f.created_at,f.file_id LIMIT 1''', (owner, session, sha256, size, list(image_types)))
                row = cur.fetchone()
            require(row is not None, 'storage_reference_unmanaged_or_expired', 410)
            return self.public(row)

    def published_output(self, owner, session, path):
        identity(owner, session)
        managed_output_path(session, path)
        with self.transaction(owner) as cur:
            cur.execute('''SELECT f.file_id, f.status, f.expires_at > clock_timestamp() AS unexpired
                FROM platform.storage_source_copies s JOIN platform.storage_files f
                  ON (f.file_id, f.owner_key, f.session_id) = (s.file_id, s.owner_key, s.session_id)
                WHERE s.owner_key=%s AND s.session_id=%s AND s.source_path=%s AND f.kind='ordinary'
                ORDER BY s.recorded_at DESC, f.created_at DESC, f.file_id DESC LIMIT 1''',
                (owner, session, path))
            row = cur.fetchone()
            if row is None:
                return None
            require(row['status'] == 'active' and row['unexpired'], 'storage_file_expired', 410)
            return str(row['file_id'])

    def record_source_copy(self, owner, session, file_id, *, path, version, runtime_fingerprint,native_guard=None):
        identity(owner, session)
        require(isinstance(path, str) and path.startswith('/workspace/') and len(path) <= 4096 and
                not any(part in ('', '.', '..') for part in path.split('/')[1:]) and
                not re.search(r'[\\\x00-\x1f\x7f]', path), 'storage_source_invalid', 400)
        require(isinstance(version, str) and 1 <= len(version) <= 4096 and isinstance(runtime_fingerprint, str)
                and SHA.fullmatch(runtime_fingerprint), 'storage_source_invalid', 400)
        with self.transaction(owner) as cur:
            self.output_guard(cur,owner,session,native_guard)
            row = self._row(cur, owner, session, file_uuid(file_id))
            require(row['status'] in ('reserved', 'active'), 'storage_file_expired', 410)
            cur.execute('''INSERT INTO platform.storage_source_copies(file_id,owner_key,session_id,runtime_fingerprint,
              source_path,source_version_hash,expected_sha256,expected_bytes)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
              (file_id, owner, session, runtime_fingerprint, path, hashlib.sha256(version.encode()).hexdigest(),
               row['sha256'], row['expected_bytes']))

    def bind_task_file(self, owner, session, file_id, *, task_id, role):
        identity(owner, session)
        require(isinstance(task_id, str) and SESSION.fullmatch(task_id) and role in ('input', 'intermediate', 'output'),
                'storage_task_invalid', 400)
        with self.transaction(owner) as cur:
            row = self._row(cur, owner, session, file_uuid(file_id))
            require(row['status'] == 'active' and row['expires_at'] > self.now(cur), 'storage_file_expired', 410)
            cur.execute('''INSERT INTO platform.storage_task_files(owner_key,session_id,task_id,file_id,role)
              VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (owner, session, task_id, file_id, role))
            # Explicitly accepted enterprise inputs get the enterprise lifetime;
            # this never touches a desktop source or another session's object.
            if role == 'input' and row['kind'] == 'ordinary':
                cur.execute('''UPDATE platform.storage_files SET kind='enterprise_input',job_id=%s,
                  expires_at=LEAST(expires_at,clock_timestamp()+interval '1 hour') WHERE file_id=%s''', (task_id, file_id))

    def begin_download(self, owner, session, file_id):
        identity(owner, session)
        with self.transaction(owner) as cur:
            row = self._row(cur, owner, session, file_uuid(file_id))
            now = self.now(cur)
            require(row['status'] == 'active' and row['expires_at'] > now, 'storage_file_expired', 410)
            lease_id = str(uuid4())
            cur.execute('''INSERT INTO platform.storage_downloads(lease_id,file_id,owner_key,session_id,expires_at,expected_bytes,expected_sha256)
              VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                (lease_id, str(row['file_id']), owner, session, now + timedelta(seconds=DOWNLOAD_LEASE_SECONDS), row['expected_bytes'], row['sha256']))
            return {**self.public(row), 'lease_id': lease_id, 'lease_seconds': DOWNLOAD_LEASE_SECONDS}

    def read_download(self, owner, session, lease_id, *, block_hashes=False):
        identity(owner, session)
        require(self.blobs is not None, 'storage_backend_unavailable', 503)
        with self.transaction(owner) as cur:
            cur.execute('''SELECT f.* FROM platform.storage_files f JOIN platform.storage_downloads d USING(file_id)
              WHERE d.lease_id=%s AND d.owner_key=%s AND d.session_id=%s AND d.completed_at IS NULL
              AND d.expires_at>clock_timestamp() AND f.status='active' ''', (file_uuid(lease_id), owner, session))
            row = cur.fetchone()
            require(row is not None, 'storage_download_expired', 410)
            read = self.blobs.download_hashes if block_hashes else self.blobs.read
            return read(owner, str(row['file_id']), row['expected_bytes'], row['sha256'])

    def finish_download(self, owner, session, lease_id, *, bytes_received, sha256, desktop_saved_verified=False):
        """Only trusted desktop parent may assert saved_verified; no public route does.

        Web transfer completion means bytes transferred, not durable browser save.
        Repeated completion never renews an expiry or triggers a business rerun.
        """
        identity(owner, session)
        require(type(desktop_saved_verified) is bool, 'storage_receipt_invalid', 400)
        with self.transaction(owner) as cur:
            cur.execute('SELECT * FROM platform.storage_downloads WHERE lease_id=%s AND owner_key=%s AND session_id=%s',
                        (file_uuid(lease_id), owner, session))
            download = cur.fetchone()
            require(download is not None, 'storage_download_not_found', 404)
            require(type(bytes_received) is int and bytes_received == download['expected_bytes'] and sha256 == download['expected_sha256'],
                    'storage_receipt_mismatch', 400)
            if download['completed_at'] is not None:
                return {'status': 'already_completed'}
            require(download['expires_at'] > self.now(cur), 'storage_download_expired', 410)
            cur.execute('UPDATE platform.storage_downloads SET completed_at=clock_timestamp() WHERE lease_id=%s', (str(download['lease_id']),))
            # A recognition workbook or an Oracle preview is an intermediate
            # delivery: the person still needs the source and result to review
            # the order and use its version-bound confirmation. Only these
            # server-recorded sales jobs qualify; no client flag changes TTL.
            cur.execute('''SELECT EXISTS(
              SELECT 1 FROM platform.storage_task_files output
              JOIN platform.storage_files f ON f.file_id=output.file_id
                AND f.owner_key=output.owner_key AND f.session_id=output.session_id
              JOIN platform.project_jobs j ON j.owner_key=output.owner_key
                AND j.session_id=output.session_id AND j.job_id::text=output.task_id
              WHERE output.file_id=%s AND output.owner_key=%s AND output.session_id=%s
                AND output.role='output' AND f.kind='enterprise_output' AND f.status='active'
                AND f.expires_at>clock_timestamp() AND j.connector='sales-orders'
                AND j.action IN ('recognize','recognize_test','recognize_production','prepare')
                AND j.status='completed' AND j.encrypted_result IS NOT NULL
                AND j.content_expires_at>clock_timestamp()) AS sales_review''',
                (str(download['file_id']), owner, session))
            if cur.fetchone()['sales_review']:
                cur.execute('''UPDATE platform.storage_files
                  SET first_transfer_at=COALESCE(first_transfer_at,clock_timestamp())
                  WHERE file_id=%s AND owner_key=%s AND session_id=%s AND status='active' ''',
                    (str(download['file_id']), owner, session))
                return {'status': 'completed', 'browser_saved_to_disk_verified': False if not desktop_saved_verified else None}
            ttl = 0 if desktop_saved_verified else RETRY_DOWNLOAD_SECONDS
            cur.execute('''UPDATE platform.storage_files SET first_transfer_at=COALESCE(first_transfer_at,clock_timestamp()),
              expires_at=LEAST(expires_at,clock_timestamp()+(%s*interval '1 second'))
              WHERE file_id=%s AND status='active' AND kind='enterprise_output' ''', (ttl, str(download['file_id'])))
            # Dependencies live until delivery, not merely until a successful
            # intermediate RPC. Explicit task links preserve unrelated work.
            cur.execute('''UPDATE platform.storage_files f SET expires_at=LEAST(f.expires_at,clock_timestamp()),
              deletion_reason='task_delivered' WHERE f.owner_key=%s AND f.session_id=%s
              AND f.kind IN ('enterprise_input','enterprise_intermediate') AND f.status='active'
              AND EXISTS(SELECT 1 FROM platform.storage_task_files input JOIN platform.storage_task_files output
                ON input.owner_key=output.owner_key AND input.session_id=output.session_id AND input.task_id=output.task_id
                WHERE input.file_id=f.file_id AND input.role IN ('input','intermediate') AND output.file_id=%s AND output.role='output')''',
                (owner, session, str(download['file_id'])))
            cur.execute('''SELECT DISTINCT f.job_id FROM platform.storage_files f
              JOIN platform.storage_task_files input ON input.file_id=f.file_id
              JOIN platform.storage_task_files output ON output.owner_key=input.owner_key AND output.session_id=input.session_id
                AND output.task_id=input.task_id
              WHERE output.file_id=%s AND f.owner_key=%s AND f.session_id=%s AND f.job_id IS NOT NULL''',
                (str(download['file_id']), owner, session))
            jobs = [r['job_id'] for r in cur.fetchall() if re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', r['job_id'])]
            if jobs:
                cur.execute('''UPDATE platform.project_jobs SET encrypted_result=NULL,content_expires_at=clock_timestamp()
                  WHERE owner_key=%s AND session_id=%s AND job_id::text=ANY(%s) AND status NOT IN ('running','cancelling')''', (owner, session, jobs))
                cur.execute('''DELETE FROM platform.project_session_state WHERE owner_key=%s AND session_id=%s
                  AND lifecycle_job_id::text=ANY(%s)''', (owner, session, jobs))
            return {'status': 'completed', 'browser_saved_to_disk_verified': False if not desktop_saved_verified else None}

    def abandon_download(self, owner, session, lease_id):
        identity(owner, session)
        with self.transaction(owner) as cur:
            cur.execute('''UPDATE platform.storage_downloads SET completed_at=clock_timestamp()
              WHERE lease_id=%s AND owner_key=%s AND session_id=%s AND completed_at IS NULL''', (file_uuid(lease_id), owner, session))

    def complete_task(self, owner, session, job_id, *, cancelled=False, keep_retry_workbook=False):
        identity(owner, session)
        require(isinstance(job_id, str) and SESSION.fullmatch(job_id), 'storage_job_invalid', 400)
        with self.transaction(owner) as cur:
            kinds = list(KINDS - {'ordinary'}) if cancelled else ['enterprise_input', 'enterprise_intermediate']
            cur.execute('''UPDATE platform.storage_files SET expires_at=LEAST(expires_at,clock_timestamp()),deletion_reason=%s
              WHERE owner_key=%s AND session_id=%s AND (job_id=%s OR EXISTS(
                SELECT 1 FROM platform.storage_task_files t WHERE t.file_id=storage_files.file_id AND t.task_id=%s
                AND t.owner_key=storage_files.owner_key AND t.session_id=storage_files.session_id))
              AND kind=ANY(%s) AND status IN ('reserved','active')
              AND NOT (%s AND kind='enterprise_input' AND lower(name) LIKE '%%.xlsx' AND EXISTS (
                SELECT 1 FROM platform.storage_task_files t JOIN platform.project_artifacts a
                  ON a.managed_file_id=t.file_id AND a.owner_key=t.owner_key AND a.session_id=t.session_id
                WHERE t.file_id=storage_files.file_id AND t.owner_key=storage_files.owner_key
                  AND t.session_id=storage_files.session_id AND t.task_id=%s AND t.role='input'
                  AND a.source='native_message'))''',
                ('task_cancelled' if cancelled else 'task_delivered', owner, session, job_id, job_id, kinds,
                 keep_retry_workbook, job_id))
            changed = cur.rowcount
            cur.execute('''SELECT DISTINCT f.job_id FROM platform.storage_files f
              JOIN platform.storage_task_files t ON t.file_id=f.file_id
              WHERE t.owner_key=%s AND t.session_id=%s AND t.task_id=%s AND f.job_id IS NOT NULL''', (owner, session, job_id))
            jobs = [r['job_id'] for r in cur.fetchall()] + [job_id]
            jobs = [value for value in jobs if re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', value)]
            if jobs:
                cur.execute('''UPDATE platform.project_jobs SET encrypted_result=NULL,content_expires_at=clock_timestamp()
                  WHERE owner_key=%s AND session_id=%s AND job_id::text=ANY(%s) AND status NOT IN ('running','cancelling')''', (owner, session, jobs))
                cur.execute('''DELETE FROM platform.project_session_state WHERE owner_key=%s AND session_id=%s
                  AND lifecycle_job_id::text=ANY(%s)''', (owner, session, jobs))
            return changed

    def expire_session(self, owner, session):
        identity(owner, session)
        with self.transaction(owner) as cur:
            cur.execute('''UPDATE platform.storage_files SET expires_at=LEAST(expires_at,clock_timestamp()),deletion_reason='session_expired'
              WHERE owner_key=%s AND session_id=%s AND status IN ('reserved','active')''', (owner, session))
            return cur.rowcount

    def expire_file(self, owner, session, file_id):
        identity(owner, session)
        with self.transaction(owner) as cur:
            self._row(cur, owner, session, file_uuid(file_id))
            cur.execute('''UPDATE platform.storage_files SET expires_at=LEAST(expires_at,clock_timestamp()),deletion_reason='user_deleted'
              WHERE file_id=%s AND status IN ('reserved','active')''', (file_id,))
            return {'status': 'cleanup_requested'}

    def list_files(self, owner, *, offset=0, maximum=100):
        require(type(offset) is int and 0 <= offset <= 10000 and type(maximum) is int and 1 <= maximum <= 100,
                'storage_page_invalid', 400)
        with self.transaction(owner) as cur:
            cur.execute('''SELECT * FROM platform.storage_files WHERE owner_key=%s AND status<>'deleted'
              ORDER BY created_at DESC,file_id LIMIT %s OFFSET %s''', (owner, maximum, offset))
            return [{**self.public(row), 'session_id': row['session_id']} for row in cur.fetchall()]

    def events(self, owner, *, after=0, maximum=100):
        require(type(after) is int and after >= 0 and type(maximum) is int and 1 <= maximum <= 100, 'storage_page_invalid', 400)
        with self.transaction(owner) as cur:
            cur.execute('''SELECT event_id,file_id,session_id,kind,action,created_at FROM platform.storage_events
              WHERE owner_key=%s AND event_id>%s ORDER BY event_id LIMIT %s''', (owner, after, maximum))
            return [{**dict(row), 'file_id': str(row['file_id'])} for row in cur.fetchall()]

    def usage(self, owner):
        with self.transaction(owner) as cur:
            cur.execute('''SELECT kind,count(*) AS files,COALESCE(sum(expected_bytes),0) AS charged_bytes,
              COALESCE(sum(physical_bytes),0) AS verified_physical_bytes,
              COALESCE(sum(expected_bytes) FILTER(WHERE status='reserved'),0) AS reserved_bytes,
              COALESCE(sum(expected_bytes) FILTER(WHERE status='reclaiming' OR expires_at<=clock_timestamp()),0) AS pending_cleanup_bytes
              FROM platform.storage_files WHERE owner_key=%s AND status<>'deleted' GROUP BY kind ORDER BY kind''', (owner,))
            rows = [dict(row) for row in cur.fetchall()]
            cur.execute('''SELECT count(*) AS files,COALESCE(sum(expected_bytes),0) AS bytes,
              count(*) FILTER(WHERE state='manual_review') AS manual_review_files
              FROM platform.storage_source_copies WHERE owner_key=%s AND state<>'removed_confirmed' ''', (owner,))
            sources = dict(cur.fetchone())
            charged = int(sum(r['charged_bytes'] for r in rows))
            pool = self._pool_usage(cur)
            return {'scope': 'managed_cloud_files', 'quota_bytes': self.limits.owner_bytes,
                    'session_quota_bytes': self.limits.session_bytes,
                    'quota_mode': 'shared_burst' if self.limits.shared_burst else 'strict',
                    'personal_byte_limit_enforced': not self.limits.shared_burst,
                    'soft_quota': {'owner_bytes': self.limits.owner_bytes,
                                   'session_bytes': self.limits.session_bytes},
                    'borrowed_bytes': max(0, charged - self.limits.owner_bytes),
                    'over_soft_quota': charged > self.limits.owner_bytes,
                    'global_capacity_warning': pool['warning_level'],
                    'shared_pool': pool,
                    'charged_bytes': charged, 'categories': rows,
                    'native_workspace_disk_quota_enforced': False, 'desktop_local_files_included': False,
                    'temporary_source_copies_awaiting_confirmation': sources}

    def _pool_usage(self, cur):
        cur.execute('''SELECT COALESCE(sum(expected_bytes),0) AS charged,
          COALESCE(sum(expected_bytes) FILTER(WHERE status='reserved'),0) AS reserved,
          COALESCE(sum(expected_bytes) FILTER(WHERE status='reclaiming' OR expires_at<=clock_timestamp()),0) AS cleanup,
          count(*) AS files FROM platform.storage_files WHERE status<>'deleted' ''')
        row = cur.fetchone()
        charged, reserved, limit = int(row['charged']), int(row['reserved']), self.limits.global_bytes
        threshold = next((n for n in (95, 90, 80) if charged * 100 >= limit * n), 0)
        level = {0: 'normal', 80: 'warning', 90: 'high', 95: 'critical'}[threshold]
        remaining = max(0, limit - charged)
        backend = {'status': 'unavailable'}
        if self.blobs is not None:
            try:
                backend = dict(self.blobs.capacity_snapshot(), status='available')
                spare = max(0, backend['available_bytes'] - backend['minimum_free_bytes'] - reserved)
                remaining = min(remaining, spare)
                backend['unreserved_available_bytes'] = spare
                backend['minimum_free_protected'] = backend['available_bytes'] >= backend['minimum_free_bytes'] + reserved
                if not backend['minimum_free_protected']:
                    level = 'critical'
            except (OSError, StorageError):
                # Telemetry does not make a failed backend healthy. Admission
                # still calls the unchanged strict capacity() under the PG lock.
                backend = {'status': 'unavailable'}
                remaining = 0
        return {'charged_bytes': charged, 'limit_bytes': limit, 'reserved_bytes': reserved,
                'pending_cleanup_bytes': int(row['cleanup']), 'files': int(row['files']),
                'remaining_bytes': remaining, 'thresholds_percent': [80, 90, 95],
                'threshold_reached_percent': threshold, 'warning_level': level,
                'backend': backend, 'snapshot_is_reservation': False}

    def admin_usage(self, *, after_owner='', maximum=500):
        """Metadata-only collector view; caller must enforce administrator access.

        Pagination covers users, while global totals and soft-overage counts
        always cover the whole ledger. It reserves no per-user disk space.
        """
        require(isinstance(after_owner, str) and (not after_owner or OWNER.fullmatch(after_owner)) and
                type(maximum) is int and 1 <= maximum <= 500, 'storage_page_invalid', 400)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute("SET LOCAL statement_timeout='3s'")
            cur.execute("SET LOCAL lock_timeout='500ms'")
            pool = self._pool_usage(cur)
            cur.execute('''SELECT count(*) AS users,
              count(*) FILTER(WHERE charged>%s) AS over_soft_quota_users,
              COALESCE(sum(GREATEST(charged-%s,0)),0) AS borrowed_bytes
              FROM (SELECT owner_key,sum(expected_bytes) AS charged FROM platform.storage_files
                WHERE status<>'deleted' GROUP BY owner_key) usage''',
                (self.limits.owner_bytes, self.limits.owner_bytes))
            summary = {k: int(v) for k, v in cur.fetchone().items()}
            cur.execute('''SELECT owner_key,count(*) AS files,sum(expected_bytes) AS charged_bytes,
              COALESCE(sum(expected_bytes) FILTER(WHERE status='reserved'),0) AS reserved_bytes
              FROM platform.storage_files WHERE status<>'deleted' AND owner_key>%s
              GROUP BY owner_key ORDER BY owner_key LIMIT %s''', (after_owner, maximum + 1))
            rows = list(cur.fetchall())
        users = [{'owner_key': row['owner_key'], 'owner_label': '用户 …' + row['owner_key'][-6:],
                  'charged_bytes': int(row['charged_bytes']), 'files': int(row['files']),
                  'reserved_bytes': int(row['reserved_bytes']), 'soft_quota_bytes': self.limits.owner_bytes,
                  'borrowed_bytes': max(0, int(row['charged_bytes']) - self.limits.owner_bytes)}
                 for row in rows[:maximum]]
        alerts = []
        if pool['threshold_reached_percent']:
            alerts.append({'code': 'storage_pool_capacity', 'scope': 'global',
                           'severity': pool['warning_level'],
                           'message': '共享存储计费占用达到 ' + str(pool['threshold_reached_percent']) + '% 保护预警线。'})
        if pool['backend']['status'] != 'available':
            alerts.append({'code': 'storage_backend_unavailable', 'scope': 'global', 'severity': 'critical',
                           'message': '无法核实实际可用空间；需要检查存储挂载与访问。'})
        elif not pool['backend']['minimum_free_protected']:
            alerts.append({'code': 'storage_backend_capacity_low', 'scope': 'global', 'severity': 'critical',
                           'message': '实际剩余空间触及保护线，新文件写入将被拒绝。'})
        if summary['over_soft_quota_users']:
            alerts.append({'code': 'storage_personal_soft_quota', 'scope': 'users', 'severity': 'warning',
                           'message': str(summary['over_soft_quota_users']) + ' 位用户超出个人参考额度，正在使用共享余量。'})
        return {'status': 'available', 'scope': 'managed_cloud_files',
                'quota_mode': 'shared_burst' if self.limits.shared_burst else 'strict',
                'global': pool, 'users': users, 'user_summary': summary, 'alerts': alerts,
                'alert_state': 'firing' if alerts else 'clear',
                'next_after_owner': users[-1]['owner_key'] if len(rows) > maximum else None,
                'users_page_complete': len(rows) <= maximum,
                'identity_labels_are_employee_numbers': False,
                'file_names_or_contents_included': False, 'monitoring_only_no_notifications_sent': True}

    def cleanup(self, *, maximum=32):
        """Bounded reclaim with persistent token; unknown outcomes retain quota."""
        require(type(maximum) is int and 1 <= maximum <= 128, 'storage_batch_invalid')
        require(self.blobs is not None, 'storage_backend_unavailable', 503)
        with self.connect() as conn, conn.cursor() as cur:
            # Fill the bounded batch with reclaimable files first. Older files
            # that are still downloading must not monopolize every sweep.
            cur.execute('''WITH due AS NOT MATERIALIZED (
                SELECT f.file_id,f.owner_key,f.session_id,f.expires_at,
                    EXISTS (SELECT 1 FROM platform.storage_downloads d WHERE d.file_id=f.file_id
                            AND d.completed_at IS NULL AND d.expires_at>statement_timestamp()) AS downloading
                FROM platform.storage_files f
                WHERE f.status<>'deleted' AND f.expires_at<=statement_timestamp() AND f.retry_at<=statement_timestamp()
                    AND (f.status<>'reclaiming' OR f.lease_until<=statement_timestamp())
              ), available AS (
                SELECT file_id,owner_key,session_id FROM due WHERE NOT downloading ORDER BY expires_at,file_id LIMIT %s
              )
              SELECT * FROM available UNION ALL
              (SELECT file_id,owner_key,session_id FROM due WHERE downloading ORDER BY expires_at,file_id
                   LIMIT (SELECT %s-count(*) FROM available))''', (maximum,maximum))
            candidates = list(cur.fetchall())
        counts = {'deleted': 0, 'deferred': 0, 'reclaimed_bytes': 0}
        for candidate in candidates:
            owner, session, fid = candidate['owner_key'], candidate['session_id'], str(candidate['file_id'])
            token = str(uuid4())
            with self.transaction(owner) as cur:
                row = self._row(cur, owner, session, fid)
                now = self.now(cur)
                if row['status'] == 'deleted' or row['expires_at'] > now or row['retry_at'] > now or (
                        row['status'] == 'reclaiming' and row['lease_until'] > now):
                    continue
                cur.execute('''SELECT 1 FROM platform.storage_downloads WHERE file_id=%s AND completed_at IS NULL
                  AND expires_at>clock_timestamp() LIMIT 1''', (fid,))
                if cur.fetchone():
                    counts['deferred'] += 1
                    continue
                cur.execute('''UPDATE platform.storage_files SET status='reclaiming',cleanup_token=%s,
                  lease_until=clock_timestamp()+interval '3 minutes' WHERE file_id=%s''', (token, fid))
                cur.execute('''INSERT INTO platform.storage_events(file_id,owner_key,session_id,kind,action)
                  VALUES(%s,%s,%s,%s,'expired') ON CONFLICT DO NOTHING''', (fid, owner, session, row['kind']))
                # Access checks examine managed_file_id before decrypting. Keep
                # only the opaque tombstone needed to render an expired link.
                cur.execute('''UPDATE platform.project_artifacts SET encrypted_payload='',file_bytes=0
                  WHERE owner_key=%s AND session_id=%s AND managed_file_id=%s''', (owner, session, fid))
                cur.execute("UPDATE platform.storage_source_copies SET state='cleanup_required' WHERE file_id=%s AND state='temporary_retained'", (fid,))
                cur.execute("UPDATE platform.hybrid_attachments SET name='expired-'||id::text,size_bytes=0 WHERE managed_file_id=%s", (fid,))
            try:
                removed = self.blobs.remove(owner, fid, row['expected_bytes'], row['sha256'])
                error = None
            except Exception:
                removed, error = 0, 'physical_cleanup_unconfirmed'
            with self.transaction(owner) as cur:
                current = self._row(cur, owner, session, fid)
                if current['status'] != 'reclaiming' or str(current['cleanup_token']) != token:
                    counts['deferred'] += 1
                    continue
                if error:
                    cur.execute('''UPDATE platform.storage_files SET last_error=%s,retry_at=clock_timestamp()+interval '1 minute',
                      lease_until=clock_timestamp() WHERE file_id=%s''', (error, fid))
                    counts['deferred'] += 1
                    continue
                cur.execute('''UPDATE platform.storage_files SET status='deleted',deleted_at=clock_timestamp(),physical_bytes=0,
                  name='',media_type='',last_error=NULL,cleanup_token=NULL,lease_until=NULL WHERE file_id=%s''', (fid,))
                cur.execute('''INSERT INTO platform.storage_events(file_id,owner_key,session_id,kind,action)
                  VALUES(%s,%s,%s,%s,'deleted') ON CONFLICT DO NOTHING''', (fid, owner, session, row['kind']))
                cur.execute('DELETE FROM platform.storage_downloads WHERE file_id=%s', (fid,))
                counts['deleted'] += 1
                counts['reclaimed_bytes'] += removed
        return counts


def configured():
    return os.getenv('MAIMI_STORAGE_POLICY') == 'managed-cloud-files-r72'


def purge_enterprise_content(*, connect=_connect, maximum=100):
    """Drop expired encrypted business content, keeping idempotency/stop receipts."""
    require(type(maximum) is int and 1 <= maximum <= 1000, 'storage_batch_invalid')
    with connect() as conn, conn.cursor() as cur:
        cur.execute('''WITH due AS(SELECT owner_key,session_id,state_key FROM platform.project_session_state
          WHERE expires_at<=clock_timestamp() ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED)
          DELETE FROM platform.project_session_state s USING due d WHERE s.owner_key=d.owner_key
          AND s.session_id=d.session_id AND s.state_key=d.state_key''', (maximum,))
        states = cur.rowcount
        cur.execute('''WITH due AS(SELECT job_id FROM platform.project_jobs WHERE content_expires_at<=clock_timestamp()
          AND encrypted_result IS NOT NULL AND status NOT IN ('running','cancelling')
          ORDER BY content_expires_at LIMIT %s FOR UPDATE SKIP LOCKED)
          UPDATE platform.project_jobs j SET encrypted_result=NULL FROM due WHERE j.job_id=due.job_id''', (maximum,))
        results=cur.rowcount
        cur.execute('''WITH due AS(SELECT id FROM platform.hybrid_runs WHERE content_expires_at<=clock_timestamp()
          AND status<>'running' AND (input_text<>'' OR result IS NOT NULL OR attachment_ids<>'{}'::uuid[])
          ORDER BY content_expires_at LIMIT %s FOR UPDATE SKIP LOCKED)
          UPDATE platform.hybrid_runs r SET input_text='',result=NULL,attachment_ids='{}'::uuid[],document_refs='[]'::jsonb
          FROM due WHERE r.id=due.id''', (maximum,))
        return {'states_removed': states, 'results_removed': results, 'legacy_run_content_removed': cur.rowcount}


def default_store():
    require(configured(), 'storage_policy_not_enabled', 503)
    root = os.getenv('MAIMI_STORAGE_ROOT', '')
    require(bool(root), 'storage_backend_unavailable', 503)
    backend=os.getenv('MAIMI_STORAGE_BACKEND','nfs')
    require(backend in ('nfs','single-node-local-test'),'storage_backend_invalid',503)
    source=os.getenv('MAIMI_STORAGE_NFS_SOURCE','')
    local=None
    if backend=='nfs':
        require(bool(source),'storage_nfs_source_required',503)
    else:
        require(not source,'storage_backend_ambiguous',503)
        from .storage_local_test import LocalObjectsIdentity,LocalObjectsError
        try:
            local=LocalObjectsIdentity.load(os.getenv('MAIMI_STORAGE_LOCAL_IDENTITY_FILE',''),root=root,
                deployment_mode=os.getenv('MAIMI_DEPLOYMENT_MODE',''),node_name=os.getenv('MAIMI_NODE_NAME',''))
        except (OSError,ValueError,LocalObjectsError) as error:
            code=str(error) if isinstance(error,LocalObjectsError) else 'storage_local_identity_unavailable'
            raise StorageError(code,503) from None
    try:
        shared_burst = os.getenv('MAIMI_STORAGE_SHARED_BURST', 'false').lower()
        require(shared_burst in ('true', 'false'), 'storage_limits_invalid', 503)
        limits = Limits(owner_bytes=int(os.getenv('MAIMI_STORAGE_OWNER_BYTES', str(2 * 1024**3))),
                        session_bytes=int(os.getenv('MAIMI_STORAGE_SESSION_BYTES', str(512 * 1024**2))),
                        global_bytes=int(os.getenv('MAIMI_STORAGE_GLOBAL_BYTES',str(120*1024**3))),
                        shared_burst=shared_burst == 'true')
        minimum_free=int(os.getenv('MAIMI_STORAGE_MIN_FREE_BYTES',str(10*1024**3)))
    except ValueError:
        raise StorageError('storage_limits_invalid', 503) from None
    return Storage(blobs=BlobStore(root,expected_nfs_source=source if backend=='nfs' else None,
        local_test_identity=local,minimum_free_bytes=minimum_free), limits=limits)
