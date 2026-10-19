"""Read-only developer monitoring candidate; no HTTP ingestion or Docker access.

Only fixed-schema, trusted producers may write this metadata store. The HTTP
reader revalidates the real IAM Cookie, and resolves ownership from the database.
"""
from __future__ import annotations
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from uuid import UUID
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

SCHEMA='maimi-developer-monitor-v1'
# Explicit human authorization, 2026-10-19. These are monitor READ permissions.
# This list never participates in service/SSH or administrator write authority.
DEVELOPERS=frozenset({'M00096389','M00000246','M00091350','M00063756','M00003260','M00057343'})
USER=re.compile(r'^M[0-9]{8}$')
WINDOWS={'1h':3600,'24h':86400,'7d':604800}
SERVICES=frozenset({'gateway','personal-cloud-manager','office-service','service-broker','postgres','redis','downloads','retention-worker'})
STATES=frozenset({'running','created','stopped','exited','restarting','unknown'})
RESOURCE_FIELDS=('cpu_usage_ns','cpu_cores','memory_used_bytes','memory_limit_bytes','pids_current','pids_limit','storage_bytes')
EXTRA_UNITS={'restarts':'count','oom_kills':'count','pids_limit_hits':'count','cpu_throttled_seconds':'seconds','network_rx_bytes':'bytes','network_tx_bytes':'bytes','network_rx_bytes_per_second':'bytes/s','network_tx_bytes_per_second':'bytes/s','io_read_bytes':'bytes','io_write_bytes':'bytes','io_read_bytes_per_second':'bytes/s','io_write_bytes_per_second':'bytes/s'}
RESOURCE_FIELDS=(*RESOURCE_FIELDS,*EXTRA_UNITS)
HEADERS={'Cache-Control':'no-store, private','Pragma':'no-cache','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cross-Origin-Resource-Policy':'same-origin','Vary':'Cookie'}

class Invalid(Exception):pass
class Denied(Exception):
 def __init__(self,status=403,code='MONITOR_ACCESS_DENIED'):self.status,self.code=status,code

def require(value):
 if not value:raise Invalid('INVALID_MONITOR_METADATA')
def exact(value,fields):require(isinstance(value,dict) and set(value)==set(fields))
def number(value):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=2**63-1
def uid(value):
 try:return isinstance(value,str) and str(UUID(value))==value
 except (ValueError,AttributeError):return False
def metric(value=None,unit='count',reason='NOT_INSTRUMENTED',sampled_at=None,status=None):
 return {'value':value,'unit':unit,'status':status or ('measured' if value is not None else 'unknown'),'reason':None if value is not None else reason,'sampled_at':sampled_at}

def validate_snapshot(value,now):
 exact(value,{'schema','sampled_at','sequence','inventory_complete','host','workers','services','storage'})
 require(value['schema']=='maimi-monitor-collector-v1' and number(value['sampled_at']) and value['sampled_at']<=now+5)
 require(type(value['sequence']) is int and value['sequence']>=0)
 require(type(value['inventory_complete']) is bool)
 exact(value['host'],{'cpu_count','memory_total_bytes','memory_available_bytes','data_available_bytes'})
 for v in value['host'].values():require(v is None or number(v))
 require(isinstance(value['storage'],list) and len(value['storage'])<=4096)
 storage_seen=set()
 for row in value['storage']:
  exact(row,{'workspace_id','storage_bytes','sampled_at','complete','stale'})
  require(uid(row['workspace_id']) and row['workspace_id'] not in storage_seen);storage_seen.add(row['workspace_id'])
  require(row['storage_bytes'] is None or number(row['storage_bytes']))
  require(row['sampled_at'] is None or number(row['sampled_at']) and row['sampled_at']<=now+5)
  require(type(row['complete']) is bool and type(row['stale']) is bool)
 require(isinstance(value['workers'],list) and len(value['workers'])<=4096 and isinstance(value['services'],list) and len(value['services'])<=128)
 seen=set()
 for kind in ('workers','services'):
  for row in value[kind]:
   identity='workspace_id' if kind=='workers' else 'service'
   exact(row,{identity,'instance_id','state','health','oom_killed',*RESOURCE_FIELDS})
   require(uid(row[identity]) if kind=='workers' else row[identity] in SERVICES)
   require(isinstance(row['instance_id'],str) and re.fullmatch(r'[a-f0-9]{64}',row['instance_id']) and row['state'] in STATES)
   require(row['health'] in (None,'healthy','unhealthy','starting') and (row['oom_killed'] is None or type(row['oom_killed']) is bool))
   require(row['instance_id'] not in seen);seen.add(row['instance_id'])
   for key in RESOURCE_FIELDS:require(row[key] is None or number(row[key]))
 return value

