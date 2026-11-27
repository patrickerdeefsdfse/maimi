"""Bounded Office output writer. Candidate only; not installed or deployed.

The final name is reserved exclusively with mode 000. After the complete stream
is verified and synced, fchmod makes it readable. This is readable publication,
not atomic namespace rename and not protection against a malicious same-UID
process that deliberately changes permissions. Never run this writer as root.
"""
import hashlib
import json
import os
import re
import signal
import stat
import sys

PROTOCOL = 'megmeet-office-output-v1'
MAX_OUTPUT = 128 * 1024 * 1024
HEADER_LIMIT = 8192
COMMIT = b'\nMEGMEET_OFFICE_PUBLISH_V1\n'
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refusal(Exception):
    pass


def need(condition, code):
    if not condition:
        raise Refusal(code)


def identity(info):
    return info.st_dev, info.st_ino


def read_request(stream):
    header = stream.readline(HEADER_LIMIT + 1)
    need(len(header) <= HEADER_LIMIT and header.endswith(b'\n'), 'OFFICE_OUTPUT_HEADER_INVALID')
    try:
        request = json.loads(header)
    except (ValueError, UnicodeError):
        raise Refusal('OFFICE_OUTPUT_HEADER_INVALID') from None
    need(isinstance(request, dict) and set(request) == {
        'protocol', 'root', 'root_dev', 'root_ino', 'output', 'size_bytes', 'sha256',
    }, 'OFFICE_OUTPUT_HEADER_INVALID')
    need(request['protocol'] == PROTOCOL, 'OFFICE_OUTPUT_HEADER_INVALID')
    root = request['root']
    need(isinstance(root, str) and root.startswith('/') and len(root) <= 4096
         and not re.search(r'[\x00-\x1f\x7f]', root), 'OFFICE_OUTPUT_ROOT_INVALID')
    for key in ('root_dev', 'root_ino'):
        need(isinstance(request[key], str) and re.fullmatch(r'[0-9]{1,20}', request[key])
             and int(request[key]) < 2 ** 64, 'OFFICE_OUTPUT_ROOT_INVALID')
    output = request['output']
    need(isinstance(output, str) and 0 < len(output) <= 2048
         and not re.search(r'[\x00-\x1f\x7f:\\]', output), 'OFFICE_REMOTE_PATH_INVALID')
    parts = output.split('/')
    need(len(parts) <= 128 and all(part and part not in ('.', '..')
         and not part.endswith((' ', '.')) and len(os.fsencode(part)) <= 255
         for part in parts), 'OFFICE_REMOTE_PATH_INVALID')
    size = request['size_bytes']
    need(type(size) is int and 0 < size <= MAX_OUTPUT, 'OFFICE_REMOTE_OUTPUT_INVALID')
    need(isinstance(request['sha256'], str) and re.fullmatch('[a-f0-9]{64}', request['sha256']),
         'OFFICE_REMOTE_OUTPUT_INVALID')
    return request, parts


def open_parents(root, parts, expected_root):
    handles = []
    try:
        before = os.lstat(root)
        need(stat.S_ISDIR(before.st_mode), 'OFFICE_OUTPUT_ROOT_CHANGED')
        handles.append(os.open(root, DIRECTORY))
        after = os.lstat(root)
        need(stat.S_ISDIR(after.st_mode) and identity(before) == identity(after)
             == identity(os.fstat(handles[0])) == expected_root, 'OFFICE_OUTPUT_ROOT_CHANGED')
        for part in parts[:-1]:
            parent = handles[-1]
            before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            need(stat.S_ISDIR(before.st_mode), 'OFFICE_REMOTE_PATH_INVALID')
            handles.append(os.open(part, DIRECTORY, dir_fd=handles[-1]))
            after = os.stat(part, dir_fd=parent, follow_symlinks=False)
            need(stat.S_ISDIR(after.st_mode) and identity(before) == identity(after)
                 == identity(os.fstat(handles[-1])), 'OFFICE_OUTPUT_PARENT_CHANGED')
        return handles
    except BaseException:
        for handle in reversed(handles):
            os.close(handle)
        raise


def verify_parents(request, parts, held):
    check = open_parents(request['root'], parts, identity(os.fstat(held[0])))
    try:
        need(len(check) == len(held) and all(identity(os.fstat(a)) == identity(os.fstat(b))
             for a, b in zip(check, held)), 'OFFICE_OUTPUT_PARENT_CHANGED')
    finally:
        for handle in reversed(check):
            os.close(handle)


