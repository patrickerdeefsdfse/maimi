"""Offline native021 snapshot admission. No implicit stop, migration or restore.

Administrative invocation only. The manager must already be stopped, both named
volumes unreferenced, and destinations empty/root-owned. Docker helper containers
have no network and read source volumes only. A separate pass verifies archives
before producing the exact owner receipt consumed by manager.py.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid


LABEL='com.megmeet.personal-cloud.manager'
DIGEST=re.compile(r'^[a-f0-9]{64}$')
OWNER=re.compile(r'^[a-f0-9]{32}$')


class SnapshotRefused(RuntimeError):
    pass


HELPER=r'''
from pathlib import Path,PurePosixPath
import hashlib,json,os,posixpath,stat,sys,tarfile
mode=sys.argv[1]
output=Path('/snapshot')
limit=64*1024*1024*1024
results={}
for kind in ('state','files'):
    archive=output/(kind+'.tar.gz')
    if mode=='create':
        source=Path('/source-'+kind);total=0;count=0
        for root,dirs,files in os.walk(source,followlinks=False):
            for name in [*dirs,*files]:
                p=Path(root)/name;s=p.lstat();count+=1
                if count>1000000:raise ValueError('SNAPSHOT_FILE_LIMIT')
                if stat.S_ISLNK(s.st_mode):
                    if Path(os.readlink(p)).is_absolute() or not p.resolve().is_relative_to(source):raise ValueError('SNAPSHOT_EXTERNAL_LINK')
                elif stat.S_ISREG(s.st_mode):total+=s.st_size
                elif not stat.S_ISDIR(s.st_mode):raise ValueError('SNAPSHOT_SPECIAL_FILE')
                if total>limit:raise ValueError('SNAPSHOT_SIZE_LIMIT')
        with archive.open('xb') as dest,tarfile.open(fileobj=dest,mode='w:gz',compresslevel=3) as tar:
            for name in sorted(source.iterdir()):tar.add(name,arcname=name.name,recursive=True)
    elif mode!='verify':raise ValueError('SNAPSHOT_MODE_INVALID')
    count=0;total=0
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar:
            name=PurePosixPath(member.name);count+=1
            if name.is_absolute() or '..' in name.parts or not name.parts:raise ValueError('SNAPSHOT_MEMBER_INVALID')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):raise ValueError('SNAPSHOT_MEMBER_INVALID')
            if member.issym() or member.islnk():
                target=PurePosixPath(member.linkname)
                combined=PurePosixPath(posixpath.normpath(str(name.parent/target) if member.issym() else str(target)))
                if target.is_absolute() or combined.is_absolute() or '..' in combined.parts:raise ValueError('SNAPSHOT_EXTERNAL_LINK')
            if member.isfile():
                with tar.extractfile(member) as stream:
                    while chunk:=stream.read(1024*1024):total+=len(chunk)
            if count>1000000 or total>limit:raise ValueError('SNAPSHOT_LIMIT')
    with archive.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    results[kind]={'sha256':digest,'members':count,'uncompressed_bytes':total,'archive_bytes':archive.stat().st_size}
print(json.dumps({'schema':'native021-volume-snapshot-verification-v1','status':'VERIFIED','volumes':results},sort_keys=True))
'''


class DockerCLI:
    def command(self,*args,input=None,timeout=30):
        try:
            result=subprocess.run(['docker',*args],input=input,text=True,capture_output=True,timeout=timeout)
            if result.returncode:raise SnapshotRefused('DOCKER_STEP_FAILED')
            return result.stdout.strip()
        except (OSError,subprocess.TimeoutExpired):raise SnapshotRefused('DOCKER_STEP_FAILED') from None

    def manager(self,container):
        # Fixed projection; never collect environment, secrets, command or logs.
        value=self.command('inspect','--type','container','--format',
            '{"id":{{json .Id}},"running":{{json .State.Running}},"state":{{json .State.Status}},'
            '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
            '"source_runtime":"{{range .Config.Env}}{{if eq . "PERSONAL_CLOUD_WORKER_RUNTIME=0.2.0-rc.2"}}0.2.0-rc.2{{end}}{{end}}"}',container)
        return json.loads(value)

    def active_managers(self):
        return self.command('ps','--filter','label=com.docker.compose.project=megmeet-desktop-server',
            '--filter','label=com.docker.compose.service=personal-cloud-manager','--format','{{.ID}}').splitlines()

    def volume(self,name):
        value=self.command('volume','inspect','--format',
            '{"name":{{json .Name}},"created_at":{{json .CreatedAt}},"labels":{{json .Labels}}}',name)
        return json.loads(value)

    def references(self,name):
        return self.command('ps','-a','--filter','volume='+name,'--format','{{.ID}}').splitlines()

    def helper(self,image,volumes,destination,mode):
        name='maimi-r65-snapshot-'+uuid.uuid4().hex[:16]
        args=['run','--name',name,'--rm','-i','--network','none','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--memory','512m','--pids-limit','32','--init',
            '--user','0:0','--mount','type=bind,src='+str(destination)+',dst=/snapshot'+(',readonly' if mode=='verify' else ''),
            '--tmpfs','/tmp:rw,noexec,nosuid,size=16777216','--entrypoint','python']
        for kind,volume in volumes.items():
            args.extend(['--mount','type=volume,src='+volume['name']+',dst=/source-'+kind+',readonly'])
        try:
            return json.loads(self.command(*args,image,'-I','-B','-',mode,input=HELPER,timeout=1800))
        finally:
            # Bound cleanup also covers CLI timeouts; only this random name.
            try:self.command('rm','-f',name,timeout=20)
            except SnapshotRefused:pass


def protected_directory(path):
    path=Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
        raise SnapshotRefused('UNSAFE_DESTINATION')
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022:
        raise SnapshotRefused('UNSAFE_DESTINATION')
    return path


def write_exclusive(path,data):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600),'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())


def prepare_owner_snapshot(*,docker,owner,namespace,manager_id,image_id,release_sha,helper_image,
                           backup_directory,receipts_directory):
    if (not OWNER.fullmatch(owner) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,28}[a-z0-9]',namespace)
        or not DIGEST.fullmatch(manager_id) or not DIGEST.fullmatch(release_sha)
        or not re.fullmatch(r'sha256:[a-f0-9]{64}',image_id)
        or not re.fullmatch(r'sha256:[a-f0-9]{64}',helper_image)):
        raise SnapshotRefused('SNAPSHOT_IDENTITY_INVALID')
    backup=protected_directory(backup_directory)
    receipts=protected_directory(receipts_directory)
    if any(backup.iterdir()) or backup==receipts or backup.is_relative_to(receipts) or receipts.is_relative_to(backup):
        raise SnapshotRefused('EMPTY_DISTINCT_BACKUP_DIRECTORY_REQUIRED')
    target=receipts/(owner+'.json')
    if target.exists() or target.is_symlink():raise SnapshotRefused('OWNER_RECEIPT_ALREADY_EXISTS')

    def stopped():
        manager=docker.manager(manager_id)
        if (manager.get('id')!=manager_id or manager.get('running') is not False
            or manager.get('state') not in ('exited','created')
            or manager.get('project')!='megmeet-desktop-server' or manager.get('service')!='personal-cloud-manager'
            or manager.get('source_runtime')!='0.2.0-rc.2' or docker.active_managers()):
            raise SnapshotRefused('MANAGER_MUST_ALREADY_BE_STOPPED')
    def volume(kind):
        name=namespace+'-'+owner+'-'+kind
        value=docker.volume(name)
        if (value.get('name')!=name or not isinstance(value.get('created_at'),str) or not value['created_at']
            or (value.get('labels') or {}).get(LABEL)!=namespace
            or (value.get('labels') or {}).get('com.megmeet.personal-cloud.owner')!=owner):
            raise SnapshotRefused('VOLUME_OWNERSHIP_MISMATCH')
        if docker.references(name):raise SnapshotRefused('VOLUME_STILL_REFERENCED')
        return value

    stopped();volumes={kind:volume(kind) for kind in ('state','files')}
    created=docker.helper(helper_image,volumes,backup,'create')
    verified=docker.helper(helper_image,volumes,backup,'verify')
    if created!=verified or verified.get('schema')!='native021-volume-snapshot-verification-v1' or verified.get('status')!='VERIFIED':
        raise SnapshotRefused('SNAPSHOT_VERIFICATION_MISMATCH')
    if set(verified.get('volumes',{}))!={'state','files'}:raise SnapshotRefused('SNAPSHOT_VERIFICATION_MISMATCH')
    for kind in ('state','files'):
        info=verified['volumes'][kind]
        if not isinstance(info,dict) or not DIGEST.fullmatch(str(info.get('sha256',''))):raise SnapshotRefused('SNAPSHOT_VERIFICATION_MISMATCH')
        with (backup/(kind+'.tar.gz')).open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=info['sha256']:raise SnapshotRefused('SNAPSHOT_VERIFICATION_MISMATCH')
    stopped()
    if any(volume(kind)!=volumes[kind] for kind in ('state','files')):raise SnapshotRefused('SNAPSHOT_VOLUME_CHANGED')
    verification=json.dumps(verified,sort_keys=True,separators=(',',':')).encode()
    write_exclusive(backup/'verification.json',verification)
    receipt={'schema':'megmeet-runtime-owner-snapshot-r65-v1','status':'SNAPSHOT_VERIFIED',
        'owner':owner,'manager_namespace':namespace,'source_runtime':'0.2.0-rc.2','target_runtime':'0.2.1-rc.2',
        'release_manifest_sha256':release_sha,'image_id':image_id,
        'state_volume':{key:volumes['state'][key] for key in ('name','created_at')},
        'files_volume':{key:volumes['files'][key] for key in ('name','created_at')},
        'backup':{'state_sha256':verified['volumes']['state']['sha256'],'files_sha256':verified['volumes']['files']['sha256'],
                  'verification_receipt_sha256':hashlib.sha256(verification).hexdigest()}}
    write_exclusive(target,json.dumps(receipt,sort_keys=True,separators=(',',':')).encode())
    return {'schema':'r65-owner-snapshot-preparation-v1','status':'PREPARED_NOT_ACTIVATED',
            'services_changed':False,'source_volumes_modified':False,'activation_written':False,
            'owner_receipt_written':True,'backups_verified':2,'restore_performed':False}
