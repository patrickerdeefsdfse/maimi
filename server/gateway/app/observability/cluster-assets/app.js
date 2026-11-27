'use strict';
const MonitorView = (() => {
  const labels = {available:'已采集',unavailable:'采集不可用',unhealthy:'检查未通过',not_instrumented:'尚未接入',not_configured:'尚未配置',not_connected:'尚未接入',partial:'部分已采集',stale:'数据已过期',failed:'执行失败',requires_employee_verification:'需实际账号验证',not_applicable_single_node_test:'单机测试不适用',dashboard_only:'仅页面提醒，未接通知渠道',nodes:'节点指标',services:'应用组件',host:'本机资源',entry:'旧版入口探测',local_app:'本机到应用就绪检查',external_entry:'服务器到外部 HTTPS 入口',api:'集群管理接口',nas:'NAS',database_backup:'数据库备份',database_metrics:'数据库与排队指标',managed_storage:'托管文件统计',nas_backup:'NAS 备份',production:'正式集群',business_login:'实际登录与业务',remote_host_disks:'其他服务器磁盘',notifications:'告警通知',system:'系统盘',data:'数据盘',chat:'聊天池',execution:'执行池',collector:'采集器',database:'数据库',storage:'共享存储'};
  const alerts = {
    COLLECTOR_STALE:['采集数据已过期','检查采集定时器。下方旧数据不能代表此刻状态。'],
    COLLECTOR_UNAVAILABLE:['采集器没有可用数据','检查采集任务、文件权限和最近一次执行结果。'],
    NODE_UNHEALTHY:['计算节点未就绪或已报告资源压力','检查节点状态和主机资源；不要直接删除仍可能运行的任务。'],
    NODE_METRICS_MISSING:['节点用量指标没有采集到','检查指标服务及节点通信，缺少数据不代表用量为零。'],
    NODE_METRICS_STALE:['节点用量指标已过期','检查指标服务与采集时间，暂不根据旧用量判断资源充足。'],
    NODE_CPU_HIGH:['节点 CPU 使用接近调度预算','查看当前执行任务和排队；持续偏高时增加 CPU 或调整并发。'],
    NODE_MEMORY_BUDGET_HIGH:['节点工作集接近 K3s 调度预算','这是 kubelet 工作集口径，不等于主机内存耗尽。结合主机可用内存和节点压力判断。'],
    NODE_MEMORY_CAPACITY_DIFFERENT:['主机与 K3s 上报的总内存不同','核对扩容后的节点上报和预留配置；下方分别展示两种口径。'],
    HOST_CPU_HIGH:['主机 CPU 短时使用偏高','结合持续采样和当前任务判断，避免仅凭一次短时峰值扩容。'],
    HOST_MEMORY_HIGH:['主机实际可用内存偏低','检查当前进程和执行并发；不要把可回收缓存当作无法释放的内存。'],
    WORKLOAD_NOT_READY:['组件尚未就绪','检查启动状态、等待原因和最近一次发布。'],
    WORKLOAD_FAILED:['组件或一次性任务运行失败','查看该组件的结束原因，再决定是否修复；不要自动重放未知任务。'],
    WORKLOAD_TERMINATION_DELAYED:['组件结束超过 5 分钟','核对真实进程是否已停止；不要仅清除删除标记。'],
    RECENT_MEMORY_RESTART:['组件最近因内存限制重启','当前就绪与历史重启分别记录，请检查组件内存限制。'],
    DISK_CAPACITY:['磁盘可用容量偏低','清理已确认可删除的旧制品，或扩容；不要直接清业务目录。'],
    DISK_INODES:['磁盘可用文件数量偏低','检查小文件和缓存数量；即使还有容量，也可能无法新建文件。'],
    LOCAL_APP_UNAVAILABLE:['本机到应用的就绪检查未通过','查看下方健康与依赖检查，检查应用组件、数据库和内部服务通信。'],
    API_UNAVAILABLE:['集群管理接口未就绪','检查 K3s 服务与节点连接，业务入口状态需单独判断。'],
    EXTERNAL_ENTRY_FAILED:['服务器视角的外部入口检查未通过','这不等于员工电脑访问失败。按下方失败层次处理 DNS、网络或证书，不关闭证书校验。'],
    EXTERNAL_ENTRY_PARTIAL:['部分解析地址的入口检查失败','核对域名各解析地址和网络路径；不能把一次成功当作全部地址正常。'],
    DATABASE_BACKUP_NOT_CONFIGURED:['数据库备份尚未接入监控','完成备份任务安装并发布结果；未接入不能视为已有备份。'],
    DATABASE_BACKUP_METADATA_UNAVAILABLE:['数据库备份结果无法确认','检查备份结果文件和权限；不要据此声称备份成功。'],
    DATABASE_BACKUP_FAILED:['最近一次数据库备份失败','检查备份任务结果与目标磁盘，保留上一份成功备份。'],
    DATABASE_BACKUP_STALE:['最近成功备份已超过 36 小时','检查备份定时器和最近执行结果。'],
    BACKUP_SAME_HOST_ONLY:['目前只有同机备份','它能帮助回退，但不能抵御整台服务器或磁盘损坏；另需独立备份位置。'],
    DATABASE_RESTORE_NOT_VERIFIED:['备份尚未完成恢复验证','在独立验证库中完成恢复演练后，才能确认备份可用。'],
    DATABASE_RESTORE_FAILED:['备份恢复验证失败','保留失败证据与已有备份，检查归档完整性和恢复流程。'],
    DATABASE_METRICS_UNAVAILABLE:['数据库统计读取失败','检查数据库连接与只读统计权限；不以旧统计判定数据库正常。'],
    DATABASE_CONNECTIONS_HIGH:['数据库连接数偏高','检查连接池和空闲连接，避免达到连接上限。'],
    QUEUE_WAIT_HIGH:['请求排队偏长','检查执行池占用和资源；持续排队时调整并发或扩容，不重放未确认任务。'],
    QUEUE_METRICS_UNAVAILABLE:['排队指标无法确认','检查数据库统计，不能把未知队列显示为空。'],
    WORKER_OUTCOME_UNCERTAIN:['存在执行结果尚未确认的实例','先核对真实运行与停止证据，再接续；不要自动释放或重复执行。'],
    STORAGE_METRICS_UNAVAILABLE:['共享存储用量没有采集到','检查存储挂载和统计接口，不把未知容量当作可用。']
  };
  const reasons = {DNS_FAILED:'域名解析失败',TCP_TIMEOUT:'网络连接超时',TCP_CONNECTION_FAILED:'网络连接失败',TLS_CERTIFICATE_FAILED:'证书链或域名校验失败',TLS_HANDSHAKE_FAILED:'加密连接协商失败',TLS_TIMEOUT:'加密连接超时',HTTP_TIMEOUT:'健康响应超时',HTTP_CONNECTION_FAILED:'健康响应连接失败',HTTP_REDIRECT:'健康地址发生跳转',HTTP_UNHEALTHY:'健康地址返回异常状态',HEALTH_BODY_UNEXPECTED:'响应不是预期的应用健康结果',ENTRY_PROBE_TIMEOUT:'整段探测超时',PROBE_UNAVAILABLE:'未取得可用探测结果',INTERNAL_CONNECTION_FAILED:'内部服务连接失败'};
  const states = {CLAIMED:'正在分配',BOUND:'正在启动',READY:'可用',UNCERTAIN:'结果待确认',DELETING:'正在结束',WAITING:'等待中',completed:'已完成',terminating:'正在结束',ready:'运行正常',starting:'正在启动',failed:'运行失败',not_ready:'尚未就绪',Pending:'正在启动',Running:'运行中',Succeeded:'已完成',Failed:'运行失败',Unknown:'状态未知',passed:'通过',not_checked:'未检查'};
  const label = value => labels[value] ?? value ?? '未知';
  const bytes = n => typeof n !== 'number' || !Number.isFinite(n) ? '未知' : n >= 1073741824 ? (n/1073741824).toFixed(2)+' GiB' : (n/1048576).toFixed(1)+' MiB';
  const number = n => typeof n === 'number' && Number.isFinite(n) ? n.toFixed(2) : '未知';
  const percent = n => typeof n === 'number' && Number.isFinite(n) ? (n*100).toFixed(1)+'%' : '未知';
  const timestamp = n => typeof n === 'number' && Number.isFinite(n) ? new Date(n*1000).toLocaleString('zh-CN') : '尚无记录';
  function message(a) {
    let pair=alerts[a.code];
    if(!pair && String(a.code).endsWith('_COLLECTION_FAILED')) pair=['部分监控项目采集失败','检查该项目的采集程序和权限，缺失数据不能视为正常。'];
    if(!pair) pair=[a.message||'监控检查需要关注','查看对应服务和最近采集结果。'];
    const detail=[];
    if(a.reason)detail.push(reasons[a.reason]??'探测未完成');
    if(typeof a.percent==='number')detail.push(a.percent.toFixed(1)+'%');
    if(typeof a.waiting==='number')detail.push(a.waiting+' 个请求等待');
    if(typeof a.oldest_seconds==='number')detail.push('最长 '+a.oldest_seconds+' 秒');
    return {title:pair[0],action:pair[1],detail:detail.join(' · '),scope:label(a.scope),severity:a.severity||'warning'};
  }
  function serviceStatus(row) {
    const state=row.display_state || (row.phase==='Succeeded'?'completed':row.ready?'ready':row.phase==='Failed'?'failed':row.deleting_at?'terminating':'not_ready');
    return {state,label:states[state]??'状态未知',readiness:state==='completed'?'不适用（一次性任务）':state==='terminating'?'正在结束':row.ready?'已就绪':'未就绪'};
  }
  function hostMemory(host={}) {
    const m=host.memory??{},known=typeof m.MemTotal==='number'&&m.MemTotal>0&&typeof m.MemAvailable==='number';
    return {total:bytes(m.MemTotal),available:bytes(m.MemAvailable),used:known?bytes(m.MemTotal-m.MemAvailable):'未知',usedRatio:known?(m.MemTotal-m.MemAvailable)/m.MemTotal:null,cached:bytes(m.Cached),reclaimableSlab:bytes(m.SReclaimable)};
  }
  function entryStatus(v={}) {
    return v.status==='application_ready'?'应用依赖就绪':v.status==='placeholder_ready'?'仅测试页就绪':v.status==='available'?'检查通过':v.status==='unhealthy'?'检查未通过':'未能完成检查';
  }
  const known = x => typeof x==='number' && Number.isFinite(x);
  function hostsRows(v) {
    return ['A1','A2','B1','B2','C'].map(role=>{
      const h=v.hosts?.[role]??{}, observed=h.status==='observed',r=observed?h.resources??{}:{},m=hostMemory(r);
      return [role,observed?'已采集':label(h.status??'unavailable'),timestamp(h.sampled_at),percent(r.cpu_used_ratio),m.available+' / '+m.total,bytes(r.disks?.system?.available_bytes)+' / '+bytes(r.disks?.system?.total_bytes),bytes(r.disks?.data?.available_bytes)+' / '+bytes(r.disks?.data?.total_bytes)];
    });
  }
  function observerRows(v) {
    return ['A1','A2'].map(role=>{const o=v.observers?.[role]??{};return [role,o.status==='observed'?'数据新鲜':label(o.status??'unavailable'),timestamp(o.sampled_at),known(o.age_seconds)?Math.round(o.age_seconds)+' 秒':'未知',v.selected_observer===role?'当前展示来源':'独立核对来源'];});
  }
  function ownerRows(v) {
    const rows=[];
    for(const owner of v.owners?.rows??[]){
      const queues=Object.entries(owner.queues??{}).map(([k,q])=>label(k)+' '+(q.waiting??'未知')+' 等待 / '+(known(q.oldest_seconds)?Math.ceil(q.oldest_seconds)+' 秒':'未知')).join('；');
      const instances=owner.instances?.length?owner.instances:[null];
      for(const i of instances){const u=i?.usage??{},ok=u.status==='available';rows.push([owner.owner_label??'用户',i?label(i.pool)+' / '+(states[i.state]??'未知'):'当前无绑定实例',i?.execution_node||'尚未分配',ok?number(u.cpu_cores)+' 核':'用量未知',ok?bytes(u.memory_bytes):'用量未知',i?number(i.cpu_millis/1000)+' 核 / '+bytes(i.memory_mib*1048576):'不适用',queues,bytes(owner.workspace_bytes),bytes(owner.temporary_bytes)]);}
    }
    return rows;
  }
  return {labels,states,reasons,label,bytes,number,percent,timestamp,message,serviceStatus,hostMemory,entryStatus,hostsRows,observerRows,ownerRows};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=MonitorView;
if(typeof document!=='undefined'){
const $=id=>document.getElementById(id),V=MonitorView;
function node(tag,text,className){const n=document.createElement(tag);n.textContent=text??'';if(className)n.className=className;return n;}
const pages=new Map();
function table(id,rows){
 let p=pages.get(id);if(!p){p={page:0,term:'',rows:[]};pages.set(id,p);}
 p.rows=rows;
 function draw(){let filtered=p.rows.filter(r=>r.some(x=>String(x).toLowerCase().includes(p.term)));const count=Math.max(1,Math.ceil(filtered.length/20));p.page=Math.min(p.page,count-1);
  $(id).replaceChildren(...filtered.slice(p.page*20,p.page*20+20).map(row=>{const tr=node('tr');for(const value of row)tr.append(node('td',value));return tr;}));
  if(p.info){p.info.textContent='第 '+(p.page+1)+' / '+count+' 页 · '+filtered.length+' 项';p.prev.disabled=p.page===0;p.next.disabled=p.page>=count-1;}}
 if(rows.length>20&&!p.control){p.control=node('div',null,'table-controls');const input=node('input');input.type='search';input.placeholder='搜索当前列表';input.setAttribute('aria-label','搜索列表');p.info=node('span');p.prev=node('button','上一页');p.next=node('button','下一页');p.control.append(input,p.info,p.prev,p.next);$(id).closest('.table-scroll').before(p.control);
  input.addEventListener('input',()=>{p.term=input.value.toLowerCase();p.page=0;draw();});p.prev.addEventListener('click',()=>{p.page--;draw();});p.next.addEventListener('click',()=>{p.page++;draw();});}
 draw();
}
function cards(id,rows){$(id).replaceChildren(...rows.map(([title,value,hint])=>{const n=node('div',null,'card');n.append(node('span',title),node('strong',value),node('span',hint));return n;}));}
function render(v){
 $('content').hidden=false;$('access').hidden=true;$('error').hidden=true;
 document.dispatchEvent(new CustomEvent('maimi-monitor-snapshot',{detail:v}));
 const fresh=v.freshness?.state==='fresh';$('content').classList.toggle('stale',!fresh);
 $('freshness').textContent=(v.environment??'')+' · '+(fresh?'采样 '+(v.freshness.age_seconds??0)+' 秒前':'数据不可用或已过期')+' · '+V.timestamp(v.generated_at);$('freshness').className=fresh?'ok':'warning';
 const production=v.environment==='PRODUCTION';
 const db=v.database??{},disk=v.host?.disks?.data,mem=V.hostMemory(v.host),ready=(v.nodes??[]).filter(x=>x.ready).length;
 cards('cards',[['计算节点',(v.nodes??[]).length?ready+'/'+v.nodes.length:'尚未取得','就绪 / 已观察'],['主机实际可用内存'+(production?' · '+(v.host_role??'未知'):''),mem.available,'物理总量 '+mem.total+'；已用 '+mem.used],['数据盘可用',V.bytes(disk?.available_bytes),'总容量 '+V.bytes(disk?.total_bytes)],['应用自身就绪',V.entryStatus(v.local_app),production?'当前 AP → HTTP /ready':'本机 → 应用内部服务 /ready']]);
 const all=[...(v.alerts??[]),...(v.storage?.alerts??[])];
 $('alerts').replaceChildren(...(all.length?all.map(x=>{const m=V.message(x),p=node('div',null,'alert '+(m.severity==='critical'?'critical':m.severity==='info'?'notice':'warning'));p.append(node('strong',m.title+(m.scope?' · '+m.scope:'')),node('p',m.detail),node('p',m.action));return p;}):[node('p','当前已接入项目没有活动告警；仍需查看尚未覆盖的项目。','ok')]));
 const external=v.external_entry??{};
 cards('entry-cards',[['应用内部 /health',V.entryStatus(v.local_app?.checks?.['/health']),'进程健康，不代表员工能登录'],['应用内部 /ready',V.entryStatus(v.local_app?.checks?.['/ready']),'应用依赖与就绪检查'],['服务器访问 HTTPS 入口',V.entryStatus(external),'仅代表'+(v.host_role??'本机')+'视角，保留证书校验']]);
 const stepLabel=value=>({passed:'通过',failed:'失败',not_checked:'未检查'})[value]??'未能完成检查';
 table('entry-steps',[['域名解析',stepLabel(external.dns)],['网络连接',stepLabel(external.tcp)],['证书与加密连接',external.tls_verified?'验证通过':external.tls==='failed'?'验证未通过':'未完成验证'],['应用健康响应',external.http_status??'未取得响应'],['需要处理',V.reasons[external.reason]??(external.status==='available'?'本次检查通过':'尚无可用结果')]]);
 $('entry-note').textContent='本机内部就绪正常与服务器无法解析域名可以同时发生。此处不替代员工电脑登录、聊天和文件操作验收。';
 cards('host-cards',[['主机已用 / 总内存',mem.used+' / '+mem.total,'已用按总量减 MemAvailable 计算，约 '+V.percent(mem.usedRatio)],['主机可用内存',mem.available,'包含内核估计可安全回收的内存'],['页缓存 / 可回收内核缓存',mem.cached+' / '+mem.reclaimableSlab,'缓存有助于性能，不等于内存泄漏'],['主机 CPU 短时使用',V.percent(v.host?.cpu_used_ratio),'本次约 0.2 秒采样；负载 '+(v.host?.load??[]).map(V.number).join(' / ')]]);
 table('disks',Object.entries(v.host?.disks??{}).map(([name,d])=>[V.label(name),V.bytes(d.available_bytes)+' / '+V.bytes(d.total_bytes),d.total_bytes>0?V.percent(1-d.available_bytes/d.total_bytes):'未知',d.inodes_total>0?d.inodes_available+' / '+d.inodes_total:'文件系统未提供',d.inodes_total>0?V.percent(1-d.inodes_available/d.inodes_total):'不适用']));
 table('nodes',(v.nodes??[]).map(x=>[x.name,x.ready?(x.pressure?.length?'已报告资源压力':'就绪'):'未就绪',V.number(x.cpu_used)+' / '+V.number(x.cpu_capacity),V.bytes(x.memory_used_bytes),V.bytes(x.memory_allocatable_bytes??x.memory_capacity_bytes),x.memory_allocatable_bytes===undefined?'旧版采集未区分':V.bytes(x.memory_capacity_bytes),V.label(x.metrics_status)]));
 cards('pools',Object.entries(db.pools??{}).map(([k,p])=>[V.label(k),(p.waiting??'未知')+' 个请求等待','最长 '+Math.ceil(p.oldest_seconds??0)+' 秒 · '+Object.entries(p.states??{}).filter(([s])=>s!=='RELEASED').map(([s,n])=>(V.states[s]??'其他状态')+': '+n).join('，')]));
 $('database-summary').textContent=db.status==='available'?'数据库连接 '+db.connections+' / '+db.max_connections+'；数据量 '+V.bytes(db.bytes)+'；当前连接到'+(db.standby?'备库':'主库'):'数据库与排队指标暂时无法确认。';
 $('production-details').hidden=!production;
 if(production){
  table('production-hosts',V.hostsRows(v));table('production-observers',V.observerRows(v));
  table('production-nas',['A1','A2','C'].map(role=>{const h=v.hosts?.[role]??{},n=h.status==='observed'?h.nas??{}:{};return [role,n.status==='observed'?'已挂载并采集':V.label(n.status??'unavailable'),n.source??'未知',V.bytes(n.available_bytes)+' / '+V.bytes(n.total_bytes),typeof n.inodes_available==='number'?n.inodes_available:'未知'];}));
  table('production-entry',Object.entries(v.entry_observers??{}).flatMap(([role,entries])=>Object.entries(entries).map(([name,e])=>[role,name,({placeholder_ready:'仍为负载均衡测试页',unqualified:'应用健康契约待确认',available:'检查通过',application_ready:'应用依赖就绪',unavailable:'未完成检查',unhealthy:'检查未通过'})[e.status]??'未知',e.dns==='passed'?'解析通过':e.dns==='explicit_address_not_dns_test'?'指定地址，未测 DNS':e.dns==='failed'?'解析失败':'未检查',e.tls_verified?'证书校验通过':e.tls==='not_applicable_private_http'?'内部 HTTP':'未完成证书验证',V.reasons[e.reason]??e.reason??'—'])));
  table('production-db',['B1','B2'].map(role=>{const p=v.postgres_hosts?.[role]??{};return [role,p.status==='observed'?(p.recovery?'备库':'主库'):'状态未知',p.connections??'未知',V.bytes(p.database_bytes),p.recovery?(p.receiver??[]).map(x=>x.status).join(' / ')||'未取得复制状态':(p.replication??[]).map(x=>x.name+' '+x.state+' / '+x.sync_state).join('；')||'未取得副本状态'];}));
  table('production-backups',['B1','B2'].map(role=>{const b=v.database_backup_hosts?.[role]??{};return [role,V.label(b.status??'unavailable'),V.timestamp(b.last_success_at),b.restore_status==='verified'?'验证通过':b.restore_status==='failed'?'验证失败':'未验证',V.timestamp(b.restore_verified_at),({local_same_host:'同机备份',shared_nas:'共享 NAS（共用存储故障范围）',independent_off_host:'独立异机位置'})[b.destination_scope]??'未确认'];}));
  table('production-owners',V.ownerRows(v));
  $('owners-summary').textContent=!Array.isArray(v.owners?.rows)?'用户资源统计尚未取得；不显示为零。':(v.owners?.rows?.length??0)+' 个有实例或正在排队的用户；'+(v.owners?.page_complete?'当前列表完整。':'仅显示前 500 个用户，列表不完整。');
 }
 $('single-backup').hidden=production;
 const backup=v.database_backup??{};
 cards('backup-cards',[['最近备份状态',({available:'最近一次成功',failed:'最近一次失败',stale:'成功记录已过期',not_configured:'尚未接入',unavailable:'结果无法确认'})[backup.status]??'尚未接入','最近尝试：'+V.timestamp(backup.last_attempt_at)],['最近成功备份',V.timestamp(backup.last_success_at),'归档大小 '+V.bytes(backup.archive_bytes)],['最近恢复验证',backup.restore_status==='verified'?'已完成':backup.restore_status==='failed'?'验证失败':'尚未验证',V.timestamp(backup.restore_verified_at)]]);
 $('backup-note').textContent=backup.destination_scope==='local_same_host'?'当前是本机私有备份，保留最近 '+backup.retention_count+' 份；不能抵御整机或磁盘损坏，尚不代表已有独立异地备份。':'备份范围尚未确认，不能把未接入当作备份完成。';
 table('services',(v.services??[]).map(x=>{const s=V.serviceStatus(x);return[x.name,s.label,s.readiness,x.restarts??0];}));
 $('coverage').replaceChildren(...Object.entries(v.coverage??{}).map(([k,s])=>node('p',V.label(k)+'：'+V.label(s))));
 const storage=v.storage??{},global=storage.global??{};
 $('storage-summary').textContent=storage.status==='available'?'共享池占用 '+V.bytes(global.charged_bytes)+' / '+V.bytes(global.limit_bytes):'存储统计：'+V.label(storage.status);
 if(storage.users_page_complete===false)$('storage-summary').textContent+='；用户明细仅前 500 条，总池统计覆盖全部用户。';
 table('storage-users',(storage.users??[]).map(x=>[x.owner_label??'未归属',V.bytes(x.charged_bytes),V.bytes(x.soft_quota_bytes),V.bytes(x.borrowed_bytes)]));
}
// Trend views use the same authenticated snapshots as the existing detail tables.
const roles = ['A1', 'A2', 'B1', 'B2', 'C'];
const colors = ['#85e0d0', '#a6bcff', '#f3c683', '#edace2', '#ff9fa6'];
const historyRanges = {day: 1, week: 7, month: 30};
let snapshot = null, history = null, range = 'day', panel = 'work';
let clusterRequest = null, historyRequest = null, signedOut = false;
const dateLabel = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'numeric', day:'numeric'});
const timeLabel = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hour12:false});
function labelAt(at, time) {
  return dateLabel.format(at * 1000) + (time ? ' ' + timeLabel.format(at * 1000) : '');
}

