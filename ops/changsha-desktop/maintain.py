#!/usr/bin/env python3
"""Local Linux maintenance. Metadata-only status and quiescent owned-volume backups."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

MANAGER_LABEL='com.megmeet.personal-cloud.manager'
COMPOSE_LABEL='com.docker.compose.project'
NAME=re.compile(r'^[a-z0-9][a-z0-9_-]{2,100}$')


def docker(*args):
    p=subprocess.run(['docker',*args],capture_output=True,text=True,check=False)
    if p.returncode:raise RuntimeError('Docker metadata operation failed; inspect service locally')
    return p.stdout.strip()


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()


def inventory(project,namespace):
    if not NAME.fullmatch(project) or not NAME.fullmatch(namespace):raise ValueError('Invalid owned namespace')
    containers={}
    volumes={}
    for label in (COMPOSE_LABEL+'='+project,MANAGER_LABEL+'='+namespace):
        for line in docker('ps','-a','--filter','label='+label,'--format','{{json .}}').splitlines():
            item=json.loads(line);containers[item['ID']]=item
        for name in docker('volume','ls','-q','--filter','label='+label).splitlines():
            if not NAME.fullmatch(name):raise ValueError('Unexpected volume name')
            # Only selected non-secret ownership fields are read, never full inspect.
            value=json.loads(docker('volume','inspect','--format','{{json .Labels}}',name)) or {}
            if value.get(COMPOSE_LABEL)!=project and value.get(MANAGER_LABEL)!=namespace:
                raise ValueError('Volume ownership changed')
            volumes[name]={key:val for key,val in value.items() if key in
                (COMPOSE_LABEL,'com.docker.compose.volume',MANAGER_LABEL,'com.megmeet.personal-cloud.owner')}
    return containers,volumes


def status(project,namespace,backup_root=None):
    containers,volumes=inventory(project,namespace)
    data_root=docker('info','--format','{{.DockerRootDir}}')
    disk=shutil.disk_usage(data_root)
    running=[v for v in containers.values() if v.get('State')=='running']
    unhealthy=sum('unhealthy' in v.get('Status','') for v in containers.values())
    warnings=[]
    if len(running)<5:warnings.append('required_application_services_stopped')
    if unhealthy:warnings.append('unhealthy_service')
    if disk.free<10*1024**3 or disk.free/disk.total<0.15:warnings.append('disk_space_low')
    backup_age=None
    if backup_root:
        manifests=list(Path(backup_root).glob('snapshot-*/manifest.json'))
        if manifests:backup_age=round((datetime.now(timezone.utc).timestamp()-max(p.stat().st_mtime for p in manifests))/3600,1)
        if backup_age is None or backup_age>48:warnings.append('backup_missing_or_older_than_48_hours')
    memory={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,_,value=line.partition(':')
        if key in ('MemTotal','MemAvailable'):memory[key]=int(value.split()[0])*1024
    if memory.get('MemAvailable',0)<512*1024**2:warnings.append('available_memory_low')
    return {'checked_at':datetime.now(timezone.utc).isoformat(),'running_services':len(running),
        'owned_volumes':len(volumes),'unhealthy_services':unhealthy,'disk_free_bytes':disk.free,
        'disk_total_bytes':disk.total,'memory':memory,'latest_backup_age_hours':backup_age,
        'warnings':warnings,'status':'attention' if warnings else 'healthy',
        'note':'metadata only; does not assert real IAM, model availability, or hard disk quota'}


def verify_snapshot(folder):
    folder=Path(folder).resolve()
    manifest=json.loads((folder/'manifest.json').read_text())
    if manifest.get('schema')!=1 or not manifest.get('volumes'):raise ValueError('Invalid snapshot manifest')
    for name,value in manifest['volumes'].items():
        if not NAME.fullmatch(name) or value.get('file')!=name+'.tar.gz':raise ValueError('Unsafe snapshot entry')
        file=folder/value['file']
        if file.is_symlink() or not file.is_file() or file.stat().st_size!=value['bytes'] or digest(file)!=value['sha256']:
            raise ValueError('Snapshot integrity differs')
    return {'status':'verified','volumes':len(manifest['volumes']),'credentials_included':False,
        'restore':'Use an empty isolated stack and retain original UID/GID; never overwrite a running volume.'}


def backup(project,namespace,destination,image):
    containers,volumes=inventory(project,namespace)
    if any(v.get('State') in ('running','restarting','paused') for v in containers.values()):
        raise RuntimeError('Backup requires a maintenance window: stop this application and its personal Hosts first; no service was stopped automatically')
    if not volumes:raise RuntimeError('No owned data volumes found')
    # Reject unrelated readers/writers as well as this application's stopped set.
    for name in volumes:
        if docker('ps','-q','--filter','volume='+name):raise RuntimeError('An owned volume is still mounted by a running container')
    dest=Path(destination).resolve();dest.mkdir(parents=True,exist_ok=True,mode=0o700)
    if dest.is_symlink():raise ValueError('Backup destination must be a real directory')
    data_root=Path(docker('info','--format','{{.DockerRootDir}}')).resolve()
    if dest==data_root or data_root in dest.parents:raise ValueError('Backup destination must be outside Docker data root')
    image_id=docker('image','inspect','--format','{{.Id}}',image)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',image_id):raise ValueError('Backup image missing')
    now=datetime.now(timezone.utc)
    folder=dest/('snapshot-'+now.strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(4).hex())
    folder.mkdir(mode=0o700)
    manifest={'schema':1,'created_at':now.isoformat(),'project':project,'namespace':namespace,
        'quiescent':True,'credentials_included':False,'volumes':{}}
    code="import tarfile,sys; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz',dereference=False); t.add('/source',arcname='.',recursive=True); t.close()"
    for name,labels in sorted(volumes.items()):
        # Recheck immediately before capture; deployment scripts must not run concurrently.
        if docker('ps','-q','--filter','volume='+name):raise RuntimeError('Volume became active; incomplete snapshot retained for diagnosis')
        target=folder/(name+'.tar.gz')
        with target.open('xb') as stream:
            result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--user','0:0',
                '--cap-drop','ALL','--cap-add','DAC_READ_SEARCH','--security-opt','no-new-privileges',
                '--mount',f'type=volume,source={name},target=/source,readonly',
                '--entrypoint','python',image_id,'-c',code],stdout=stream,stderr=subprocess.PIPE)
        if result.returncode:raise RuntimeError('Volume archive failed; snapshot was not marked complete')
        if docker('ps','-q','--filter','volume='+name):raise RuntimeError('Volume became active; snapshot was not marked complete')
        os.chmod(target,0o600)
        manifest['volumes'][name]={'file':target.name,'bytes':target.stat().st_size,'sha256':digest(target),'labels':labels}
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    os.chmod(folder/'manifest.json',0o600)
    verify_snapshot(folder)
    return {'status':'backed_up','snapshot':str(folder),'volumes':len(volumes),
        'next':'Copy the private snapshot and separately escrow service configuration/secrets to approved off-machine backup storage.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','backup','verify'])
    parser.add_argument('--project',default='megmeet-desktop-server')
    parser.add_argument('--namespace',default='megmeet-personal-server-v1')
    parser.add_argument('--destination',help='Private backup directory, preferably an off-machine mounted disk')
    parser.add_argument('--snapshot')
    parser.add_argument('--image',default='megmeet-ai-gateway:sso-20260921-r11')
    args=parser.parse_args()
    if os.name!='posix':raise RuntimeError('Run on the Linux application server')
    if args.action=='status':result=status(args.project,args.namespace,args.destination)
    elif args.action=='verify':result=verify_snapshot(args.snapshot or '')
    else:
        if not args.destination:raise ValueError('--destination is required')
        result=backup(args.project,args.namespace,args.destination,args.image)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 2 if result.get('status')=='attention' else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,ValueError,OSError) as error:
        print('MAINTENANCE_STOP: '+str(error),file=sys.stderr);raise SystemExit(1)
