"""Explicit local synthetic Docker evidence. Never targets existing owners."""
import asyncio
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import uuid

from runtime_admission import admit_if_new,new_receipt_matches
from runtime_release import ReleaseDocker,prepare_release,activate_release,restore_release,NEW,OLD


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(30);self.sock.connect('/var/run/docker.sock')


async def api(method,path,body=None,missing_ok=False):
    def invoke():
        conn=UnixConnection('localhost',timeout=30)
        try:
            raw=None if body is None else json.dumps(body).encode()
            conn.request(method,path,body=raw,headers={'Content-Type':'application/json'})
            response=conn.getresponse();data=response.read(1024*1024)
            if response.status==404 and missing_ok:return None
            if response.status>=400:raise RuntimeError('LOCAL_DOCKER_API_'+str(response.status))
            return json.loads(data) if data else None
        finally:conn.close()
    return await asyncio.to_thread(invoke)


def run():
    if os.environ.get('MAIMI_R65_LOCAL_SYNTHETIC_DOCKER')!='1' or os.geteuid()!=0:
        raise RuntimeError('EXPLICIT_LOCAL_ROOT_TEST_REQUIRED')
    docker=ReleaseDocker();candidate=Path(__file__).resolve().parent
    helper=os.environ['MAIMI_R65_TEST_PYTHON_IMAGE'];worker=os.environ['MAIMI_R65_TEST_WORKER_IMAGE']
    old_worker=os.environ['MAIMI_R65_TEST_OLD_WORKER_IMAGE']
    if docker.active_service('gateway') or docker.active_service('personal-cloud-manager'):
        raise RuntimeError('EXISTING_LOCAL_PROJECT_ACTIVE_NO_MUTATION')
    namespace='r65-test-'+uuid.uuid4().hex[:12];owner=uuid.uuid4().hex;new_owner=uuid.uuid4().hex
    containers=[];source_names=[];results={};network=None
    with tempfile.TemporaryDirectory(prefix='maimi-r65-monday-') as tmp:
        root=Path(tmp);release=root/'release';restore=root/'restore';release.mkdir(mode=0o700);restore.mkdir(mode=0o700)
        try:
            gateway=docker.command('create','--label','com.docker.compose.project=megmeet-desktop-server',
                '--label','com.docker.compose.service=gateway','--entrypoint','python',helper,'-c','pass');containers.append(gateway)
            manager=docker.command('create','--label','com.docker.compose.project=megmeet-desktop-server',
                '--label','com.docker.compose.service=personal-cloud-manager','--env','PERSONAL_CLOUD_WORKER_RUNTIME='+OLD,
                '--env','PERSONAL_CLOUD_WORKER_IMAGE='+old_worker,'--env','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA='+'a'*64,
                '--env','PERSONAL_CLOUD_MANAGER_NAMESPACE='+namespace,
                '--entrypoint','python',helper,'-c','pass');containers.append(manager)
            for kind in ('state','files'):
                name=namespace+'-'+owner+'-'+kind;source_names.append(name)
                docker.command('volume','create','--label','com.megmeet.personal-cloud.manager='+namespace,
                    '--label','com.megmeet.personal-cloud.owner='+owner,name)
                seed="from pathlib import Path; import os; p=Path('/sample'); (p/'example.txt').write_text('old synthetic "+kind+"'); os.chown(p/'example.txt',1000,1000); os.chmod(p/'example.txt',0o600); os.chown(p,1000,1000); os.chmod(p,0o700)"
                docker.command('run','--rm','--network','none','--user','0:0','--mount','type=volume,src='+name+',dst=/sample',
                    '--entrypoint','python',helper,'-c',seed)
            plan=dict(schema='r65-offline-release-plan-v1',namespace=namespace,gateway_id=gateway,source_manager_id=manager,
                source_image_id=old_worker,source_release_sha='a'*64,target_image_id=worker,target_release_sha='b'*64,helper_image_id=helper)
            prepared=prepare_release(docker=docker,plan=plan,root=release)
            assert prepared['owners']==1
            # Reopen archive and actual manager snapshot admission use real code.
            check="import sys,json;sys.path.insert(0,'/candidate');from test_admission import load_manager;m=load_manager(PERSONAL_CLOUD_WORKER_IMAGE="+repr(worker)+",PERSONAL_CLOUD_MANAGER_NAMESPACE="+repr(namespace)+",PERSONAL_CLOUD_RUNTIME_RELEASE_SHA='"+'b'*64+"');m.image_id="+repr(worker)+";m.NATIVE021_NEW_OWNERS_DIR='/new';m.NATIVE021_RECEIPTS_DIR='/receipts';r=json.load(open('/receipts/"+owner+".json'));m.require_native021_history_receipt("+repr(owner)+",(m.image_id,r['state_volume']['name'],r['state_volume']['created_at']),(r['files_volume']['name'],r['files_volume']['created_at']));print('ADMITTED')"
            output=docker.command('run','--rm','--network','none','--user','0:0','--read-only',
                '--mount','type=bind,src='+str(candidate)+',dst=/candidate,readonly',
                '--mount','type=bind,src='+str(release/'owners')+',dst=/receipts,readonly',
                '--mount','type=bind,src='+str(release/'new-owners')+',dst=/new,readonly','--entrypoint','python',helper,'-B','-c',check)
            assert output=='ADMITTED';results['real_snapshot_manager_admission']=True
            network='r65-test-net-'+uuid.uuid4().hex[:16]
            docker.command('network','create','--internal',network)
            secret=root/'manager-secret';secret.write_text('synthetic-local-only-'+'x'*40);secret.chmod(0o600)
            target_manager=docker.command('create','--network','none','--user','0:0','--read-only',
                '--tmpfs','/tmp:rw,noexec,nosuid,size=16777216','--workdir','/candidate',
                '--label','com.docker.compose.project=megmeet-desktop-server',
                '--label','com.docker.compose.service=personal-cloud-manager',
                '--mount','type=bind,src='+str(candidate)+',dst=/candidate,readonly',
                '--mount','type=bind,src='+str(release)+',dst=/opt/personal-cloud/runtime-r65,readonly',
                '--mount','type=bind,src='+str(release/'new-owners')+',dst=/opt/personal-cloud/runtime-r65/new-owners',
                '--mount','type=bind,src='+str(secret)+',dst=/run/secrets/personal-cloud-manager,readonly',
                '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
                '--env','PERSONAL_CLOUD_MANAGER_NAMESPACE='+namespace,
                '--env','PERSONAL_CLOUD_WORKER_NETWORK='+network,'--env','PERSONAL_CLOUD_WORKER_RUNTIME='+NEW,
                '--env','PERSONAL_CLOUD_WORKER_IMAGE='+worker,'--env','PERSONAL_CLOUD_RUNTIME_RELEASE_SHA='+'b'*64,
                '--env','PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE=/opt/personal-cloud/runtime-r65/activation.json',
                '--env','PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR=/opt/personal-cloud/runtime-r65/owners',
                '--env','PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR=/opt/personal-cloud/runtime-r65/new-owners',
                '--entrypoint','python',helper,'-B','-m','uvicorn','manager:app','--host','127.0.0.1','--port','8092')
            containers.append(target_manager);docker.command('start',target_manager)
            for attempt in range(30):
                try:
                    if docker.manager_probe(target_manager).get('status')=='ready':break
                except Exception:pass
                time.sleep(.2)
            else:raise RuntimeError('SYNTHETIC_MANAGER_NOT_READY')
            active=activate_release(docker=docker,root=release,target_manager_id=target_manager)
            assert active['status']=='ACTIVE_INGRESS_STILL_STOPPED';results['real_manager_global_activation']=True
            # New account proof runs fixed Node helper in the actual target image.
            created=asyncio.run(admit_if_new(docker=api,owner=new_owner,namespace=namespace,image_id=worker,
                release_sha='b'*64,receipts_directory=release/'new-owners',snapshot_directory=release/'owners'))
            assert created;results['real_new_owner_empty_proof']=True
            again=asyncio.run(admit_if_new(docker=api,owner=new_owner,namespace=namespace,image_id=worker,
                release_sha='b'*64,receipts_directory=release/'new-owners',snapshot_directory=release/'owners'))
            assert again is False
            # Emulate newer runtime writes. Restore must not overwrite these bytes.
            for kind,name in zip(('state','files'),source_names):
                docker.command('run','--rm','--network','none','--user','1000:1000',
                    '--mount','type=volume,src='+name+',dst=/sample','--entrypoint','python',helper,'-c',
                    "from pathlib import Path;Path('/sample/example.txt').write_text('new synthetic "+kind+"')")
            docker.command('stop','--time','10',target_manager)
            restored=restore_release(docker=docker,root=release,stopped_target_manager_id=target_manager,destination=restore)
            assert restored['owners']==1 and restored['new_native_owners_preserved']==1
            mapping=json.loads((restore/'restore.json').read_text())
            for kind,name in zip(('state','files'),source_names):
                for v,expected in ((name,'new synthetic '+kind),(mapping['owners'][owner][kind]['name'],'old synthetic '+kind)):
                    data=docker.command('run','--rm','--network','none','--user','1000:1000','--read-only',
                        '--mount','type=volume,src='+v+',dst=/sample,readonly','--entrypoint','python',helper,'-c',
                        "from pathlib import Path;print(Path('/sample/example.txt').read_text())")
                    assert data==expected
            results['old_backup_content_restored']=True;results['newer_source_bytes_preserved']=True
            # Real manager config selects clones, then a uid1000 helper mounts
            # exactly those volume Sources and reads old content successfully.
            check="import sys,json;from types import SimpleNamespace;sys.path.insert(0,'/candidate');from test_admission import load_manager;m=load_manager(PERSONAL_CLOUD_WORKER_RUNTIME='0.2.0-rc.2',PERSONAL_CLOUD_WORKER_IMAGE="+repr(old_worker)+",PERSONAL_CLOUD_MANAGER_NAMESPACE="+repr(namespace)+",PERSONAL_CLOUD_RUNTIME_RELEASE_SHA='"+'a'*64+"',PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR='',PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR='',PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE='/opt/personal-cloud/runtime-r41/activation.json',PERSONAL_CLOUD_RESTORE_BINDINGS_FILE='/opt/personal-cloud/runtime-r65/restore.json');m.image_id="+repr(old_worker)+";m.RESTORE_BINDINGS_FILE='/restore/restore.json';m.runtime_activated=lambda:True;print(json.dumps(m.container_config("+repr(owner)+",SimpleNamespace(catalog={},lease='mcl_'+'L'*43),'R'*43)['HostConfig']['Mounts']))"
            output=docker.command('run','--rm','--network','none','--user','0:0','--read-only',
                '--mount','type=bind,src='+str(candidate)+',dst=/candidate,readonly',
                '--mount','type=bind,src='+str(restore)+',dst=/restore,readonly','--entrypoint','python',helper,'-B','-c',check)
            mounts=json.loads(output)
            assert [m['Source'] for m in mounts]==[mapping['owners'][owner][k]['name'] for k in ('state','files')]
            results['actual_manager_restore_mount_mapping']=True
        finally:
            for cid in reversed(containers):docker.command('rm','-f',cid)
            if network:docker.command('network','rm',network)
            # Only the unique namespace generated by this test is enumerated.
            for name in docker.owned_volumes(namespace):
                if docker.references(name):raise RuntimeError('TEST_CONTAINER_REFERENCE_LEFT')
                docker.command('volume','rm',name)
            results['test_volumes_remaining']=len(docker.owned_volumes(namespace))
    print(json.dumps(dict(schema='r65-monday-real-docker-v1',status='PASSED',company_requests=0,**results),sort_keys=True))

if __name__=='__main__':run()