function valueLabel(value, unit) {
  if (!Number.isFinite(value)) return '未采集';
  if (unit === '%') return V.percent(value);
  if (unit === 'bytes') return V.bytes(value);
  return Number(value.toFixed(1)).toLocaleString('zh-CN') + (unit ? ' ' + unit : '');
}

function trend(id, series, unit, daily = false) {
  const canvas = $(id + '-chart');
  if (canvas.closest('[hidden]')) return;
  const width = Math.max(180, Math.floor(canvas.getBoundingClientRect().width)), height = 184;
  const scale = Math.min(devicePixelRatio || 1, 2);
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext('2d');
  ctx.scale(scale, scale);
  ctx.font = '11px system-ui';
  ctx.fillStyle = '#e0e5ea';
  const points = series.flatMap(g => g.points);
  const legendNode = $(id + '-legend');
  legendNode.replaceChildren();
  if (!points.length) {
    const message = history?.status === 'available' ? '此时段暂无采样' : '等待可用的历史统计';
    ctx.textAlign = 'center';
    ctx.fillText(message, width / 2, height / 2);
    canvas.setAttribute('aria-label', canvas.closest('article').querySelector('h3').textContent + '，' + message);
    return;
  }
  const end = history.generated_at || Date.now()/1000;
  const step = daily ? 86400 : history.step;
  const origin = daily ? Math.floor((end+28800)/86400)*86400-28800 : end;
  const leftTime = daily ? origin - (Math.max(2,historyRanges[range])-1)*86400 : end - historyRanges[range]*86400;
  const rightTime = origin;
  const peak = Math.max(1, ...points.map(p => p[1]));
  const high = unit === '%' ? Math.max(1, peak) : peak * 1.08;
  const left = unit === 'bytes' ? 74 : 47, right = width - 10, top = 12, bottom = height - 28;
  const x = t => left + (t - leftTime) / (rightTime - leftTime) * (right-left);
  const y = v => bottom - v / high * (bottom-top);
  for (let i=0; i<=3; i++) {
    const v = high*i/3, row = y(v);
    ctx.beginPath();
    ctx.moveTo(left,row); ctx.lineTo(right,row);
    ctx.strokeStyle = '#737e89'; ctx.stroke();
    const axis = unit==='bytes' ? V.bytes(v) : unit==='%' ? Math.round(v*100)+'%' : Number(v.toFixed(1)).toString();
    ctx.fillText(axis, 1, row+3);
  }
  const tooltipRows = [];
  ctx.save(); ctx.beginPath(); ctx.rect(left-3, top-3, right-left+6, bottom-top+6); ctx.clip();
  series.forEach(({name, points: pts}, index) => {
    ctx.strokeStyle = colors[index % colors.length];
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 2;
    ctx.setLineDash(index % 2 ? [5, 2] : []);
    let previous = null;
    ctx.beginPath();
    for (const p of pts) {
      if (!previous || p[0]-previous[0] > step*1.5) ctx.moveTo(x(p[0]),y(p[1]));
      else ctx.lineTo(x(p[0]),y(p[1]));
      previous = p;
    }
    ctx.stroke();
    // Isolated samples remain visible, including the final point in each run.
    pts.forEach((p,i) => {
      if (i === pts.length-1 || pts[i+1][0]-p[0] > step*1.5 || i === 0) {
        ctx.beginPath(); ctx.arc(x(p[0]), y(p[1]), 2.3, 0, 2*Math.PI); ctx.fill();
      }
    });
    const recent = pts.at(-1);
    const text = name + ' ' + valueLabel(recent?.[1],unit);
    tooltipRows.push(recent ? text + '，' + labelAt(recent[0],!daily) : text);
    const item = node('span',text,'legend-item color-'+index);
    item.title = recent ? '最后采样 ' + labelAt(recent[0],!daily) : '此时段暂无采样';
    legendNode.append(item);
  });
  ctx.restore();
  ctx.fillStyle = '#e0e5ea'; ctx.textAlign = 'left';
  ctx.fillText(labelAt(leftTime,!daily && range==='day'), left, height-6);
  ctx.textAlign = 'right'; ctx.fillText(labelAt(rightTime,!daily && range==='day'), right, height-6);
  canvas.setAttribute('aria-label', tooltipRows.join('；'));
}

