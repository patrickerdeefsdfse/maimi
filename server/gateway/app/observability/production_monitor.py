"""Production-only read adapter. Existing cluster_monitor IAM gate/report stay intact.

Exactly two ConfigMap GETs using the projected Gateway service-account token.
The token/CA never appear in the returned view, logs or exception text.
"""
import concurrent.futures,hashlib,http.client,ipaddress,json,math,os,socket,ssl,stat,time
from pathlib import Path
UIDS = {name: os.environ.get("MAIMI_MONITOR_" + name[-2:].upper() + "_UID", "") for name in ("cluster-a1", "cluster-a2")}
CA_SHA = os.environ.get('MAIMI_MONITOR_CA_SHA256', '').strip().lower()
IDENTITY=Path('/run/maimi-monitor-api')
MESSAGES={
 'HOST_SAMPLE_UNAVAILABLE':'主机监控结果尚未取得，请检查该主机采集与发布链路',
 'K3S_REBOOT_RECOVERY_NOT_QUALIFIED':'集群节点开机恢复尚未完成验收',
 'HOST_SERVICES_UNKNOWN':'主机服务状态尚未取得', 'SERVICE_NOT_RUNNING':'主机上的指定服务未运行',
 'K3S_NODE_UNHEALTHY':'集群节点未就绪或报告资源压力','K3S_NODE_METRICS_UNKNOWN':'节点用量指标尚未取得',
 'K3S_API_UNAVAILABLE':'集群管理接口的监控请求未通过','PG_STATE_UNKNOWN':'数据库本机状态尚未取得',
 'PG_SYNC_REPLICA_MISSING':'数据库同步副本未处于预期状态','PG_RECEIVER_NOT_STREAMING':'数据库备库接收进程未正常复制',
 'PG_REPLAY_LAG_UNKNOWN':'数据库复制延迟无法确认','PG_REPLAY_LAG_HIGH':'数据库复制延迟偏高',
 'PG_PRIMARY_COUNT_INVALID':'数据库主库数量不符合预期','PG_PROXY_UNAVAILABLE':'应用节点数据库代理无法确认',
 'PG_PROXY_ROUTE_COUNT':'数据库代理路由数量不符合预期','PG_PROXY_PRIMARY_MISMATCH':'数据库代理与当前主库不一致',
 'PATRONI_PATH_UNAVAILABLE':'数据库健康管理接口无法访问','PATRONI_DCS_STALE':'数据库协调信息已过期',
 'PATRONI_WATCHDOG_FAILED':'数据库隔离保护报告失败','PG_BACKUP_NOT_QUALIFIED':'正式数据库备份尚未完成配置或验收',
 'PG_BACKUP_FAILED':'正式数据库最近备份失败','PG_RESTORE_NOT_VERIFIED':'正式数据库恢复验证尚未完成',
 'NAS_MOUNT_UNAVAILABLE':'共享存储挂载或采集不可用','NAS_SOURCE_MISMATCH':'共享存储来源与预期不一致',
 'NAS_CAPACITY':'共享存储可用空间偏低','NAS_INODES':'共享存储文件数量余量偏低','NAS_BACKUP_UNKNOWN':'共享存储备份情况尚未确认',
 'ENTRY_NOT_APPLICATION_QUALIFIED':'该入口视角尚未通过正式应用验收','ENTRY_TEST_PAGE_ONLY':'目前是负载均衡测试页，尚非正式应用健康检查',
 'HARD_FAILURE_RECOVERY_NOT_ACCEPTED':'断电或网络分区下的完整业务接续尚未验收','BACKUP_FAILURE_DOMAIN_SHARED':'备份位置仍与业务共用故障范围',
 'OBSERVER_UNAVAILABLE':'一个应用节点的监控视角缺失或过期，另一视角的成功不能替代它'}
def age(value,now):
 return max(0,now-value) if type(value) in (int,float) and math.isfinite(value) and 0<value<=now+5 else None
