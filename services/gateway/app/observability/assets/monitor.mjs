const API='/v1/developer-monitor';
export const labels={cpu_cores:'CPU 使用',memory_used_bytes:'内存使用',memory_limit_bytes:'内存限额',pids_current:'进程与线程',pids_limit:'进程限额',storage_bytes:'存储占用',requests:'请求次数',failed:'失败次数',duration_p95_ms:'95% 请求耗时',input_tokens:'输入 Token',output_tokens:'输出 Token',cache_read_tokens:'缓存读取 Token',cache_write_tokens:'缓存写入 Token',network_rx_bytes:'网络接收累计',network_tx_bytes:'网络发送累计',network_rx_bytes_per_second:'网络接收速率',network_tx_bytes_per_second:'网络发送速率',io_read_bytes:'磁盘读取累计',io_write_bytes:'磁盘写入累计',io_read_bytes_per_second:'磁盘读取速率',io_write_bytes_per_second:'磁盘写入速率',oom_kills:'内存不足终止次数',restarts:'容器重启次数',pids_limit_hits:'进程限额触发次数',cpu_throttled_seconds:'CPU 限流累计时间'};
const reasonLabels={NOT_INSTRUMENTED:'尚未接入计量',NOT_REPORTED:'尚未收到采样',NO_RESOURCE_SAMPLE:'无资源采样',PARTIAL_RESOURCE_SAMPLE:'部分资源缺少采样',DELTA_SAMPLE_REQUIRED:'等待连续采样',PROVIDER_USAGE_MISSING:'模型未返回完整用量',NO_DURATION_SAMPLES:'暂无耗时样本',NOT_APPLICABLE:'不适用',LOCAL_DESKTOP_TELEMETRY_NOT_ENABLED:'桌面本地尚未接入采集',OWNER_BINDINGS_UNAVAILABLE:'用户归属暂不可核验'};
const statuses={running:'运行中',created:'待启动',stopped:'已停止',exited:'已退出',restarting:'重启中',multiple_instances:'多实例运行',unknown:'未知',fresh:'正常',stale:'数据过期',missing:'无采样',available:'已接入',partial:'部分采集',unavailable:'暂不可用',not_reported:'未采集',not_instrumented:'未接入',not_implemented:'未接入',platform_aggregates_only:'平台汇总趋势'};
const serviceNames={'gateway':'平台网关','personal-cloud-manager':'工作区管理器','office-service':'共享 Office','service-broker':'远程服务器连接','postgres':'数据库','redis':'缓存与会话','downloads':'文件下载','retention-worker':'保留与回收'};
const alertText={CONTAINER_INVENTORY_UNAVAILABLE:['容器清单暂不可用','当前资源统计不完整，不能据此判断在线人数或总使用量。'],EVENT_SAMPLES_INCOMPLETE:['部分用量记录缺失','此时段存在未收集到的事件，用量仅代表已采集部分。'],CONTAINER_OOM_OBSERVED:['观察到内存不足终止','检查对应服务或任务的内存峰值与并发。'],CONTAINER_HEALTH_UNHEALTHY:['服务健康检查失败','容器进程运行不代表服务已经可用。'],COLLECTOR_MISSING:['尚未收到云端采样','采集器没有提供有效数据，当前资源使用未知。'],COLLECTOR_STALE:['云端采样已过期','请检查采集器状态。历史值不代表当前资源使用。'],OWNER_BINDINGS_UNAVAILABLE:['无法核验用户归属','暂不把容器资源分配到个人用量中。'],WORKERS_UNBOUND:['部分工作区归属未核实','未归属资源不会混入已确认用户的统计。'],PIDS_NEAR_LIMIT:['进程与线程数接近限额','检查子进程回收与并发任务，避免再次出现无法创建进程。'],MEMORY_NEAR_LIMIT:['内存使用接近限额','检查任务并发与内存峰值。'],DISK_SPACE_LOW:['存储可用空间不足','检查保留周期、任务产物和备份容量。']};
const unknown=(reason='NOT_REPORTED',unit='count')=>({value:null,status:'unknown',reason,unit});
export function formatMetric(metric){
 if(!metric||metric.value===null||!Number.isFinite(metric.value)||metric.status==='unknown')return {text:'未采集',unit:'',unknown:true,note:reasonLabels[metric?.reason]??'暂无可靠数据'};
 const n=metric.value;let value=n,unit=metric.unit??'count';
 if(unit==='bytes'||unit==='bytes/s'){const rate=unit==='bytes/s';const u=['B','KiB','MiB','GiB','TiB'];let i=0;while(value>=1024&&i<u.length-1){value/=1024;i++;}unit=u[i]+(rate?'/秒':'');}
 else if(unit==='ms'&&n>=1000){value=n/1000;unit='秒';}
 else if(unit==='cores')unit='核';else if(unit==='count')unit='';else if(unit==='tokens')unit='Token';else if(unit==='seconds')unit='秒';
 return {text:new Intl.NumberFormat('zh-CN',{maximumFractionDigits:Number.isInteger(value)?0:value<10?2:1}).format(value),unit,unknown:false,note:''};
}
export function clockText(value){if(!Number.isFinite(value))return '尚无采样';return new Date(value*1000).toLocaleString('zh-CN',{hour12:false});}
export function chartSegments(points,gapSeconds=Infinity){
 const parts=[];let current=[];let previous=-Infinity;
 for(const point of Array.isArray(points)?points:[]){const t=point.time??point.sampled_at??point.t;
  if(!Number.isFinite(t)||t<=previous)continue;if(t-previous>gapSeconds&&current.length){parts.push(current);current=[];}previous=t;
  if(!Number.isFinite(point.value)){if(current.length)parts.push(current);current=[];continue;}
  current.push({time:t,value:point.value});
 }if(current.length)parts.push(current);return parts;
}
const $=id=>document.getElementById(id);
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(cls)el.className=cls;return el;}
function empty(target,text){target.replaceChildren(node('div',text,'empty'));}
function badge(text,tone=''){return node('span',text,'badge '+tone);}
function metricValue(m){const f=formatMetric(m);const value=node('div',f.text,'metric-value'+(f.unknown?' unavailable':''));if(f.unit)value.append(node('span',f.unit,'metric-unit'));if(f.note)value.title=f.note;return value;}
function metricCard(title,m,note=''){const box=node('article',undefined,'metric-card');box.append(node('div',title,'metric-title'),node('span',undefined,'metric-status'+(formatMetric(m).unknown?' unknown':'')),metricValue(m),node('div',note||formatMetric(m).note||'当前采样','metric-note'));return box;}
function normalMetric(v,available=true,unit='count'){return available&&Number.isFinite(v)?{value:v,unit,status:'measured'}:unknown('NOT_REPORTED',unit);}
let state={overview:null,users:[],services:[],alerts:[],page:0,allowed:false,displayStale:false},refreshing=false,refreshAbort,detailAbort,accessGeneration=0;
class AccessError extends Error{constructor(status){super('ACCESS');this.status=status;}}
async function api(section,signal){
 const response=await fetch(`${API}/${section}${section.includes('?')?'&':'?'}window=${encodeURIComponent($('window').value)}`,{credentials:'same-origin',cache:'no-store',redirect:'error',headers:{Accept:'application/json'},signal});
 if(response.status===401||response.status===403){const error=new AccessError(response.status);deny(error);throw error;}
 if(!response.ok)throw Error(response.status===429?'请求较频繁，请稍后刷新。':'监控数据暂时不可用，请稍后重试。');
 const text=await response.text();if(text.length>12*1024*1024)throw Error('监控响应超出读取限制。');
 const value=JSON.parse(text);if(value?.schema!=='maimi-developer-monitor-v1')throw Error('监控数据格式不匹配。');return value;
}
function deny(error){accessGeneration++;refreshAbort?.abort();detailAbort?.abort();state={overview:null,users:[],services:[],alerts:[],page:0,allowed:false,displayStale:true};for(const id of ['summary','trends','user-body','services','alerts','coverage','detail-body','alert-preview'])$(id).replaceChildren();$('user-detail').close();$('app').hidden=true;$('access-state').hidden=false;$('access-state').textContent=error.status===401?'登录已失效，请从麦米重新登录后进入监控。':'当前账号没有监控访问权限。';}
function showTab(tab){if(!['overview','users','services','alerts'].includes(tab))return;for(const e of document.querySelectorAll('[data-tab]'))e.classList.toggle('active',e.dataset.tab===tab);for(const e of document.querySelectorAll('.panel'))e.hidden=e.id!==`panel-${tab}`;$('page-title').textContent={overview:'运行总览',users:'用户用量',services:'沙箱与服务',alerts:'告警与数据覆盖'}[tab];}
function renderOverview(){
 const d=state.overview,coverage=d.coverage??{},fresh=d.freshness?.status==='fresh',mapped=coverage.ownership==='available';
 const partial=coverage.window_complete?.model!==true?'仅已采集范围；尚未覆盖完整时段':'所选时间范围';
 const cards=[['已登记云端用户',normalMetric(d.registered_users,mapped),'按已核验的独立工号统计'],['运行中云端沙箱',normalMetric(d.running_workers,fresh&&mapped),'仅统计已核验归属的沙箱'],['主机可用内存',d.host?.memory_available_bytes,'主机总量 '+formatMetric(d.host?.memory_total_bytes).text+' '+formatMetric(d.host?.memory_total_bytes).unit],['数据盘可用容量',d.host?.data_available_bytes,'可用空间；不等同备份容量已验证'],['公司模型代理请求',d.model?.requests,partial],['模型输入用量',d.model?.input_tokens,partial],['模型输出用量',d.model?.output_tokens,partial],['企业任务失败',d.tasks?.failed,coverage.window_complete?.task===true?'所选时间范围':'仅已记录终态的企业任务；范围见数据覆盖页']];
 $('summary').replaceChildren(...cards.map(([title,m,n])=>metricCard(title,m,n)));
 $('app').classList.toggle('stale-data',d.freshness?.status!=='fresh');$('freshness').textContent=(statuses[d.freshness?.status]??'未知')+' · 采样 '+clockText(d.freshness?.sampled_at);
 $('source-note').textContent='报告生成 '+clockText(d.generated_at);
 renderTrends(d.series??d.trends??[]);
 renderAlerts($('alert-preview'),state.alerts.slice(0,4));
}
function renderTrends(series){
 let rows=Array.isArray(series)?series:Array.isArray(series?.points)?[
  ['cpu_cores','受管容器 CPU 使用','cores'],['containers_memory_used_bytes','受管容器内存使用','bytes'],['observed_workers','云端沙箱数量','count']
 ].map(([key,label,unit])=>({key,label,unit,gapSeconds:(series.bucket_seconds??60)*1.8,points:series.points.map(p=>({time:p.sampled_at,value:p[key]}))})):[];
 if(!rows.length)rows=[{label:'云端 CPU 使用',unit:'cores',points:[]},{label:'云端内存使用',unit:'bytes',points:[]},{label:'运行沙箱数量',unit:'count',points:[]}];
 $('trends').replaceChildren(...rows.slice(0,6).map(row=>{
  const card=node('article',undefined,'trend-card');card.append(node('h3',row.label??labels[row.key]??row.key??'资源趋势'));
  const segments=chartSegments(row.points,row.gapSeconds),points=segments.flat();
  if(points.length<2){card.append(node('div','等待连续采样后展示趋势','empty'));return card;}
  const minX=points[0].time,maxX=points.at(-1).time,peak=Math.max(...points.map(p=>p.value)),maxY=Math.max(peak,1);
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 360 140');svg.setAttribute('role','img');svg.setAttribute('aria-label',(row.label??'资源')+'随时间变化');
  for(let i=0;i<3;i++){const line=document.createElementNS(svg.namespaceURI,'line');for(const[k,v]of Object.entries({x1:5,y1:15+i*50,x2:355,y2:15+i*50,stroke:'#edf2f1'}))line.setAttribute(k,v);svg.append(line);}
  for(const part of segments){const shape=document.createElementNS(svg.namespaceURI,part.length===1?'circle':'polyline');if(part.length===1){shape.setAttribute('cx',5+(part[0].time-minX)/(maxX-minX)*350);shape.setAttribute('cy',120-part[0].value/maxY*105);shape.setAttribute('r','2');shape.setAttribute('fill','#128b7e');}else{shape.setAttribute('points',part.map(p=>`${5+(p.time-minX)/(maxX-minX)*350},${120-p.value/maxY*105}`).join(' '));shape.setAttribute('fill','none');shape.setAttribute('stroke','#128b7e');shape.setAttribute('stroke-width','2');}svg.append(shape);}
  card.append(svg);const cap=node('div',undefined,'chart-caption');const f=formatMetric({value:peak,unit:row.unit});cap.append(node('span',new Date(minX*1000).toLocaleTimeString('zh-CN',{hour12:false})),node('span','峰值 '+f.text+' '+f.unit),node('span',new Date(maxX*1000).toLocaleTimeString('zh-CN',{hour12:false})));card.append(cap);return card;
 }));
}
const columns=[['工号',null],['云端 CPU','cpu_cores'],['云端内存','memory_used_bytes'],['进程／线程','pids_current'],['存储占用','storage_bytes'],['模型输入','input_tokens'],['模型输出','output_tokens'],['企业任务失败','failed']];
function renderUsers(){
 const query=$('user-search').value.trim().toUpperCase(),all=state.users.filter(x=>typeof x.user_no==='string'&&x.user_no.includes(query));const pages=Math.max(1,Math.ceil(all.length/30));state.page=Math.min(state.page,pages-1);
 const head=node('tr');head.append(...columns.map(([name])=>node('th',name)));$('user-head').replaceChildren(head);
 const rows=all.slice(state.page*30,(state.page+1)*30).map(user=>{const tr=node('tr');const id=node('td'),button=node('button',user.user_no,'user-link');button.addEventListener('click',()=>showDetail(user.user_no));id.append(button,node('small',`${state.overview?.freshness?.status==='fresh'?(user.running_workers??'未知'):'未知'} 个运行沙箱 · ${user.workspace_count??'未知'} 个工作区`));tr.append(id);
  for(const[,key]of columns.slice(1)){const m=key==='input_tokens'||key==='output_tokens'?user.model?.[key]:key==='failed'?user.tasks?.failed:user.cloud?.[key];const f=formatMetric(m),td=node('td',f.text+(f.unit?' '+f.unit:''));if(f.unknown)td.append(node('small',f.note));tr.append(td);}return tr;});
 if(!rows.length){const tr=node('tr'),td=node('td','没有符合条件的用户');td.colSpan=columns.length;tr.append(td);rows.push(tr);}$('user-body').replaceChildren(...rows);$('user-count').textContent=`共 ${all.length} 位用户；仅显示已核验归属和可信计量`;$('page-number').textContent=`${state.page+1} / ${pages}`;$('prev').disabled=state.page===0;$('next').disabled=state.page>=pages-1;
}
function renderServices(){if(!state.services.length){empty($('services'),'尚未收到服务采样');return;}$('services').replaceChildren(...state.services.map(row=>{
 const card=node('article',undefined,'service-card'),heading=node('div',undefined,'card-heading'),fresh=state.overview?.freshness?.status==='fresh'&&!state.displayStale;
 const health=row.health==='unhealthy'?'健康检查失败':row.health==='starting'?'健康检查中':statuses[row.state]??'未知';
 heading.append(node('h3',serviceNames[row.service]??row.service),badge(fresh?health:'上次状态：'+health,fresh&&row.state==='running'&&row.health!=='unhealthy'?'ok':'warn'));card.append(heading);
 const primary=['cpu_cores','memory_used_bytes','pids_current','pids_limit'],grid=node('div',undefined,'service-metrics');
 for(const key of primary){const cell=node('div');cell.append(node('div',labels[key],'metric-title'),metricValue(row.resources?.[key]));grid.append(cell);}card.append(grid);
 const more=node('details'),summary=node('summary','更多资源与限额'),extra=node('div',undefined,'service-metrics');
 for(const [key,m] of Object.entries(row.resources??{})){if(primary.includes(key))continue;const cell=node('div');cell.append(node('div',labels[key]??key,'metric-title'),metricValue(m));extra.append(cell);}
 more.append(summary,node('p',`实例数：${row.instance_count??'未知'}；采样时间：${clockText(state.overview?.freshness?.sampled_at)}`,'hint'),extra);card.append(more);return card;
 }));}