function renderCharts() {
  document.querySelectorAll('[data-panel]').forEach(p => p.hidden = p.dataset.panel !== panel);
  document.querySelectorAll('[data-chart-tab]').forEach(b => b.setAttribute('aria-pressed',String(b.dataset.chartTab===panel)));
  const rows = history?.status === 'available' ? history.series : [];
  const series = (metric, scope, name, field='average') => ({
    name, points: rows.filter(p=>p.metric===metric && p.scope===scope && Number.isFinite(p[field])).map(p=>[p.bucket,p[field]])
  });
  const pools = (metric, field) => ['chat','execution'].map(k => series(metric,k,V.label(k),field));
  if (panel==='work') {
    trend('sandbox', pools('allocated','average'), '个');
    trend('queue', pools('waiting','high'), '个');
    trend('delay', pools('wait_seconds','high'), '秒');
  } else if (panel==='resource') {
    for (const [id,metric] of [['cpu','host_cpu'],['memory','host_memory'],['system','disk_system'],['disk','disk_data']]) {
      trend(id,roles.map(role=>series(metric,role,role)),'%');
    }
    trend('files',[series('storage_bytes','all','占用')],'bytes');
    trend('db',[series('db_connections','all','连接')],'个');
  } else {
    const fields = (source, spec) => spec.map(([key,name])=>({
      name, points:(history?.[source]??[]).filter(r=>Number.isFinite(r[key])).map(r=>[r.day,r[key]])
    }));
    trend('activity',fields('days',[['dau','日活'],['wau','七日活跃']]),'人',true);
    trend('calls',fields('days',[['model_requests','模型调用'],['failed','失败操作']]),'次',true);
    trend('platform',fields('channels',[['web','网页版'],['desktop','桌面版']]),'人',true);
  }
}

