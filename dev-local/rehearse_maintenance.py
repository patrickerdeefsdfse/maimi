"""Exercise actual offline owned-volume capture; no employee or live stack data."""
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tarfile
import tempfile

if os.getenv('WSL_DISTRO_NAME')!='MegmeetDev':raise SystemExit('local rehearsal only')
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('maintenance',root/'ops/changsha-desktop/maintain.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
project='megmeet-backup-test-'+secrets.token_hex(5);namespace=project+'-personal'
names=[project+'-database',namespace+'-files']
image='megmeet-ai-gateway:personal-cloud-20260918-r6'
try:
    for name in names:
        m.docker('volume','create','--label','com.docker.compose.project='+project,name)
        m.docker('run','--rm','--network','none','--user','0:0','--mount',f'type=volume,source={name},target=/fixture',
            '--entrypoint','python',image,'-c',"from pathlib import Path; p=Path('/fixture/synthetic.txt'); p.write_text('owned backup fixture'); p.chmod(0o600)")
    with tempfile.TemporaryDirectory(prefix='megmeet-maintenance-') as folder:
        result=m.backup(project,namespace,folder,image)
        assert result['volumes']==2
        snapshot=Path(result['snapshot'])
        assert m.verify_snapshot(snapshot)['status']=='verified'
        for name in names:
            with tarfile.open(snapshot/(name+'.tar.gz')) as archive:
                assert archive.extractfile('./synthetic.txt').read()==b'owned backup fixture'
        print('OWNED_QUIESCENT_VOLUMES_BACKUP_AND_CONTENTS: PASS')
        target=snapshot/(names[0]+'.tar.gz')
        target.write_bytes(target.read_bytes()+b'changed')
        try:m.verify_snapshot(snapshot)
        except ValueError:pass
        else:raise AssertionError('tamper accepted')
        print('SNAPSHOT_TAMPER_REJECTED: PASS')
finally:
    for name in names:
        labels=json.loads(m.docker('volume','inspect','--format','{{json .Labels}}',name))
        assert labels.get('com.docker.compose.project')==project
        m.docker('volume','rm',name)
    print('OWNED_REHEARSAL_VOLUMES_REMOVED: PASS')
