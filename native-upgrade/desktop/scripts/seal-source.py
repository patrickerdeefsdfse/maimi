"""Seal exact public upstream sources; never read company state or credentials."""
from pathlib import Path
import hashlib,json,tarfile

HERE=Path(__file__).resolve().parents[1]
UPSTREAM=HERE.parent/'upstream'
SHA='157903b22c2881d3678d846755afe957a48df834e747deda805a1195ce25a146'
COMMIT='687f282e8625c6b8d11ff58211a90524be2e4317'
archive=UPSTREAM/'source-687f282.tar.gz'
if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA: raise RuntimeError('ARCHIVE_HASH_MISMATCH')
source=UPSTREAM/'source'/('deepseek-harness-'+COMMIT)
files=[]
links=[]
prefix='deepseek-harness-'+COMMIT+'/'
with tarfile.open(archive,'r|gz') as tar:
    for member in tar:
        if member.isdir(): continue
        if member.issym():
            links.append({'path':member.name[len(prefix):],'target':member.linkname,'staged':False})
            continue
        if not member.isfile() or not member.name.startswith(prefix): raise RuntimeError('UNEXPECTED_TAR_MEMBER')
        relative=member.name[len(prefix):]
        if not relative or any(p in ('','.','..') for p in relative.split('/')): raise RuntimeError('UNSAFE_MEMBER')
        item=source/relative
        if item.is_symlink() or not item.is_file(): raise RuntimeError('SOURCE_MEMBER_MISSING')
        data=item.read_bytes()
        with tar.extractfile(member) as handle: archived=handle.read()
        if data!=archived: raise RuntimeError('SOURCE_MEMBER_CHANGED')
        files.append({'path':relative,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'mode':0o755 if member.mode&0o111 else 0o644})
files.sort(key=lambda f:f['path'])
out={'schema':'maimi-dsh-native-desktop-source-lock-v1','version':'0.2.1-rc.2','commit':COMMIT,'source_archive_sha256':SHA,'files':files,'source_bytes':sum(f['bytes'] for f in files),'upstream_links_not_staged':links,'company_requests':0}
(HERE/'upstream-source-lock.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'SEALED','files':len(files),'source_bytes':out['source_bytes'],'company_requests':0}))