// Native meters keep proportional bars accessible and work under a strict CSP.
function ranking(id, rows, unit, empty) {
  const div = $(id);
  div.replaceChildren();
  const items = rows.filter(r=>Number.isFinite(r.value) && r.value>=0).sort((a,b)=>b.value-a.value).slice(0,10);
  if (!items.length) { div.append(node('p',empty,'muted')); return; }
  for (const item of items) {
    const row = node('div',null,'ranking-row');
    const label = node('span',item.name);
    const value = node('strong',valueLabel(item.value,unit));
    const bar = document.createElement('meter');
    bar.min = 0; bar.max = Math.max(items[0].value,1); bar.value = item.value;
    bar.setAttribute('aria-label',item.name + ' ' + value.textContent);
    row.append(label,value,bar); div.append(row);
  }
}

function meterLine(name,ratio,value) {
  const line=node('div',null,'meter-line');
  line.append(node('span',name),node('b',value));
  if(Number.isFinite(ratio)) {
    const bar=document.createElement('meter');
    bar.min=0; bar.max=1; bar.value=Math.min(1,Math.max(0,ratio));
    bar.setAttribute('aria-label',name+' '+value);
    line.append(bar);
  }
  return line;
}

function renderLive(v) {
  snapshot=v;
  renderDevices();
  const production=v.environment==='PRODUCTION';
  const fleet=production?roles:[v.host_role||'测试机'];
  $('fleet').replaceChildren(...fleet.map(role=>{
    const host=production?v.hosts?.[role]:{status:v.host?'observed':null,resources:v.host};
    const observed=host?.status==='observed', r=observed?host.resources??{}:{}, m=V.hostMemory(r);
    const box=node('article',null,'host-tile');
    box.append(node('strong',role),node('span',observed?'已采集':'未采集','muted'),meterLine('CPU',r.cpu_used_ratio,V.percent(r.cpu_used_ratio)),meterLine('内存',m.usedRatio,V.percent(m.usedRatio)));
    return box;
  }));
  const owners=(v.owners?.rows??[]).map(o=>{
    const measured=(o.instances??[]).filter(i=>i.usage?.status==='available' && Number.isFinite(i.usage.memory_bytes));
    return {name:o.owner_label + (measured.length !== o.instances?.length ? '（部分）' : ''), value:measured.length ? measured.reduce((n,i)=>n+i.usage.memory_bytes,0):null};
  });
  ranking('owner-ranking',owners,'bytes','当前无已采到内存的用户实例。');
  const storage=v.storage??{};
  ranking('storage-ranking', (storage.users??[]).map(u=>({name:u.owner_label??'未归属',value:u.charged_bytes})),'bytes',storage.status==='available'?'当前没有已计量的用户文件。':'用户文件用量未采集。');
  $('storage-scope').textContent=(storage.users_page_complete===false?'已返回用户中的前十位。':'当前已采到的个人用量。')+'包含上传预留。';
  const g=storage.status==='available'?storage.global??{}:{};
  const used=Number.isFinite(g.charged_bytes)&&g.limit_bytes>0?g.charged_bytes/g.limit_bytes:null;
  $('storage-gauge').replaceChildren(meterLine('托管文件及上传预留',used,Number.isFinite(used)?V.percent(used):'未采集'),node('p',V.bytes(g.charged_bytes)+' / '+V.bytes(g.limit_bytes),'muted'));
  const backups=production?['B1','B2'].map(role=>[role,v.database_backup_hosts?.[role]]):[['本机',v.database_backup]];
  $('backup-glance').replaceChildren(...backups.map(([role,b])=>{
    const line=node('div',null,'backup-line');
    line.append(node('strong',role),node('span',b?.last_success_at?V.timestamp(b.last_success_at):'暂无成功时间'),node('small',b?.restore_status==='verified'?'已通过恢复验证':b?.restore_status==='failed'?'恢复验证失败':'恢复待验证'));
    return line;
  }));
}