def read_projected(name,limit):
 root=IDENTITY.resolve(strict=True);path=(IDENTITY/name).resolve(strict=True)
 if not path.is_relative_to(root):raise ValueError('projected_path')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno())
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022 or s.st_size>limit:raise ValueError('projected_metadata')
  data=f.read(limit+1)
  if len(data)>limit:raise ValueError('projected_limit')
  return data
class APIConnection(http.client.HTTPSConnection):
 def __init__(self,ip,context):
  self.api_ip=ip;super().__init__('kubernetes.default.svc',443,context=context,timeout=2.5)
 def connect(self):
  raw=socket.create_connection((self.api_ip,443),2.5)
  try:self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
  except BaseException:raw.close();raise
def get_configmaps():
 address=os.environ.get('KUBERNETES_SERVICE_HOST','')
 if ipaddress.ip_address(address) not in ipaddress.ip_network(os.environ.get('MAIMI_MONITOR_SERVICE_CIDR', '')):raise ValueError('production_api_address')
 ca=read_projected('ca.crt',32768)
 if hashlib.sha256(ca).hexdigest()!=CA_SHA:raise ValueError('production_ca_pin')
 context=ssl.create_default_context(cadata=ca.decode('ascii'));token=read_projected('token',16384).decode('ascii').strip()
 if not token or '\r' in token or '\n' in token:raise ValueError('projected_token_format')
 def get(name):
  c=APIConnection(address,context)
  try:
   c.request('GET','/api/v1/namespaces/maimi-monitoring/configmaps/'+name,headers={'Authorization':'Bearer '+token,'Connection':'close'})
   r=c.getresponse();data=r.read(262145)
   if r.status!=200 or len(data)>262144:raise ValueError('metadata_response')
   return json.loads(data)
  except Exception:return {}
  finally:c.close()
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
  futures={name:executor.submit(get,name) for name in UIDS};return {name:f.result() for name,f in futures.items()}