def verify_target(parent, leaf, handle, original, size, mode):
    current = os.fstat(handle)
    path_info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    need(stat.S_ISREG(current.st_mode) and current.st_uid == os.geteuid()
         and current.st_nlink == 1 and current.st_size == size
         and stat.S_IMODE(current.st_mode) == mode
         and identity(current) == identity(original) == identity(path_info)
         and stat.S_ISREG(path_info.st_mode) and path_info.st_nlink == 1,
         'OFFICE_OUTPUT_CHANGED')


def publish(stream):
    need(os.geteuid() != 0, 'OFFICE_OUTPUT_UNPRIVILEGED_WRITER_REQUIRED')
    request, parts = read_request(stream)
    handles = open_parents(request['root'], parts, (int(request['root_dev']), int(request['root_ino'])))
    parent, leaf = handles[-1], parts[-1]
    handle = None
    original = None
    committed = False
    try:
        verify_parents(request, parts, handles)
        try:
            handle = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0, dir_fd=parent)
        except FileExistsError:
            raise Refusal('OFFICE_OUTPUT_EXISTS_USE_NEW_NAME') from None
        original = os.fstat(handle)
        verify_target(parent, leaf, handle, original, 0, 0)
        remaining = request['size_bytes']
        digest = hashlib.sha256()
        while remaining:
            chunk = stream.read1(min(65536, remaining))
            need(bool(chunk), 'OFFICE_REMOTE_OUTPUT_INTEGRITY')
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(handle, chunk[offset:])
                need(written > 0, 'OFFICE_OUTPUT_WRITE_FAILED')
                offset += written
            remaining -= len(chunk)
        # EOF alone can also mean a cancelled/broken download. The trusted Node
        # caller writes this trailer only after validating the complete body.
        need(stream.read(len(COMMIT)) == COMMIT and not stream.read(1)
             and digest.hexdigest() == request['sha256'],
             'OFFICE_REMOTE_OUTPUT_INTEGRITY')
        os.fsync(handle)
        verify_parents(request, parts, handles)
        verify_target(parent, leaf, handle, original, request['size_bytes'], 0)
        os.fchmod(handle, 0o600)
        os.fsync(handle)
        verify_parents(request, parts, handles)
        verify_target(parent, leaf, handle, original, request['size_bytes'], 0o600)
        committed = True
        return {'protocol': PROTOCOL, 'ok': True, 'bytes': request['size_bytes'],
                'nlink': 1, 'publication': 'complete-before-readable'}
    finally:
        pending = sys.exc_info()[1]
        cleanup = 'not-needed'
        if handle is not None:
            # Close before unlink. Never find or remove hidden NFS sillyrename aliases.
            os.close(handle)
        if original is not None and not committed:
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if identity(info) == identity(original) and stat.S_ISREG(info.st_mode):
                    os.unlink(leaf, dir_fd=parent)
                    cleanup = 'owned-output-removed'
                else:
                    cleanup = 'foreign-output-preserved'
            except FileNotFoundError:
                cleanup = 'output-absent'
            except OSError:
                cleanup = 'owned-output-cleanup-unconfirmed'
        if pending is not None:
            pending.cleanup = cleanup
        for directory in reversed(handles):
            os.close(directory)


def main():
    def cancelled(signum, frame):
        raise Refusal('OFFICE_OUTPUT_CANCELLED')

    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    try:
        result = publish(sys.stdin.buffer)
    except Refusal as exc:
        result = {'protocol': PROTOCOL, 'ok': False, 'code': str(exc),
                  'cleanup': getattr(exc, 'cleanup', 'not-needed')}
    except OSError as exc:
        # Only fixed public codes; never include paths or raw OS diagnostics.
        code = 'OFFICE_OUTPUT_PERMISSION_DENIED' if exc.errno in (1, 13) else 'OFFICE_OUTPUT_PUBLISH_FAILED'
        result = {'protocol': PROTOCOL, 'ok': False, 'code': code,
                  'cleanup': getattr(exc, 'cleanup', 'not-needed')}
    except Exception:
        result = {'protocol': PROTOCOL, 'ok': False, 'code': 'OFFICE_OUTPUT_PUBLISH_FAILED'}
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
