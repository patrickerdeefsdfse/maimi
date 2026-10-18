"""Package a completed runtime tree. No company or Docker operation is performed."""
from pathlib import Path
import argparse,hashlib,json,os,subprocess,tarfile

def declared_paths(tree,manifest):
    allowed={'candidate-manifest.json'}
    allowed.update('opt/dsh/node_modules/'+name for name in manifest['module_files'])
    allowed.update(name.removeprefix('/') for name in manifest['company_source_targets'])
    directories=set()
    for name in allowed:
        for parent in Path(name).parents:
            if parent.as_posix()!='.':directories.add(parent.as_posix())
    paths=sorted(tree.rglob('*'))
    for p in paths:
        rel=p.relative_to(tree).as_posix()
        if p.is_symlink():
            if rel not in allowed or not p.resolve(strict=True).is_relative_to(tree):raise ValueError('UNDECLARED_OR_EXTERNAL_SYMLINK')
        elif p.is_dir():
            # Empty npm package directories carry no executable content.
            if rel not in directories and not rel.startswith('opt/dsh/node_modules/'):raise ValueError('UNDECLARED_DIRECTORY')
        elif p.is_file():
            if rel not in allowed:raise ValueError('UNDECLARED_ARCHIVE_FILE')
        else:raise ValueError('SPECIAL_FILE')
    return paths

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tree',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();tree=args.tree.resolve();out=args.output.resolve()
    if out.exists() or out.is_relative_to(tree):raise ValueError('NEW_EXTERNAL_ARCHIVE_REQUIRED')
    checked=subprocess.run(['node',str(Path(__file__).with_name('verify-runtime.mjs')),str(tree)],check=True,text=True,capture_output=True)
    verified=json.loads(checked.stdout)
    paths=declared_paths(tree,json.loads((tree/'candidate-manifest.json').read_text(encoding='utf-8')))
    out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('xb') as raw:
        with tarfile.open(fileobj=raw,mode='w:gz',compresslevel=6) as archive:
            for p in paths:
                info=archive.gettarinfo(str(p),p.relative_to(tree).as_posix());info.uid=0;info.gid=0;info.uname='root';info.gname='root';info.mtime=0
                if info.isfile():
                    with p.open('rb') as source:archive.addfile(info,source)
                else:archive.addfile(info)
    with out.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
    receipt={'schema':'maimi-native021-runtime-archive-v1','status':'PACKAGED_NOT_DEPLOYED','file':out.name,'bytes':out.stat().st_size,'sha256':digest,'manifest_sha256':verified['manifest_sha256'],'runtime':'0.2.1-rc.2','company_changed':False}
    out.with_name(out.name+'.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8');print(json.dumps(receipt))
if __name__=='__main__':main()
