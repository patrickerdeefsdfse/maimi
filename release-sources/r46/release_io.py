"""Bounded local Docker control and durable journal writes; no raw output printing."""
from contextlib import contextmanager, ExitStack
import fcntl, json, os, selectors, signal, stat, subprocess, time, uuid
from pathlib import Path

ENV={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','DOCKER_HOST':'unix:///var/run/docker.sock'}
class Stop(ValueError):pass

def run(label,args,timeout=60):
 proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,start_new_session=True)
 selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ,True);selector.register(proc.stderr,selectors.EVENT_READ,False)
 out=bytearray();size=0;start=time.monotonic();notice=start;completed=False
 try:
  while selector.get_map():
   now=time.monotonic()
   if now-start>timeout:raise Stop('R46_COMMAND_TIMEOUT_'+label)
   if now-notice>=10:
    print(json.dumps({'status':'R46_RELEASE_WAITING','operation':label,'elapsed_seconds':int(now-start),'maximum_seconds':timeout}),flush=True);notice=now
   for key,_ in selector.select(min(1,max(0.01,timeout-(now-start)))):
    block=os.read(key.fd,65536)
    if not block:selector.unregister(key.fileobj);continue
    size+=len(block)
    if size>2*1024*1024:raise Stop('R46_COMMAND_OUTPUT_LIMIT')
    if key.data:out.extend(block)
  try:code=proc.wait(timeout=max(0.1,timeout-(time.monotonic()-start)))
  except subprocess.TimeoutExpired:raise Stop('R46_COMMAND_TIMEOUT_'+label) from None
  if code!=0:raise Stop('R46_COMMAND_FAILED_'+label)
  completed=True;return bytes(out)
 finally:
  selector.close()
  if not completed:
   # The direct child can exit while a descendant still holds either pipe.
   # Terminate the session we created, including that descendant.
   try:os.killpg(proc.pid,signal.SIGTERM)
   except ProcessLookupError:pass
   deadline=time.monotonic()+2
   while time.monotonic()<deadline:
    proc.poll()
    try:os.killpg(proc.pid,0)
    except ProcessLookupError:break
    time.sleep(0.02)
   try:os.killpg(proc.pid,signal.SIGKILL)
   except ProcessLookupError:pass
   proc.wait(timeout=2)
  proc.stdout.close();proc.stderr.close()

def safe_parent(path):
 path=Path(path)
 if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):raise Stop('R46_UNSAFE_WRITE_PATH')
 for p in path.parents:
  s=p.stat()
  if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:raise Stop('R46_UNSAFE_WRITE_PARENT')
 return path
def syncdir(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def write_new(path,raw):
 path=safe_parent(path);fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
 syncdir(path.parent)
def replace(path,raw):
 path=safe_parent(path)
 if path.exists():
  s=path.stat()
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_nlink!=1 or stat.S_IMODE(s.st_mode)&0o022:raise Stop('R46_UNSAFE_REPLACE')
 temporary=path.parent/('.r46-write-'+uuid.uuid4().hex)
 write_new(temporary,raw);os.replace(temporary,path);syncdir(path.parent)
def mkdir(path):
 path=safe_parent(path);path.mkdir(mode=0o700);syncdir(path.parent)
def unlink_marker(path):
 path=safe_parent(path);s=path.stat()
 if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_uid!=0:raise Stop('R46_UNSAFE_MARKER')
 path.unlink();syncdir(path.parent)
@contextmanager
def locked(paths):
 descriptors=[]
 try:
  for path in paths:
   path=safe_parent(path);fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);descriptors.append(fd)
   s=os.fstat(fd)
   if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:raise Stop('R46_UNSAFE_LOCK')
   try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise Stop('R46_RELEASE_BUSY')
  yield
 finally:
  for fd in reversed(descriptors):os.close(fd)
