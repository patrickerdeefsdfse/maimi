"""Stream only immutable candidate runtime paths to C; never duplicate a VHD tree."""
from pathlib import Path
import subprocess,sys,tarfile,re
image=sys.argv[1]
assert re.fullmatch(r'sha256:[0-9a-f]{64}',image)
out=Path('/mnt/c/Users/LI YUHAO/.codex/tmp/dsh020-r41-linux-delivery')
out.mkdir(parents=True,exist_ok=True)
dest=out/'runtime-linux-x64-020-rc2.tar.gz'
part=out/'runtime-linux-x64-020-rc2.tar.gz.partial'
cid=subprocess.check_output(['docker','create','--network','none','--entrypoint','true',image],text=True).strip()
try:
 proc=subprocess.Popen(['docker','export',cid],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 with tarfile.open(fileobj=proc.stdout,mode='r|') as src,tarfile.open(str(part),mode='w|gz',compresslevel=6) as tar:
  for member in src:
   name=member.name.lstrip('./')
   if not any(name==p or name.startswith(p+'/') for p in ('opt/dsh','opt/personal-cloud')):continue
   assert '..' not in Path(name).parts
   member.name=name
   tar.addfile(member,src.extractfile(member) if member.isfile() else None)
 assert proc.wait(timeout=30)==0,'EXPORT_FAILED'
 part.replace(dest)
finally:
 if 'proc' in locals() and proc.poll() is None:
  proc.kill();proc.wait(timeout=10)
 subprocess.run(['docker','rm',cid],check=True,stdout=subprocess.DEVNULL)
print({'status':'ARCHIVE_STREAMED','bytes':dest.stat().st_size})
