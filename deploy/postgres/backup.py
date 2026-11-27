"""Production logical backup plus private socket-only restore proof; no live writes."""
import argparse,fcntl,hashlib,json,os,pwd,re,shutil,socket,stat,subprocess,tempfile,time,uuid
from pathlib import Path
ROOT=Path('/data/maimi-pg-r96-backup')
MON=Path('/data/maimi-production/monitoring')
BIN=Path('/usr/lib/postgresql/16/bin')
SOURCE='/data/maimi-pg-r93/run'
SYSTEM='7702291738686796224'
HOSTS={'B1':'CSJD-DSH-DB01-P','B2':'CSJD-DSH-DB02-P'}
SQL="""SELECT json_build_object('system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
 'recovery',pg_is_in_recovery(),'version',current_setting('server_version_num')::int,
 'database_bytes',pg_database_size(current_database()),
 'migrations',(SELECT count(*) FROM platform.schema_migrations),
 'migration_fingerprint',(SELECT md5(string_agg(version::text,'|' ORDER BY version::text)) FROM platform.schema_migrations),
 'tables',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('platform','execution') AND c.relkind IN ('r','p')))
"""
class Failure(Exception):pass
def need(ok,code):
 if not ok:raise Failure(code)
def safe_dir(path,uid=0,mode=None):
 s=path.lstat();need(stat.S_ISDIR(s.st_mode) and not path.is_symlink() and s.st_uid==uid and not s.st_mode&0o022,'DIRECTORY_IDENTITY')
 if mode is not None:need(stat.S_IMODE(s.st_mode)==mode,'DIRECTORY_MODE')
 return s
