"""One owned project-temp directory; no recursive delete or old-data cleanup."""
import json,os,stat
from pathlib import Path
ROOT=Path('/data/megmeet-project-temp')
TARGET='/var/tmp/megmeet-projects'
UID=GID=10001
class Stop(ValueError):pass

def checked(path=ROOT,*,create=False,intent=None,allow_pending=False):
 path=Path(path)
 if path!=ROOT or not path.is_absolute() or path.name!='megmeet-project-temp':raise Stop('R42_TEMP_PATH_OUTSIDE_SCOPE')
 if os.name!='posix':raise Stop('R42_TEMP_REQUIRES_LINUX')
 for parent in (*reversed(path.parents),):
  s=parent.lstat()
  if not stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode) or s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:raise Stop('R42_TEMP_PARENT_UNSAFE')
 flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
 parentfd=os.open(path.parent,flags)
 try:
  exists=os.path.lexists(path)
  if not exists:
   if not create:return {'exists':False,'path':str(path),'uid':UID,'gid':GID,'mode':'0700'}
   if intent is None:raise Stop('R42_TEMP_CREATE_INTENT_REQUIRED')
   intent(allow_create=True)  # Durable intent before mkdir.
   try:os.mkdir(path.name,0o700,dir_fd=parentfd)
   except FileExistsError:raise Stop('R42_TEMP_APPEARED_DURING_CREATE') from None
   os.fsync(parentfd)
  info=os.stat(path.name,dir_fd=parentfd,follow_symlinks=False)
  if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):raise Stop('R42_TEMP_NOT_DIRECTORY')
  fd=os.open(path.name,flags,dir_fd=parentfd)
  try:
   actual=os.fstat(fd)
   if (actual.st_dev,actual.st_ino)!=(info.st_dev,info.st_ino):raise Stop('R42_TEMP_CHANGED_DURING_OPEN')
   if (actual.st_uid,actual.st_gid,stat.S_IMODE(actual.st_mode))!=(UID,GID,0o700):
    # Resume only our empty root-created directory after a durable intent.
    if intent is None or (actual.st_uid,actual.st_gid,stat.S_IMODE(actual.st_mode))!=(0,0,0o700) or os.listdir(fd):raise Stop('R42_TEMP_PERMISSIONS_CHANGED')
    intent(allow_create=False)
    if not create:
     if not allow_pending:raise Stop('R42_TEMP_PREPARATION_PENDING')
     return {'exists':True,'preparation_pending':True,'path':str(path),'uid':0,'gid':0,'mode':'0700'}
    os.fchown(fd,UID,GID);os.fchmod(fd,0o700);os.fsync(fd)
   final=os.fstat(fd)
   if (final.st_uid,final.st_gid,stat.S_IMODE(final.st_mode))!=(UID,GID,0o700):raise Stop('R42_TEMP_PERMISSIONS_CHANGED')
   last=os.stat(path.name,dir_fd=parentfd,follow_symlinks=False)
   if (last.st_dev,last.st_ino)!=(final.st_dev,final.st_ino):raise Stop('R42_TEMP_CHANGED_DURING_CHECK')
   return {'exists':True,'path':str(path),'uid':UID,'gid':GID,'mode':'0700','device':final.st_dev,'inode':final.st_ino}
  finally:os.close(fd)
 finally:os.close(parentfd)
