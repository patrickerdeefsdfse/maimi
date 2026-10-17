"""Reviewed ephemeral Python program, sent over SSH stdin. No remote installation."""
import shlex

PROGRAM = r'''import hashlib,json,os,stat,sys,uuid
class Failure(Exception):pass
def need(ok,code):
 if not ok:raise Failure(code)
def directory(root,parts):
 need(isinstance(root,str) and root.startswith('/') and all(p not in ('','.','..') for p in root.split('/')[1:]),'PATH_NOT_ALLOWED')
 fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
 try:
  for part in root.split('/')[1:]+parts:
   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
  return fd
 except BaseException:os.close(fd);raise
def pathparts(value,cwd=False):
 need(isinstance(value,str) and 1<=len(value)<=512 and not any(ord(c)<32 or ord(c)==127 for c in value) and '\\' not in value,'PATH_NOT_ALLOWED')
 if cwd and value=='.':return []
 parts=value.split('/');need(all(p not in ('','.','..') and not p.startswith('.') for p in parts),'PATH_NOT_ALLOWED')
 need(all(p.lower() not in ('id_rsa','id_ed25519','authorized_keys','known_hosts') and not p.lower().endswith(('.pem','.key','.p12','.pfx')) for p in parts),'PATH_NOT_ALLOWED')
 return parts
def identity(v):return(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
def read_at(parent,name,limit):
 fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
 with os.fdopen(fd,'rb') as stream:
  before=os.fstat(stream.fileno());need(stat.S_ISREG(before.st_mode) and before.st_nlink==1,'PATH_NOT_ALLOWED');need(before.st_size<=limit,'FILE_TOO_LARGE')
  data=stream.read(limit+1);after=os.fstat(stream.fileno());need(identity(before)==identity(after) and len(data)==before.st_size,'FILE_CHANGED')
  return data,before
def work(v):
 action=v['action'];root=v['root'];a=v['arguments']
 if root=='__AUTHENTICATED_HOME__':
  import pwd
  root=pwd.getpwuid(os.getuid()).pw_dir
 if action=='run_command':
  fd=directory(root,pathparts(a['relative_cwd'],True));os.fchdir(fd);os.close(fd)
  os.execv('/bin/sh',['/bin/sh','-c',a['command']])
 parts=pathparts(a['relative_path']);parent=directory(root,parts[:-1]);name=parts[-1]
 try:
  if action=='read_file':
   data,info=read_at(parent,name,a['max_bytes']);text=data.decode('utf-8');need(not any(ord(c)<32 and c not in '\n\r\t' or ord(c)==127 for c in text),'PATH_NOT_ALLOWED')
   return {'content':text,'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data),'truncated':False}
  need(action=='write_file','ARGUMENTS_INVALID');expected=a['expected_sha256'];created=expected=='MISSING';old=None
  try:data,old=read_at(parent,name,32768)
  except FileNotFoundError:need(created,'FILE_NOT_FOUND')
  else:need(not created and hashlib.sha256(data).hexdigest()==expected,'FILE_CHANGED')
  data=a['content'].encode('utf-8');need(len(data)<=16384 and '\x00' not in a['content'],'ARGUMENTS_INVALID')
  temp='.maimi-write-'+uuid.uuid4().hex
  fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
  try:
   with os.fdopen(fd,'wb') as stream:
    if old is not None:os.fchmod(stream.fileno(),stat.S_IMODE(old.st_mode)&0o777)
    stream.write(data);stream.flush();os.fsync(stream.fileno())
   if created:
    # link is an atomic exclusive create; an existing file is never replaced.
    try:os.link(temp,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
    except FileExistsError:raise Failure('FILE_CHANGED')
   else:
    current=os.stat(name,dir_fd=parent,follow_symlinks=False)
    need(identity(current)==identity(old) and stat.S_ISREG(current.st_mode),'FILE_CHANGED')
    os.replace(temp,name,src_dir_fd=parent,dst_dir_fd=parent)
  finally:
   try:os.unlink(temp,dir_fd=parent)
   except FileNotFoundError:pass
   os.fsync(parent)
  return {'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data),'created':created}
 finally:os.close(parent)
try:
 raw=sys.stdin.buffer.read(50001);need(len(raw)<=50000,'ARGUMENTS_INVALID');v=json.loads(raw)
 result=work(v);print(json.dumps({'ok':True,'result':result},ensure_ascii=False,separators=(',',':')))
except FileNotFoundError:print('{"ok":false,"error_code":"FILE_NOT_FOUND"}')
except (NotADirectoryError,PermissionError):print('{"ok":false,"error_code":"PATH_NOT_ALLOWED"}')
except Failure as e:print(json.dumps({'ok':False,'error_code':str(e)}))
except Exception:print('{"ok":false,"error_code":"OPERATION_FAILED"}')
'''

COMMAND = '/usr/bin/python3 -I -S -c ' + shlex.quote(PROGRAM)

ENVIRONMENT_CHECKS = (
    ('os','uname -a; if test -r /etc/os-release; then head -c 2048 /etc/os-release; fi'),
    ('resources','getconf _NPROCESSORS_ONLN; free -m; df -Pm / /data 2>/dev/null'),
    ('runtimes','for c in python3 python node npm git gcc make; do command -v "$c" || :; done'),
    ('ntp','if command -v timedatectl >/dev/null 2>&1; then timedatectl show -p NTPSynchronized -p NTP -p Timezone; elif command -v chronyc >/dev/null 2>&1; then chronyc tracking; else exit 127; fi'),
    ('sftp','if command -v systemctl >/dev/null 2>&1; then systemctl is-active sshd.service ssh.service 2>/dev/null; else exit 127; fi'),
)
ORACLE_CHECKS = (
    ('oracle_tools','for c in sqlplus lsnrctl; do command -v "$c" || :; done'),
    ('oracle_processes',"ps -eo comm= | grep -E '^(ora_pmon_|tnslsnr)' | head -n 32"),
)
