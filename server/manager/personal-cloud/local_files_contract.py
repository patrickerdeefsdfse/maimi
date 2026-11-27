"""Exact single-node test contract; production NFS never falls back to this."""
import re

BACKEND = 'single-node-local-test'
PREFIX = 'maimi.megmeet.com/'
NODE = 'maimi-r69-test'
NODE_UID = '9e50deaf-c3a6-47ab-99fd-c9f9246f0614'
STORAGE_CLASS = 'maimi-test-shared-local-retain'
STORAGE_CLASS_UID = 'ddd4e9ff-2b89-4f95-b135-ff781bf0a826'
HOST_ROOT = '/data/maimi-r66-test/application-storage/shared/owners'
MOUNT_ROOT = '/host-owners'
INSTANCES = {'maimi-r78-app': 'maimi-r78-chat', 'maimi-r78-execution': 'maimi-r78-execution'}
SC_PATH = '/apis/storage.k8s.io/v1/storageclasses/' + STORAGE_CLASS
NODE_PATH = '/api/v1/nodes/' + NODE

class LocalFilesError(Exception): pass
def need(ok, code):
    if not ok: raise LocalFilesError(code)
def is_uid(value): return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value) is not None
def is_owner(value): return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{32}',value) is not None
def path(owner):
    need(is_owner(owner),'LOCAL_FILES_OWNER_INVALID')
    return HOST_ROOT+'/'+owner+'/workspace'
def annotations(owner):
    return {PREFIX+'files-backend': BACKEND, PREFIX+'local-node-uid': NODE_UID, PREFIX+'local-owner-path': path(owner)}
def verify_node(value):
    need(isinstance(value,dict),'LOCAL_FILES_NODE_MISSING')
    m=value.get('metadata',{});s=value.get('status',{})
    need(m.get('name')==NODE and m.get('uid')==NODE_UID and not m.get('deletionTimestamp') and
         m.get('labels',{}).get('kubernetes.io/hostname')==NODE,
         'LOCAL_FILES_NODE_IDENTITY_CHANGED')
    need(any(x.get('type')=='Ready' and x.get('status')=='True' for x in s.get('conditions',[])),
         'LOCAL_FILES_NODE_NOT_READY')
def verify_class(value):
    need(isinstance(value,dict),'LOCAL_FILES_CLASS_MISSING')
    m=value.get('metadata',{});a=m.get('annotations',{})
    need(m.get('name')==STORAGE_CLASS and m.get('uid')==STORAGE_CLASS_UID and not m.get('deletionTimestamp') and
         value.get('provisioner')=='kubernetes.io/no-provisioner' and value.get('reclaimPolicy')=='Retain' and
         value.get('volumeBindingMode')=='WaitForFirstConsumer' and not value.get('parameters') and
         value.get('allowVolumeExpansion',False) is False and not value.get('mountOptions') and not value.get('allowedTopologies') and
         a.get('storageclass.kubernetes.io/is-default-class','false')!='true' and
         a.get('storageclass.beta.kubernetes.io/is-default-class','false')!='true',
         'LOCAL_FILES_CLASS_POLICY_CHANGED')
def claim(value):
    need(isinstance(value,dict),'LOCAL_FILES_CLAIM_INVALID')
    m=value.get('metadata',{});s=value.get('spec',{});labels=m.get('labels',{})
    owner=labels.get(PREFIX+'owner');ns=m.get('namespace');ident=m.get('uid')
    need(is_owner(owner) and ns in INSTANCES and is_uid(ident) and not m.get('deletionTimestamp') and
         m.get('name')=='mmw-'+owner+'-files' and labels.get(PREFIX+'instance')==INSTANCES[ns] and
         not m.get('ownerReferences'),'LOCAL_FILES_CLAIM_IDENTITY_REFUSED')
    need(all(m.get('annotations',{}).get(k)==v for k,v in annotations(owner).items()) and
         s.get('accessModes')==['ReadWriteOnce'] and s.get('volumeMode','Filesystem')=='Filesystem' and
         s.get('storageClassName')==STORAGE_CLASS and s.get('resources',{}).get('requests',{}).get('storage')=='5Gi' and
         not s.get('dataSource') and not s.get('dataSourceRef') and not s.get('selector'),
         'LOCAL_FILES_CLAIM_POLICY_REFUSED')
    name='r78-files-'+ident
    need(not s.get('volumeName') or s['volumeName']==name,'LOCAL_FILES_CLAIM_PV_CHANGED')
    return {'owner':owner,'namespace':ns,'name':m['name'],'uid':ident,'pv_name':name,'path':path(owner)}
def pv(descriptor):
    d=descriptor
    return {'apiVersion':'v1','kind':'PersistentVolume',
        'metadata':{'name':d['pv_name'],'labels':{PREFIX+'owner':d['owner'],PREFIX+'component':'local-test-files'},
                    'annotations':{**annotations(d['owner']),PREFIX+'claim-uid':d['uid']}},
        'spec':{'capacity':{'storage':'5Gi'},'accessModes':['ReadWriteOnce'],'volumeMode':'Filesystem',
                'storageClassName':STORAGE_CLASS,'persistentVolumeReclaimPolicy':'Retain',
                'local':{'path':d['path']},'claimRef':{'apiVersion':'v1','kind':'PersistentVolumeClaim',
                    'namespace':d['namespace'],'name':d['name'],'uid':d['uid']},
                'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchFields':[
                    {'key':'metadata.name','operator':'In','values':[NODE]}]}]}}}}
def subset(a,b):
    if isinstance(a,dict):return isinstance(b,dict) and all(k in b and subset(v,b[k]) for k,v in a.items())
    if isinstance(a,list):return isinstance(b,list) and len(a)==len(b) and all(subset(x,y) for x,y in zip(a,b))
    return type(a) is type(b) and a==b
def verify_pv(value, descriptor, expected_uid=None, *, bound=False):
    need(isinstance(value,dict),'LOCAL_FILES_PV_UNAVAILABLE')
    m=value.get('metadata',{});s=value.get('spec',{})
    need(subset(pv(descriptor),value) and is_uid(m.get('uid')) and not m.get('deletionTimestamp') and
         not m.get('ownerReferences') and (expected_uid is None or expected_uid==m['uid']),
         'LOCAL_FILES_PV_IDENTITY_CHANGED')
    # Only a local source is allowed; an injected second source cannot pass.
    need(set(s)<= {'capacity','accessModes','volumeMode','storageClassName','persistentVolumeReclaimPolicy',
                  'local','claimRef','nodeAffinity','mountOptions'} and not s.get('mountOptions') and
         set(s.get('local',{}))=={'path'},'LOCAL_FILES_PV_SOURCE_CHANGED')
    if bound:need(value.get('status',{}).get('phase')=='Bound','LOCAL_FILES_PV_NOT_BOUND')
    else:need(value.get('status',{}).get('phase','') in ('','Pending','Available','Bound'),
              'LOCAL_FILES_PV_PHASE_REFUSED')
    return m['uid']