function renderDevices() {
  const release = snapshot?.desktop_release;
  const devices = history?.devices ?? [];
  const available = history?.status === 'available';
  ranking('device-summary',devices.map(r=>({name:r.version+' · 最近在线 '+r.recently_online,value:r.devices})),
    '台',available?'采样期内尚无设备登记。':'桌面设备暂未取得。');
  const current = devices.find(row => row.version === release);
  const count = available && (current || devices.length < 100) ? (current?.recently_online ?? 0) : null;
  const text = release ? '公司当前发布 '+release+(count === null ? '' : ' · 最近在线且已使用该版本 '+count+' 台') : '当前公司发布版本暂不可用';
  $('device-summary').prepend(node('p',text));
}

function renderHistory(v) {
  history=v;
  const available=v?.status==='available';
  const d=v?.devices??[];
  const online=d.reduce((n,r)=>n+r.recently_online,0);
  $('history-status').textContent=available
    ? '最近采样 '+V.timestamp(v.last_sample)+' · '+(v.last_sample && Date.now()/1000-v.last_sample<=150?'采集中':'采集延迟')+(v.last_event_loss?' · 有统计事件未保存':'')
    : '历史统计暂不可用';
  cards('history-kpis',[
    ['今日活跃用户',available?v.activity?.dau??'未采集':'未采集','按实际操作去重'],
    ['近七日活跃用户',available?v.activity?.wau??'未采集':'未采集','按用户跨天去重'],
    ['桌面最近在线',available&&d.length?online:'未采集到设备','最近三分钟的设备数'],
    ['监控表占用',available?V.bytes(v.database_bytes):'未采集','含历史统计和索引']
  ]);
  renderDevices();
  ranking('model-ranking',(v?.users??[]).map(u=>({name:u.owner_label,value:u.model_requests??0})),'次',available?'此时段暂无使用记录。':'用户排行暂未取得。');
  table('history-users',(v?.users??[]).map(u=>[u.owner_label,u.model_requests??0,u.active_days,u.failed,u.token_samples?(u.input_tokens+u.output_tokens).toLocaleString():'未计量',V.timestamp(u.last_seen)]));
  $('people-note').textContent=available?'按模型调用排序。'+(v.users_complete?'显示此时段全部已记录用户。':'当前只显示前 500 位。'):'用户统计暂不可用。';
  const r=v?.retention;
  $('history-retention').textContent=r?'分钟明细保留 '+r.minutes_days+' 天，小时汇总 '+r.hours_days+' 天，日汇总 '+r.daily_days+' 天。逐用户和设备统计保留 '+r.per_user_days+' 天。':'';
  renderCharts();
}

