"""Fetch the fixed public upstream archive, verify it, and extract regular files only."""
from pathlib import Path, PurePosixPath
import hashlib, json, os, tarfile, tempfile, urllib.request

HERE = Path(__file__).resolve().parents[1]
UPSTREAM = HERE.parent / 'upstream'
COMMIT = '687f282e8625c6b8d11ff58211a90524be2e4317'
SHA = '157903b22c2881d3678d846755afe957a48df834e747deda805a1195ce25a146'
URL = 'https://codeload.github.com/deepseek-ai/deepseek-harness/tar.gz/' + COMMIT
MAX_BYTES = 40000000

def digest(p):
    with p.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

def main():
    UPSTREAM.mkdir(exist_ok=True)
    archive = UPSTREAM / 'source-687f282.tar.gz'
    if archive.is_symlink(): raise RuntimeError('ARCHIVE_SYMLINK_REFUSED')
    if not archive.exists():
        fd, raw = tempfile.mkstemp(prefix='dsh-source-', suffix='.part', dir=UPSTREAM)
        temp = Path(raw)
        try:
            with os.fdopen(fd, 'wb') as output:
                with urllib.request.urlopen(URL, timeout=45) as response:
                    if response.url != URL: raise RuntimeError('UNEXPECTED_DOWNLOAD_REDIRECT')
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_BYTES: raise RuntimeError('ARCHIVE_SIZE_EXCEEDED')
                        output.write(chunk)
            if digest(temp) != SHA: raise RuntimeError('ARCHIVE_HASH_MISMATCH')
            os.replace(temp, archive)
        finally:
            temp.unlink(missing_ok=True)
    if digest(archive) != SHA: raise RuntimeError('ARCHIVE_HASH_MISMATCH')
    target = UPSTREAM / 'source' / ('deepseek-harness-' + COMMIT)
    if target.exists():
        if target.is_symlink() or not target.is_dir(): raise RuntimeError('SOURCE_ROOT_INVALID')
        print(json.dumps({'status': 'ARCHIVE_VERIFIED_SOURCE_EXISTS_RUN_INSPECT', 'archive_sha256': SHA, 'company_requests': 0}))
        return
    prefix = target.name + '/'
    count = 0
    with tarfile.open(archive, 'r|gz') as bundle:
        for member in bundle:
            if member.isdir() or member.issym(): continue
            if not member.isfile() or not member.name.startswith(prefix): raise RuntimeError('UNSUPPORTED_ARCHIVE_MEMBER')
            relative = PurePosixPath(member.name[len(prefix):])
            if relative.is_absolute() or not relative.parts or '..' in relative.parts: raise RuntimeError('UNSAFE_ARCHIVE_PATH')
            destination = target.joinpath(*relative.parts)
            for parent in [destination, *destination.parents]:
                if parent.is_symlink(): raise RuntimeError('SOURCE_SYMLINK_REFUSED')
                if parent == UPSTREAM: break
            with bundle.extractfile(member) as source: data = source.read()
            if destination.exists():
                if not destination.is_file() or destination.read_bytes() != data: raise RuntimeError('EXISTING_SOURCE_DIFFERS')
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as output: output.write(data)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            count += 1
    print(json.dumps({'status': 'VERIFIED_PUBLIC_SOURCE', 'files': count, 'archive_sha256': SHA, 'company_requests': 0}))

if __name__ == '__main__': main()
