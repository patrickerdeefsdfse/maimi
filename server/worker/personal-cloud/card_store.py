"""Linux-only private card-v1 store; caller authenticates the trusted gateway permit.

This is not a public upload API. root is the owner's trusted DSH_HOME, never a
request path. Ordinary native attachments and workspace files are untouched.
Every operation takes the same flock. A read already admitted before expiry may
finish within the CLI deadline; collection retries BUSY instead of blocking users.
"""
from contextlib import contextmanager
import argparse, fcntl, hashlib, io, json, os, pathlib, re, select, stat, sys, time

TTL=172800
MAX_BYTES=128*1024*1024
MAX_REFS=64
MAX_HASH_BYTES=256*1024*1024
OWNER=re.compile(r'[a-f0-9]{32}')
SID=re.compile(r'cfs_[a-f0-9]{32}')
ARTIFACT=re.compile(r'pjf_[A-Za-z0-9_-]{32}')
SHA=re.compile(r'[a-f0-9]{64}')
UUID=re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')
FIELDS={'storage_kind','storage_id','owner_key','artifact_id','creation_job_id','sha256','bytes','name','media_type','created_at','expires_at'}
REF_FIELDS={'storage_id','sha256','bytes','expires_at'}

class StoreError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)
def require(value,code):
    if not value:raise StoreError(code)
def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()
def decoded(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            require(key not in result,'DUPLICATE_KEY');result[key]=value
        return result
    try:return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:require(False,'INVALID_JSON'))
    except StoreError:raise
    except (ValueError,UnicodeError,TypeError):raise StoreError('INVALID_JSON') from None
def integer(value):return type(value) is int
def identity(info):return {'dev':info.st_dev,'ino':info.st_ino,'bytes':info.st_size}

def descriptor(value,owner):
    require(isinstance(value,dict) and set(value)==FIELDS,'INVALID_DESCRIPTOR')
    require(value['storage_kind']=='card-v1' and OWNER.fullmatch(owner or '') and value['owner_key']==owner,'OWNER_MISMATCH')
    require(all(isinstance(value[key],str) for key in ('storage_id','artifact_id','creation_job_id','sha256')),'INVALID_DESCRIPTOR')
    require(SID.fullmatch(value['storage_id']) and ARTIFACT.fullmatch(value['artifact_id']) and UUID.fullmatch(value['creation_job_id']) and SHA.fullmatch(value['sha256']),'INVALID_DESCRIPTOR')
    require(integer(value['bytes']) and 1<=value['bytes']<=MAX_BYTES,'INVALID_BYTES')
    name=value['name'];media=value['media_type']
    require(isinstance(name,str) and 1<=len(name.encode())<=240 and name not in ('.','..') and not re.search(r'[\\/\x00-\x1f\x7f]',name),'INVALID_NAME')
    require(isinstance(media,str) and 1<=len(media)<=160 and not re.search(r'[\x00-\x20\x7f]',media),'INVALID_MEDIA')
    require(integer(value['created_at']) and integer(value['expires_at']) and value['created_at']>=0 and value['expires_at']==value['created_at']+TTL,'INVALID_EXPIRY')
    return dict(value)