function accessLost(status) {
  signedOut=true;
  clusterRequest?.abort();
  historyRequest?.abort();
  snapshot=null; history=null; pages.clear();
  $('content').hidden=true;
  $('content').replaceChildren();
  $('freshness').textContent=status===401?'等待登录':'访问受限';
  $('access').hidden=status!==401;
  $('error').hidden=status!==403;
  if (status===403) $('error').textContent='当前账号没有监控权限。';
  $('refresh').disabled=false;
}

async function refreshHistory() {
  if (!snapshot || signedOut || document.hidden) return;
  historyRequest?.abort();
  const ctrl=new AbortController(), chosen=range;
  historyRequest=ctrl;
  const timeout=setTimeout(()=>ctrl.abort(),12000);
  try {
    const response=await fetch('/v1/developer-monitor/history/'+chosen,{credentials:'same-origin',cache:'no-store',signal:ctrl.signal});
    if (signedOut || historyRequest!==ctrl) return;
    if(response.status===401 || response.status===403) { accessLost(response.status); return; }
    if(!response.ok) throw Error('history unavailable');
    const v=await response.json();
    if (!signedOut && historyRequest===ctrl) renderHistory(v);
  } catch (_) {
    if(!signedOut && historyRequest===ctrl) renderHistory(null);
  } finally {
    clearTimeout(timeout);
    if(historyRequest===ctrl) historyRequest=null;
  }
}

