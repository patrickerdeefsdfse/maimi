import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.observability.monitor import MetadataStore,Monitor,Invalid,make_router
from app.observability.collector_adapter import transform,Ingestor,read_private_sample

from app.observability.bootstrap import Runtime,install
from helpers import collector_samples

class AdapterTests(unittest.TestCase):
 def setUp(self):
  self.samples=collector_samples()
  self.now=max(s['collected_at'] for s in self.samples)+1
  self.store=MetadataStore(clock=lambda:self.now)
 def tearDown(self):self.store.close()
 def test_synthetic_collector_samples_ingest_and_replay(self):
  ingest=Ingestor(self.store,clock=lambda:self.now)
  for sample in self.samples:self.assertTrue(ingest.ingest(sample))
  self.assertFalse(ingest.ingest(self.samples[-1]));self.assertEqual(len(self.store.read('24h')[0]),2)
 def test_strict_conditional_owner_schema(self):
  bad=deepcopy(self.samples[0]);worker=next(x for x in bad['containers'] if x['service_class']=='cloud-worker');worker['owner_ref']=None
  with self.assertRaises(Invalid):transform(bad,1,self.now)
  bad=deepcopy(self.samples[0]);service=next(x for x in bad['containers'] if x['service_class']!='cloud-worker');service['owner_ref']='a'*32
  with self.assertRaises(Invalid):transform(bad,1,self.now)
 def test_secret_extra_field_rejected_at_any_depth(self):
  for change in (lambda s:s.update(credentials='forbidden'),lambda s:s['containers'][0]['metrics']['cpu'].update(command='forbidden')):
   bad=deepcopy(self.samples[0]);change(bad)
   with self.assertRaises(Invalid):transform(bad,1,self.now)
 def test_nan_boolean_and_future_time_rejected(self):
  for value in (float('nan'),True,-1):
   bad=deepcopy(self.samples[0]);bad['host']['memory_available_bytes']=value
   with self.assertRaises(Invalid):transform(bad,1,self.now)
  bad=deepcopy(self.samples[0]);bad['collected_at']=self.now+10
  with self.assertRaises(Invalid):transform(bad,1,self.now)
 def test_collector_restart_identity_changes(self):
  before=transform(self.samples[0],1,self.now);sample=deepcopy(self.samples[0]);sample['collector_instance']='f'*32;after=transform(sample,2,self.now)
  self.assertNotEqual(before['workers'][0]['instance_id'],after['workers'][0]['instance_id'])
 def test_shared_network_cannot_be_counted_twice(self):
  sample=deepcopy(self.samples[0]);sample['containers'][0]['metrics']['network']['shared_namespace']=True
  value=transform(sample,1,self.now);self.assertIsNone(value['workers'][0]['network_rx_bytes'])
 def test_partial_empty_sample_does_not_make_zero_trend(self):
  sample=deepcopy(self.samples[0]);sample['status']='partial';sample['coverage']['docker']='unavailable';sample['containers']=[]
  Ingestor(self.store,clock=lambda:self.now).ingest(sample);point=self.store.trends('24h')['points'][0]
  self.assertIsNone(point['observed_workers']);self.assertIsNone(point['containers_memory_used_bytes']);self.assertIsNotNone(point['host_memory_available_bytes'])
 def test_source_read_is_bounded_and_no_raw_output(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory).resolve()/'latest.json';path.write_text('x'*(2*1024*1024+1))
   with self.assertRaises(Invalid):read_private_sample(path)
 def test_multiple_workers_same_owner_preserved(self):
  sample=deepcopy(self.samples[0]);row=deepcopy(sample['containers'][0]);row['container_id']='e'*64;sample['containers'].append(row)
  result=transform(sample,1,self.now);self.store.record_resources(result);self.assertEqual(len(result['workers']),2)

