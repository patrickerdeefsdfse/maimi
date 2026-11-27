"""Selected PVC/SC/PV proofs for local state; never mounts SQLite on NFS."""
import hashlib,re
from k8s_worker_provider import require
PREFIX='maimi.megmeet.com/'
def state_name(owner,node):
 require(re.fullmatch('[a-f0-9]{32}',owner) and re.fullmatch('[a-z0-9][a-z0-9.-]{0,252}',node),'STATE_IDENTITY_INVALID')
 return 'mmw-'+owner+'-state-'+hashlib.sha256(node.encode()).hexdigest()[:10]
async def verify_local_state(provider,spec,pvc,pvc_uid,*,require_bound):
 cfg=provider.config;node=spec.node_name or cfg.worker_node
 meta=pvc.get('metadata',{});data=pvc.get('spec',{});annotations=meta.get('annotations',{})
 require(meta.get('name')==state_name(spec.owner,node) and meta.get('uid')==pvc_uid and not meta.get('deletionTimestamp') and annotations.get(PREFIX+'state-node')==node and annotations.get(PREFIX+'state-layout')=='pg-workspace-node-local-v1','LOCAL_STATE_IDENTITY_CHANGED')
 require(data.get('storageClassName')==cfg.state_storage_class and data.get('accessModes')==['ReadWriteOnce'] and data.get('volumeMode','Filesystem')=='Filesystem','LOCAL_STATE_CLAIM_INVALID')
 status,sc=await provider.call('GET','/apis/storage.k8s.io/v1/storageclasses/'+cfg.state_storage_class)
 require(status==200 and sc.get('provisioner')=='rancher.io/local-path' and sc.get('reclaimPolicy')=='Retain' and sc.get('volumeBindingMode')=='WaitForFirstConsumer' and not sc.get('metadata',{}).get('deletionTimestamp'),'LOCAL_STATE_CLASS_UNQUALIFIED')
 name=data.get('volumeName')
 if not name:
  require(not require_bound,'LOCAL_STATE_NOT_BOUND');return
 require(re.fullmatch('[a-z0-9][a-z0-9.-]{0,252}',name),'LOCAL_STATE_PV_NAME_INVALID')
 status,pv=await provider.call('GET','/api/v1/persistentvolumes/'+name)
 require(status==200 and not pv.get('metadata',{}).get('deletionTimestamp'),'LOCAL_STATE_PV_UNAVAILABLE')
 p=pv.get('spec',{});ref=p.get('claimRef',{});sources=[x for x in ('local','hostPath') if x in p]
 allowed={'capacity','accessModes','persistentVolumeReclaimPolicy','storageClassName','volumeMode','mountOptions','claimRef','nodeAffinity','local','hostPath'}
 require(set(p)<=allowed and len(sources)==1 and p.get('persistentVolumeReclaimPolicy')=='Retain' and p.get('storageClassName')==cfg.state_storage_class and p.get('accessModes')==['ReadWriteOnce'] and p.get('volumeMode','Filesystem')=='Filesystem','LOCAL_STATE_PV_SOURCE_REFUSED')
 path=p[sources[0]].get('path','');root=cfg.local_state_root.rstrip('/')
 require(path.startswith(root+'/') and '..' not in path.split('/') and ref.get('namespace')==cfg.namespace and ref.get('name')==meta['name'] and ref.get('uid')==pvc_uid,'LOCAL_STATE_PV_BINDING_REFUSED')
 terms=p.get('nodeAffinity',{}).get('required',{}).get('nodeSelectorTerms',[])
 require(bool(terms),'LOCAL_STATE_NODE_AFFINITY_REQUIRED')
 for term in terms:
  tests=[('matchExpressions','kubernetes.io/hostname'),('matchFields','metadata.name')]
  require(any(any(x.get('key')==key and x.get('operator')=='In' and x.get('values')==[node] for x in term.get(kind,[])) for kind,key in tests),'LOCAL_STATE_WRONG_NODE')
 require(not require_bound or pvc.get('status',{}).get('phase')=='Bound','LOCAL_STATE_NOT_BOUND')
