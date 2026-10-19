"""Local synthetic business test; no company connection or package alteration."""
from pathlib import Path
import argparse,json,subprocess,time
HERE=Path(__file__).resolve().parent
IMAGE='sha256:cc7303e44347adbf8c2cbec80e45f72966f829f7e0bdda07878d33ee6495041d'
def run(*args,timeout=30,check=True):
 return subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=check)
def main(tree):
 assert run('docker','context','inspect','--format','{{json .Endpoints.docker.Host}}').stdout.strip()=='"unix:///var/run/docker.sock"'
 command=['docker','run','-d','--pull','never','--network','none','--add-host','r65-no-network:127.0.0.1','--read-only','--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges','--init','--pids-limit','512','--memory','1610612736','--memory-swap','1610612736','--cpus','1','--workdir','/workspace']
 for directory in ('state','workspace'):
  command+=['--tmpfs',f'/{directory}:rw,nosuid,nodev,size=128m,uid=1000,gid=1000']
 command+=['--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=128m,uid=1000,gid=1000']
 for directory in ('dsh','personal-cloud'):
  command+=['--mount',f'type=bind,source={tree/"opt"/directory},target=/opt/{directory},readonly']
 command+=['--mount',f'type=bind,source={HERE/"business.mjs"},target=/audit/business.mjs,readonly']
 for key,value in {'CLOUD_ROUTE_TOKEN':'A'*43,'CLOUD_MODEL_LEASE':'mcl_'+'B'*43,'CLOUD_MODEL_BASE_URL':'http://r65-no-network:8080/internal/personal-cloud/model/v1','CLOUD_MODEL_CATALOG_COUNT':'1','CLOUD_MODEL_CATALOG_0':json.dumps({'data':[{'id':'deepseek-v4.1-flash'}],'default_model':'deepseek-v4.1-flash'})}.items():
  command+=['--env',key+'='+value]
 command+=[IMAGE];cid=None;passed=False
 try:
  cid=run(*command).stdout.strip();began=time.monotonic()
  probe="fetch('http://127.0.0.1:3084/__cloud/ready',{headers:{Authorization:'Bearer '+process.env.CLOUD_ROUTE_TOKEN}}).then(r=>process.exit(r.status===200?0:2)).catch(()=>process.exit(3))"
  while time.monotonic()-began<90:
   if run('docker','exec',cid,'node','-e',probe,check=False,timeout=8).returncode==0:break
   time.sleep(.5)
  else:raise RuntimeError('WORKER_NOT_READY')
  print('WORKER_READY',round(time.monotonic()-began,2),flush=True)
  outcome=run('docker','exec',cid,'node','/audit/business.mjs',timeout=150,check=False)
  (HERE/'test-output.txt').write_text(outcome.stdout+outcome.stderr,encoding='utf-8')
  for name in ('business-result.json','business-checkpoints.json'):
   saved=run('docker','exec',cid,'node','-e',"process.stdout.write(require('fs').readFileSync('/tmp/"+name+"','utf8'))",check=False)
   if saved.returncode==0:(HERE/name).write_text(saved.stdout,encoding='utf-8')
  print(outcome.stdout,flush=True)
  passed=outcome.returncode==0 and json.loads((HERE/'business-result.json').read_text(encoding='utf-8')).get('status')=='PASSED'
 finally:
  if cid:
   logs=run('docker','logs',cid,check=False)
   (HERE/'worker.log').write_text((logs.stdout+logs.stderr).replace('A'*43,'[synthetic]').replace('mcl_'+'B'*43,'[synthetic]'),encoding='utf-8')
   run('docker','stop','--time','15',cid,timeout=20,check=False)
   run('docker','rm','-f',cid,check=False)
 if not passed:raise SystemExit(1)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--tree',type=Path,required=True);a=p.parse_args();main(a.tree.resolve())
