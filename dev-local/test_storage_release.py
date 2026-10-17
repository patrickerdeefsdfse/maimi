"""Owned local PostgreSQL rehearsal; creates and removes only a random fixture."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

if os.environ.get('WSL_DISTRO_NAME')!='MegmeetDev':raise SystemExit('local MegmeetDev only')
root=Path(__file__).resolve().parents[1]
name='megmeet-storage-test-'+secrets.token_hex(6)
label='com.megmeet.storage-test='+name
def docker(*a):return subprocess.check_output(['docker',*a],text=True).strip()
try:
    docker('run','-d','--name',name,'--label',label,'--network','bridge','--tmpfs','/var/lib/postgresql/data',
      '-e','POSTGRES_PASSWORD=synthetic-storage-only','-e','POSTGRES_DB=synthetic','megmeet-desktop-postgres:16-20260918')
    for _ in range(45):
        if subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
        time.sleep(1)
    else:raise RuntimeError('synthetic PostgreSQL not ready')
    ip=docker('inspect','--format','{{(index .NetworkSettings.Networks "bridge").IPAddress}}',name)
    redis_name=name+'-redis'
    docker('run','-d','--name',redis_name,'--label',label,'--network','bridge',
        'megmeet-desktop-redis:7-20260918','redis-server','--save','','--appendonly','no')
    redis_ip=docker('inspect','--format','{{(index .NetworkSettings.Networks "bridge").IPAddress}}',redis_name)
    env=dict(os.environ,IAM_TEST_DATABASE_URL=f'postgresql://postgres:synthetic-storage-only@{ip}:5432/synthetic',
        REDIS_TEST_URL=f'redis://{redis_ip}:6379/0')
    result=subprocess.run([sys.executable,'-m','pytest','-q',str(root/'services/gateway/tests'),*sys.argv[1:]],env=env,cwd=root/'services/gateway')
    code=result.returncode
finally:
    found=docker('ps','-aq','--filter','label='+label)
    if found:docker('rm','-f',*found.splitlines())
raise SystemExit(code)