def adapt(configmaps,now=None,local_node=None):
 now=time.time() if now is None else now;observers={};fresh=[]
 for name,uid in UIDS.items():
  role=name[-2:].upper()
  try:
   cm=configmaps[name];assert uid and cm['metadata']['uid']==uid;raw=cm['data']['sample.json'];assert len(raw.encode())<=196608
   v=json.loads(raw);assert v['schema']=='maimi-production-monitor-sample/v1' and v['environment']=='PRODUCTION' and v['observer']==role
   elapsed=age(v['sampled_at'],now);assert elapsed is not None
   observers[role]={'status':'observed' if elapsed<=120 else 'stale','sampled_at':v['sampled_at'],'age_seconds':elapsed,'sample':v}
   if elapsed<=120:fresh.append(role)
  except (KeyError,TypeError,ValueError,AssertionError):observers[role]={'status':'unavailable'}
 preference={'maimi-a1':'A1','maimi-a2':'A2'}.get(local_node)
 selected=preference if preference in fresh else max(fresh,key=lambda role:observers[role]['sampled_at']) if fresh else None
 sample=observers[selected]['sample'] if selected else {};hosts=sample.get('hosts',{});warnings=[];seen=set()
 for role,view in observers.items():
  if view['status']!='observed':warnings.append({'code':'OBSERVER_UNAVAILABLE','scope':role,'severity':'critical','message':MESSAGES['OBSERVER_UNAVAILABLE']});continue
  for alert in view['sample'].get('alerts',[]):
   key=(alert.get('code'),alert.get('scope'),alert.get('reason'))
   if key in seen:continue
   seen.add(key);item=dict(alert);item['message']=MESSAGES.get(item.get('code'),item.get('message','监控检查需要关注'));warnings.append(item)
 nodes=[{**n,'cpu_capacity':n.get('cpu_allocatable'),
         'cpu_used':n.get('cpu_used') if n.get('metrics_status')=='available' else None,
         'memory_used_bytes':n.get('memory_working_set_bytes') if n.get('metrics_status')=='available' else None}
        for n in sample.get('k3s',{}).get('nodes',[])]
 services=[]
 for role,host in hosts.items():
  if host.get('status')!='observed':continue
  for name,s in host.get('services',{}).get('units',{}).items():
   ready=s.get('ActiveState')=='active' and s.get('SubState')=='running'
   services.append({'name':role+' / '+name,'ready':ready,'phase':'Running' if ready else 'Unknown','display_state':'ready' if ready else 'not_ready','restarts':int(s.get('NRestarts') or 0)})
 # Pod data comes from the same identity-bound fresh observer as the host data.
 pods=sample.get('application_pods',{});pod_age=age(pods.get('sampled_at'),now)
 pod_fresh=pod_age is not None and pod_age<=120 and pods.get('status') in ('observed','partial')
 if pod_fresh:
  for row in pods.get('rows',[]):
   services.append({**row,'name':row.get('namespace','')+' / '+row.get('name',''),'source':'kubernetes_pod'})
 pod_coverage=('四个命名空间均已采集' if pods.get('status')=='observed' else '部分命名空间未取得状态') if pod_fresh else '未取得新鲜的 Pod 状态；不能按零个实例处理'
 if pod_fresh and pods.get('rows_truncated'):pod_coverage+='；列表较长，优先展示异常组件，数量汇总仍覆盖全部观测实例'
 # Display the observer actually selected, not an unavailable preferred host.
 chosen_host=hosts.get(selected or '',{});chosen_host=chosen_host if chosen_host.get('status')=='observed' else {};external=sample.get('entry',{}).get('domain',{'status':'unavailable'})
 backups={r:hosts.get(r,{}).get('database_backup',{'status':'unavailable','restore_status':'unknown'}) if hosts.get(r,{}).get('status')=='observed' else {'status':'unavailable','restore_status':'unknown'} for r in ('B1','B2')}
 return {'schema':'maimi-cluster-sample/v1','environment':'PRODUCTION','sampled_at':sample.get('sampled_at'),'freshness':{'state':'fresh' if selected else 'unavailable','age_seconds':int(observers[selected]['age_seconds']) if selected else None},
  'nodes':nodes,'services':services,'application_pods':pods if pod_fresh else {'status':'unavailable','rows':None},'host':chosen_host.get('resources',{}),'host_role':selected,'hosts':hosts,
  'observers':observers,'selected_observer':selected,'entry_observers':{r:v.get('sample',{}).get('entry',{}) for r,v in observers.items()},
  'external_entry':external,'local_app':sample.get('entry',{}).get({'A1':'AP1','A2':'AP2'}.get(selected,''),{'status':'unavailable'}),'database_backup':{'status':'not_configured','restore_status':'unknown'},'database_backup_hosts':backups,
  'postgres_hosts':{r:hosts.get(r,{}).get('postgres',{'status':'unavailable'}) if hosts.get(r,{}).get('status')=='observed' else {'status':'unavailable'} for r in ('B1','B2')},'alerts':warnings,
  'coverage':{'五台主机采集':'已接入' if len(hosts)==5 and all(h.get('status')=='observed' for h in hosts.values()) else '部分尚未取得',
   '两个独立观察视角':'均有新鲜数据' if len(fresh)==2 else '视角覆盖不完整','三节点资源指标':'实时集群API',
   '数据库复制与代理':'本机只读SQL、证书校验的健康接口及代理元数据','正式应用组件':pod_coverage,
   '开机恢复':'尚未验收','断电接续':'尚未验收','共享存储备份':'尚未确认','数据库备份':' / '.join(r+': '+{'available':'最近备份成功','failed':'最近备份失败','stale':'成功记录过期','not_configured':'尚未接入'}.get(b.get('status'),'结果未知') for r,b in backups.items()),'通知渠道':'当前仅页面提醒',
   '每用户资源与任务':'待接正式用户实例、任务和临时目录统计；缺失不按零计算'},
  'owners':{'status':'not_instrumented','rows':None,'missing':['instance_cpu_memory','owner_queue_wait','owner_failed_tasks','workspace_temporary_bytes']},
  'complete_application_HA_accepted':False}
def sample_view(path=None,now=None):
 try:maps=get_configmaps()
 except Exception:maps={}
 return adapt(maps,now,os.environ.get('MAIMI_MONITOR_LOCAL_NODE'))
