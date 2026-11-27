"""Offline release and non-destructive restore building blocks.

These APIs do not stop company services or replace their containers. The outer
maintenance transaction must stop ingress and the old manager, remove owned
workers without removing volumes, and retain their identity in its journal.
No function is exposed as an employee RPC or a ready-to-run production CLI.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from runtime_admission import directory, read_receipt, write_receipt
from runtime_snapshot import DockerCLI, SnapshotRefused, prepare_owner_snapshot, LABEL, ARCHIVE_POLICY

OWNER_LABEL='com.megmeet.personal-cloud.owner'
OLD='0.2.0-rc.2'
NEW='0.2.1-rc.2'


class ReleaseRefused(RuntimeError): pass


def digest(path):
    p=Path(path)
    if p.is_symlink():raise ReleaseRefused('SYMLINK_BACKUP_REJECTED')
    hashed=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):hashed.update(chunk)
    return hashed.hexdigest()


class ReleaseDocker(DockerCLI):
    def service(self,cid):
        raw=self.command('inspect','--type','container','--format',
            '{"id":{{json .Id}},"running":{{json .State.Running}},"state":{{json .State.Status}},'
            '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}',cid)
        return json.loads(raw)

    def active_service(self,service):
        return self.command('ps','--no-trunc','--filter','label=com.docker.compose.project=megmeet-desktop-server',
            '--filter','label=com.docker.compose.service='+service,'--format','{{.ID}}').splitlines()

    def owned_volumes(self,namespace):
        return sorted(self.command('volume','ls','--filter','label='+LABEL+'='+namespace,'--format','{{.Name}}').splitlines())

    def image(self,image):
        # Match the manager's Engine /images/<ref>/json identity. Do not compare
        # a containerd multi-platform index ID with a platform-config digest.
        return json.loads(self.command('image','inspect','--format',
            '{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},'
            '"runtime":{{json (index .Config.Labels "com.megmeet.dsh.runtime")}}}',image))

    def manager_profile(self,cid):
        selected={'worker_image':'PERSONAL_CLOUD_WORKER_IMAGE','release':'PERSONAL_CLOUD_RUNTIME_RELEASE_SHA',
            'runtime':'PERSONAL_CLOUD_WORKER_RUNTIME','activation':'PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE',
            'snapshots':'PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR','new_owners':'PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR',
            'namespace':'PERSONAL_CLOUD_MANAGER_NAMESPACE'}
        fields=[]
        for key,env in selected.items():
            fields.append('"'+key+'":{{range .Config.Env}}{{if eq (index (split . "=") 0) "'+env+'"}}{{json (index (split . "=") 1)}}{{end}}{{end}}')
        return json.loads(self.command('inspect','--type','container','--format','{'+','.join(fields)+'}',cid))

    def source_profile(self,cid):
        selected={'worker_image':'PERSONAL_CLOUD_WORKER_IMAGE','release':'PERSONAL_CLOUD_RUNTIME_RELEASE_SHA',
            'runtime':'PERSONAL_CLOUD_WORKER_RUNTIME','namespace':'PERSONAL_CLOUD_MANAGER_NAMESPACE'}
        fields=[]
        for key,env in selected.items():
            fields.append('"'+key+'":{{range .Config.Env}}{{if eq (index (split . "=") 0) "'+env+'"}}{{json (index (split . "=") 1)}}{{end}}{{end}}')
        return json.loads(self.command('inspect','--type','container','--format','{'+','.join(fields)+'}',cid))

    def manager_probe(self,cid):
        code='''import json,os,urllib.request
from pathlib import Path
secret=Path(os.getenv('PERSONAL_CLOUD_MANAGER_SECRET_FILE','/run/secrets/personal-cloud-manager')).read_text().strip()
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
request=urllib.request.Request('http://127.0.0.1:8092/v1/status',headers={'Authorization':'Bearer '+secret})
with opener.open(request,timeout=3) as response:
 data=json.loads(response.read(8193))
 print(json.dumps({k:data.get(k) for k in ('status','worker_runtime','runtime_admission_enabled','active_hosts')}))
'''
        return json.loads(self.command('exec','-i',cid,'python','-I','-B','-',input=code,timeout=8))

    def create_restore_volume(self,name,namespace,owner,restore_id):
        if self.command('volume','ls','--filter','name=^'+name+'$','--format','{{.Name}}'):
            raise ReleaseRefused('RESTORE_VOLUME_ALREADY_EXISTS')
        self.command('volume','create','--label',LABEL+'='+namespace,'--label',OWNER_LABEL+'='+owner,
            '--label','com.megmeet.personal-cloud.restore='+restore_id,name)
        value=self.volume(name)
        if (value.get('name')!=name or (value.get('labels') or {}).get('com.megmeet.personal-cloud.restore')!=restore_id):
            raise ReleaseRefused('RESTORE_VOLUME_CREATION_RACE')
        return value

    def restore_helper(self,image,volumes,backup,mode):
        name='r65-restore-helper-'+uuid.uuid4().hex
        args=['run','--name',name,'--rm','-i','--network','none','--read-only','--user','0:0',
            '--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges:true','--memory','512m','--pids-limit','32','--init',
            '--mount','type=bind,src='+str(backup)+',dst=/snapshot,readonly','--entrypoint','python']
        if mode=='restore':args+=['--cap-add','CHOWN','--cap-add','FOWNER']
        for kind,v in volumes.items():
            args+=['--mount','type=volume,src='+v['name']+',dst=/restore-'+kind+',volume-nocopy'+(',readonly' if mode=='verify' else '')]
        try:return json.loads(self.command(*args,image,'-I','-B','-',mode,input=RESTORE_HELPER,timeout=1800))
        finally:
            try:self.command('rm','-f',name,timeout=20)
            except SnapshotRefused:pass


RESTORE_HELPER=ARCHIVE_POLICY+r'''
from pathlib import Path,PurePosixPath
import hashlib,json,os,stat,sys,tarfile
mode=sys.argv[1];result={}
for kind in ('state','files'):
 root=Path('/restore-'+kind);archive=Path('/snapshot')/(kind+'.tar.gz')
 if mode=='restore' and any(root.iterdir()):raise ValueError('DESTINATION_NOT_EMPTY')
 with tarfile.open(archive,'r:gz') as tar:
  members=tar.getmembers()
  if len(members)>1000000 or sum(m.size for m in members)>64*1024**3:raise ValueError('LIMIT')
  expected=set()
  for m in members:
   accepted_member(m,root,expected)
  if mode=='restore':
   def checked(m,destination):
    clean=tarfile.data_filter(m,destination)
    return clean.replace(uid=m.uid,gid=m.gid,mode=m.mode,uname=None,gname=None)
   tar.extractall(root,members=members,filter=checked)
   os.chown(root,1000,1000);os.chmod(root,0o700)
   # Persist regular data first, then directory entries from leaves to root.
   for m in members:
    if m.isfile() or m.islnk():
     fd=os.open(root/m.name,os.O_RDONLY|os.O_NOFOLLOW)
     try:os.fsync(fd)
     finally:os.close(fd)
   directories=[root/m.name for m in members if m.isdir()]
   for path in sorted(directories,key=lambda p:len(p.parts),reverse=True)+[root]:
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
  elif mode!='verify':raise ValueError('MODE')
  found=set()
  for parent,dirs,files in os.walk(root,followlinks=False):
   for name in [*dirs,*files]:found.add((Path(parent)/name).relative_to(root).as_posix())
  if found!=expected:raise ValueError('TREE_MISMATCH')
  for m in members:
   path=root/m.name;s=path.lstat()
   if s.st_uid!=m.uid or s.st_gid!=m.gid:raise ValueError('OWNER_MISMATCH')
   if not m.issym() and stat.S_IMODE(s.st_mode)!=m.mode:raise ValueError('MODE_MISMATCH')
   if m.issym():
    if not path.is_symlink() or os.readlink(path)!=m.linkname:raise ValueError('LINK_MISMATCH')
   elif m.isdir():
    if not stat.S_ISDIR(s.st_mode):raise ValueError('TYPE_MISMATCH')
   else:
    if not stat.S_ISREG(s.st_mode):raise ValueError('TYPE_MISMATCH')
    with tar.extractfile(m) as a,path.open('rb') as b:
     if hashlib.file_digest(a,'sha256').digest()!=hashlib.file_digest(b,'sha256').digest():raise ValueError('CONTENT_MISMATCH')
 with archive.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
 result[kind]={'archive_sha256':digest,'members':len(members),'content_verified':True}
print(json.dumps({'schema':'r65-restore-content-verification-v1','status':'VERIFIED','volumes':result},sort_keys=True))
'''


def check_plan(plan):
    if os.name!='posix':raise ReleaseRefused('HOST_LINUX_REQUIRED')
    if sys.version_info<(3,10):raise ReleaseRefused('HOST_PYTHON_310_REQUIRED')
    required={'schema','namespace','gateway_id','source_manager_id','source_image_id','source_release_sha',
        'target_image_id','target_release_sha','helper_image_id'}
    if set(plan)!=required or plan['schema']!='r65-offline-release-plan-v1':raise ReleaseRefused('PLAN_INVALID')
    if not re.fullmatch('[a-z0-9][a-z0-9-]{1,28}[a-z0-9]',plan['namespace']):raise ReleaseRefused('PLAN_INVALID')
    for key in ('gateway_id','source_manager_id','source_release_sha','target_release_sha'):
        if not re.fullmatch('[a-f0-9]{64}',plan[key]):raise ReleaseRefused('PLAN_INVALID')
    for key in ('source_image_id','target_image_id','helper_image_id'):
        if not re.fullmatch('sha256:[a-f0-9]{64}',plan[key]):raise ReleaseRefused('PLAN_INVALID')


def stopped(docker,cid,service):
    s=docker.service(cid)
    if (s.get('id')!=cid or s.get('running') is not False or s.get('state') not in ('created','exited')
            or s.get('project')!='megmeet-desktop-server' or s.get('service')!=service
            or docker.active_service(service)):
        raise ReleaseRefused('SERVICES_MUST_ALREADY_BE_QUIESCENT')


def inventory(docker,namespace):
    owners={}
    for name in docker.owned_volumes(namespace):
        match=re.fullmatch(re.escape(namespace)+'-([a-f0-9]{32})-(state|files)',name)
        if not match:raise ReleaseRefused('UNKNOWN_OR_RESTORED_VOLUME_IN_NAMESPACE')
        owner,kind=match.groups();v=docker.volume(name)
        if (v.get('name')!=name or not v.get('created_at') or (v.get('labels') or {}).get(LABEL)!=namespace
                or (v.get('labels') or {}).get(OWNER_LABEL)!=owner or docker.references(name)):
            raise ReleaseRefused('VOLUME_NOT_QUIESCENT_OR_OWNED')
        owners.setdefault(owner,{})[kind]={'name':name,'created_at':v['created_at']}
    if any(set(v)!={'state','files'} for v in owners.values()):raise ReleaseRefused('INCOMPLETE_OWNER_VOLUMES')
    return owners


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def create_directory(path):
    path.mkdir(mode=0o700)
    sync_directory(path.parent)


@contextmanager
def locked(root):
    import fcntl
    root=directory(root)
    fd=os.open(root/'.release.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ReleaseRefused('RELEASE_ALREADY_RUNNING') from None
        yield root
    finally:os.close(fd)


def prepare_release(*,docker,plan,root):
    check_plan(plan)
    docker.helper_python_version(plan['helper_image_id'])
    with locked(root) as root:
        if any(p.name!='.release.lock' for p in root.iterdir()):raise ReleaseRefused('EMPTY_RELEASE_DIRECTORY_REQUIRED')
        stopped(docker,plan['gateway_id'],'gateway');stopped(docker,plan['source_manager_id'],'personal-cloud-manager')
        if docker.source_profile(plan['source_manager_id'])!=dict(worker_image=plan['source_image_id'],
                release=plan['source_release_sha'],runtime=OLD,namespace=plan['namespace']):
            raise ReleaseRefused('SOURCE_MANAGER_SCOPE_MISMATCH')
        if docker.image(plan['source_image_id'])!={'id':plan['source_image_id'],'os':'linux','arch':'amd64','runtime':OLD}:
            raise ReleaseRefused('SOURCE_IMAGE_IDENTITY_MISMATCH')
        target=docker.image(plan['target_image_id'])
        if target!={'id':plan['target_image_id'],'os':'linux','arch':'amd64','runtime':NEW}:raise ReleaseRefused('TARGET_IMAGE_IDENTITY_MISMATCH')
        owners=inventory(docker,plan['namespace'])
        write_receipt(root/'plan.json',plan);write_receipt(root/'inventory.json',owners)
        for name in ('owners','new-owners','backups'):create_directory(root/name)
        for owner in sorted(owners):
            backup=root/'backups'/owner;create_directory(backup)
            prepare_owner_snapshot(docker=docker,owner=owner,namespace=plan['namespace'],
                manager_id=plan['source_manager_id'],image_id=plan['target_image_id'],
                release_sha=plan['target_release_sha'],helper_image=plan['helper_image_id'],
                backup_directory=backup,receipts_directory=root/'owners')
        stopped(docker,plan['gateway_id'],'gateway');stopped(docker,plan['source_manager_id'],'personal-cloud-manager')
        if inventory(docker,plan['namespace'])!=owners:raise ReleaseRefused('OWNER_SET_CHANGED')
        result={'schema':'r65-prepared-release-v1','status':'PREPARED_NOT_ACTIVE','owners':len(owners),
            'plan_sha256':digest(root/'plan.json'),'inventory_sha256':digest(root/'inventory.json')}
        write_receipt(root/'prepared.json',result)
        return result


def verify_backups(root,plan,owners):
    for owner,pair in owners.items():
        receipt=read_receipt(root/'owners'/(owner+'.json'));backup=root/'backups'/owner
        if (receipt.get('owner')!=owner or receipt.get('manager_namespace')!=plan['namespace']
            or receipt.get('source_runtime')!=OLD or receipt.get('target_runtime')!=NEW
            or receipt.get('image_id')!=plan['target_image_id'] or receipt.get('release_manifest_sha256')!=plan['target_release_sha']
            or receipt.get('state_volume')!=pair['state'] or receipt.get('files_volume')!=pair['files']):
            raise ReleaseRefused('BACKUP_SCOPE_MISMATCH')
        if (digest(backup/'verification.json')!=receipt['backup']['verification_receipt_sha256']
            or any(digest(backup/(kind+'.tar.gz'))!=receipt['backup'][kind+'_sha256'] for kind in ('state','files'))):
            raise ReleaseRefused('BACKUP_DIGEST_MISMATCH')


def read_prepared(root):
    plan=read_receipt(root/'plan.json');check_plan(plan)
    prepared=read_receipt(root/'prepared.json');owners=read_receipt(root/'inventory.json',maximum_bytes=4*1024*1024)
    if prepared!=dict(schema='r65-prepared-release-v1',status='PREPARED_NOT_ACTIVE',owners=len(owners),
            plan_sha256=digest(root/'plan.json'),inventory_sha256=digest(root/'inventory.json')):
        raise ReleaseRefused('PREPARATION_CHANGED')
    verify_backups(root,plan,owners)
    return plan,owners


def activation_payload(plan):
    return dict(schema='megmeet-runtime-activation-r65-v1',runtime=NEW,
        release_manifest_sha256=plan['target_release_sha'],image_id=plan['target_image_id'],status='ACTIVE',
        history_admission='OWNER_SNAPSHOT_RECEIPTS_REQUIRED',legacy_overlays=False)


def activate_release(*,docker,root,target_manager_id):
    if not re.fullmatch('[a-f0-9]{64}',target_manager_id):raise ReleaseRefused('TARGET_MANAGER_INVALID')
    with locked(root) as root:
        plan,owners=read_prepared(root)
        stopped(docker,plan['gateway_id'],'gateway')
        old=docker.service(plan['source_manager_id']);new=docker.service(target_manager_id)
        if old.get('running') is not False or new!={'id':target_manager_id,'running':True,'state':'running',
                'project':'megmeet-desktop-server','service':'personal-cloud-manager'}:
            raise ReleaseRefused('TARGET_MANAGER_STATE_INVALID')
        if docker.active_service('personal-cloud-manager')!=[target_manager_id]:raise ReleaseRefused('MANAGER_NOT_EXCLUSIVE')
        if docker.manager_profile(target_manager_id)!=dict(worker_image=plan['target_image_id'],release=plan['target_release_sha'],
                runtime=NEW,activation='/opt/personal-cloud/runtime-r65/activation.json',
                snapshots='/opt/personal-cloud/runtime-r65/owners',new_owners='/opt/personal-cloud/runtime-r65/new-owners',
                namespace=plan['namespace']):
            raise ReleaseRefused('TARGET_MANAGER_CONFIG_MISMATCH')
        if inventory(docker,plan['namespace'])!=owners:raise ReleaseRefused('OWNER_SET_CHANGED')
        expected=dict(status='ready',worker_runtime=NEW,runtime_admission_enabled=False,active_hosts=0)
        if docker.manager_probe(target_manager_id)!=expected:raise ReleaseRefused('TARGET_MANAGER_NOT_PREPARED')
        gate=root/'activation.json';payload=activation_payload(plan)
        encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
        intent=dict(schema='r65-activation-intent-v1',nonce=uuid.uuid4().hex,manager_id=target_manager_id,
            plan_sha256=digest(root/'plan.json'),activation_sha256=hashlib.sha256(encoded).hexdigest())
        # The target binding must be durable before a readable ACTIVE gate can
        # exist. Interrupted activation is deliberately not automatically retried.
        write_receipt(root/'activation-intent.json',intent)
        gate_fd=None
        try:
            # Keep the exact newly-created inode open until the completion
            # receipt is durable. O_EXCL failure never grants cleanup ownership.
            gate_fd=os.open(gate,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            view=memoryview(encoded)
            while view:
                written=os.write(gate_fd,view)
                if written<=0:raise OSError('ACTIVATION_WRITE_FAILED')
                view=view[written:]
            os.fsync(gate_fd);sync_directory(root)
            if docker.manager_probe(target_manager_id)!={**expected,'runtime_admission_enabled':True}:
                raise ReleaseRefused('ACTIVATION_NOT_OBSERVED')
            stopped(docker,plan['gateway_id'],'gateway')
            result=dict(schema='r65-release-activation-v1',status='ACTIVE_INGRESS_STILL_STOPPED',
                owners_prepared=len(owners),services_started=False,manager_id=target_manager_id)
            write_receipt(root/'activated.json',result)
            return result
        except Exception:
            if gate_fd is not None:
                actual=os.fstat(gate_fd)
                try:current=gate.lstat()
                except FileNotFoundError:current=None
                os.lseek(gate_fd,0,os.SEEK_SET);raw=os.read(gate_fd,len(encoded)+1)
                if (current is not None and (actual.st_dev,actual.st_ino)==(current.st_dev,current.st_ino)
                        and encoded.startswith(raw)):
                    # Invalidate this inode before moving it, so even a failed
                    # directory sync cannot leave a valid ACTIVE payload live.
                    os.ftruncate(gate_fd,0)
                    try:os.fsync(gate_fd)
                    finally:
                        gate.rename(root/('activation-failed-'+intent['nonce']+'.json'))
                        sync_directory(root)
                elif current is not None:
                    raise ReleaseRefused('ACTIVATION_REPLACED_RECOVERY_REQUIRED') from None
            raise
        finally:
            if gate_fd is not None:os.close(gate_fd)


def restore_release(*,docker,root,stopped_target_manager_id,destination):
    """Restore old snapshots to new names; preserve every post-upgrade source."""
    if sys.version_info<(3,10):raise ReleaseRefused('HOST_PYTHON_310_REQUIRED')
    root=directory(root);plan,_=read_prepared(root)
    docker.helper_python_version(plan['helper_image_id'])
    with locked(root) as root:
        plan,owners=read_prepared(root);destination=directory(destination)
        if any(destination.iterdir()) or destination==root or destination.is_relative_to(root):
            raise ReleaseRefused('EMPTY_SEPARATE_RESTORE_DIRECTORY_REQUIRED')
        stopped(docker,plan['gateway_id'],'gateway');stopped(docker,stopped_target_manager_id,'personal-cloud-manager')
        if (root/'activation-intent.json').exists():
            intent=read_receipt(root/'activation-intent.json')
            expected=dict(schema='r65-activation-intent-v1',nonce=intent.get('nonce'),manager_id=stopped_target_manager_id,
                plan_sha256=digest(root/'plan.json'),activation_sha256=hashlib.sha256(
                    json.dumps(activation_payload(plan),sort_keys=True,separators=(',',':')).encode()).hexdigest())
            if intent!=expected or not re.fullmatch('[a-f0-9]{32}',str(intent.get('nonce'))):
                raise ReleaseRefused('RESTORE_MANAGER_NOT_ACTIVATION_INTENT_TARGET')
        if (root/'activated.json').exists():
            activated=read_receipt(root/'activated.json')
            if activated!=dict(schema='r65-release-activation-v1',status='ACTIVE_INGRESS_STILL_STOPPED',
                    owners_prepared=len(owners),services_started=False,manager_id=stopped_target_manager_id):
                raise ReleaseRefused('RESTORE_MANAGER_NOT_ACTIVATED_TARGET')
        before=inventory(docker,plan['namespace'])
        if any(before.get(owner)!=pair for owner,pair in owners.items()):raise ReleaseRefused('SOURCE_VOLUME_CHANGED')
        if (root/'activation.json').exists():
            if read_receipt(root/'activation.json')!=activation_payload(plan):raise ReleaseRefused('ACTIVATION_SCOPE_CHANGED')
            (root/'activation.json').rename(root/('activation-before-restore-'+uuid.uuid4().hex+'.json'))
            sync_directory(root)
        restore_id=uuid.uuid4().hex[:16];restored={}
        write_receipt(destination/'restore-intent.json',dict(schema='r65-restore-intent-v1',restore_id=restore_id,
            source_plan_sha256=digest(root/'plan.json'),source_volumes_preserved=True))
        for owner,pair in owners.items():
            volumes={}
            for kind in ('state','files'):
                name=plan['namespace']+'-'+owner+'-restore-'+restore_id+'-'+kind
                value=docker.create_restore_volume(name,plan['namespace'],owner,restore_id)
                if (value.get('labels') or {}).get(OWNER_LABEL)!=owner or docker.references(name):raise ReleaseRefused('RESTORE_VOLUME_INVALID')
                volumes[kind]={'name':name,'created_at':value['created_at']}
            backup=root/'backups'/owner
            created=docker.restore_helper(plan['helper_image_id'],volumes,backup,'restore')
            checked=docker.restore_helper(plan['helper_image_id'],volumes,backup,'verify')
            if created!=checked or checked.get('status')!='VERIFIED':raise ReleaseRefused('RESTORE_CONTENT_MISMATCH')
            for kind,v in volumes.items():
                current=docker.volume(v['name'])
                if current['created_at']!=v['created_at'] or docker.references(v['name']):raise ReleaseRefused('RESTORE_VOLUME_CHANGED')
                if checked['volumes'][kind]['archive_sha256']!=digest(backup/(kind+'.tar.gz')):raise ReleaseRefused('RESTORE_CONTENT_MISMATCH')
            restored[owner]=volumes
        stopped(docker,plan['gateway_id'],'gateway');stopped(docker,stopped_target_manager_id,'personal-cloud-manager')
        # Original and newly-created native021 owners remain untouched. Do not
        # admit new native021 history into the old runtime during rollback.
        for pair in before.values():
            for v in pair.values():
                if docker.volume(v['name'])['created_at']!=v['created_at'] or docker.references(v['name']):raise ReleaseRefused('SOURCE_VOLUME_CHANGED')
        mapping=dict(schema='megmeet-runtime-restore-bindings-r65-v1',status='RESTORE_VERIFIED',runtime=OLD,
            manager_namespace=plan['namespace'],image_id=plan['source_image_id'],
            release_manifest_sha256=plan['source_release_sha'],owners=restored)
        write_receipt(destination/'restore.json',mapping)
        return {'status':'RESTORED_TO_NEW_VOLUMES_NOT_ACTIVATED','owners':len(restored),
            'source_volumes_modified':False,'services_started':False,'new_native_owners_preserved':len(before)-len(owners)}