class BootstrapTests(unittest.TestCase):
 def test_disabled_does_not_create_routes_or_read_files(self):
  app=FastAPI()
  with patch('app.observability.bootstrap.read_assets',side_effect=AssertionError('no file reads')):self.assertIsNone(install(app,environ={}))
  self.assertFalse(any('developer-monitor' in r.path for r in app.routes))
 def test_environment_cannot_add_unapproved_reader(self):
  app=FastAPI();self.assertIsNone(install(app,environ={'MAIMI_MONITOR_ENABLED':'true','MAIMI_MONITOR_READ_USERS':'M00003307'},producer=object(),ui={}));self.assertEqual(app.state.maimi_monitor_status,'configuration_invalid')
 def test_missing_assets_do_not_break_chat_app_construction(self):
  app=FastAPI()
  with patch('app.observability.bootstrap.read_assets',side_effect=FileNotFoundError()):runtime=install(app,environ={'MAIMI_MONITOR_ENABLED':'true'},producer=object())
  self.assertIsNotNone(runtime);self.assertEqual(app.state.maimi_monitor_status,'assets_unavailable')
 def test_start_stop_opens_private_store_and_drains_before_close(self):
  with tempfile.TemporaryDirectory() as directory:
   calls=[]
   class Producer:
    def configure(self,store):self.store=store;calls.append('configure');store.enable_source('model')
    def shutdown(self):self.store.read('24h');calls.append('shutdown');return True
   producer=Producer();runtime=Runtime(producer=producer,state_directory=Path(directory).resolve(),collector_file=Path(directory).resolve()/'missing.json')
   async def run():
    await runtime.start();self.assertEqual(runtime.status,'ready');await asyncio.sleep(.02);self.assertEqual(runtime.collector_status,'not_reported');await runtime.stop();self.assertEqual(runtime.status,'stopped');self.assertIsNone(runtime.store)
   asyncio.run(run());self.assertEqual(calls,['configure','shutdown'])
 def test_storage_failure_does_not_break_chat_startup(self):
  runtime=Runtime(producer=object(),state_directory=Path('/definitely-absent-monitor-dir'))
  asyncio.run(runtime.start());self.assertEqual(runtime.status,'unavailable');self.assertIsNone(runtime.store)
 def test_unfinished_producer_is_not_closed_under_writer(self):
  with tempfile.TemporaryDirectory() as directory:
   producer=SimpleNamespace(configure=lambda s:None,shutdown=lambda:False)
   runtime=Runtime(producer=producer,state_directory=Path(directory).resolve(),collector_file=Path(directory).resolve()/'missing')
   async def run():await runtime.start();await runtime.stop()
   asyncio.run(run());self.assertEqual(runtime.status,'stop_incomplete');runtime.store.read('24h');runtime.store.close()
 def test_monitor_heavy_read_does_not_block_async_business_heartbeat(self):
  store=MetadataStore();read=store.read
  def slow(window):time.sleep(.1);return read(window)
  store.read=slow
  async def bindings():return []
  async def run():
   task=asyncio.create_task(Monitor(store,bindings).report());ticks=0
   while not task.done():await asyncio.sleep(.005);ticks+=1
   await task;self.assertGreater(ticks,5)
  asyncio.run(run());store.close()

class ActualIAMTests(unittest.TestCase):
 def test_real_iam_cookie_revalidation_and_revocation_not_user_headers(self):
  from app import iam_oauth as iam
  sid='synthetic-session-'+'x'*32
  class Sessions:
   record={'account_no':'synthetic-account','user_no':'M00096389'}
   epoch=0
   async def session_read(self,value):return self.record if value==sid else None
   async def session_delete(self,value):self.record=None
   async def account_revocation_version(self,value):return self.epoch
  sessions=Sessions()
  async def mapper(account):return {'account_no':account,'user_no':'M00096389','display_name':'Synthetic','organization_codes':[]}
  service=iam.IAMService(SimpleNamespace(identity_mode='directory'),sessions,mapper=mapper)
  async def with_service(callback):return await callback(service)
  store=MetadataStore()
  async def bindings():return []
  app=FastAPI();app.include_router(make_router(Monitor(store,bindings)))
  with patch.object(iam,'_with_service',with_service),TestClient(app) as client:
   self.assertEqual(client.get('/v1/developer-monitor/access',headers={'x-user-no':'M00096389'}).status_code,401)
   client.cookies.set(iam.SESSION_COOKIE,sid)
   self.assertEqual(client.get('/v1/developer-monitor/access').status_code,200)
   self.assertEqual(client.get('/v1/developer-monitor/access',headers={'Authorization':'Bearer synthetic'}).status_code,401)
   sessions.epoch=1;self.assertEqual(client.get('/v1/developer-monitor/access').status_code,401)
  store.close()

if __name__=='__main__':unittest.main()