def atomic(path,value):
 safe_dir(path.parent)
 if path.exists():
  s=path.lstat();need(stat.S_ISREG(s.st_mode) and not path.is_symlink() and s.st_uid==0,'RECEIPT_IDENTITY')
 data=(json.dumps(value,separators=(',',':'))+'\n').encode();fd,tmp=tempfile.mkstemp(prefix='.backup-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as f:os.fchmod(f.fileno(),0o600);f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.lexists(tmp):os.unlink(tmp)
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--role',choices=HOSTS,required=True);args=parser.parse_args();role=args.role
 need(os.geteuid()==0 and socket.gethostname()==HOSTS[role],'HOST_IDENTITY')
 safe_dir(ROOT);safe_dir(ROOT/'data');safe_dir(MON)
 who=pwd.getpwnam('postgres');uid,gid=who.pw_uid,who.pw_gid
 for name in ('psql','pg_dump','pg_dumpall','pg_restore','initdb','pg_ctl','postgres'):
  s=(BIN/name).lstat();need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,'PG_BINARY_METADATA')
 lock=os.open(ROOT/'backup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise Failure('BACKUP_ALREADY_RUNNING')
 transaction=uuid.uuid4().hex;work=ROOT/'data'/('backup-'+transaction)
 # The PostgreSQL user can enter its private transaction only; the root parent
 # has no writable permission for that user and no company share is mounted.
 os.mkdir(work,0o700);os.chown(work,uid,gid);job_stat=work.lstat()
 log=open(work/'commands.log','xb');os.chmod(work/'commands.log',0o600)
 started=False;phase='SOURCE_METADATA';receipt={'schema':'maimi-production-database-backup/v1','environment':'PRODUCTION','role':role,'database':'maimi','system_identifier':SYSTEM,
  'sampled_at':time.time(),'last_attempt_at':time.time(),'last_attempt_status':'failed','last_success_at':None,'restore_status':'unknown','restore_verified_at':None,'destination_scope':'local_same_host','archive_bytes':None,
  'transaction_id':transaction,'source_live_mutations':0,'business_rows_returned':False,'credentials_returned':False,'retention_count':3}
 prior_path=MON/'database-backup.json'
 if prior_path.exists():
  s=prior_path.lstat();need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022 and s.st_size<16384,'PRIOR_RECEIPT')
  prior=json.loads(prior_path.read_bytes());need(prior.get('system_identifier')==SYSTEM and prior.get('role')==role,'PRIOR_IDENTITY')
  receipt['last_success_at']=prior.get('last_success_at')
 def drop():
  os.setgroups([]);os.setgid(gid);os.setuid(uid);os.umask(0o077)
 def run(name,arguments,timeout=30,capture=False,output=None,read_only=False):
  env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','HOME':str(work),'TMPDIR':str(work)}
  if read_only:env['PGOPTIONS']='-c default_transaction_read_only=on -c statement_timeout=60000 -c lock_timeout=5000'
  p=subprocess.run([str(BIN/name)]+arguments,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE if capture else output or log,stderr=log,timeout=timeout,env=env,cwd=work,preexec_fn=drop)
  need(p.returncode==0,'PG_'+name.upper()+'_EXIT_'+str(p.returncode))
  if capture:need(len(p.stdout)<32768,'METADATA_LIMIT');return p.stdout
 def query(path,db='maimi',port='5432'):
  raw=run('psql',['-X','-w','-q','-A','-t','-v','ON_ERROR_STOP=1','-h',str(path),'-p',port,'-U','postgres','-d',db,'-c',SQL],capture=True,read_only=True)
  return json.loads(raw)
 restore=work/'restore';sock=work/'socket';archive=work/'maimi.dump';globals_file=work/'roles-without-passwords.sql'
 try:
  source=query(SOURCE);need(source['system_identifier']==SYSTEM and 160000<=source['version']<170000 and source['migrations']>=30,'SOURCE_DATABASE_IDENTITY')
  capacity=os.statvfs(ROOT);need(capacity.f_bavail*capacity.f_frsize>=max(2*1024**3,source['database_bytes']*4),'BACKUP_CAPACITY')
  receipt['source_recovery']=source['recovery'];receipt['source_metadata']=source
  phase='LOGICAL_DUMP'
  run('pg_dump',['-w','-h',SOURCE,'-U','postgres','-d','maimi','--format=custom','--compress=6','--lock-wait-timeout=5s','--file='+str(archive)],timeout=120,read_only=True)
  with globals_file.open('xb') as out:
   os.chmod(globals_file,0o600)
   run('pg_dumpall',['-w','-h',SOURCE,'-U','postgres','-l','postgres','--globals-only','--no-role-passwords'],timeout=30,output=out,read_only=True)
  need(archive.stat().st_size>0,'ARCHIVE_EMPTY');receipt['archive_bytes']=archive.stat().st_size
  receipt['archive_sha256']=hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()
  phase='ISOLATED_INIT'
  run('initdb',['-D',str(restore),'--auth-local=trust','--auth-host=reject','--no-locale','--encoding=UTF8'],timeout=30)
  os.mkdir(sock,0o700);os.chown(sock,uid,gid)
  phase='ISOLATED_START';started=True
  run('pg_ctl',['-D',str(restore),'-l',str(work/'restore-server.log'),'-w','-t','20','-o',f"-k {sock} -p 55432 -c listen_addresses='' -c max_connections=10 -c shared_buffers=32MB",'start'],timeout=25)
  phase='ISOLATED_RESTORE'
  run('psql',['-X','-w','-q','-v','ON_ERROR_STOP=1','-h',str(sock),'-p','55432','-U','postgres','-d','postgres','-c','CREATE DATABASE maimi_restore'],timeout=15)
  run('pg_restore',['-w','-h',str(sock),'-p','55432','-U','postgres','-d','maimi_restore','--exit-on-error','--no-owner','--no-acl','--no-tablespaces',str(archive)],timeout=120)
  restored=query(sock,'maimi_restore','55432');need(restored['system_identifier']!=SYSTEM,'RESTORE_MUST_NOT_BE_LIVE')
  need(all(restored[k]==source[k] for k in ('migrations','migration_fingerprint','tables')),'RESTORE_METADATA_MISMATCH')
  phase='ISOLATED_STOP'
  run('pg_ctl',['-D',str(restore),'-w','-t','20','-m','fast','stop'],timeout=25);started=False
  need(not (restore/'postmaster.pid').exists(),'RESTORE_STILL_RUNNING')
  after=query(SOURCE);need(after['system_identifier']==SYSTEM,'LIVE_IDENTITY_CHANGED')
  receipt.update(last_attempt_status='success',last_success_at=time.time(),restore_status='verified',restore_verified_at=time.time(),restore_metadata=restored,
   restore_scope='Separate private PostgreSQL cluster; logical schema/data restored, source never written; original owners/ACL retained in archive but not replayed in isolated proof',
   globals_scope='Role definitions without password hashes; runtime secrets are NOT exported and must be recovered from separate secret custody',source_recovery_after=after['recovery'])
  phase='RESTORE_PRIVATE_CLEANUP'
  need(work.lstat().st_ino==job_stat.st_ino and work.resolve().parent==ROOT/'data','JOB_PATH_CHANGED')
  need(restore.resolve()==work/'restore' and not restore.is_symlink() and not (restore/'postmaster.pid').exists(),'CLEANUP_SCOPE')
  # Only the private cluster created above is removed; the dump and proof remain.
  shutil.rmtree(restore);sock.rmdir()
  for f in work.iterdir():
   need(f.is_file() and not f.is_symlink(),'BACKUP_FILE_IDENTITY');os.chown(f,0,0);os.chmod(f,0o600)
  os.chown(work,0,0);os.chmod(work,0o700)
  receipt['sampled_at']=time.time();atomic(work/'proof.json',receipt);atomic(MON/'database-backup.json',receipt)
  phase='RETENTION'
  candidates=[]
  for old in (ROOT/'data').iterdir():
   if not re.fullmatch('backup-[0-9a-f]{32}',old.name) or old.is_symlink():continue
   try:
    safe_dir(old,0,0o700);proof=old/'proof.json';st=proof.lstat();need(stat.S_ISREG(st.st_mode) and st.st_uid==0 and st.st_size<16384,'PROOF_METADATA');v=json.loads(proof.read_bytes())
    need(v.get('role')==role and v.get('system_identifier')==SYSTEM and v.get('last_attempt_status')=='success' and v.get('restore_status')=='verified','PROOF_SCOPE')
    candidates.append((v['last_success_at'],old))
   except (OSError,ValueError,Failure):continue
  removed=0
  for _,old in sorted(candidates,reverse=True)[3:]:
   need(old.resolve().parent==ROOT/'data' and not old.is_symlink() and not (old/'restore').exists(),'RETENTION_SCOPE')
   shutil.rmtree(old);removed+=1
  print(json.dumps({'status':'BACKUP_AND_ISOLATED_RESTORE_VERIFIED','role':role,'transaction_id':transaction,'archive_bytes':receipt['archive_bytes'],'source_recovery':source['recovery'],'source_mutations':0,'restore_cluster_stopped':True,'private_restore_removed':True,'old_verified_backups_removed':removed,'backup_scope':'local_same_host'}))
 except BaseException as error:
  receipt.update(sampled_at=time.time(),phase=phase,error=str(error) if isinstance(error,Failure) else type(error).__name__,restore_status='failed' if phase.startswith('ISOLATED_') else 'unknown')
  stop_ok=not started
  if started:
   try:run('pg_ctl',['-D',str(restore),'-w','-t','20','-m','fast','stop'],timeout=25);stop_ok=True
   except Exception:pass
  receipt['isolated_cluster_stop_confirmed']=stop_ok
  atomic(MON/'database-backup.json',receipt)
  print(json.dumps({'status':'BACKUP_OR_RESTORE_FAILED','role':role,'phase':phase,'error':receipt['error'],'isolated_cluster_stop_confirmed':stop_ok,'source_mutations':0}))
  raise SystemExit(2)
 finally:log.close();os.close(lock)
if __name__=='__main__':main()
