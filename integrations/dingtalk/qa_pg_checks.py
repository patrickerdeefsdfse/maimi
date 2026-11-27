"""Run synthetic PostgreSQL and ASGI checks against the project's built gateway image."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parent
GATEWAY=os.environ.get("MAIMI_QA_GATEWAY_IMAGE","maimi-r105-upload-parity-gateway:20261104")
PG=os.environ.get("MAIMI_QA_POSTGRES_IMAGE","postgres:16")

def docker(*args,check=True):
    return subprocess.run(["docker",*args],capture_output=True,text=True,timeout=120,check=check)

if __name__=="__main__":
    name="maimi-im-fixture-"+uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="maimi-im-tests-") as tmp:
        root=Path(tmp)
        root.chmod(0o755)
        for pattern in ("*.py","*.sql","pair.*"):
            for src in ROOT.glob(pattern):
                shutil.copyfile(src,root/src.name)
        # The gateway image runs without root. Only the three synthetic result files
        # need to be writable by its worker, not the source directory.
        for report in ("postgres-validation.json","magic-ingress-validation.json","delegation-validation.json"):
            (root/report).touch(mode=0o600)
            (root/report).chmod(0o666)
        try:
            docker("run","-d","--pull=never","--name",name,"--network","none","--tmpfs","/var/lib/postgresql/data","-e","POSTGRES_HOST_AUTH_METHOD=trust",PG)
            for _ in range(30):
                if docker("exec",name,"pg_isready","-U","postgres",check=False).returncode==0: break
                time.sleep(1)
            else:raise RuntimeError("FIXTURE_DATABASE_UNAVAILABLE")
            for script in ("test_routing.py","test_identity_pg.py","test_ingress.py","test_delegation.py"):
                # -I omits script dir from sys.path; invoke runpy with the fixed fixture path.
                code="import runpy,sys;sys.path.insert(0,'/proof');runpy.run_path('/proof/"+script+"',run_name='__main__')"
                result=docker("run","--rm","--pull=never","--network","container:"+name,"--read-only","--cap-drop=ALL",
                    "--security-opt=no-new-privileges","-v",str(root)+":/proof","--entrypoint","python3",GATEWAY,"-I","-B","-c",code,check=False)
                print(script, result.stderr[-2200:])
                if result.returncode:raise SystemExit(result.returncode)
        finally:
            docker("rm","-f","-v",name,check=False)