class MetadataStore:
 """Bounded metadata only. No prompt, filename, command, credential or raw error.

 Latest two collector samples and bounded platform trends are retained;
 events retain at most seven days/100,000 rows. Per-user resource history
 is not retained. Platform trends are sampled at most every 30 seconds.
 SQLite file must be placed in a private operator-created directory at deploy.
 """
 def __init__(self,path=':memory:',clock=time.time,event_limit=100000):
  require(type(event_limit) is int and 10<=event_limit<=100000)
  self.clock,self.event_limit=clock,event_limit;self.lock=threading.RLock();self.active_sources=set();self.next_prune=0
  if str(path)!=':memory:':
   file=Path(path);require(file.is_absolute() and file.parent.is_dir() and not file.parent.is_symlink() and file.parent.resolve()==file.parent)
   parent=file.parent.stat()
   if os.name=='posix':require(parent.st_uid==os.geteuid() and stat.S_IMODE(parent.st_mode)&0o077==0)
   try:fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
   except FileExistsError:pass
   info=file.lstat();require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and not file.is_symlink())
   if os.name=='posix':require(info.st_uid==os.geteuid() and stat.S_IMODE(info.st_mode)&0o077==0)
  self.db=sqlite3.connect(path,check_same_thread=False,isolation_level=None)
  self.db.row_factory=sqlite3.Row
  self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL')
  self.db.execute('CREATE TABLE IF NOT EXISTS samples(sequence INTEGER PRIMARY KEY, sampled REAL NOT NULL, payload TEXT NOT NULL)')
  self.db.execute('CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,completed REAL NOT NULL,status TEXT NOT NULL,duration REAL,input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,cache_write_tokens INTEGER)')
  self.db.execute('CREATE INDEX IF NOT EXISTS event_completed ON events(completed)')
  self.db.execute('CREATE TABLE IF NOT EXISTS sources(kind TEXT PRIMARY KEY, since REAL NOT NULL)')
  self.db.execute('CREATE TABLE IF NOT EXISTS losses(kind TEXT PRIMARY KEY, before_time REAL NOT NULL)')
  self.db.execute('CREATE TABLE IF NOT EXISTS trends(sampled REAL PRIMARY KEY,payload TEXT NOT NULL)')
 def close(self):self.db.close()
 def enable_source(self,kind):
  require(kind in {'model','task','office'})
  with self.lock:
   if kind not in self.active_sources:self.db.execute('INSERT INTO sources VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET since=excluded.since',(kind,self.clock()))
   self.active_sources.add(kind)
 def record_loss(self,kind,at):
  require(kind in {'model','task','office'} and number(at) and at<=self.clock()+5)
  with self.lock:self.db.execute('INSERT INTO losses VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET before_time=MAX(before_time,excluded.before_time)',(kind,at))
 def record_resources(self,snapshot):
  value=json.loads(json.dumps(snapshot,allow_nan=False));validate_snapshot(value,self.clock())
  with self.lock:
   latest=self.db.execute('SELECT sequence,sampled,payload FROM samples ORDER BY sequence DESC LIMIT 1').fetchone()
   require(latest is None or value['sequence']>latest['sequence'] and value['sampled_at']>latest['sampled'])
   self.db.execute('BEGIN IMMEDIATE')
   try:
    self.db.execute('INSERT INTO samples VALUES(?,?,?)',(value['sequence'],value['sampled_at'],json.dumps(value,separators=(',',':'))))
    prior=json.loads(latest['payload']) if latest else None
    last_trend=self.db.execute('SELECT MAX(sampled) FROM trends').fetchone()[0]
    # Store only platform aggregates, at most once per 30 seconds. Per-user
    # resource history is not retained; attribution stays with the live DB.
    if last_trend is None or value['sampled_at']-last_trend>=30:
     point=platform_trend(value,prior)
     self.db.execute('INSERT INTO trends VALUES(?,?)',(value['sampled_at'],json.dumps(point,separators=(',',':'))))
    self.db.execute('DELETE FROM trends WHERE sampled<?',(self.clock()-604800,))
    self.db.execute('DELETE FROM trends WHERE sampled NOT IN (SELECT sampled FROM trends ORDER BY sampled DESC LIMIT 20161)')
    self.db.execute('DELETE FROM samples WHERE sequence NOT IN (SELECT sequence FROM samples ORDER BY sequence DESC LIMIT 2)')
    self.db.execute('COMMIT')
   except BaseException:self.db.execute('ROLLBACK');raise
 def record_event(self,*,trusted_owner,event):
  return self._record_event(trusted_owner=trusted_owner,event=event,reobserved=False)
 def record_observed_event(self,*,trusted_owner,event):
  require(isinstance(event,dict) and event.get('kind') in {'office','task'})
  return self._record_event(trusted_owner=trusted_owner,event=event,reobserved=True)
 def _record_event(self,*,trusted_owner,event,reobserved):
  # trusted_owner is supplied by an authenticated gateway producer, never an
  # HTTP event payload. No user-facing endpoint calls this method.
  require(isinstance(trusted_owner,str) and USER.fullmatch(trusted_owner))
  require(isinstance(event,dict))
  event={**{'cache_read_tokens':None,'cache_write_tokens':None},**event}
  exact(event,{'event_id','kind','completed_at','status','duration_ms','input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'})
  require(uid(event['event_id']) and event['kind'] in {'model','task','office'} and event['status'] in {'succeeded','failed','cancelled','unknown'})
  require(number(event['completed_at']) and self.clock()-604800<=event['completed_at']<=self.clock()+5)
  for key in ('duration_ms','input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'):require(event[key] is None or type(event[key]) is int and number(event[key]))
  if event['kind']!='model':require(all(event[x] is None for x in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens')))
  row=(event['event_id'],trusted_owner,event['kind'],event['completed_at'],event['status'],event['duration_ms'],event['input_tokens'],event['output_tokens'],event['cache_read_tokens'],event['cache_write_tokens'])
  with self.lock:
   require(event['kind'] in self.active_sources)
   previous=self.db.execute('SELECT * FROM events WHERE event_id=?',(event['event_id'],)).fetchone()
   if previous is not None:
    if reobserved:require(previous['owner']==trusted_owner and previous['kind']==event['kind'] and previous['status']==event['status'])
    else:require(tuple(previous)==row)
    return
   self.db.execute('BEGIN IMMEDIATE')
   try:
    self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',row)
    now=self.clock()
    if now>=self.next_prune:
     self.db.execute('DELETE FROM events WHERE completed<?',(now-604800,));self.next_prune=now+60
    excess=max(0,self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]-self.event_limit)
    remove=self.db.execute('SELECT event_id,completed,kind FROM events ORDER BY completed ASC LIMIT ?',(excess,)).fetchall() if excess else []
    for v in remove:self.db.execute('INSERT INTO losses VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET before_time=MAX(before_time,excluded.before_time)',(v['kind'],v['completed']))
    self.db.executemany('DELETE FROM events WHERE event_id=?',[(v['event_id'],) for v in remove])
    self.db.execute('COMMIT')
   except BaseException:self.db.execute('ROLLBACK');raise
 def read(self,window):
  with self.lock:
   samples=[json.loads(r['payload']) for r in self.db.execute('SELECT payload FROM samples ORDER BY sequence DESC LIMIT 2')]
   events=[dict(r) for r in self.db.execute('SELECT * FROM events WHERE completed>=?',(self.clock()-WINDOWS[window],))]
   sources={r['kind']:r['since'] for r in self.db.execute('SELECT * FROM sources') if r['kind'] in self.active_sources}
   losses={r['kind']:r['before_time'] for r in self.db.execute('SELECT * FROM losses')}
  return samples,events,sources,losses
 def trends(self,window):
  # Bounded chart output: latest measured aggregate in each UTC bucket;
  # no interpolation and no claim that missing intervals equal zero.
  bucket={'1h':60,'24h':300,'7d':1800}[window];points={}
  with self.lock:
   for row in self.db.execute('SELECT sampled,payload FROM trends WHERE sampled>=? ORDER BY sampled',(self.clock()-WINDOWS[window],)):
    points[int(row['sampled']//bucket)]=json.loads(row['payload'])
  return {'scope':'PLATFORM_AGGREGATES_NO_USER_ATTRIBUTION','bucket_seconds':bucket,'aggregation':'LATEST_MEASURED_SAMPLE_PER_BUCKET','points':list(points.values())}

def binding_map(rows):
 require(isinstance(rows,list) and len(rows)<=4096)
 result={}
 for row in rows:
  exact(row,{'workspace_id','owner_user_no','enabled'})
  require(uid(row['workspace_id']) and USER.fullmatch(row['owner_user_no']) and type(row['enabled']) is bool and row['workspace_id'] not in result)
  result[row['workspace_id']]=row
 return result

def resource_unit(key):return EXTRA_UNITS.get(key,'bytes' if key.endswith('_bytes') else 'cores' if key=='cpu_cores' else 'count')
def resource_view(row,current,previous):
 out={key:metric(row.get(key),resource_unit(key),'NOT_REPORTED',current['sampled_at']) for key in RESOURCE_FIELDS if key not in ('cpu_usage_ns','cpu_cores')}
 # Collector computes rates using monotonic time and unchanged process/cgroup
 # identity. Never divide counters by a wall-clock delta (NTP can jump).
 out['cpu_cores']=metric(row['cpu_cores'],'cores','DELTA_SAMPLE_REQUIRED',current['sampled_at'])
 return out

def platform_trend(current,previous):
 views=[resource_view(x,current,previous) for x in current['workers']+current['services'] if x['state']=='running']
 def total(key):
  values=[x[key]['value'] for x in views]
  return sum(values) if current['inventory_complete'] and all(x is not None for x in values) else None
 return {'sampled_at':current['sampled_at'],'scope':'OBSERVED_RUNNING_MANAGED_CONTAINERS','cpu_cores':total('cpu_cores'),'containers_memory_used_bytes':total('memory_used_bytes'),'containers_pids_current':total('pids_current'),'host_memory_available_bytes':current['host']['memory_available_bytes'],'data_available_bytes':current['host']['data_available_bytes'],'observed_workers':len(current['workers']) if current['inventory_complete'] else None}

def event_summary(rows,kind,sources,now):
 if kind not in sources:return {k:metric(unit=u) for k,u in [('requests','count'),('failed','count'),('duration_p95_ms','ms'),('input_tokens','tokens'),('output_tokens','tokens'),('cache_read_tokens','tokens'),('cache_write_tokens','tokens')]}
 items=[x for x in rows if x['kind']==kind];dur=sorted(x['duration'] for x in items if x['duration'] is not None)
 result={'requests':metric(len(items),sampled_at=now),'failed':metric(sum(x['status']=='failed' for x in items),sampled_at=now),'duration_p95_ms':metric(dur[max(0,math.ceil(len(dur)*.95)-1)] if dur else None,'ms','NO_DURATION_SAMPLES',now)}
 for name in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'):
  values=[x[name] for x in items if x[name] is not None]
  complete=kind=='model' and len(values)==len(items)
  result[name]=metric(sum(values) if complete else None,'tokens','PROVIDER_USAGE_MISSING' if kind=='model' else 'NOT_APPLICABLE',now)
 return result

class Monitor:
 def __init__(self,store,read_bindings,clock=time.time):
  self.store,self.read_bindings,self.clock=store,read_bindings,clock
  self.cache={};self.cache_lock=asyncio.Lock()
 async def report(self,window='24h'):
  require(window in WINDOWS)
  async with self.cache_lock:
   now=self.clock();cached=self.cache.get(window)
   if cached and 0<=now-cached['generated_at']<5:return cached
   result=await self._report(window);self.cache[window]=result;return result
 async def _report(self,window='24h'):
  require(window in WINDOWS);now=self.clock()
  try:rows=await self.read_bindings()
  except Exception:rows=None
  return await asyncio.to_thread(self._aggregate,window,rows,now)
 def _aggregate(self,window,rows,now):
  samples,events,sources,losses=self.store.read(window)
  try:bindings=binding_map(rows);binding_status='available'
  except Exception:bindings={};binding_status='unavailable'
  latest=samples[0] if samples else None;prev=samples[1] if len(samples)>1 else None
  age=max(0,now-latest['sampled_at']) if latest else None
  fresh={'status':'missing' if age is None else 'stale' if age>120 else 'fresh','age_seconds':age,'sampled_at':latest['sampled_at'] if latest else None,'stale_after_seconds':120}
  matched=[x for x in latest['workers'] if x['workspace_id'] in bindings] if latest else []
  inventory=latest is not None and latest['inventory_complete']
  coverage={'cloud_collector':('available' if inventory else 'partial') if latest else 'not_reported','ownership':binding_status,'workers_observed':len(latest['workers']) if inventory else None,'workers_bound':len(matched) if inventory and binding_status=='available' else None,'workers_unbound':len(latest['workers'])-len(matched) if inventory and binding_status=='available' else None,'desktop':'not_reported','resource_history':'platform_aggregates_only','event_sources':{k:('available' if k in sources else 'not_instrumented') for k in ('model','task','office')},'event_source_since':sources,'window_complete':{k:k in sources and sources[k]<=now-WINDOWS[window] and losses.get(k,0)<now-WINDOWS[window] for k in ('model','task','office')},'retention_seconds':604800}
  users=defaultdict(list);workspaces_by_owner=defaultdict(set);events_by_owner=defaultdict(list)
  storage_by_workspace={s['workspace_id']:s for s in latest['storage']} if latest else {}
  for binding in bindings.values():
   users[binding['owner_user_no']];workspaces_by_owner[binding['owner_user_no']].add(binding['workspace_id'])
  for row in matched:users[bindings[row['workspace_id']]['owner_user_no']].append(row)
  # Event owners were supplied by the trusted gateway hook, not the collector.
  for row in events:users[row['owner']];events_by_owner[row['owner']].append(row)
  items=[]
  for owner,workers in sorted(users.items()):
   selected_workers=[w for w in workers if w['state']=='running'] or workers
   resources=[resource_view(w,latest,prev) for w in selected_workers]
   combined={}
   for key in RESOURCE_FIELDS:
    if key=='cpu_usage_ns':continue
    values=[r[key]['value'] for r in resources];unit=resource_unit(key)
    combined[key]=metric(sum(values) if values and all(x is not None for x in values) else None,unit,'NO_RESOURCE_SAMPLE' if not values else 'PARTIAL_RESOURCE_SAMPLE',fresh['sampled_at'])
   own_workspaces=workspaces_by_owner[owner]
   stored=[storage_by_workspace[ws] for ws in own_workspaces if ws in storage_by_workspace]
   storage_complete=bool(own_workspaces) and len(stored)==len(own_workspaces) and all(s['complete'] and not s['stale'] and s['storage_bytes'] is not None and s['sampled_at'] is not None for s in stored)
   combined['storage_bytes']=metric(sum(s['storage_bytes'] for s in stored) if storage_complete else None,'bytes','STORAGE_UNAVAILABLE_OR_PARTIAL_OR_STALE',min(s['sampled_at'] for s in stored if s['sampled_at'] is not None) if any(s['sampled_at'] is not None for s in stored) else None)
   own=events_by_owner[owner]
   items.append({'user_no':owner,'workspace_count':len(own_workspaces),'running_workers':sum(w['state']=='running' for w in workers) if inventory and binding_status=='available' else None,'cloud':combined,'model':event_summary(own,'model',sources,now),'tasks':event_summary(own,'task',sources,now),'office':event_summary(own,'office',sources,now),'desktop':{'status':'not_reported','reason':'LOCAL_DESKTOP_TELEMETRY_NOT_ENABLED'}})
  service_groups=defaultdict(list)
  for x in latest['services'] if latest else []:service_groups[x['service']].append(x)
  services=[]
  for name,instances in sorted(service_groups.items()):
   running=[x for x in instances if x['state']=='running'];selected=running or instances
   views=[resource_view(x,latest,prev) for x in selected];resources={}
   for key in views[0]:
    values=[x[key]['value'] for x in views]
    resources[key]=metric(sum(values) if all(x is not None for x in values) else None,resource_unit(key),'PARTIAL_RESOURCE_SAMPLE',fresh['sampled_at'])
   health='unhealthy' if any(x['health']=='unhealthy' for x in selected) else 'healthy' if running and all(x['health']=='healthy' for x in running) else 'starting' if any(x['health']=='starting' for x in selected) else None
   services.append({'service':name,'state':'running' if len(running)==1 else 'multiple_instances' if len(running)>1 else instances[0]['state'] if len(instances)==1 else 'unknown','health':health,'instance_count':len(instances),'running_instances':len(running),'resources':resources})
  alerts=[]
  if fresh['status']!='fresh':alerts.append({'code':'COLLECTOR_'+fresh['status'].upper(),'severity':'warning','scope':'platform'})
  if latest and not inventory:alerts.append({'code':'CONTAINER_INVENTORY_UNAVAILABLE','severity':'warning','scope':'platform'})
  if binding_status!='available':alerts.append({'code':'OWNER_BINDINGS_UNAVAILABLE','severity':'warning','scope':'platform'})
  if coverage['workers_unbound']:alerts.append({'code':'WORKERS_UNBOUND','severity':'warning','scope':'platform'})
  for kind,at in losses.items():
   if at>=now-WINDOWS[window]:alerts.append({'code':'EVENT_SAMPLES_INCOMPLETE','severity':'warning','scope':kind})
  if latest and fresh['status']=='fresh':
   for row in latest['workers']+latest['services']:
    if row['oom_killed'] is True:alerts.append({'code':'CONTAINER_OOM_OBSERVED','severity':'warning','scope':row.get('service','cloud-worker')})
    if row['health']=='unhealthy':alerts.append({'code':'CONTAINER_HEALTH_UNHEALTHY','severity':'warning','scope':row.get('service','cloud-worker')})
    if row['pids_current'] is not None and row['pids_limit'] and row['pids_current']/row['pids_limit']>=.9:
     alerts.append({'code':'PIDS_NEAR_LIMIT','severity':'warning','scope':row.get('service','cloud-worker')})
  host={key:metric(value,'count' if key=='cpu_count' else 'bytes','NOT_REPORTED',fresh['sampled_at']) for key,value in (latest['host'] if latest else {k:None for k in ('cpu_count','memory_total_bytes','memory_available_bytes','data_available_bytes')}).items()}
  return {'schema':SCHEMA,'generated_at':now,'window':window,'coverage':coverage,'freshness':fresh,'overview':{'registered_users':len(set(x['owner_user_no'] for x in bindings.values())) if binding_status=='available' else None,'running_workers':sum(x['state']=='running' for x in matched) if inventory and binding_status=='available' else None,'host':host,'model':event_summary(events,'model',sources,now),'tasks':event_summary(events,'task',sources,now),'office':event_summary(events,'office',sources,now),'series':self.store.trends(window)},'users':items,'services':services,'alerts':alerts}

async def actual_iam(request):
 from app import iam_oauth
 if request.headers.get('authorization') or not request.cookies.get(iam_oauth.SESSION_COOKIE):raise Denied(401,'MONITOR_LOGIN_REQUIRED')
 try:user=await iam_oauth.authenticate_request(request)
 except iam_oauth.IAMFailure as error:
  raise Denied(401 if error.status in (401,403) else 503,'MONITOR_LOGIN_REQUIRED' if error.status in (401,403) else 'MONITOR_ACCESS_UNAVAILABLE') from None
 if not isinstance(user,dict) or user.get('auth_provider')!='iam_direct':raise Denied(401,'MONITOR_LOGIN_REQUIRED')
 return user.get('user_no')

async def actual_bindings():
 def read():
  from app.history_store import _connect
  with _connect() as conn,conn.cursor() as cur:
   cur.execute('SET TRANSACTION READ ONLY')
   cur.execute("SELECT id::text AS workspace_id,owner_user_no,enabled FROM platform.hybrid_workspaces WHERE kind='personal' ORDER BY id LIMIT 4097")
   return [dict(r) for r in cur.fetchall()]
 return await asyncio.to_thread(read)

def make_router(monitor,*,authenticate=actual_iam,allowed_users=DEVELOPERS,ui=None,clock=time.time):
 require(isinstance(allowed_users,(set,frozenset,tuple)) and all(isinstance(x,str) and USER.fullmatch(x) for x in allowed_users))
 allowed=frozenset(allowed_users);router=APIRouter();requests=defaultdict(deque)
 async def gate(request):
  if request.method not in ('GET','HEAD') or request.headers.get('sec-fetch-site')=='cross-site':raise Denied()
  owner=await authenticate(request)
  if owner not in allowed:raise Denied()
  now=clock();q=requests[owner]
  while q and q[0]<=now-60:q.popleft()
  if len(q)>=60:raise Denied(429,'MONITOR_REFRESH_LIMIT')
  q.append(now);return owner
 async def answer(request,section,user_no=None):
  try:
   await gate(request)
   if set(request.query_params)-{'window'} or len(request.query_params.getlist('window'))>1:raise Denied(400,'MONITOR_QUERY_INVALID')
   window=request.query_params.get('window','24h')
   if window not in WINDOWS:raise Denied(400,'MONITOR_QUERY_INVALID')
   if section=='access':return JSONResponse({'schema':SCHEMA,'generated_at':clock(),'allowed':True,'coverage':{'scope':'ACCESS_ONLY'},'freshness':{'status':'not_applicable'}},headers=HEADERS)
   data=await monitor.report(window);base={k:data[k] for k in ('schema','generated_at','window','coverage','freshness')}
   if section=='access':base['allowed']=True
   elif user_no is not None:
    if not USER.fullmatch(user_no):raise Denied(400,'MONITOR_USER_INVALID')
    item=next((x for x in data['users'] if x['user_no']==user_no),None)
    if item is None:raise Denied(404,'MONITOR_USER_NOT_FOUND')
    base['item']=item
   elif section=='overview':base.update(data['overview'])
   else:base['items']=data[section]
   return JSONResponse(base,headers=HEADERS)
  except Denied as error:return JSONResponse({'schema':SCHEMA,'error':{'code':error.code}},status_code=error.status,headers=HEADERS)
  except Exception:return JSONResponse({'schema':SCHEMA,'error':{'code':'MONITOR_DATA_UNAVAILABLE'}},status_code=503,headers=HEADERS)
 @router.get('/v1/developer-monitor/access')
 async def access(request:Request):return await answer(request,'access')
 @router.get('/v1/developer-monitor/overview')
 async def overview(request:Request):return await answer(request,'overview')
 @router.get('/v1/developer-monitor/users')
 async def users(request:Request):return await answer(request,'users')
 @router.get('/v1/developer-monitor/users/{user_no}')
 async def user(request:Request,user_no:str):return await answer(request,'users',user_no)
 @router.get('/v1/developer-monitor/services')
 async def services(request:Request):return await answer(request,'services')
 @router.get('/v1/developer-monitor/alerts')
 async def alerts(request:Request):return await answer(request,'alerts')
 assets=dict(ui or {})
 require(set(assets)<={'index.html','monitor.js','monitor.mjs','monitor.css'} and all(isinstance(x,bytes) for x in assets.values()))
 async def page(request,name):
  try:
   await gate(request)
   if request.url.query or name not in assets:raise Denied(404,'MONITOR_NOT_FOUND')
   mime={'index.html':'text/html','monitor.js':'text/javascript','monitor.mjs':'text/javascript','monitor.css':'text/css'}[name]
   return Response(assets[name],media_type=mime,headers={**HEADERS,'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"})
  except Denied as error:return JSONResponse({'schema':SCHEMA,'error':{'code':error.code}},status_code=error.status,headers=HEADERS)
  except Exception:return JSONResponse({'schema':SCHEMA,'error':{'code':'MONITOR_ACCESS_UNAVAILABLE'}},status_code=503,headers=HEADERS)
 @router.get('/maimi/developer/monitor')
 @router.get('/maimi/developer/monitor/')
 async def index(request:Request):return await page(request,'index.html')
 @router.get('/maimi/developer/monitor/assets/{name}')
 async def asset(request:Request,name:str):return await page(request,name)
 return router