function renderAlerts(target,rows){if(!rows.length){empty(target,state.overview?.freshness?.status==='fresh'?'当前采集范围内没有触发告警':'尚无可靠告警结论');return;}target.replaceChildren(...rows.map(row=>{const known=alertText[row.code],box=node('div',undefined,'alert-row'),copy=node('div');copy.append(node('div',known?.[0]??'运行状态需要检查','alert-title'),node('div',known?.[1]??row.code,'alert-description'),node('div','范围：'+(serviceNames[row.scope]??({platform:'平台','cloud-worker':'云端工作区',model:'公司模型代理请求',task:'企业任务',office:'远程 Office 作业'}[row.scope])??'受管资源'),'alert-description'));box.append(node('span','!', 'alert-icon '+(row.severity==='critical'?'critical':'')),copy);return box;}));}
function renderCoverage(){const c=state.overview?.coverage??{};const rows=[['云端采集',c.cloud_collector,'容器和宿主指标的采样状态。'],['用户归属',c.ownership,`观察到 ${c.workers_observed??'—'} 个沙箱，${c.workers_bound??'—'} 个已核实归属，${c.workers_unbound??'—'} 个未归属。`],['桌面本地',c.desktop,'本地资源不计入云端；未上报时无法推算。'],['资源历史',c.resource_history,'趋势仅显示有效采样，缺口不会补零。'],...Object.entries(c.event_sources??{}).map(([k,s])=>[({model:'公司模型代理请求',task:'企业任务（已记录终态）',office:'远程 Office 作业'}[k]??k),s,c.window_complete?.[k]===true?'已覆盖所选时间范围。':'尚未覆盖完整时间范围；可见统计仅代表已采集部分。'])];$('coverage').replaceChildren(...rows.map(([name,status,description])=>{const row=node('div',undefined,'coverage-row'),top=node('div',undefined,'row-top');top.append(node('span',name),badge(statuses[status]??'未知',status==='available'?'ok':''));row.append(top,node('p',description));return row;}));}
async function showDetail(user){detailAbort?.abort();const controller=detailAbort=new AbortController();$('detail-title').textContent=user+' · 用量详情';empty($('detail-body'),'正在读取…');$('user-detail').showModal();const timeout=setTimeout(()=>controller.abort(),15000);try{const data=await api('users/'+encodeURIComponent(user),controller.signal);if(detailAbort!==controller||!$('user-detail').open)return;const item=data.item,body=$('detail-body');body.replaceChildren();for(const [group,name]of [['cloud','云端资源'],['model','公司模型代理请求'],['tasks','企业任务（已记录终态）'],['office','远程 Office 作业']]){body.append(node('h3',name));const grid=node('div',undefined,'summary-grid');for(const [key,m]of Object.entries(item[group]??{}))grid.append(metricCard(labels[key]??key,m));body.append(grid);}body.append(node('p','桌面本地：未采集。云端和共享服务不会推算为本机使用量。','hint'));}catch(error){if(error instanceof AccessError){deny(error);return;}if($('user-detail').open&&detailAbort===controller)empty($('detail-body'),controller.signal.aborted?'详情读取超时，请关闭后重试。':'详情暂时不可用。');}finally{clearTimeout(timeout);}}
async function refresh(){if(refreshing||!state.allowed||document.hidden)return;refreshing=true;$('refresh').disabled=true;$('window').disabled=true;const generation=accessGeneration,controller=refreshAbort=new AbortController();const timeout=setTimeout(()=>controller.abort(),15000);try{
 const [overview,users,services,alerts]=await Promise.all(['overview','users','services','alerts'].map(p=>api(p,controller.signal)));
 if(!Array.isArray(users.items)||users.items.length>4096||!Array.isArray(services.items)||!Array.isArray(alerts.items))throw Error('监控数据格式不匹配。');
 if(!state.allowed||generation!==accessGeneration)return;
 state={...state,overview,users:users.items,services:services.items,alerts:alerts.items,displayStale:false};$('error').hidden=true;renderOverview();renderUsers();renderServices();renderAlerts($('alerts'),state.alerts);renderCoverage();
 }catch(error){if(error instanceof AccessError){deny(error);return;}if(!state.allowed||generation!==accessGeneration)return;
 state.displayStale=true;$('app').classList.add('stale-data');const previous=state.overview?.window;if(previous&&['1h','24h','7d'].includes(previous))$('window').value=previous;
 const range=({'1h':'最近 1 小时','24h':'最近 24 小时','7d':'最近 7 天'})[previous];
 $('error').hidden=false;$('error').textContent=(controller.signal.aborted?'本次刷新超时。':'监控数据刷新失败。')+(range?`以下保留上次“${range}”的数据。`:'尚无可用的历史数据。')+'当前实时状态未知，请查看采样时间。';$('freshness').textContent='刷新失败 · 上次采样 '+clockText(state.overview?.freshness?.sampled_at);renderServices();
 }finally{clearTimeout(timeout);refreshing=false;$('refresh').disabled=false;$('window').disabled=false;}}
async function start(){
 document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));document.querySelectorAll('[data-open]').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.open)));
 $('refresh').addEventListener('click',refresh);$('window').addEventListener('change',()=>{state.page=0;refresh();});$('user-search').addEventListener('input',()=>{state.page=0;renderUsers();});$('prev').addEventListener('click',()=>{state.page--;renderUsers();});$('next').addEventListener('click',()=>{state.page++;renderUsers();});$('detail-close').addEventListener('click',()=>$('user-detail').close());$('user-detail').addEventListener('close',()=>detailAbort?.abort());
 try{const control=new AbortController();const timeout=setTimeout(()=>control.abort(),15000);let access;try{access=await api('access',control.signal);}finally{clearTimeout(timeout);}if(access.allowed!==true)throw new AccessError(403);state.allowed=true;$('access-state').hidden=true;$('app').hidden=false;await refresh();}catch(error){if(error instanceof AccessError)deny(error);else $('access-state').textContent='暂时无法验证监控权限，请稍后重新打开页面。';}
 setInterval(refresh,30000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
}
if(typeof document!=='undefined')void start();
