"""Direct completed r40 + desktop 0.12.0 proof and expected static state.

Pure reads via injected bounded no-symlink read/exists. Never calls predecessor
Product/check/health, never reads credentials, never mutates receipt dictionaries.
"""
from copy import deepcopy
import hashlib,json,re
from pathlib import PurePosixPath
R38_IDS = {'db-migrate': '44291dfffc58682e8b791ba45f3285ca92f56c30623afcf824b4537a801b89d4', 'downloads': '46c2a336d70faa17c101cf8b59fc0029267cb020646ea67d16f55f4ac7f56770', 'gateway': 'f5b0e56e516508493850e84ec877fead744521e769cdc8e554aef83eaa5aa3d8', 'office-service': 'e7444311557bcb1e34c5a623fb2f286453ea8841989c59f917a41f4a69681300', 'personal-cloud-manager': '36707033ee2f1e067b43971fdcd2077d745366c406accef161f5a68ee17cee7f', 'postgres': '03663601f0917f3d85a8ac246810c33f7acf82343800da3a83b61ed663ca5199', 'redis': 'a6b499d6f8f7413bac09cd1bd7d31d718d782f0abe3eec5c9b647ba1b704b97f', 'retention-worker': 'd0899a25f26f83f02fce45fffff3e8d99247d40d29ff3dfc880ea6f501f92ba7'}
IDS = {'db-migrate': '44291dfffc58682e8b791ba45f3285ca92f56c30623afcf824b4537a801b89d4', 'downloads': 'e4eb16b67cb9a5f4e97ff6e24616e9090bac59419a2b4ea69ec8a5f2ae5d7238', 'gateway': '558f5957b7419154beb26cfb248e3a921fe688f8a474c8b8dd94622fb0363515', 'office-service': 'e7444311557bcb1e34c5a623fb2f286453ea8841989c59f917a41f4a69681300', 'personal-cloud-manager': '06c2a7a1799c120de795aa000f40e8f8292fc276fd459044c8fde2e894f854cc', 'postgres': '03663601f0917f3d85a8ac246810c33f7acf82343800da3a83b61ed663ca5199', 'redis': 'a6b499d6f8f7413bac09cd1bd7d31d718d782f0abe3eec5c9b647ba1b704b97f', 'retention-worker': 'd0899a25f26f83f02fce45fffff3e8d99247d40d29ff3dfc880ea6f501f92ba7'}
SOURCES = {'gateway': {'/app/app/auth_middleware.py': '7e7371481c7f7101bc3ae99c13588e4e715405ad971e4ee29582878132cb4ee9', '/app/app/connector_official_search.py': '5e61c4caa74f32e5879fec102011e819283be2b246ad5ae8a6acdf3c61d52ea1', '/app/app/desktop_auth.py': '942697bc8071aaf9a6b90b82eee3504d97711ac725f794476ea00c9d7e5154ce', '/app/app/desktop_device_auth.py': 'c847d621bb63e732629004919a02a3b9c48c0ba13b1e050a2d95d580f069a83c', '/app/app/drawing_office.py': 'd500689e4bc531c429e38957ad0542f3c4eed4ffeae1a02cd74bb3ea17463b0a', '/app/app/enterprise_connectors.py': '3ae266f8fac2de5975714a001c352a41be4c362dde49e68065e1c99a3e691d54', '/app/app/iam_oauth.py': 'd9cf31c6b327f8773c2a97930cfa6760de93104e2baeecedbd78eef6bb0d2991', '/app/app/main.py': '46471d0242a4f1c5980caee47872cd25655d0df2095183b37317ad095ea25bda', '/app/app/personal_cloud.py': 'd569840cb6449021ae3d0ccd74c36961651beef10433d211b394abf2c94c0f70', '/app/app/personal_cloud_plugin_static.py': '93aec0833d5dc526c4faa8108d4fd2b67042716a5e06de6b7a62e3702cccad02', '/app/app/personal_cloud_static.py': 'ca94b2ed19eee66b6f7b7a5201d9a4bcf6b4c324b0f11f23893f46147fded958', '/app/app/personal_cloud_web.py': '4b7156951a4039c937ba9a14ace56eefcc4eef7ad2a1cce1aa7d943f3c85f96c', '/app/app/personal_search.py': '2da5cdf3ee655dd62b28fc4d771cf505faa019ec2c77fff2c6fa6fe5565c915e', '/app/app/project_platform.py': 'ca20276d338315e02986c07b7e80a7e6104f5e5b09e43a1dc69b034afb74a632', '/app/maimi_projects/card_renderer.py': 'c95a3db4b5c3abea6e49fb829b2b564b808839fdcc5d62cfee417c4907032076', '/app/maimi_projects/disk_semantics.py': 'd98ad04493d4e7affe0c93780f446be6cc70c3ee7b9ed59109566be48435c13a', '/app/maimi_projects/heatcost_flow.py': 'c758dced7c7c6bdac31fee095a77c60bf3d0012e25ef410259e4f50dec2ff48f', '/app/maimi_projects/monitor_projection.py': '264b6899d61b06919c64a5b191cce31b381947c39eaa9e6c69e55ae5aed6a1c8', '/app/maimi_projects/service.py': '26d90122e7c5c9115db33cbd3f40cf2a694b251bba5dd3a149c7c4ba2ffafe3a', '/app/maimi_projects/monitor.py': '779ddee53acea0f46bb59223a5321ec9c10b4cb08d134b8dc94164ec1bf29858'}, 'personal-cloud-manager': {'/opt/personal-cloud/manager.py': '5ca3905ffb947665b33292fa24ac7d313e1c1f668f0bcd535df2865bb8e61e9c'}}
PINS = {'r40_manifest': 'bf86e8d3a703b9c9c0cd32533ddd54c02a4df2e6069fd3c8677dc341415762d7', 'zip': {'filename': 'MEGMEET-Download-AutoUpdate-0.12.0-r40-20260930.zip', 'sha256': 'd1d465fdec0bb674e87a9c17bacdb77493e9d7c2bb04ce54de27deb150e7578d', 'size_bytes': 304361148}, 'manifest': '90d2465874b148f9d268ac3276c5ae6d60d8078692474ae3a7759a1ee1f300a7', 'release': {'filename': 'MEGMEET-AI-Setup-0.12.0-x64.exe', 'schema_version': 1, 'sha256': '6b5117e206f655eda1d9ab78eeb0037c618735397f3252ba44deee7080e83580', 'size_bytes': 302943123, 'status': 'published', 'version': '0.12.0'}}
TARGETS = {'/opt/personal-cloud/manager.py': {'file': 'public/manager.py', 'original': '3aefe0c695faec63b7007e026b3fce446e54b2886722935483a61bb1961832cd'}, '/app/app/iam_oauth.py': {'file': 'public/iam_oauth.py', 'original': 'a51f4d2ff6112daec29b9919f7fef072e11232228cd3923878bc205c3a3e54a9'}, '/app/app/desktop_auth.py': {'file': 'public/desktop_auth.py', 'original': '9b81b3ccd7151ab4fca48d00b7826c310bb61ecac569f5e8b9f790b5b34ba0fc'}, '/app/app/desktop_device_auth.py': {'file': 'public/desktop_device_auth.py', 'original': None}, '/app/app/personal_cloud_web.py': {'file': 'public/personal_cloud_web.py', 'original': '9c9d0c1dc13f883033bac77d91f470d5ae0fa594c2dde1dbe02e3358fa73f2a5'}, '/app/app/personal_search.py': {'file': 'public/personal_search.py', 'original': 'daab99700383e53c14190034b607985d38b5c39b2a2458e34e3419fdf10af305'}, '/app/app/connector_official_search.py': {'file': 'public/connector_official_search.py', 'original': 'e91e71c0cbe1ee89b1608f25e224c05d7305f997b823a89a418839dbb6fd89b3'}}

