"""Explicit gateway startup wiring. Disabled unless an operator opts in.

This package gets only a read-only host metadata directory, never a Docker
socket, /proc of the host, document volume or user workspace.
"""
import asyncio
import os
from pathlib import Path
from .monitor import MetadataStore,Monitor,make_router,actual_bindings,DEVELOPERS,require
from .collector_adapter import Ingestor

STATE_DIRECTORY=Path('/var/lib/maimi-monitor')
COLLECTOR_FILE=Path('/run/maimi-monitor/latest.json')

class Runtime:
 def __init__(self,*,producer,bindings=actual_bindings,state_directory=STATE_DIRECTORY,collector_file=COLLECTOR_FILE):
  self.producer=producer;self.bindings=bindings;self.state_directory=state_directory;self.collector_file=collector_file
  self.store=None;self.monitor=None;self.ingestor=None;self.poll_task=None;self.stop_event=asyncio.Event()
  self.status='not_started';self.collector_status='not_reported';self.producer_started=False
 async def report(self,window):
  if self.monitor is None:raise RuntimeError('MONITOR_NOT_READY')
  result=dict(await self.monitor.report(window));result['coverage']={**result['coverage'],'collector_file':self.collector_status,'statistics_cache_seconds':5}
  return result
 async def start(self):
  try:
   # Parent must be operator-provisioned, owned by the gateway service UID,
   # mode 0700. Existing database permissions are checked, never repaired.
   self.store=await asyncio.to_thread(MetadataStore,self.state_directory/'metadata.sqlite')
   self.ingestor=Ingestor(self.store);self.monitor=Monitor(self.store,self.bindings)
   self.producer.configure(self.store);self.producer_started=True
   self.status='ready';self.poll_task=asyncio.create_task(self._poll(),name='maimi-monitor-metadata-ingest')
  except Exception:
   self.status='unavailable';self.monitor=None
   if self.store is not None and not self.producer_started:self.store.close();self.store=None
   # A monitoring configuration/storage failure does not break chat startup.
 async def _poll(self):
  while not self.stop_event.is_set():
   try:
    await asyncio.to_thread(self.ingestor.ingest_file,self.collector_file,0)
    self.collector_status='available'
   except FileNotFoundError:self.collector_status='not_reported'
   except Exception:self.collector_status='unavailable'
   try:await asyncio.wait_for(self.stop_event.wait(),5)
   except asyncio.TimeoutError:pass
 async def stop(self):
  self.stop_event.set()
  if self.poll_task is not None:await self.poll_task
  stopped=True
  if self.producer_started:
   try:stopped=await asyncio.to_thread(self.producer.shutdown)
   except Exception:stopped=False
  # If a bounded producer flush has not finished, don't close SQLite under a
  # live writer. The process will release it on exit; report incomplete stop.
  if self.store is not None and stopped:self.store.close();self.store=None
  self.monitor=None;self.status='stopped' if stopped else 'stop_incomplete'

def read_assets(directory):
 result={}
 for name in ('index.html','monitor.css','monitor.mjs'):
  file=directory/name
  require(file.is_file() and not file.is_symlink() and file.stat().st_size<=512*1024)
  result[name]=file.read_bytes()
 return result

def install(app,*,environ=None,producer=None,bindings=actual_bindings,ui=None):
 """Call once during gateway app construction; no disk IO while disabled.

 Production app: from .observability.bootstrap import install
                 monitor_runtime = install(app)
 Mappings place the reviewed UI in app/observability/assets/.
 """
 env=os.environ if environ is None else environ
 enabled=env.get('MAIMI_MONITOR_ENABLED','false')
 if enabled not in ('false','true'):
  app.state.maimi_monitor_status='configuration_invalid';return None
 if enabled=='false':app.state.maimi_monitor_status='disabled';return None
 allowed=DEVELOPERS
 if 'MAIMI_MONITOR_READ_USERS' in env:
  requested=frozenset(x.strip() for x in env['MAIMI_MONITOR_READ_USERS'].split(',') if x.strip())
  # Deployment can restrict the authorized set; widening needs a reviewed
  # policy/source update. No environment or HTTP parameter grants SSH/admin.
  if not requested<=DEVELOPERS:
   app.state.maimi_monitor_status='configuration_invalid';return None
  allowed=requested
 if producer is None:
  try:from app import usage_producer
  except ImportError:app.state.maimi_monitor_status='producer_unavailable';return None
  producer=usage_producer
 runtime=Runtime(producer=producer,bindings=bindings)
 try:assets=read_assets(Path(__file__).parent/'assets') if ui is None else ui
 except (OSError,ValueError):assets={}
 except Exception:assets={}
 app.state.maimi_monitor_status='prepared' if assets else 'assets_unavailable'
 app.include_router(make_router(runtime,allowed_users=allowed,ui=assets))
 app.add_event_handler('startup',runtime.start);app.add_event_handler('shutdown',runtime.stop)
 return runtime
