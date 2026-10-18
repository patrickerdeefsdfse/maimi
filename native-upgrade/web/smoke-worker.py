"""Run the assembled worker on local Docker with synthetic identities, no network."""
from pathlib import Path
import argparse,json,subprocess,time,tempfile,re

HERE=Path(__file__).resolve().parent
IMAGE='sha256:cc7303e44347adbf8c2cbec80e45f72966f829f7e0bdda07878d33ee6495041d'

def run(*args,timeout=20,check=True):
    return subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=check)

def smoke(tree:Path,modules:Path|None=None,image_id=None):
    tree=tree.resolve()
    if run('docker','context','inspect','--format','{{json .Endpoints.docker.Host}}').stdout.strip()!='"unix:///var/run/docker.sock"':
        raise ValueError('LOCAL_DOCKER_REQUIRED')
    manifest=json.loads((tree/'candidate-manifest.json').read_text())
    if manifest['runtime']!='0.2.1-rc.2':raise ValueError('RUNTIME_MISMATCH')
    if image_id and (modules or not re.fullmatch(r'sha256:[a-f0-9]{64}',image_id)):raise ValueError('EXACT_IMAGE_WITHOUT_MODULE_OVERRIDE_REQUIRED')
    token='A'*43;lease='mcl_'+'B'*43
    debug_dir=tempfile.TemporaryDirectory(prefix='maimi-r65-smoke-')
    debug_worker=Path(debug_dir.name)/'worker.mjs'
    worker=(tree/'opt/personal-cloud/worker.mjs').read_text(encoding='utf-8')
    marker=" buffer='';if(!cookie)throw new Error('cloud native bootstrap timeout');"
    if worker.count(marker)!=1:raise ValueError('SMOKE_DIAGNOSTIC_ANCHOR_CHANGED')
    worker=worker.replace(marker," if(!cookie)console.error('SYNTHETIC_SMOKE_STARTUP',buffer.slice(-16000));"+marker)
    debug_worker.write_text(worker,encoding='utf-8');debug_worker.chmod(0o644)
    Path(debug_dir.name).chmod(0o755)
    command=['docker','run','-d','--pull','never','--network','none','--read-only','--user','1000:1000',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--init','--pids-limit','512',
        '--memory','1610612736','--memory-swap','1610612736','--cpus','1','--workdir','/workspace']
    for directory in ('state','workspace'):
        command+=['--tmpfs',f'/{directory}:rw,nosuid,nodev,size=128m,uid=1000,gid=1000']
    command+=['--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=128m,uid=1000,gid=1000']
    if not image_id:
        for directory in ('dsh','personal-cloud'):
            command+=['--mount',f'type=bind,source={tree / "opt" / directory},target=/opt/{directory},readonly']
        command+=['--mount',f'type=bind,source={debug_worker},target=/opt/personal-cloud/worker.mjs,readonly']
    if modules is not None:
        command+=['--mount',f'type=bind,source={modules.resolve()},target=/opt/dsh/node_modules,readonly']
    for key,value in {'CLOUD_ROUTE_TOKEN':token,'CLOUD_MODEL_LEASE':lease,
        'CLOUD_MODEL_BASE_URL':'http://r65-no-network:8080/internal/personal-cloud/model/v1',
        'CLOUD_MODEL_CATALOG_COUNT':'1','CLOUD_MODEL_CATALOG_0':json.dumps({'data':[{'id':'deepseek-v4.1-flash'}],'default_model':'deepseek-v4.1-flash'})}.items():
        command+=['--env',key+'='+value]
    command+=[image_id or IMAGE]
    cid=None
    try:
        cid=run(*command,timeout=30).stdout.strip();began=time.monotonic();ready=False
        js="fetch('http://127.0.0.1:3084/__cloud/ready',{headers:{Authorization:'Bearer '+process.env.CLOUD_ROUTE_TOKEN}}).then(async r=>{let d=await r.json();process.exit(r.status===200&&d.status==='ready'?0:2)}).catch(()=>process.exit(3))"
        while time.monotonic()-began<85:
            probe=run('docker','exec',cid,'node','-e',js,timeout=8,check=False)
            if probe.returncode==0:ready=True;break
            state=run('docker','inspect','--format','{{.State.Running}}',cid).stdout.strip()
            if state!='true':break
            time.sleep(.5)
        logs=run('docker','logs',cid,check=False)
        safe_logs=(logs.stdout+logs.stderr).replace(token,'[synthetic]').replace(lease,'[synthetic]')
        safe_logs=re.sub(r'([?&]token=)[A-Za-z0-9_-]+',r'\1[redacted]',safe_logs)
        (HERE/'local-startup.log').write_text(safe_logs,encoding='utf-8')
        if not ready:raise ValueError('NATIVE_STARTUP_FAILED_SEE_LOCAL_LOG')
        checks=r"""
const base='http://127.0.0.1:3084';
const headers={Authorization:'Bearer '+process.env.CLOUD_ROUTE_TOKEN};
const a=await fetch(base+'/__cloud/ready');if(a.status!==401)throw Error('unauthorized');
const b=await fetch(base+'/',{headers});const html=await b.text();if(b.status!==200||!html.includes('__DSH_BOOT__'))throw Error('native html');
const c=await fetch(base+'/__cloud/activity',{headers});if(c.status!==200)throw Error('task state');
const privateRoute=await fetch(base+'/api/megmeet-cloud/task-state',{headers});if(privateRoute.status!==404)throw Error('private route exposed');
for(const contentType of ['application/json','Application/JSON; charset=utf-8']){
 const rpcId=crypto.randomUUID();
 const d=await fetch(base+'/api/workspace/create',{method:'POST',headers:{...headers,'Content-Type':contentType},body:JSON.stringify({type:'client-request',rpcId,method:'workspace/create',payload:{args:{request:{path:'/tmp'}}}})});
 const reply=await d.json();
 if(reply.rpcId!==rpcId||reply.result?.ok!==false||!reply.result.error?.message.includes('云端工作区'))throw Error('workspace restriction');
}
console.log(JSON.stringify({unauthenticated_rejected:true,native_html:true,task_state:true,private_control_route_hidden:true,foreign_workspace_rejected:true,mixed_case_media_guard:true}));
"""
        checked=run('docker','exec',cid,'node','--input-type=module','-e',checks,timeout=30,check=False)
        if checked.returncode:
            (HERE/'local-check-error.log').write_text(checked.stderr.replace(token,'[synthetic]').replace(lease,'[synthetic]'),encoding='utf-8')
            raise ValueError('NATIVE_CHECK_FAILED_SEE_LOCAL_CHECK_ERROR')
        result=json.loads(checked.stdout)
        elapsed=round(time.monotonic()-began,3)
        run('docker','stop','--time','15',cid,timeout=20)
        state=json.loads(run('docker','inspect','--format','{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}}}',cid).stdout)
        if state['exit']==137 or state['oom']:raise ValueError('FORCED_EXIT')
        evidence={'schema':'dsh-r65-native-worker-local-smoke-v1','status':'PASSED','runtime':manifest['runtime'],
            'company_changed':False,'network':'none','credentials':'SYNTHETIC_ONLY','model_requests':0,
            'base_image':'LOCAL_SURROGATE_NOT_COMPANY_BASE','official_package_overwrites':0,
            'tested_image_id':image_id,'runtime_overlay_mounts':not bool(image_id),
            'init':True,'pids_limit':512,'memory_bytes':1610612736,'ready_seconds':elapsed,'checks':result,'stop':state}
        (HERE/'worker-smoke.json').write_text(json.dumps(evidence,indent=2)+'\n');print(json.dumps(evidence))
    finally:
        if cid:run('docker','rm','-f',cid,timeout=20,check=False)
        debug_dir.cleanup()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--tree',type=Path,required=True);p.add_argument('--modules',type=Path);p.add_argument('--image-id');args=p.parse_args();smoke(args.tree,args.modules,args.image_id)