async function refresh(manual=false) {
  if(signedOut) { if(manual) location.reload(); return; }
  if(clusterRequest || document.hidden) return;
  const ctrl=new AbortController(), timeout=setTimeout(()=>ctrl.abort(),20000);
  clusterRequest=ctrl; $('refresh').disabled=true;
  try {
    const summaryResponse = await fetch('/v1/developer-monitor/overview', {
      credentials: 'same-origin', cache: 'no-store', signal: ctrl.signal
    });
    if (summaryResponse.status === 401 || summaryResponse.status === 403) {
      accessLost(summaryResponse.status);
      return;
    }
    if (!summaryResponse.ok) throw Error('运行概况暂时无法读取，请稍后刷新。');
    const summary = await summaryResponse.json();
    if (signedOut) return;
    if (!summary.can_view_details && snapshot) {
      location.reload();
      return;
    }
    $('content').hidden = false;
    $('error').hidden = true;
    $('access').hidden = true;
    const fresh = summary.freshness === 'fresh';
    $('content').classList.toggle('stale', !fresh);
    $('freshness').textContent = fresh ? '最近采样 ' + V.timestamp(summary.sampled_at)
      : summary.freshness === 'stale' ? '采样已过期' : '采样暂不可用';
    $('freshness').className = fresh ? 'ok' : 'warning';
    const status = {application_ready: '可用', available: '可用', unhealthy: '异常'};
    const waiting = pool => Number.isInteger(summary.waiting?.[pool]) ? summary.waiting[pool] + ' 个' : '未取得';
    cards('service-cards', [
      ['应用服务', status[summary.app_status] ?? '待确认', '最近一次就绪检查'],
      ['聊天排队', waiting('chat'), '正在等待的聊天请求'],
      ['任务排队', waiting('execution'), '正在等待的执行请求']
    ]);
    if (!summary.can_view_details) return;
    const response=await fetch('/v1/developer-monitor/cluster',{credentials:'same-origin',cache:'no-store',signal:ctrl.signal});
    if(signedOut) return;
    if(response.status===401 || response.status===403) { accessLost(response.status); return; }
    if(!response.ok) throw Error('实时采集暂不可用，请稍后刷新。');
    const value=await response.json();
    if(signedOut) return;
    $('operations').hidden=false;
    render(value); renderLive(value);
    if(manual || !history || Date.now()-lastHistoryAttempt>=60000) {
      lastHistoryAttempt=Date.now();
      void refreshHistory();
    }
  } catch(e) {
    if(signedOut) return;
    $('error').hidden=false; $('error').textContent=e.name==='AbortError'?'采集请求超时。':e.message;
    $('freshness').textContent='采集失败 · 上次结果可能过期';
    $('content').classList.add('stale');
  } finally {
    clearTimeout(timeout); clusterRequest=null; $('refresh').disabled=false;
  }
}
let lastHistoryAttempt=0;
document.querySelectorAll('[data-window]').forEach(b=>b.addEventListener('click',()=>{
  if(!snapshot || signedOut || b.dataset.window===range) return;
  range=b.dataset.window;
  document.querySelectorAll('[data-window]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));
  renderHistory(null); $('history-status').textContent='正在读取所选时段…'; void refreshHistory();
}));
document.querySelectorAll('[data-chart-tab]').forEach(b=>b.addEventListener('click',()=>{panel=b.dataset.chartTab;renderCharts();}));

$('refresh').addEventListener('click',()=>refresh(true));
let resizeTimer;
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(history && !signedOut)renderCharts();},160);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden && !signedOut)refresh(true);});
setInterval(()=>refresh(),30000);
void refresh();

}
