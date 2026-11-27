"""Apply rebuilt application bundles with release hashes and no writes to shared old files."""
import os
import shutil
import re
import sys
from pathlib import Path
from prepare import HERE, OLD_STAGE, STAGE, VENDORS, digest, read, safe, save

def apply(manifest):
    recorded=read(manifest)
    verified={}
    remove=[]
    for row in recorded['files']:
        name=safe(row['path'])
        if not name.startswith(('resources/app/dsh/plugins/','resources/app/dsh/node_modules/')):
            raise ValueError('Unrecognized bundled application path '+name)
        if row.get('sha256') is not None and not re.fullmatch('[0-9a-f]{64}',row['sha256']):
            raise ValueError('Invalid browser artifact digest')
        if name in verified:
            raise ValueError('Duplicate bundle path '+name)
        target=STAGE/name
        baseline=OLD_STAGE/name
        assert target.resolve()==target and target.parent.resolve().is_relative_to(STAGE)
        old=row['previous_sha256']
        assert (digest(baseline) if baseline.is_file() else None)==old
        if row.get('input') is None:
            assert old and row.get('sha256') is None
            remove.append(name)
        else:
            artifact=(VENDORS/'browser'/row['sha256']).resolve(strict=True)
            assert artifact.is_file() and digest(artifact)==row['sha256']
        verified[name]=row
    for name,row in verified.items():
        target=STAGE/name
        fingerprint=digest(target) if target.is_file() else None
        if fingerprint==row.get('sha256'):continue
        assert fingerprint==row['previous_sha256']
        if name in remove:
            target.unlink()
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.with_name(target.name+'.new-release')
            with temp.open('xb') as dest,(VENDORS/'browser'/row['sha256']).open('rb') as src:
                shutil.copyfileobj(src,dest)
            os.replace(temp,target)
    assert verified
    save(HERE/'embedded-components.json',dict(source=str(manifest),source_sha256=digest(manifest),files=recorded['files'],
        paths=[n for n in verified if n not in remove],removed=remove))

if __name__=='__main__':apply(Path(sys.argv[1]).resolve(strict=True))