class CardStore:
    def __init__(self,root,owner,*,clock=time.time,lock_wait=2,deadline_seconds=60,checkpoint=None):
        require(isinstance(owner,str) and OWNER.fullmatch(owner),'INVALID_OWNER')
        self.home=pathlib.Path(root);require(self.home.is_absolute(),'INVALID_ROOT')
        self.owner=owner;self.clock=clock;self.lock_wait=lock_wait
        self.deadline=time.monotonic()+deadline_seconds;self.checkpoint=checkpoint or (lambda _:None)
        self.uid=os.geteuid();self.root=self.home/'card-v1';self._hashed=0
        # Ancestors are traversal-checked, not created, and may be owned by root.
        for parent in reversed((self.home,*self.home.parents)):
            info=parent.lstat();require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),'UNSAFE_ROOT')
        require(self.home.stat().st_uid==self.uid and not stat.S_IMODE(self.home.stat().st_mode)&0o022,'UNSAFE_ROOT')

    def tick(self):require(time.monotonic()<self.deadline,'DEADLINE')
    def sync(self,path):
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    def directory(self,path,create=False):
        try:info=path.lstat()
        except FileNotFoundError:
            require(create,'MISSING_DIRECTORY')
            try:path.mkdir(mode=0o700);self.sync(path.parent)
            except FileExistsError:pass
            info=path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==self.uid and not stat.S_IMODE(info.st_mode)&0o077,'UNSAFE_DIRECTORY')
    def regular(self,path,*,single=False,missing=False,maximum=MAX_BYTES):
        try:info=path.lstat()
        except FileNotFoundError:
            if missing:return None
            raise StoreError('MISSING_FILE') from None
        require(stat.S_ISREG(info.st_mode) and info.st_uid==self.uid and not stat.S_IMODE(info.st_mode)&0o077 and info.st_size<=maximum and (not single or info.st_nlink==1),'UNSAFE_FILE')
        return info
    def read_json(self,path):
        before=self.regular(path,single=True,maximum=32768)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            require(identity(os.fstat(stream.fileno()))==identity(before),'FILE_CHANGED')
            raw=stream.read(32769)
        require(len(raw)<=32768,'METADATA_LIMIT');return decoded(raw)
    def write_json(self,path,value,*,new=False):
        raw=encoded(value);require(len(raw)<=32768,'METADATA_LIMIT')
        if not new:self.regular(path,single=True,missing=True,maximum=32768)
        temporary=path.parent/(path.name+'.write')
        if temporary.exists():
            self.regular(temporary,single=True,maximum=32768);temporary.unlink();self.sync(temporary.parent)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if new:
            # All namespace metadata writers hold the same flock. Rename gives
            # an atomic single-link record, including across process death.
            require(not os.path.lexists(path),'METADATA_ALREADY_EXISTS')
            os.rename(temporary,path)
        else:os.replace(temporary,path)
        self.sync(path.parent)

    @contextmanager
    def locked(self,*,create=False):
        if not self.root.exists() and not create:
            yield False;return
        self.directory(self.root,create=create)
        lock=self.root/'.lock'
        fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            self.regular(lock,single=True,maximum=0)
            until=time.monotonic()+self.lock_wait
            while True:
                self.tick()
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    require(time.monotonic()<until,'BUSY');time.sleep(0.02)
            owner_path=self.root/'owner.json'
            expected={'schema':'card-v1-owner','owner_key':self.owner}
            if not owner_path.exists():
                require(create and set(p.name for p in self.root.iterdir())<={'.lock','owner.json.write'},'OWNER_MARKER_MISSING')
                if (self.root/'owner.json.write').exists():
                    # With no published owner marker and no payload directories,
                    # a truncated first write is recoverable for this trusted
                    # volume owner. Valid foreign markers are never replaced.
                    try:require(self.read_json(self.root/'owner.json.write')==expected,'OWNER_MISMATCH')
                    except StoreError as error:
                        if error.code!='INVALID_JSON':raise
                self.write_json(owner_path,expected,new=True)
            require(self.read_json(owner_path)==expected,'OWNER_MISMATCH')
            for name in ('entries','objects','tmp'):self.directory(self.root/name,create=create)
            yield True
        finally:os.close(fd)

    def entry(self,sid,create=False):
        require(isinstance(sid,str) and SID.fullmatch(sid),'INVALID_STORAGE_ID')
        path=self.root/'entries'/sid
        if not path.exists() and not create:return None
        self.directory(path,create=create);return path
    def publish_entry(self,d):
        # No data or canonical link may exist before a complete durable record
        # is atomically published. A killed metadata write leaves only a private
        # staging directory, never an unreadable live entry.
        entry=self.entry(d['storage_id'])
        if entry is not None:return entry
        staged=self.root/'tmp'/(d['storage_id']+'.entry')
        self.directory(staged,create=True);self.checkpoint('save-entry-stage')
        require(set(p.name for p in staged.iterdir())<={'record.json','record.json.write'},'UNKNOWN_STAGING_CONTENT')
        pending={'schema':'card-v1-record','descriptor':d,'state':'PENDING','data_identity':None,'delete':None}
        if (staged/'record.json').exists():require(self.read_json(staged/'record.json')==pending,'RESERVATION_CHANGED')
        else:self.write_json(staged/'record.json',pending,new=True)
        self.checkpoint('save-entry-record')
        entry=self.root/'entries'/d['storage_id']
        os.rename(staged,entry);self.sync(entry.parent);self.sync(staged.parent)
        return entry
    def object(self,sha,create=False):
        require(isinstance(sha,str) and SHA.fullmatch(sha),'INVALID_SHA256')
        parent=self.root/'objects'/sha[:2]
        if not parent.exists() and not create:return parent/sha
        self.directory(parent,create=create);return parent/sha
    def load(self,entry):
        value=self.read_json(entry/'record.json')
        require(isinstance(value,dict) and set(value)=={'schema','descriptor','state','data_identity','delete'},'INVALID_RECORD')
        require(value['schema']=='card-v1-record' and value['state'] in ('PENDING','ACTIVE','DELETING','DELETED'),'INVALID_RECORD')
        value['descriptor']=descriptor(value['descriptor'],self.owner)
        require(value['descriptor']['storage_id']==entry.name,'INVALID_RECORD')
        return value
    def digest_file(self,path,expected):
        before=self.regular(path);require(before.st_size==expected['bytes'],'FILE_CHANGED')
        require(self._hashed+before.st_size<=MAX_HASH_BYTES,'HASH_BUDGET');self._hashed+=before.st_size
        digest=hashlib.sha256();fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            require(identity(os.fstat(stream.fileno()))==identity(before),'FILE_CHANGED')
            while True:
                self.tick();chunk=stream.read(65536)
                if not chunk:break
                digest.update(chunk)
            after=os.fstat(stream.fileno())
        require(identity(after)==identity(before) and digest.hexdigest()==expected['sha256'],'FILE_CHANGED')
        return before
    def response(self,d):
        return {'storage_kind':'card-v1','storage_id':d['storage_id'],'kind':'file',
            'ref':{'attachmentId':'sha256:'+d['sha256'],'name':d['name'],'bytes':d['bytes']},'expires_at':d['expires_at']}

    def save(self,value,source):
        d=descriptor(value,self.owner)
        require(d['created_at']<=int(self.clock())+60 and int(self.clock())<d['expires_at'],'EXPIRED')
        with self.locked(create=True):
            entry=self.publish_entry(d);record_path=entry/'record.json'
            if record_path.exists():
                record=self.load(entry);require(record['descriptor']==d,'RESERVATION_CHANGED')
                require(record['state'] in ('PENDING','ACTIVE'),'EXPIRED')
            else:
                record={'schema':'card-v1-record','descriptor':d,'state':'PENDING','data_identity':None,'delete':None}
                self.write_json(record_path,record,new=True)
            self.checkpoint('save-pending')
            temporary=self.root/'tmp'/(d['storage_id']+'.part')
            if temporary.exists():self.regular(temporary);temporary.unlink();self.sync(temporary.parent)
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                with os.fdopen(fd,'wb') as stream:
                    size=0;digest=hashlib.sha256()
                    while True:
                        self.tick();chunk=source.read(min(65536,d['bytes']-size+1))
                        if not chunk:break
                        require(isinstance(chunk,bytes),'INVALID_BODY');size+=len(chunk)
                        require(size<=d['bytes'],'BODY_LENGTH');digest.update(chunk);stream.write(chunk)
                    require(size==d['bytes'] and digest.hexdigest()==d['sha256'],'BODY_INTEGRITY')
                    require(int(self.clock())<d['expires_at'],'EXPIRED')
                    stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o400)
                obj=self.object(d['sha256'],create=True)
                try:os.link(temporary,obj,follow_symlinks=False);self.sync(obj.parent)
                except FileExistsError:self.digest_file(obj,d)
                self.checkpoint('save-object')
                data=entry/'data'
                if data.exists():
                    require(identity(self.regular(data))==identity(self.regular(obj)),'FILE_CHANGED')
                else:os.link(obj,data,follow_symlinks=False);self.sync(entry)
                record['data_identity']=identity(self.regular(data));self.checkpoint('save-reference')
                record['state']='ACTIVE';self.write_json(record_path,record);self.checkpoint('save-active')
                return self.response(d)
            finally:
                if temporary.exists():self.regular(temporary);temporary.unlink();self.sync(temporary.parent)

    def read(self,request,emit):
        require(isinstance(request,dict) and set(request)=={'storage_id','artifact_id'},'INVALID_READ')
        require(isinstance(request['artifact_id'],str) and ARTIFACT.fullmatch(request['artifact_id']),'INVALID_ARTIFACT_ID')
        with self.locked() as present:
            require(present,'NOT_FOUND');entry=self.entry(request['storage_id']);require(entry is not None,'NOT_FOUND')
            record=self.load(entry);d=record['descriptor']
            require(d['artifact_id']==request['artifact_id'],'ARTIFACT_MISMATCH')
            require(record['state']=='ACTIVE' and int(self.clock())<d['expires_at'],'EXPIRED')
            data=entry/'data';before=self.regular(data)
            require(identity(before)==record['data_identity'] and before.st_size==d['bytes'],'FILE_CHANGED')
            fd=os.open(data,os.O_RDONLY|os.O_NOFOLLOW);digest=hashlib.sha256();size=0
            with os.fdopen(fd,'rb') as stream:
                require(identity(os.fstat(stream.fileno()))==record['data_identity'],'FILE_CHANGED')
                emit(encoded({'status':'READY',**self.response(d),'media_type':d['media_type']}))
                while True:
                    self.tick();chunk=stream.read(65536)
                    if not chunk:break
                    size+=len(chunk);digest.update(chunk);emit(chunk)
            require(size==d['bytes'] and digest.hexdigest()==d['sha256'],'FILE_CHANGED')

    def _expire_one(self,ref):
        entry=self.entry(ref['storage_id'])
        if entry is None:return {'storage_id':ref['storage_id'],'status':'ABSENT','reclaimed_bytes':0}
        record=self.load(entry);d=record['descriptor']
        require(all(d[key]==ref[key] for key in REF_FIELDS),'REFERENCE_CHANGED')
        require(int(self.clock())>=d['expires_at'],'NOT_EXPIRED')
        if record['state']=='DELETED':return {'storage_id':ref['storage_id'],'status':'DELETED','reclaimed_bytes':0,'already_deleted':True}
        data=entry/'data';obj=self.object(d['sha256'])
        temporary=self.root/'tmp'/(d['storage_id']+'.part')
        if record['state']!='DELETING':
            info=self.regular(data,missing=True);object_info=self.regular(obj,missing=True)
            if info is not None:
                require(object_info is not None and identity(info)==identity(object_info),'FILE_CHANGED')
                if record['state']=='ACTIVE':require(identity(info)==record['data_identity'],'FILE_CHANGED')
                self.digest_file(data,d)
            else:require(record['state']=='PENDING','FILE_CHANGED')
            record['state']='DELETING'
            temporary_info=self.regular(temporary,missing=True)
            record['delete']={'data_identity':identity(info) if info else None,'object_identity':identity(object_info) if object_info else None,'temporary_identity':identity(temporary_info) if temporary_info else None,'reclaimed_bytes':0}
            self.write_json(entry/'record.json',record);self.checkpoint('delete-intent')
        intent=record['delete'];require(isinstance(intent,dict) and set(intent)=={'data_identity','object_identity','temporary_identity','reclaimed_bytes'},'INVALID_DELETE_INTENT')
        info=self.regular(data,missing=True)
        if info:
            require(identity(info)==intent['data_identity'],'FILE_CHANGED');data.unlink();self.sync(entry)
        self.checkpoint('delete-reference')
        # A hard kill after save-object bypasses save's finally. Remove this
        # reservation's authenticated staging link before the final-link test.
        temporary_info=self.regular(temporary,missing=True)
        if temporary_info:
            require(identity(temporary_info)==intent['temporary_identity'],'FILE_CHANGED')
            temporary.unlink();self.sync(temporary.parent)
        self.checkpoint('delete-temporary')
        current=self.regular(obj,missing=True)
        # A new save after a crash may have republished the same digest. Never
        # claim a different inode; new references keep it alive by hard links.
        if current and identity(current)==intent['object_identity'] and current.st_nlink==1:
            obj.unlink();self.sync(obj.parent);intent['reclaimed_bytes']=d['bytes']
        self.checkpoint('delete-object')
        record['state']='DELETED';self.write_json(entry/'record.json',record);self.checkpoint('delete-complete')
        return {'storage_id':ref['storage_id'],'status':'DELETED','reclaimed_bytes':intent['reclaimed_bytes'],'already_deleted':False}

    def expire(self,request):
        require(isinstance(request,dict) and set(request)=={'refs'} and isinstance(request['refs'],list) and 1<=len(request['refs'])<=MAX_REFS,'INVALID_EXPIRE')
        refs=request['refs'];seen=set()
        for ref in refs:
            require(isinstance(ref,dict) and set(ref)==REF_FIELDS and isinstance(ref['storage_id'],str) and SID.fullmatch(ref['storage_id']) and isinstance(ref['sha256'],str) and SHA.fullmatch(ref['sha256']),'INVALID_REFERENCE')
            require(ref['storage_id'] not in seen and integer(ref['bytes']) and 1<=ref['bytes']<=MAX_BYTES and integer(ref['expires_at']) and ref['expires_at']>=TTL,'INVALID_REFERENCE');seen.add(ref['storage_id'])
        with self.locked() as present:
            if not present:return {'status':'COMPLETE','items':[{'storage_id':r['storage_id'],'status':'ABSENT','reclaimed_bytes':0} for r in refs]}
            results=[]
            for ref in refs:
                try:self.tick();results.append(self._expire_one(ref))
                except StoreError as error:results.append({'storage_id':ref['storage_id'],'status':'DEFERRED','reason':error.code,'reclaimed_bytes':0})
            return {'status':'PARTIAL' if any(r['status']=='DEFERRED' for r in results) else 'COMPLETE','items':results}