ROOT=PurePosixPath('/data/megmeet-remembered-login-20260930-r40')
MANIFEST='bf86e8d3a703b9c9c0cd32533ddd54c02a4df2e6069fd3c8677dc341415762d7'
TRANSACTION='.remembered-login-switch-20260930T053717Z-3e204899'
RELEASE='20260930-r40-remembered-login'
MODE='r40-remembered-login'
CHANGED=frozenset(('gateway','personal-cloud-manager'))
R40_FINGERPRINT='4b9f6870246ec1cdd30c7a850a1c23cf74c88d8a37a07491b75e6d7ac30a9594'
DOWNLOAD_ROOT=PurePosixPath('/data/megmeet-desktop-distribution-0.12.0-r40-20260930')
DOWNLOAD_TRANSACTION='.publish-20260930T054942Z-c857751b'
DOWNLOAD_REVISION='desktop-download-0.12.0-r40-20260930'
DOWNLOAD_OLD_MANIFEST='38d0f26ab89215070d236674105f4d84743dc74d05d2188304fbb9e037f12269'
HEX=re.compile(r'[a-f0-9]{64}')

class Stop(ValueError):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()
def require(value,code):
    if not value:raise Stop(code)
def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()

def _mounts(policy):
    mounts=policy['mounts']
    require(isinstance(mounts,list) and all(isinstance(v,dict) and set(v)=={'type','source','destination','rw'} and isinstance(v['destination'],str) and type(v['rw']) is bool for v in mounts),'R41_BASELINE_MOUNTS_INVALID')
    require(len({v['destination'] for v in mounts})==len(mounts),'R41_BASELINE_DUPLICATE_MOUNTS')
    return {v['destination']:v for v in mounts}

