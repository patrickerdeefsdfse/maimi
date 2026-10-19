"""Synthetic test data only. No host observations or company files."""
from copy import deepcopy
from app.observability.monitor import RESOURCE_FIELDS

NOW=1792375200
WS='11111111-1111-4111-8111-111111111111'
async def bindings():return [{'workspace_id':WS,'owner_user_no':'M00099999','enabled':True}]

def collector_samples():
 """Construct every field explicitly; never read real container metadata."""
 worker={'container_id':'a'*64,'service_class':'cloud-worker','owner_ref':WS.replace('-',''),'managed_namespace':'megmeet-personal-server-v1','state':'running','started_at':'2026-10-19T00:00:00Z','restart_count':0,'sampled_at':NOW,'health':None,'oom_killed':False,'identity':{'pid_start_ticks':1,'cgroup_inode':2,'cgroup_device':3,'netns_inode':4},'sample_status':'available','metrics':{
  'cpu':{'usage_usec':1000,'quota_cores':1,'cores':None},
  'memory':{'current_bytes':128*1024**2,'max_bytes':1536*1024**2,'limit_unlimited':False,'events':dict.fromkeys(('low','high','max','oom','oom_kill','oom_group_kill'),0)},
  'pids':{'current':24,'max':512,'limit_unlimited':False,'events':{'max':0}},
  'io':{'read_bytes':100,'write_bytes':200,'read_operations':1,'write_operations':2,'read_bytes_per_second':None,'write_bytes_per_second':None},
  'network':{'rx_bytes':300,'tx_bytes':400,'rx_packets':3,'tx_packets':4,'rx_bytes_per_second':None,'tx_bytes_per_second':None,'shared_namespace':False}}}
 office=deepcopy(worker);office.update(container_id='b'*64,service_class='office-service',owner_ref=None,managed_namespace=None,health='healthy')
 office['identity']['cgroup_inode']=5;office['identity']['netns_inode']=6
 first={'schema':'maimi-observability-host-sample-v1','collector_instance':'1'*32,'sample_id':'2'*32,'sequence':0,'collected_at':NOW,'monotonic_ns':1_000_000_000,'stale_after_seconds':120,'status':'available','host':{'cpu_count':8,'cpu_ticks':{'total':1000,'idle':500},'cpu_busy_percent':None,'memory_total_bytes':32*1024**3,'memory_available_bytes':24*1024**3,'disks':{k:{'total_bytes':500*1024**3,'available_bytes':300*1024**3} for k in ('root','data')}},'containers':[worker,office],'owner_storage':[{'owner_ref':WS.replace('-',''),'managed_namespace':'megmeet-personal-server-v1','logical_bytes':1024,'sampled_at':NOW,'complete':True,'stale':False,'stale_after_seconds':3600,'estimate':True}],'coverage':{'docker':'available','storage':'metadata_estimates'}}
 second=deepcopy(first);second.update(sample_id='3'*32,sequence=1,collected_at=NOW+15,monotonic_ns=16_000_000_000)
 second['host']['cpu_ticks']={'total':2000,'idle':1000};second['host']['cpu_busy_percent']=50
 for row in second['containers']:
  row['sampled_at']=NOW+15;row['metrics']['cpu'].update(usage_usec=7_501_000,cores=.5)
 return [first,second]
def snapshot(now,sequence,cpu):
 row={**{k:None for k in RESOURCE_FIELDS},'instance_id':'a'*64,'state':'running','health':None,'oom_killed':False,'cpu_usage_ns':cpu,'cpu_cores':0.5 if sequence>1 else None,'memory_used_bytes':256*1024**2,'memory_limit_bytes':1536*1024**2,'pids_current':24,'pids_limit':512,'storage_bytes':None}
 return {'schema':'maimi-monitor-collector-v1','sampled_at':now,'sequence':sequence,'inventory_complete':True,'host':{'cpu_count':8,'memory_total_bytes':32*1024**3,'memory_available_bytes':24*1024**3,'data_available_bytes':300*1024**3},'workers':[{'workspace_id':WS,**row}],'services':[{'service':'office-service',**row,'instance_id':'b'*64,'cpu_usage_ns':cpu//4,'memory_limit_bytes':8*1024**3,'pids_current':3,'pids_limit':256}],'storage':[{'workspace_id':WS,'storage_bytes':64*1024**2,'sampled_at':now-60,'complete':True,'stale':False}]}