class BoundedPipe:
    def __init__(self,fd,deadline):self.fd=fd;self.deadline=deadline;os.set_blocking(fd,False)
    def read(self,size):
        while True:
            remaining=self.deadline-time.monotonic();require(remaining>0,'DEADLINE')
            require(bool(select.select([self.fd],[],[],remaining)[0]),'DEADLINE')
            try:return os.read(self.fd,size)
            except BlockingIOError:continue
    def write(self,raw):
        while raw:
            remaining=self.deadline-time.monotonic();require(remaining>0,'DEADLINE')
            require(bool(select.select([],[self.fd],[],remaining)[1]),'DEADLINE')
            try:written=os.write(self.fd,raw[:65536]);raw=raw[written:]
            except BlockingIOError:continue

def main():
    parser=argparse.ArgumentParser(description='Private authenticated card-v1 storage helper')
    parser.add_argument('--root',required=True);parser.add_argument('--owner',required=True)
    parser.add_argument('--operation',required=True,choices=('save','read','expire'))
    args=parser.parse_args();deadline=time.monotonic()+60
    inp=BoundedPipe(0,deadline);out=BoundedPipe(1,deadline);header=bytearray();started=False
    def emit(raw):
        nonlocal started
        started=True;out.write(raw)
    try:
        while not header.endswith(b'\n'):
            require(len(header)<32768,'HEADER_LIMIT');chunk=inp.read(1);require(chunk,'MISSING_HEADER');header.extend(chunk)
        request=decoded(header)
        store=CardStore(args.root,args.owner,deadline_seconds=max(0.01,deadline-time.monotonic()))
        if args.operation=='save':result={'status':'SAVED',**store.save(request,inp)}
        elif args.operation=='read':store.read(request,emit);return 0
        else:result=store.expire(request)
        out.write(encoded(result));return 0 if result['status']!='PARTIAL' else 2
    except StoreError as error:
        # Read can have started streaming; the nonzero exit is authoritative and
        # callers must withhold the final chunk until successful process exit.
        if not started:out.write(encoded({'status':'ERROR','code':error.code}))
        return 2
    except (OSError,ValueError,TypeError):
        if not started:out.write(encoded({'status':'ERROR','code':'STORE_IO_REJECTED'}))
        return 2
if __name__=='__main__':raise SystemExit(main())
