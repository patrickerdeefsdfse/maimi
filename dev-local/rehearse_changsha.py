"""Isolated startup rehearsal for the company Compose, with synthetic data only.

Runs only in MegmeetDev. All networks are internal; no published ports or real
IAM/TokenKey request. Never invokes the fixed production project startup script.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

ROOT=Path(__file__).resolve().parents[1]
if os.environ.get('WSL_DISTRO_NAME')!='MegmeetDev' or os.geteuid()!=0:
    raise SystemExit('Only the owned local MegmeetDev rehearsal may run as root')
parser=argparse.ArgumentParser()
parser.add_argument('--release-directory',type=Path,help='already extracted release root; read only')
args=parser.parse_args()
SOURCE=(args.release_directory/'new-system') if args.release_directory else ROOT
MANIFEST=(args.release_directory/'images-manifest.json') if args.release_directory else ROOT/'releases/changsha-personal-cloud-20260918-r9/images-manifest.json'
PROJECT='megmeet-readiness-'+uuid4().hex[:12]
NAMESPACE='readiness-'+uuid4().hex[:12]
OWNER=uuid4().hex
ENV={'PATH':os.environ['PATH'],'DOCKER_HOST':'unix:///var/run/docker.sock'}
STAGE=Path(tempfile.mkdtemp(prefix='megmeet-readiness-'))
OPS=STAGE/'new-system/ops/changsha-desktop'
CLIENT=httpx.Client(timeout=100,trust_env=False)


def command(*args,check=True):
    value=subprocess.run(args,env=ENV,capture_output=True,text=True,timeout=240)
    if check and value.returncode:raise RuntimeError('command_failed:'+args[0]+':'+str(value.returncode))
    return value


def docker(*args):return command('docker',*args).stdout.strip()


def passed(name):print(name+': PASS',flush=True)


def compose(*args):return command('docker','compose','-p',PROJECT,'--env-file',str(OPS/'.env'),
    '-f',str(OPS/'compose.yaml'),'-f',str(OPS/'personal-cloud.compose.yaml'),
    '-f',str(STAGE/'rehearsal.yaml'),*args).stdout


def container(service):return compose('ps','--quiet',service).strip()


def ip(cid,network):
    return docker('inspect','--format','{{(index .NetworkSettings.Networks "'+network+'").IPAddress}}',cid)


def cleanup():
    # Only this random private manager namespace and Compose project may go.
    for cid in docker('ps','-aq','--filter','label=com.megmeet.personal-cloud.manager='+NAMESPACE).split():
        labels=json.loads(docker('inspect','--format','{{json .Config.Labels}}',cid))
        assert labels.get('com.megmeet.personal-cloud.manager')==NAMESPACE
        docker('rm','-f',cid)
    if (STAGE/'rehearsal.yaml').exists():compose('down','--volumes','--remove-orphans')
    for volume in docker('volume','ls','-q','--filter','label=com.megmeet.personal-cloud.manager='+NAMESPACE).split():
        assert re.fullmatch(re.escape(NAMESPACE)+r'-[a-f0-9]{32}-(state|files)',volume)
        docker('volume','rm',volume)
    assert not docker('ps','-aq','--filter','label=com.docker.compose.project='+PROJECT)
    assert not docker('volume','ls','-q','--filter','label=com.docker.compose.project='+PROJECT)
    assert not docker('ps','-aq','--filter','label=com.megmeet.personal-cloud.manager='+NAMESPACE)
    assert not docker('volume','ls','-q','--filter','label=com.megmeet.personal-cloud.manager='+NAMESPACE)
    passed('OWNED_REHEARSAL_RESOURCES_CLEANED')
    # Strictly the mkdtemp directory, never a caller-supplied path.
    if STAGE.parent==Path('/tmp') and STAGE.name.startswith('megmeet-readiness-'):shutil.rmtree(STAGE)


def run():
    source=SOURCE/'ops/changsha-desktop'
    shutil.copytree(source,OPS,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.env'))
    shutil.copytree(SOURCE/'db/migrations',STAGE/'new-system/db/migrations')
    manifest=json.loads(MANIFEST.read_text())
    for item in manifest['images']:
        got=docker('image','inspect','--platform','linux/amd64','--format','{{.Id}}',item['reference'])
        assert got==item['platform_image_id']
    gateway_images=[item['reference'] for item in manifest['images']
                    if re.fullmatch(r'megmeet-ai-gateway:personal-cloud-20260918-r[0-9]+',item['reference'])]
    if len(gateway_images)!=1:raise RuntimeError('release_gateway_reference_ambiguous')
    gateway_reference=gateway_images[0]
    passed('FIVE_RELEASE_IMAGE_IDENTITIES')
    spec=importlib.util.spec_from_file_location('rehearsal_config',OPS/'configure.py')
    config=importlib.util.module_from_spec(spec);spec.loader.exec_module(config)
    data={'POSTGRES_DB':'megmeet_rehearsal','POSTGRES_USER':'megmeet','POSTGRES_PASSWORD':secrets.token_hex(32),
        'IAM_BASE_URL':'https://iam-test.megmeet.com','IAM_AUTHORIZATION_BASE_URL':'https://iam-test.megmeet.com',
        'IAM_CLIENT_ID':'synthetic-client','IAM_DIRECTORY_API_KEY':'synthetic-directory-key',
        'IAM_DIRECTORY_ORG_URL':'https://example.invalid/org','IAM_DIRECTORY_ACCOUNT_URL':'https://example.invalid/account'}
    values={name:'synthetic-only' for name in config.FILES}
    values['IAM_DIRECTORY_SECRET_SOURCE']='synthetic-local-iam-directory-secret'
    values['IAM_TOKEN_ENCRYPTION_KEY_SOURCE']=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    values['PERSONAL_CLOUD_MANAGER_SECRET_SOURCE']=secrets.token_urlsafe(48)
    for name,filename in config.FILES.items():data[name]=str(STAGE/'private'/filename)
    config.validate(data,values);config.commit(OPS,data,values)
    directory_env='''
      IAM_DIRECTORY_ORG_URL: http://fixture:8081/esc-idm/api/v1/org/listAll
      IAM_DIRECTORY_ACCOUNT_URL: http://fixture:8081/esc-idm/api/v1/account/listAll
      IAM_DIRECTORY_ALLOW_HTTP: 'true'
'''
    fixture=ROOT/'dev-local/mock_company.py'
    (STAGE/'rehearsal.yaml').write_text(f'''services:
  gateway:
    ports: !reset []
    environment:{directory_env}
  iam-directory:
    environment:{directory_env}
    depends_on:
      fixture: {{condition: service_started}}
  personal-cloud-manager:
    environment:
      PERSONAL_CLOUD_MANAGER_NAMESPACE: {NAMESPACE}
      PERSONAL_CLOUD_WORKER_NETWORK: {PROJECT}_cloud-workers
      PERSONAL_CLOUD_MAX_ACTIVE_HOSTS: '1'
  fixture:
    image: {gateway_reference}
    networks: [outbound]
    user: '10001:10001'
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    command: [python, /fixture.py]
    volumes:
      - {fixture}:/fixture.py:ro
networks:
  outbound: {{internal: true}}
''')
    normalized=json.loads(compose('config','--format','json'))
    assert all(net.get('internal') is True for net in normalized['networks'].values())
    assert all(not service.get('ports') for service in normalized['services'].values())
    passed('NO_EGRESS_NO_PUBLISHED_PORTS_UNIQUE_PROJECT')
    compose('up','-d','--no-build','--pull','never','--wait','--wait-timeout','180')
    passed('FRESH_COMPANY_COMPOSE_ALL_SERVICES_READY')
    for net in normalized['networks'].values():assert json.loads(docker('network','inspect',net['name']))[0]['Internal']
    passed('ACTUAL_DOCKER_NETWORKS_INTERNAL')
    gateway=container('gateway');postgres=container('postgres');redis=container('redis')
    url='http://'+ip(gateway,PROJECT+'_data')+':8080'
    assert CLIENT.get(url+'/ready').json()['status']=='ready'
    version=docker('exec',postgres,'psql','-U','megmeet','-d','megmeet_rehearsal','-Atc',
        "select count(*) from platform.schema_migrations;")
    assert int(version)>=13
    passed('GATEWAY_READY_REAL_SCHEMA_MIGRATIONS')
    # Run SQL-dependent checks against this disposable database only, with no
    # published port, company identity, or persistent development table.
    test_env=dict(os.environ,IAM_TEST_DATABASE_URL='postgresql://megmeet:'+data['POSTGRES_PASSWORD']+'@'+ip(postgres,PROJECT+'_data')+':5432/megmeet_rehearsal')
    checked=subprocess.run([sys.executable,'-m','pytest',str(ROOT/'services/gateway/tests'),'-q','--disable-warnings','--maxfail=1'],env=test_env,timeout=180,capture_output=True,text=True)
    print(checked.stdout[-1800:],flush=True)
    assert checked.returncode==0, 'synthetic_database_tests_failed'
    passed('REAL_SYNTHETIC_POSTGRES_PERMISSION_HISTORY_PLUGIN_TESTS')
    docker('exec',postgres,'psql','-U','megmeet','-d','megmeet_rehearsal','-c',
        "create table public.readiness_marker(value text primary key);insert into public.readiness_marker values('synthetic');")
    docker('exec',redis,'redis-cli','SET','readiness:synthetic','preserved')
    namespace=PROJECT+'_cloud-workers'
    manager_url='http://'+ip(container('personal-cloud-manager'),namespace)+':8092'
    headers={'Authorization':'Bearer '+values['PERSONAL_CLOUD_MANAGER_SECRET_SOURCE']}
    models=[{'id':'deepseek-flash-v4.1' if i==0 else 'synthetic-model-'+str(i),
             'name':'合成模型 '+'x'*100} for i in range(1000)]
    body={'lease':'mcl_'+'a'*43,'expires_at':int(time.time())+850,
          'catalog':{'data':models,'default_model':'deepseek-flash-v4.1'}}
    def open_host():
        r=CLIENT.post(manager_url+'/v1/hosts/'+OWNER+'/open',headers=headers,json=body)
        assert r.status_code==200, 'manager_status_'+str(r.status_code)
        return r.json()
    initial=open_host();worker=NAMESPACE+'-'+OWNER
    count=docker('exec',worker,'node','-e',"const p=JSON.parse(require('node:fs').readFileSync('/state/profile.json','utf8'));console.log(p.find(x=>x.id==='llm-deepseek').config.models.length)")
    assert count=='1000'
    passed('REAL_NATIVE_1000_MODEL_CATALOG_OVER_128K_BOOT_WITHOUT_TRUNCATION')
    worker_id=docker('inspect','--format','{{.Id}}',worker)
    for catalog in ({'data':[{'id':'authorized-hot-model'}],'default_model':'authorized-hot-model'},
                    {'data':[],'default_model':None},body['catalog']):
        changed=CLIENT.post(manager_url+'/v1/hosts/'+OWNER+'/catalog',headers=headers,json={**body,'catalog':catalog})
        assert changed.status_code==200, 'catalog_status_'+str(changed.status_code)
        assert changed.json()['native_workspace_id']==initial['native_workspace_id']
        assert docker('inspect','--format','{{.Id}}',worker)==worker_id
    assert open_host()['native_workspace_id']==initial['native_workspace_id']
    assert docker('inspect','--format','{{.Id}}',worker)==worker_id
    passed('MANAGER_CATALOG_ADD_REVOKE_RESTORE_WITHOUT_CONTAINER_RECREATE')
    worker_url='http://'+ip(worker,namespace)+':3084'
    def native_rpc(method,args):
        response=CLIENT.post(worker_url+'/api/'+method,headers={'Authorization':'Bearer '+initial['route_token']},
          json={'type':'client-request','rpcId':str(uuid4()),'method':method,'payload':{'args':args}})
        assert response.status_code==200
        payload=response.json();assert payload['result']['ok'] is True
        return payload['result']['value']
    native_session=native_rpc('session/create',{'request':{'workspaceId':initial['native_workspace_id']}})['sessionId']
    native_rpc('session/rename',{'request':{'sessionId':native_session,'title':'Synthetic projection receipt'}})
    assert CLIENT.post(manager_url+'/v1/storage/'+OWNER,headers=headers,json={'action':'inventory'}).status_code==409
    assert CLIENT.post(manager_url+'/v1/storage/'+OWNER,json={'action':'inventory'}).status_code==401
    passed('ACTIVE_NATIVE_HISTORY_MAINTENANCE_AND_ANONYMOUS_REQUESTS_REJECTED')
    docker('exec',worker,'node','-e',"require('node:fs').writeFileSync('/workspace/readiness-marker.txt','synthetic')")
    passed('FROZEN_PERSONAL_NATIVE_WORKER_BOOT_AND_WRITE')
    compose('up','-d','--no-build','--pull','never','--force-recreate','--wait','--wait-timeout','180',
            'postgres','redis','iam-directory','gateway','retention-worker','personal-cloud-manager')
    assert docker('exec',container('postgres'),'psql','-U','megmeet','-d','megmeet_rehearsal','-Atc',
                  'select value from public.readiness_marker;')=='synthetic'
    assert docker('exec',container('redis'),'redis-cli','GET','readiness:synthetic')=='preserved'
    manager_url='http://'+ip(container('personal-cloud-manager'),namespace)+':8092'
    history=CLIENT.post(manager_url+'/v1/storage/'+OWNER,headers=headers,json={'action':'inventory'})
    if history.status_code!=200:
        code=history.json().get('detail','unknown')
        print('MAINTENANCE_ERROR_CODE:',code if re.fullmatch('[a-z_]{1,100}',code) else 'redacted',flush=True)
    assert history.status_code==200, 'offline_storage_status_'+str(history.status_code)
    snapshot=history.json()['result']
    selected=next(item for item in snapshot['sessions'] if item['sessionId']==native_session)
    assert not selected['cleanupEligible'] and snapshot['complete']
    assert any(e['type']=='session/title' and e['data']['title']=='Synthetic projection receipt' for e in selected['events'])
    script="import json,sys;from app.cloud_history import save_inventory;x=json.load(sys.stdin);save_inventory(x['owner'],'SYNTHETIC-ONLY',x['snapshot']);print('STRUCTURED_NATIVE_HISTORY_COMMITTED: PASS')"
    stored=subprocess.run(['docker','exec','-i',container('gateway'),'python','-c',script],env=ENV,
      input=json.dumps({'owner':OWNER,'snapshot':snapshot}),capture_output=True,text=True,timeout=30)
    assert stored.returncode==0 and 'STRUCTURED_NATIVE_HISTORY_COMMITTED: PASS' in stored.stdout
    assert docker('exec',container('postgres'),'psql','-U','megmeet','-d','megmeet_rehearsal','-Atc',
      "select count(*) from platform.personal_cloud_history where owner_key='"+OWNER+"' and title='Synthetic projection receipt';")=='1'
    passed('REAL_IDLE_NATIVE_INVENTORY_TO_POSTGRES_STRUCTURED_HISTORY')
    assert CLIENT.post(manager_url+'/v1/storage/'+OWNER,headers=headers,json={'action':'trash'}).json()['result']==[]
    assert not docker('ps','-aq','--filter','name='+NAMESPACE+'-'+OWNER+'-maintenance-')
    passed('TEMPORARY_MAINTENANCE_CONTAINERS_REMOVED_FILES_VOLUME_UNTOUCHED')
    restored=open_host()
    assert restored['native_workspace_id']==initial['native_workspace_id']
    assert docker('exec',worker,'node','-e',"console.log(require('node:fs').readFileSync('/workspace/readiness-marker.txt','utf8'))")=='synthetic'
    passed('FULL_RECREATE_DATABASE_REDIS_NATIVE_WORKSPACE_AND_FILE_PERSISTED')
    docker('stop',worker)
    reopened=open_host()
    assert reopened['native_workspace_id']==initial['native_workspace_id']
    passed('STOPPED_NATIVE_HOST_MANUAL_RECOVERY')
    print('COMPANY_NETWORK_IAM_TOKENKEY: NOT_TESTED',flush=True)
    print('CHANGSHA_RELEASE_ISOLATED_REHEARSAL: PASS',flush=True)


if __name__=='__main__':
    try:run()
    finally:
        CLIENT.close();cleanup()
