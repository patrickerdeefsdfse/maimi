"""Rebuild only this directory's candidate from the frozen prior candidate."""
from pathlib import Path
import hashlib
import json

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'gateway/manager-candidate/manager.py'
text=SOURCE.read_text(encoding='utf-8')

def replace(old,new):
    global text
    if text.count(old)!=1:raise RuntimeError('BASELINE_ANCHOR_MISMATCH')
    text=text.replace(old,new)

replace("NATIVE021_RECEIPTS_DIR=os.getenv('PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR','')",
"""NATIVE021_RECEIPTS_DIR=os.getenv('PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR','')
NATIVE021_NEW_OWNERS_DIR=os.getenv('PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR','')
RESTORE_BINDINGS_FILE=os.getenv('PERSONAL_CLOUD_RESTORE_BINDINGS_FILE','')
from runtime_admission import admit_if_new, new_receipt_matches, AdmissionRefused, read_receipt
if NATIVE021_NEW_OWNERS_DIR and NATIVE021_NEW_OWNERS_DIR!='/opt/personal-cloud/runtime-r65/new-owners':
    raise RuntimeError('invalid native021 new-owner receipt directory')
if RESTORE_BINDINGS_FILE and RESTORE_BINDINGS_FILE!='/opt/personal-cloud/runtime-r65/restore.json':
    raise RuntimeError('invalid administrative restore binding path')
""")
replace("if WORKER_RUNTIME==NATIVE021_RUNTIME:\n    if (any((WORKER_OVERLAY_DIR,", "if RESTORE_BINDINGS_FILE and WORKER_RUNTIME!=REVIEWED_RUNTIME:\n    raise RuntimeError('restore binding requires reviewed old runtime')\nif NATIVE021_NEW_OWNERS_DIR and WORKER_RUNTIME!=NATIVE021_RUNTIME:\n    raise RuntimeError('new-owner receipt requires native021 runtime')\nif WORKER_RUNTIME==NATIVE021_RUNTIME:\n    if (any((WORKER_OVERLAY_DIR,")
replace("    file=Path(NATIVE021_RECEIPTS_DIR)/(owner+'.json')\n    try:",
"""    if state_identity[0]==image_id and NATIVE021_NEW_OWNERS_DIR and new_receipt_matches(Path(NATIVE021_NEW_OWNERS_DIR)/(owner+'.json'),
            owner=owner,namespace=INSTANCE,release_sha=RUNTIME_RELEASE_SHA,image_id=image_id,
            state={'name':state_identity[1],'created_at':state_identity[2]},
            files={'name':files_identity[0],'created_at':files_identity[1]}):return
    file=Path(NATIVE021_RECEIPTS_DIR)/(owner+'.json')
    try:""")
replace('def container_config(owner,body,route_token):', '''def restored_binding(owner):
    if not RESTORE_BINDINGS_FILE:return None
    try:
        data=read_receipt(RESTORE_BINDINGS_FILE,maximum_bytes=4*1024*1024)
        expected={'schema':'megmeet-runtime-restore-bindings-r65-v1','status':'RESTORE_VERIFIED',
            'runtime':REVIEWED_RUNTIME,'manager_namespace':INSTANCE,'image_id':image_id,
            'release_manifest_sha256':RUNTIME_RELEASE_SHA,'owners':data.get('owners')}
        if data!=expected or not isinstance(data['owners'],dict):raise ValueError()
        value=data['owners'][owner]
        if set(value)!={'state','files'}:raise ValueError()
        for kind,volume in value.items():
            if (set(volume)!={'name','created_at'} or not isinstance(volume['created_at'],str) or not volume['created_at']
                or not re.fullmatch(re.escape(owner_key(owner))+'-restore-[a-f0-9]{16}-'+kind,volume['name'])):raise ValueError()
        return value
    except (OSError,ValueError,TypeError,KeyError,AdmissionRefused):
        raise HTTPException(503,'personal_cloud_restore_binding_required') from None


def volume_names(owner):
    value=restored_binding(owner)
    return {kind:value[kind]['name'] if value else owner_key(owner)+'-'+kind for kind in ('state','files')}


def container_config(owner,body,route_token):''')
replace("    mounts=[{'Type':'volume','Source':name+'-state','Target':'/state'},\n            {'Type':'volume','Source':name+'-files','Target':'/workspace'}]",
"    volume_pair=volume_names(owner)\n    mounts=[{'Type':'volume','Source':volume_pair['state'],'Target':'/state'},\n            {'Type':'volume','Source':volume_pair['files'],'Target':'/workspace'}]\n    if runtime==NATIVE021_RUNTIME:\n        for mount in mounts:mount['VolumeOptions']={'NoCopy':True}")
replace("        state_identity=None;files_identity=None\n        for suffix in ('state','files'):\n            volume=await docker('POST','/volumes/create',{'Name':name+'-'+suffix,'Labels':{LABEL:INSTANCE,'com.megmeet.personal-cloud.owner':owner}})",
"""        state_identity=None;files_identity=None
        if WORKER_RUNTIME==NATIVE021_RUNTIME and NATIVE021_NEW_OWNERS_DIR:
            try:
                await admit_if_new(docker=docker,owner=owner,namespace=INSTANCE,image_id=image_id,
                    release_sha=RUNTIME_RELEASE_SHA,receipts_directory=NATIVE021_NEW_OWNERS_DIR,
                    snapshot_directory=NATIVE021_RECEIPTS_DIR)
            except AdmissionRefused:
                raise HTTPException(503,'personal_cloud_new_owner_admission_required') from None
        restored=restored_binding(owner);volume_pair=volume_names(owner)
        for suffix in ('state','files'):
            if not restored:
                volume=await docker('POST','/volumes/create',{'Name':volume_pair[suffix],'Labels':{LABEL:INSTANCE,'com.megmeet.personal-cloud.owner':owner}})""")
replace("            volume=await docker('GET','/volumes/'+name+'-'+suffix)", "            volume=await docker('GET','/volumes/'+volume_pair[suffix])\n            if restored and (volume.get('Name')!=restored[suffix]['name'] or volume.get('CreatedAt')!=restored[suffix]['created_at']):\n                raise HTTPException(503,'personal_cloud_restored_volume_changed')")
replace("        elif migration_ready.get(owner)!=state_identity:", "        elif restored:\n            migration_ready[owner]=state_identity\n        elif migration_ready.get(owner)!=state_identity:")
replace("    auth(request);name=owner_key(owner);volume_name=name+'-state'\n    require_runtime_activation()", "    auth(request);name=owner_key(owner);volume_name=name+'-state'\n    require_runtime_activation()\n    if RESTORE_BINDINGS_FILE:raise HTTPException(503,'personal_cloud_restore_maintenance_pending')")
replace("    if RESTORE_BINDINGS_FILE:raise HTTPException(503,'personal_cloud_restore_maintenance_pending')",
"    if RESTORE_BINDINGS_FILE:raise HTTPException(503,'personal_cloud_restore_maintenance_pending')\n    if WORKER_RUNTIME==NATIVE021_RUNTIME:raise HTTPException(503,'personal_cloud_native021_maintenance_unreviewed')")
# Restore mode never reaches the destructive storage helper. Runtime launch uses
# the exact restored identities above, and legacy source volumes remain intact.
(ROOT/'manager.py').write_text(text,encoding='utf-8',newline='\n')
(ROOT/'manager-patch.json').write_text(json.dumps({'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    'candidate_sha256':hashlib.sha256((ROOT/'manager.py').read_bytes()).hexdigest(),
    'frozen_source_modified':False},indent=2)+'\n',encoding='utf-8')
