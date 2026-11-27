"""Narrow local-test shared-files provisioner. No delete, cleanup or HTTP API.

Only this independent component mounts the root-owned owners directory. Managers
create PVCs but have neither host filesystem access nor PV-write permission.
"""
from pathlib import Path
import sys
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import asyncio
import contextlib
import json
import os
import re
import stat
import time
import uuid
from local_files_contract import (BACKEND,PREFIX,NODE,NODE_UID,STORAGE_CLASS,STORAGE_CLASS_UID,
    INSTANCES,HOST_ROOT,MOUNT_ROOT,NODE_PATH,SC_PATH,LocalFilesError,need,is_uid,is_owner,
    claim,pv,verify_node,verify_class,verify_pv)
from k8s_worker_provider import KubernetesHTTPS
HEALTH=Path('/tmp/maimi-local-files-health.json')

def record(info):return [info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid]
def fd_record(fd):return record(os.fstat(fd))
def file_record(s):return (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def metadata(fd,owner,mode):
    s=os.fstat(fd)
    need(stat.S_ISDIR(s.st_mode) and s.st_uid==s.st_gid==owner and stat.S_IMODE(s.st_mode)==mode,
         'LOCAL_FILES_DIRECTORY_IDENTITY_REFUSED')
    return record(s)
def open_dir(parent,name):
    return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
def write_new(parent,name,value):
    data=(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
    need(len(data)<=16384,'LOCAL_FILES_RECORD_LIMIT')
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
    try:
        view=memoryview(data)
        while view:view=view[os.write(fd,view):]
        os.fsync(fd)
    finally:os.close(fd)
def read_json(parent,name):
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
    try:
        s=os.fstat(fd)
        need(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and stat.S_IMODE(s.st_mode)==0o600 and
             s.st_nlink==1 and 0<s.st_size<=16384,'LOCAL_FILES_RECORD_UNSAFE')
        data=b''
        while len(data)<=16384:
            block=os.read(fd,16385-len(data))
            if not block:break
            data+=block
        need(len(data)==s.st_size and file_record(os.fstat(fd))==file_record(s) and
             file_record(os.stat(name,dir_fd=parent,follow_symlinks=False))==file_record(s),
             'LOCAL_FILES_RECORD_CHANGED')
        value=json.loads(data)
        need(isinstance(value,dict),'LOCAL_FILES_RECORD_INVALID')
        return value
    finally:os.close(fd)

class Owners:
    def __init__(self,root,expected_root):
        import fcntl
        need(type(expected_root) in (tuple,list) and len(expected_root)==2 and
             all(type(x)is int and x>0 for x in expected_root),'LOCAL_FILES_ROOT_ID_REQUIRED')
        self.root=Path(root)
        need(self.root.is_absolute(),'LOCAL_FILES_ROOT_PATH_REQUIRED')
        for part in (*reversed(self.root.parents),self.root):
            info=part.lstat()
            need(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),'LOCAL_FILES_ROOT_PATH_UNSAFE')
        self.fd=os.open(str(root),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            self.identity=metadata(self.fd,0,0o700)
            need(self.identity[:2]==list(expected_root),'LOCAL_FILES_ROOT_CHANGED')
            self.fcntl=fcntl
        except BaseException:
            os.close(self.fd);raise
    def close(self):os.close(self.fd)
    def root_unchanged(self):
        need(metadata(self.fd,0,0o700)==self.identity and record(self.root.lstat())==self.identity,
             'LOCAL_FILES_ROOT_CHANGED')
    @contextlib.contextmanager
    def owner(self,name):
        need(is_owner(name),'LOCAL_FILES_OWNER_INVALID')
        try:self.fcntl.flock(self.fd,self.fcntl.LOCK_EX|self.fcntl.LOCK_NB)
        except BlockingIOError:raise LocalFilesError('LOCAL_FILES_ROOT_BUSY') from None
        owner=workspace=None
        try:
            self.root_unchanged()
            try:owner=open_dir(self.fd,name)
            except FileNotFoundError:
                # Publish an entirely initialized new owner atomically. Unknown
                # partial staging directories are preserved, never adopted.
                stage='.prepare-'+uuid.uuid4().hex
                os.mkdir(stage,0o700,dir_fd=self.fd)
                prepared=open_dir(self.fd,stage)
                try:
                    os.mkdir('workspace',0o700,dir_fd=prepared)
                    work=open_dir(prepared,'workspace')
                    try:
                        os.fchown(work,1000,1000)
                        identity={'schema':'maimi-r78-local-owner/v1','owner':name,'node':NODE,'node_uid':NODE_UID,
                            'root':self.identity,'owner_directory':metadata(prepared,0,0o700),
                            'workspace':metadata(work,1000,0o700)}
                    finally:os.close(work)
                    write_new(prepared,'identity.json',identity)
                    write_new(prepared,'claims.json',{'schema':'maimi-r78-local-claims/v1','owner':name,'claims':{}})
                    os.fsync(prepared)
                    os.rename(stage,name,src_dir_fd=self.fd,dst_dir_fd=self.fd)
                    os.fsync(self.fd)
                finally:os.close(prepared)
                owner=open_dir(self.fd,name)
            identity=read_json(owner,'identity.json')
            workspace=open_dir(owner,'workspace')
            need(identity=={'schema':'maimi-r78-local-owner/v1','owner':name,'node':NODE,'node_uid':NODE_UID,
                    'root':self.identity,'owner_directory':metadata(owner,0,0o700),
                    'workspace':metadata(workspace,1000,0o700)},'LOCAL_FILES_OWNER_RECORD_CHANGED')
            need(set(os.listdir(owner))=={'identity.json','claims.json','workspace'},'LOCAL_FILES_OWNER_ENTRIES_CHANGED')
            ledger=read_json(owner,'claims.json')
            need(ledger.get('schema')=='maimi-r78-local-claims/v1' and ledger.get('owner')==name and
                 isinstance(ledger.get('claims'),dict) and set(ledger['claims'])<=set(INSTANCES),
                 'LOCAL_FILES_LEDGER_REFUSED')
            yield owner,ledger
            need(metadata(workspace,1000,0o700)==identity['workspace'] and
                 record(os.stat('workspace',dir_fd=owner,follow_symlinks=False))==identity['workspace'] and
                 record(os.stat(name,dir_fd=self.fd,follow_symlinks=False))==identity['owner_directory'],
                 'LOCAL_FILES_OWNER_REPLACED')
            self.root_unchanged()
        finally:
            if workspace is not None:os.close(workspace)
            if owner is not None:os.close(owner)
            self.fcntl.flock(self.fd,self.fcntl.LOCK_UN)
    def save(self,fd,ledger):
        # Atomic replacement only of our exact root-owned metadata file. No user
        # workspace entry is read or removed.
        read_json(fd,'claims.json')
        temp='.claims-'+uuid.uuid4().hex
        write_new(fd,temp,ledger)
        os.replace(temp,'claims.json',src_dir_fd=fd,dst_dir_fd=fd)
        os.fsync(fd)

class BrokerAPI(KubernetesHTTPS):
    def validate_path(self,method,path):
        need(method in ('GET','POST'),'LOCAL_FILES_API_METHOD_REFUSED')
        if path in (NODE_PATH,SC_PATH):
            need(method=='GET','LOCAL_FILES_API_PATH_REFUSED');return
        if path=='/api/v1/persistentvolumes':
            need(method=='POST','LOCAL_FILES_API_PATH_REFUSED');return
        if re.fullmatch(r'/api/v1/persistentvolumes/r78-files-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',path):
            need(method=='GET','LOCAL_FILES_API_PATH_REFUSED');return
        for namespace in INSTANCES:
            base='/api/v1/namespaces/'+namespace+'/persistentvolumeclaims'
            if path==base+'?limit=200' or re.fullmatch(re.escape(base)+r'/mmw-[0-9a-f]{32}-files',path):
                need(method=='GET','LOCAL_FILES_API_PATH_REFUSED');return
        raise LocalFilesError('LOCAL_FILES_API_PATH_REFUSED')

class Provisioner:
    def __init__(self,api,owners):
        self.api,self.owners=api,owners
        self.lock=asyncio.Lock()
    async def get(self,path):
        status,value=await self.api.request('GET',path)
        need(status==200,'LOCAL_FILES_API_READ_FAILED')
        return value
    async def reconcile(self,value):
        async with self.lock:
            return await self._reconcile(value)
    async def _reconcile(self,value):
        descriptor=claim(value)
        base='/api/v1/namespaces/'+descriptor['namespace']+'/persistentvolumeclaims/'+descriptor['name']
        # A list response is only discovery. Bind the current exact claim UID.
        need(claim(await self.get(base))==descriptor,'LOCAL_FILES_CLAIM_CHANGED')
        verify_node(await self.get(NODE_PATH));verify_class(await self.get(SC_PATH))
        resource='/api/v1/persistentvolumes/'+descriptor['pv_name']
        with self.owners.owner(descriptor['owner']) as (fd,ledger):
            previous=ledger['claims'].get(descriptor['namespace'])
            if previous is None:
                status,_=await self.api.request('GET',resource)
                need(status==404,'LOCAL_FILES_UNREGISTERED_PV_REFUSED')
                pending={'claim':descriptor,'phase':'POST_INTENT','pv_uid':None}
                ledger['claims'][descriptor['namespace']]=pending
                self.owners.save(fd,ledger)
                # A lost response is resolved only by a later GET. Never repeat
                # the POST from an intent record and never delete an old PV.
                status,result=await self.api.request('POST','/api/v1/persistentvolumes',pv(descriptor))
                need(status==201,'LOCAL_FILES_PV_CREATE_UNCONFIRMED')
                pv_uid=verify_pv(result,descriptor)
            else:
                need(isinstance(previous,dict) and previous.get('claim')==descriptor and
                     previous.get('phase') in ('POST_INTENT','REGISTERED') and
                     (previous.get('pv_uid') is None if previous.get('phase')=='POST_INTENT' else is_uid(previous.get('pv_uid'))),
                     'LOCAL_FILES_CLAIM_UID_CHANGED')
                status,result=await self.api.request('GET',resource)
                need(status==200,'LOCAL_FILES_PV_OUTCOME_UNCONFIRMED')
                pv_uid=verify_pv(result,descriptor,previous.get('pv_uid'))
            need(claim(await self.get(base))==descriptor,'LOCAL_FILES_CLAIM_CHANGED')
            updated={'claim':descriptor,'phase':'REGISTERED','pv_uid':pv_uid}
            if ledger['claims'][descriptor['namespace']]!=updated:
                ledger['claims'][descriptor['namespace']]=updated;self.owners.save(fd,ledger)
            return {'status':'LOCAL_FILES_PV_VERIFIED','pv_uid':pv_uid}
    async def cycle(self):
        self.owners.root_unchanged()
        verify_node(await self.get(NODE_PATH));verify_class(await self.get(SC_PATH))
        count=0
        for namespace in INSTANCES:
            value=await self.get('/api/v1/namespaces/'+namespace+'/persistentvolumeclaims?limit=200')
            need(isinstance(value,dict) and isinstance(value.get('items'),list) and
                 len(value['items'])<=200 and not value.get('metadata',{}).get('continue'),
                 'LOCAL_FILES_LIST_PAGINATION_REQUIRED')
            for candidate in value['items']:
                m=candidate.get('metadata',{})
                if m.get('annotations',{}).get(PREFIX+'files-backend')!=BACKEND:continue
                if m.get('deletionTimestamp'):continue
                await self.reconcile(candidate);count+=1
        return count

def parse_root_identity(value):
    need(isinstance(value,str) and re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*',value),
         'LOCAL_FILES_ROOT_ID_REQUIRED')
    return [int(x) for x in value.split(':')]

def write_health(healthy):
    fd=os.open(HEALTH.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        name='.maimi-files-health-'+uuid.uuid4().hex
        write_new(fd,name,{'healthy':healthy,'observed_monotonic':time.monotonic()})
        os.replace(name,HEALTH.name,src_dir_fd=fd,dst_dir_fd=fd)
    finally:os.close(fd)

def health():
    try:
        fd=os.open(HEALTH.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:value=read_json(fd,HEALTH.name)
        finally:os.close(fd)
        elapsed=time.monotonic()-value.get('observed_monotonic',-1000)
        return value.get('healthy') is True and 0<=elapsed<=15
    except Exception:return False

async def serve():
    from k8s_bootstrap import RotatingTokenFile
    need(sys.platform=='linux' and os.geteuid()==0,'LOCAL_FILES_LINUX_ROOT_REQUIRED')
    write_health(False)
    configured=json.loads(os.environ.get('MAIMI_LOCAL_FILES_NAMESPACES_JSON',json.dumps(INSTANCES)))
    need(configured==INSTANCES,'LOCAL_FILES_NAMESPACES_REFUSED')
    root=parse_root_identity(os.environ.get('MAIMI_LOCAL_FILES_ROOT_ID',''))
    owners=Owners(MOUNT_ROOT,root)
    try:
        api=BrokerAPI('https://kubernetes.default.svc',
            '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt',
            RotatingTokenFile('/var/run/secrets/kubernetes.io/serviceaccount/token'),namespace='maimi-r78-app')
        provisioner=Provisioner(api,owners)
        while True:
            try:
                count=await provisioner.cycle()
                write_health(True)
                print(json.dumps({'schema':'maimi-r78-local-files-status/v1','status':'CYCLE_VERIFIED','claims':count}),flush=True)
            except Exception as exc:
                write_health(False)
                code=str(exc) if isinstance(exc,LocalFilesError) else type(exc).__name__
                if not re.fullmatch(r'[A-Za-z0-9_]{1,100}',code):code='LOCAL_FILES_CYCLE_FAILED'
                print(json.dumps({'schema':'maimi-r78-local-files-status/v1','status':'CYCLE_REFUSED','error':code}),flush=True)
            await asyncio.sleep(2)
    finally:owners.close()
if __name__=='__main__':
    if sys.argv[1:]==['--health']:raise SystemExit(0 if health() else 1)
    if sys.argv[1:]:raise SystemExit('LOCAL_FILES_ARGUMENTS_REFUSED')
    asyncio.run(serve())
