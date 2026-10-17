"""Private gateway authentication policy. SSH targets/credentials are never configured here."""
from dataclasses import dataclass,field
import os
from pathlib import Path
import re
import stat
from protocol import USERS,Rejected,decode,exact,require

def private_read(filename,maximum):
    path=Path(filename);uid=getattr(os,'geteuid',lambda:0)()
    require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)),'NOT_CONFIGURED',503)
    for parent in path.parents:
        info=parent.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0,uid} and not stat.S_IMODE(info.st_mode)&0o022,'NOT_CONFIGURED',503)
    before=path.stat();require(stat.S_ISREG(before.st_mode) and before.st_uid in {0,uid} and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==0o600 and 1<=before.st_size<=maximum,'NOT_CONFIGURED',503)
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
        actual=os.fstat(stream.fileno());raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    key=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns)
    require(key(before)==key(actual)==key(after) and len(raw)==before.st_size,'NOT_CONFIGURED',503)
    return raw

@dataclass(frozen=True)
class Registry:
    generation:int
    token:str=field(repr=False)
    allowed_users:tuple=tuple(sorted(USERS))
    def allowed(self,employee,action=None,host_id=None):
        require(employee in USERS and employee in self.allowed_users,'NOT_AUTHORIZED',403)

def load_registry(config_file='/run/secrets/maimi-service-broker.json'):
    try:
        value=decode(private_read(config_file,8192));exact(value,{'schema','enabled','policy_generation','token_file','allowed_users'})
        require(value['schema']=='maimi-service-broker-v2' and value['enabled'] is True,'NOT_CONFIGURED',503)
        require(type(value['policy_generation']) is int and 1<=value['policy_generation']<=2147483647,'NOT_CONFIGURED',503)
        path=value['token_file'];require(isinstance(path,str) and path.startswith('/run/secrets/') and '..' not in path.split('/'),'NOT_CONFIGURED',503)
        token=private_read(path,4096).decode('ascii').strip();require(re.fullmatch(r'[A-Za-z0-9_-]{32,512}',token),'NOT_CONFIGURED',503)
        users=value['allowed_users'];require(isinstance(users,list) and users and len(set(users))==len(users) and set(users)<=USERS,'NOT_CONFIGURED',503)
        return Registry(value['policy_generation'],token,tuple(users))
    except Rejected:raise
    except Exception:raise Rejected('NOT_CONFIGURED',503) from None
