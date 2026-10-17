"""Read-only validation of the separately provisioned private factory policy.

Only file identity metadata leaves this module. No phone value or phone digest
is added to the release payload, transaction journal, command, or output.
"""
from copy import deepcopy
import json,os,re,stat
from pathlib import Path

HOST_FILE='/etc/megmeet/logistics/factory-phones.json'
TARGET_FILE='/run/secrets/maimi-logistics-factory-phones.json'
ENABLED_ENV='MAIMI_LOGISTICS_FACTORY_PHONE_POLICY_ENABLED'
ALIASES={'瑞欣特':'ruixinte','埃斯蒙特':'aisimengte','硕特科技':'shuote'}
IDENTITY_KEYS=('device','inode','size','mtime_ns','ctime_ns','uid','gid','mode','links')
class Stop(ValueError):pass

def require(value,code):
 if not value:raise Stop(code)

def normalized_phone(value,*,suffix=False):
 require(isinstance(value,str),'R52_PRIVATE_POLICY_FORMAT_INVALID')
 value=re.sub(r'[ \t-]','',value.strip())
 if suffix and re.fullmatch(r'[0-9]{4}',value,re.ASCII):return value
 for prefix in ('+86','0086','86'):
  if value.startswith(prefix):value=value[len(prefix):];break
 require(re.fullmatch(r'1[3-9][0-9]{9}',value,re.ASCII),'R52_PRIVATE_POLICY_FORMAT_INVALID')
 return value[-4:] if suffix else value

def canonical_policy(value):
 require(isinstance(value,dict) and set(value)=={'schema','factory_aliases','ruixinte_sender_phone','shuote_kuayue_phone'} and type(value['schema']) is int and value['schema']==1 and value['factory_aliases']==ALIASES,'R52_PRIVATE_POLICY_FORMAT_INVALID')
 return {'schema':1,'factory_aliases':dict(ALIASES),'ruixinte_sender_phone':normalized_phone(value['ruixinte_sender_phone'],suffix=True),'shuote_kuayue_phone':normalized_phone(value['shuote_kuayue_phone'])}

def strict_object(pairs):
 result={}
 for key,value in pairs:
  require(key not in result,'R52_PRIVATE_POLICY_FORMAT_INVALID');result[key]=value
 return result

def identity(info):
 return dict(zip(IDENTITY_KEYS,(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode),info.st_nlink)))

def valid_record(value):
 require(isinstance(value,dict) and set(value)=={'schema','path',*IDENTITY_KEYS} and value.get('schema')=='r52-private-policy-identity-v1' and value.get('path')==HOST_FILE,'R52_PRIVATE_POLICY_IDENTITY_INVALID')
 require(all(type(value[key]) is int and value[key]>=0 for key in IDENTITY_KEYS),'R52_PRIVATE_POLICY_IDENTITY_INVALID')
 require(value['uid']==value['gid']==10001 and value['mode']==0o600 and value['links']==1 and 0<value['size']<=8192,'R52_PRIVATE_POLICY_IDENTITY_INVALID')
 return value

def verify(expected=None):
 """No mutation. Both fresh checks and recovery validate the current bytes."""
 if expected is not None:valid_record(expected)
 path=Path(HOST_FILE)
 try:
  parents={}
  for parent in path.parents:
   info=parent.lstat()
   require(stat.S_ISDIR(info.st_mode) and info.st_uid==info.st_gid==0 and stat.S_IMODE(info.st_mode) in (0o700,0o755),'R52_PRIVATE_POLICY_PARENT_UNSAFE')
   parents[parent]=identity(info)
  before=path.lstat()
  require(stat.S_ISREG(before.st_mode) and before.st_uid==before.st_gid==10001 and stat.S_IMODE(before.st_mode)==0o600 and before.st_nlink==1 and 0<before.st_size<=8192,'R52_PRIVATE_POLICY_UNSAFE')
  flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK
  with os.fdopen(os.open(path,flags),'rb') as handle:
   opened=os.fstat(handle.fileno());raw=handle.read(8193);after=os.fstat(handle.fileno())
  require(identity(before)==identity(opened)==identity(after)==identity(path.lstat()) and len(raw)==before.st_size,'R52_PRIVATE_POLICY_CHANGED')
  for parent,info in parents.items():require(identity(parent.lstat())==info,'R52_PRIVATE_POLICY_PARENT_CHANGED')
  value=json.loads(raw,object_pairs_hook=strict_object);policy=canonical_policy(value)
  # The provisioning tool already reduces this first number to the necessary suffix.
  require(value==policy,'R52_PRIVATE_POLICY_FORMAT_INVALID')
  record=valid_record({'schema':'r52-private-policy-identity-v1','path':HOST_FILE,**identity(before)})
  require(expected is None or record==expected,'R52_PRIVATE_POLICY_CHANGED')
  return record
 except FileNotFoundError:raise Stop('R52_PRIVATE_POLICY_NOT_CONFIGURED') from None
 except Stop:raise
 except (OSError,ValueError,TypeError,UnicodeError):raise Stop('R52_PRIVATE_POLICY_UNSAFE') from None

def gateway_mount():
 return {'type':'bind','source':HOST_FILE,'target':TARGET_FILE,'read_only':True,'bind':{'create_host_path':False}}

def expected_gateway_policy(original):
 value=deepcopy(original);rows={row['destination']:row for row in value['mounts']}
 require(TARGET_FILE not in rows and ENABLED_ENV not in value['environment'],'R52_PRIVATE_POLICY_ALREADY_PRESENT')
 require(not any(TARGET_FILE.startswith(name.rstrip('/')+'/') for name in rows),'R52_PRIVATE_POLICY_PARENT_MOUNT_PRESENT')
 rows[TARGET_FILE]={'type':'bind','source':HOST_FILE,'destination':TARGET_FILE,'rw':False}
 value['mounts']=sorted(rows.values(),key=lambda row:row['destination']);value['environment'][ENABLED_ENV]='true'
 return value

def container_identity_code(expected):
 """Check the mounted inode without transmitting policy bytes or their hashes."""
 valid_record(expected)
 keys=tuple(IDENTITY_KEYS)
 values=tuple(expected[key] for key in keys)
 return 'import json,os,stat;from pathlib import Path;p=Path('+repr(TARGET_FILE)+');s=p.lstat();actual=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode),s.st_nlink);print(json.dumps({"matches":stat.S_ISREG(s.st_mode) and actual=='+repr(values)+'}))'