def project_policies(baseline):
    require(set(baseline['policies'])==set(IDS),'R41_BASELINE_POLICY_SERVICES_CHANGED')
    result=deepcopy(baseline['policies'])
    for service in CHANGED:
        mounts=_mounts(result[service])
        for target,spec in TARGETS.items():
            owner='personal-cloud-manager' if target.startswith('/opt/personal-cloud/') else 'gateway'
            if owner==service:mounts[target]={'type':'bind','source':str(ROOT/spec['file']),'destination':target,'rw':False}
        result[service]['mounts']=sorted(mounts.values(),key=lambda v:v['destination'])
    environment=result['personal-cloud-manager']['environment']
    require(isinstance(environment,dict),'R41_BASELINE_MANAGER_ENV_INVALID')
    environment.update(PERSONAL_CLOUD_MAX_ACTIVE_HOSTS='0',PERSONAL_CLOUD_IDLE_SECONDS='2400')
    mounts=_mounts(result['downloads'])
    for destination,relative in (('/opt/distribution/app.py','app.py'),('/srv/megmeet-distribution','site')):
        require(destination in mounts and mounts[destination]['type']=='bind' and mounts[destination]['rw'] is False,'R41_DOWNLOAD_MOUNT_BOUNDARY_CHANGED')
        mounts[destination]['source']=str(DOWNLOAD_ROOT/'desktop-distribution'/relative)
    result['downloads']['mounts']=sorted(mounts.values(),key=lambda v:v['destination'])
    return result

def project_items(baseline):
    items=deepcopy(baseline['items'])
    require({s:v['id'] for s,v in items.items()}==R38_IDS,'R41_R40_BASELINE_IDS_CHANGED')
    for service,item in items.items():
        item['id']=IDS[service]
        if service!='db-migrate':item['running']=True
        if service in CHANGED:item['remembered']={'release':RELEASE,'transaction':TRANSACTION,'mode':MODE}
    return items

def login_overlay():
    services={s:{'labels':{'com.megmeet.remembered-login.'+k:v for k,v in {'release':RELEASE,'transaction':TRANSACTION,'mode':MODE}.items()}} for s in CHANGED}
    for service in CHANGED:
        services[service]['volumes']=[{'type':'bind','source':str(ROOT/spec['file']),'target':target,'read_only':True,'bind':{'create_host_path':False}} for target,spec in TARGETS.items() if ('personal-cloud-manager' if target.startswith('/opt/personal-cloud/') else 'gateway')==service]
    services['personal-cloud-manager']['environment']={'PERSONAL_CLOUD_MAX_ACTIVE_HOSTS':'0','PERSONAL_CLOUD_IDLE_SECONDS':'2400'}
    return {'services':services}

