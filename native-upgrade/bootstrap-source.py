"""Fetch/verify the fixed official archive and extract regular source files only."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,tarfile,urllib.request

ROOT=Path(__file__).resolve().parent
COMMIT='687f282e8625c6b8d11ff58211a90524be2e4317'
EXPECTED='157903b22c2881d3678d846755afe957a48df834e747deda805a1195ce25a146'
URL='https://codeload.github.com/deepseek-ai/deepseek-harness/tar.gz/'+COMMIT
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
def main():
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path);p.add_argument('--download',action='store_true');args=p.parse_args()
    archive=args.archive or ROOT/'upstream/source-687f282.tar.gz'
    if args.download:
        archive.parent.mkdir(parents=True,exist_ok=True)
        with archive.open('xb') as dest:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
            with opener.open(URL,timeout=60) as source:
                total=0
                while chunk:=source.read(1024*1024):
                    total+=len(chunk)
                    if total>64*1024*1024:raise ValueError('SOURCE_TOO_LARGE')
                    dest.write(chunk)
    with archive.open('rb') as source:
        if hashlib.file_digest(source,'sha256').hexdigest()!=EXPECTED:raise ValueError('SOURCE_ARCHIVE_MISMATCH')
    output=ROOT/'upstream/source';tree=output/('deepseek-harness-'+COMMIT)
    if tree.exists():raise ValueError('SOURCE_DIRECTORY_ALREADY_EXISTS')
    with tarfile.open(archive,'r:gz') as tar:
        members=tar.getmembers();seen=set();total=0;regular=[];links=0
        for m in members:
            name=PurePosixPath(m.name)
            if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0]!=tree.name or '\\' in m.name or m.name in seen:raise ValueError('INVALID_SOURCE_MEMBER')
            seen.add(m.name)
            if m.issym() or m.islnk():links+=1;continue
            if not (m.isdir() or m.isfile()):raise ValueError('SPECIAL_SOURCE_MEMBER')
            total+=m.size
            if total>256*1024*1024:raise ValueError('SOURCE_EXPANSION_TOO_LARGE')
            regular.append(m)
        for m in regular:
            target=output/m.name
            if m.isdir():target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with tar.extractfile(m) as source,target.open('xb') as dest:dest.write(source.read())
                target.chmod(0o755 if m.mode&0o111 else 0o644)
    print(json.dumps({'status':'VERIFIED_AND_EXTRACTED','commit':COMMIT,'regular_files':sum(m.isfile() for m in regular),'links_not_extracted':links,'company_changed':False}))
if __name__=='__main__':main()
