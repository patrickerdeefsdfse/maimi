"""Private host-file ingestion. No web route, caller-controlled path or Docker."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from uuid import UUID
if __package__:
 from .monitor import Invalid,require,STATES,RESOURCE_FIELDS
else:
 from monitor import Invalid,require,STATES,RESOURCE_FIELDS

MAX_BYTES=2*1024*1024
SCHEMA=Path(__file__).resolve().parent/'collector.schema.json'

def validate(value,schema,depth=0):
 """Strict evaluator for the small fixed collector schema, no external refs.

 Every schema feature is checked or rejected; this is not a general JSON
 Schema implementation. The schema is shipped code, never input from a user.
 """
 require(depth<=32 and isinstance(schema,dict))
 require(set(schema)<={'$schema','type','additionalProperties','required','properties','const','enum','anyOf','allOf','if','then','else','pattern','minimum','maximum','maxItems','items'})
 if 'anyOf' in schema:
  require(set(schema)=={'anyOf'})
  successes=0
  for alternative in schema['anyOf']:
   try:validate(value,alternative,depth+1);successes+=1
   except Invalid:pass
  require(successes>0)
  return
 if 'allOf' in schema:
  for part in schema['allOf']:validate(value,part,depth+1)
 if 'if' in schema:
  try:validate(value,schema['if'],depth+1);matched=True
  except Invalid:matched=False
  branch='then' if matched else 'else'
  if branch in schema:validate(value,schema[branch],depth+1)
 if 'type' in schema:
  kinds=schema['type'] if isinstance(schema['type'],list) else [schema['type']]
  require(all(x in {'object','array','string','integer','number','boolean','null'} for x in kinds))
  types={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),'integer':type(value) is int,'number':type(value) in (int,float) and math.isfinite(value),'boolean':type(value) is bool,'null':value is None}
  require(any(types[k] for k in kinds))
 if 'const' in schema:require(type(value) is type(schema['const']) and value==schema['const'])
 if 'enum' in schema:require(any(type(value) is type(x) and value==x for x in schema['enum']))
 if isinstance(value,dict):
  require(set(schema.get('required',[]))<=set(value))
  if schema.get('additionalProperties') is False:require(set(value)<=set(schema.get('properties',{})))
  for key,part in schema.get('properties',{}).items():
   if key in value:validate(value[key],part,depth+1)
 if isinstance(value,list):
  require(len(value)<=schema.get('maxItems',4096) and 'items' in schema)
  for item in value:validate(item,schema['items'],depth+1)
 if isinstance(value,str):require(len(value)<=512 and ('pattern' not in schema or re.fullmatch(schema['pattern'],value)))
 if type(value) in (int,float):require(math.isfinite(value) and schema.get('minimum',value)<=value<=schema.get('maximum',value))

def read_private_sample(path,expected_uid=0):
 file=Path(path)
 require(file.is_absolute() and file.resolve()==file and not file.is_symlink())
 fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
 try:
  before=os.fstat(fd)
  require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=MAX_BYTES)
  if os.name=='posix':require(before.st_uid==expected_uid and stat.S_IMODE(before.st_mode)&0o007==0 and stat.S_IMODE(before.st_mode)&0o022==0)
  raw=os.read(fd,MAX_BYTES+1);after=os.fstat(fd)
  require(len(raw)<=MAX_BYTES and (before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns))
  def unique(pairs):
   value={}
   for key,item in pairs:require(key not in value);value[key]=item
   return value
  value=json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(Invalid()),object_pairs_hook=unique)
  require(isinstance(value,dict));return value
 finally:os.close(fd)

def transform(sample,sequence,now):
 validate(sample,json.loads(SCHEMA.read_text(encoding='utf-8')))
 require(sample['collected_at'] is not None and sample['collected_at']<=now+5)
 host=sample['host'];workers=[];services=[];storage=[]
 for source in sample['containers']:
  cls=source['service_class'];metrics=source['metrics']
  binding=source['managed_namespace']=='megmeet-personal-server-v1' and source['owner_ref'] is not None
  if cls=='cloud-worker':require(binding)
  else:require(source['owner_ref'] is None)
  identity=[sample['collector_instance'],source['container_id'],source['started_at'],source['restart_count'],source['identity']]
  row={**{k:None for k in RESOURCE_FIELDS},'instance_id':hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest(),'state':source['state'] if source['state'] in STATES else 'unknown','cpu_usage_ns':None if metrics['cpu']['usage_usec'] is None else metrics['cpu']['usage_usec']*1000,'cpu_cores':metrics['cpu']['cores'],'memory_used_bytes':metrics['memory']['current_bytes'],'memory_limit_bytes':metrics['memory']['max_bytes'],'pids_current':metrics['pids']['current'],'pids_limit':metrics['pids']['max'],'storage_bytes':None,'restarts':source['restart_count'],'oom_kills':metrics['memory']['events'].get('oom_kill') if metrics['memory']['events'] else None,'pids_limit_hits':metrics['pids']['events'].get('max') if metrics['pids']['events'] else None}
  for key in ('read_bytes','write_bytes','read_bytes_per_second','write_bytes_per_second'):row['io_'+key]=metrics['io'][key]
  if metrics['network']['shared_namespace'] is False:
   for key in ('rx_bytes','tx_bytes','rx_bytes_per_second','tx_bytes_per_second'):row['network_'+key]=metrics['network'][key]
  if source['sample_status'] not in ('available','partial'):
   for key in row:
    if key not in ('instance_id','state'):row[key]=None
  row['health']=source['health'];row['oom_killed']=source['oom_killed']
  if cls=='cloud-worker':row['workspace_id']=str(UUID(source['owner_ref']));workers.append(row)
  else:row['service']=cls;services.append(row)
 for source in sample['owner_storage']:
  require(source['sampled_at'] is None or source['sampled_at']<=now+5)
  stale=source['stale'] or source['sampled_at'] is None or now-source['sampled_at']>source['stale_after_seconds']
  storage.append({'workspace_id':str(UUID(source['owner_ref'])),'storage_bytes':source['logical_bytes'],'sampled_at':source['sampled_at'],'complete':source['complete'],'stale':stale})
 return {'schema':'maimi-monitor-collector-v1','sampled_at':sample['collected_at'],'sequence':sequence,'inventory_complete':sample['coverage']['docker']=='available','host':{'cpu_count':host['cpu_count'],'memory_total_bytes':host['memory_total_bytes'],'memory_available_bytes':host['memory_available_bytes'],'data_available_bytes':host['disks']['data']['available_bytes']},'workers':workers,'services':services,'storage':storage}

class Ingestor:
 def __init__(self,store,clock=time.time):
  self.store,self.clock=store,clock
  with store.lock:store.db.execute('CREATE TABLE IF NOT EXISTS collector_cursor(instance TEXT PRIMARY KEY,sequence INTEGER,sampled REAL,monotonic_ns INTEGER)')
 def ingest(self,sample):
  # Local trusted producer only. Replayed files are harmless and do not create
  # fake fresh samples; collector restart starts a new monotonic series.
  with self.store.lock:
   latest=self.store.db.execute('SELECT MAX(sequence) FROM samples').fetchone()[0]
   value=transform(sample,0 if latest is None else latest+1,self.clock())
   old=self.store.db.execute('SELECT * FROM collector_cursor WHERE instance=?',(sample['collector_instance'],)).fetchone()
   if old and sample['sequence']<=old['sequence']:return False
   if old:require(sample['monotonic_ns']>old['monotonic_ns'])
   self.store.record_resources(value)
   self.store.db.execute('INSERT INTO collector_cursor VALUES(?,?,?,?) ON CONFLICT(instance) DO UPDATE SET sequence=excluded.sequence,sampled=excluded.sampled,monotonic_ns=excluded.monotonic_ns',(sample['collector_instance'],sample['sequence'],sample['collected_at'],sample['monotonic_ns']))
   self.store.db.execute('DELETE FROM collector_cursor WHERE instance NOT IN (SELECT instance FROM collector_cursor ORDER BY sampled DESC LIMIT 16)')
   return True
 def ingest_file(self,path,expected_uid=0):return self.ingest(read_private_sample(path,expected_uid))