def _evidence(read,exists):
    """The independent recorded fingerprint pins inherited commands and policies."""
    samples={}
    def take(path):
        path=PurePosixPath(path);raw=read(path)
        require(isinstance(raw,bytes) and len(raw)<=8*1024**2,'R41_EVIDENCE_READ_INVALID')
        if path in samples:require(samples[path]==raw,'R41_EVIDENCE_CHANGED_DURING_READ')
        samples[path]=raw;return raw
    folder=ROOT/TRANSACTION;download_folder=DOWNLOAD_ROOT/DOWNLOAD_TRANSACTION
    forbidden=(ROOT/'.update-incomplete',folder/'rollback-intent.json',folder/'restored-r38-vmware-dashboard.json',
               DOWNLOAD_ROOT/'.publication-incomplete',DOWNLOAD_ROOT/'.extraction-incomplete',download_folder/'rolled-back.json')
    require(not any(exists(p) for p in forbidden),'R41_PREDECESSOR_INCOMPLETE_OR_ROLLED_BACK')
    manifest_raw=take(ROOT/'release-manifest.json');require(sha(manifest_raw)==MANIFEST,'R41_R40_MANIFEST_CHANGED')
    manifest=json.loads(manifest_raw)
    require(manifest.get('schema')=='r40-remembered-login-manifest-v1' and manifest.get('status')=='FROZEN','R41_R40_MANIFEST_NOT_FROZEN')
    raw=take(ROOT/'transition.json');receipt=json.loads(raw)
    prepared_raw=take(folder/'prepared.json');prepared=json.loads(prepared_raw)
    baseline_raw=take(folder/'baseline.json');baseline=json.loads(baseline_raw)
    progress_raw=take(folder/'progress.json');progress=json.loads(progress_raw)
    expected={'release':RELEASE,'manifest_sha256':MANIFEST,'status':'PREPARED','previous_mode':'r38-vmware-dashboard',
      'backup_directory':str(folder),'original_service_ids':R38_IDS,'changed_services':['gateway','personal-cloud-manager'],
      'manager_restarted':True,'workers_disconnected':True,'history_format_changed':False,'history_maintenance':'R29_PRESERVED',
      'images_changed':False,'baseline_sha256':sha(baseline_raw)}
    require(prepared==expected,'R41_R40_PREPARED_CHANGED')
    cloud_ids={**IDS,'downloads':R38_IDS['downloads']}
    require(receipt.get('status')=='APPLIED' and receipt.get('effective_mode')==MODE and receipt.get('current_service_ids')==cloud_ids and receipt.get('nginx_configuration_changed') is False and receipt.get('health',{}).get('status')=='ready'
      and all(receipt.get(k)==v for k,v in expected.items() if k!='status'),'R41_R40_RECEIPT_CHANGED')
    require(take(folder/'applied.json')==raw and progress.get('phase')=='FINISHED' and progress.get('target')==MODE and progress.get('creating') is None and progress.get('created',{}).get(MODE)=={s:cloud_ids[s] for s in CHANGED},'R41_R40_JOURNAL_CHANGED')
    r40_fingerprint=sha(raw+prepared_raw+baseline_raw+progress_raw)
    require(r40_fingerprint==R40_FINGERPRINT,'R41_RECORDED_R40_FINGERPRINT_CHANGED')
    require(isinstance(baseline.get('command'),list) and all(isinstance(v,str) for v in baseline['command']) and
      (baseline['command'][:2]==['docker','compose'] or baseline['command'][:4]==['docker','--host','unix:///var/run/docker.sock','compose']),'R41_BASELINE_COMMAND_INVALID')
    command=list(baseline['command'])+['-f',str(folder/('login-'+MODE+'.compose.json'))]
    compose_paths={baseline['command'][i+1] for i,arg in enumerate(baseline['command'][:-1]) if arg=='-f'}
    require(isinstance(baseline.get('compose_files'),dict) and set(baseline['compose_files'])==compose_paths,'R41_INHERITED_COMPOSE_SET_CHANGED')
    for path,digest in baseline['compose_files'].items():
        require(PurePosixPath(path).is_absolute() and isinstance(digest,str) and HEX.fullmatch(digest) and sha(take(PurePosixPath(path)))==digest,'R41_INHERITED_COMPOSE_CHANGED')
    require(json.loads(take(PurePosixPath(command[-1])))==login_overlay(),'R41_R40_OVERLAY_CHANGED')
    # Publication is complete in its own direct journal; do not replay r32.
    desktop_raw=take(DOWNLOAD_ROOT/'transition.json');desktop_receipt=json.loads(desktop_raw)
    dp_raw=take(download_folder/'prepared.json');dp=json.loads(dp_raw)
    expected_dp={'revision':DOWNLOAD_REVISION,'status':'PREPARED','backup_directory':str(download_folder),'pins':PINS,'previous_downloads_id':R38_IDS['downloads'],'cloud_fingerprint':r40_fingerprint}
    require(dp==expected_dp and take(download_folder/'r40-transition.json')==raw,'R41_DOWNLOAD_PREPARED_CHANGED')
    expected_download={**expected_dp,'status':'APPLIED','downloads_id':IDS['downloads'],'version':'0.12.0','only_downloads_changed':True,'nginx_configuration_changed':False,'tls_configuration_changed':False,'employee_acceptance_pending':True}
    require(desktop_receipt==expected_download and take(download_folder/'applied.json')==desktop_raw,'R41_DOWNLOAD_RECEIPT_CHANGED')
    ds_raw=take(download_folder/'progress.json');ds=json.loads(ds_raw)
    require(ds.get('phase')=='FINISHED' and ds.get('new_id')==IDS['downloads'] and ds.get('downloads_id')==IDS['downloads'] and ds.get('rollback') is False and not ds.get('restored_id'),'R41_DOWNLOAD_JOURNAL_CHANGED')
    old=json.loads(take(download_folder/'desktop0111-transition.json'))
    require(old.get('status')=='APPLIED' and old.get('revision')=='desktop-download-0.11.1-r32-20260929' and old.get('new_downloads_id')==R38_IDS['downloads'] and old.get('distribution_manifest_sha256')==DOWNLOAD_OLD_MANIFEST,'R41_DOWNLOAD_PREDECESSOR_SNAPSHOT_CHANGED')
    distribution=DOWNLOAD_ROOT/'desktop-distribution';distribution_raw=take(distribution/'distribution-manifest.json')
    require(sha(distribution_raw)==PINS['manifest'],'R41_DOWNLOAD_MANIFEST_CHANGED')
    distribution_manifest=json.loads(distribution_raw)
    # The manifest is pinned; its small public descriptors are checked here.
    files=distribution_manifest['files']
    for path in ('app.py','compose.yaml','compose.nginx-network.yaml','site/release.json'):
        item=files[path];body=take(distribution/path)
        require(item=={'sha256':sha(body),'size_bytes':len(body)},'R41_DOWNLOAD_DESCRIPTOR_CHANGED')
    require(json.loads(samples[distribution/'site/release.json'])==PINS['release'],'R41_DOWNLOAD_RELEASE_CHANGED')
    policies=project_policies(baseline);items=project_items(baseline)
    for path,before in list(samples.items()):require(read(path)==before,'R41_EVIDENCE_CHANGED_DURING_READ')
    require(not any(exists(p) for p in forbidden),'R41_PREDECESSOR_CHANGED_DURING_READ')
    fingerprint=sha(encoded({str(path):sha(raw) for path,raw in samples.items()}))
    return {'raw':raw,'baseline':baseline,'receipt':receipt,'prepared':prepared,'folder':folder,'ids':dict(IDS),'command':command,
       'fingerprint':fingerprint,'policies':policies,'items':items,'r40_fingerprint':r40_fingerprint,
       'desktop_raw':desktop_raw,'desktop_receipt':desktop_receipt,'desktop_folder':download_folder,
       'download_identity':{'id':IDS['downloads'],'name':'/megmeet-desktop-distribution-downloads-1','publication':DOWNLOAD_TRANSACTION,'configs':','.join(str(distribution/p) for p in ('compose.yaml','compose.nginx-network.yaml'))}}

def evidence(read,exists):
    """Read must reject symlinks/nonregular/unsafe ownership and bound its input.

    exists should use lexists semantics so even a dangling marker is rejected.
    No filesystem, Docker, HTTP, health, legacy import or mutation is performed
    except calls to these two supplied read-only functions.
    """
    try:return _evidence(read,exists)
    except Stop:raise
    except (ValueError,TypeError,KeyError,IndexError,AttributeError):raise Stop('R41_DIRECT_EVIDENCE_INVALID') from None
