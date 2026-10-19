import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from app.observability.monitor import MetadataStore,Monitor,Invalid,Denied,make_router,DEVELOPERS,binding_map
from helpers import snapshot,WS,NOW,bindings

class StoreTests(unittest.TestCase):
 def setUp(self):self.now=NOW;self.store=MetadataStore(clock=lambda:self.now,event_limit=10)
 def tearDown(self):self.store.close()
 def report(self,fn=bindings):return asyncio.run(Monitor(self.store,fn,clock=lambda:self.now).report())
 def event(self,kind='model',**kw):return {'event_id':str(uuid4()),'kind':kind,'completed_at':self.now,'status':'succeeded','duration_ms':10,'input_tokens':None,'output_tokens':None,**kw}
 def test_missing_is_unknown_not_zero(self):
  data=self.report();self.assertIsNone(data['overview']['running_workers']);self.assertIsNone(data['overview']['model']['input_tokens']['value']);self.assertEqual(data['freshness']['status'],'missing')
 def test_first_cpu_unknown_then_collector_rate(self):
  self.store.record_resources(snapshot(self.now-60,1,100));self.assertIsNone(self.report()['users'][0]['cloud']['cpu_cores']['value'])
  self.store.record_resources(snapshot(self.now,2,200));self.assertEqual(self.report()['users'][0]['cloud']['cpu_cores']['value'],.5)
 def test_duplicate_instance_and_unknown_fields_rejected(self):
  item=snapshot(self.now,1,100);item['workers'][0]['employee']='M00096389'
  with self.assertRaises(Invalid):self.store.record_resources(item)
  item=snapshot(self.now,1,100);item['workers']*=2
  with self.assertRaises(Invalid):self.store.record_resources(item)
 def test_replay_and_time_reversal_rejected(self):
  item=snapshot(self.now,1,100);self.store.record_resources(item)
  with self.assertRaises(Invalid):self.store.record_resources(item)
  with self.assertRaises(Invalid):self.store.record_resources(snapshot(self.now-10,2,100))
 def test_cross_user_resource_never_reassigned(self):
  item=snapshot(self.now,1,100);item['workers'][0]['workspace_id']=str(uuid4());self.store.record_resources(item)
  data=self.report();self.assertEqual(data['coverage']['workers_unbound'],1);self.assertIsNone(data['users'][0]['cloud']['memory_used_bytes']['value'])
 def test_binding_failure_unknown_counts(self):
  async def broken():raise RuntimeError('private error must never be emitted')
  self.store.record_resources(snapshot(self.now,1,100));data=self.report(broken)
  self.assertIsNone(data['overview']['registered_users']);self.assertIsNone(data['overview']['running_workers']);self.assertEqual(data['users'],[])
 def test_distinct_owner_count_not_workspaces(self):
  async def two():return [*(await bindings()),{'workspace_id':str(uuid4()),'owner_user_no':'M00099999','enabled':True}]
  self.assertEqual(self.report(two)['overview']['registered_users'],1)
 def test_partial_inventory_is_unknown(self):
  item=snapshot(self.now,1,100);item['inventory_complete']=False;item['workers']=[];self.store.record_resources(item)
  data=self.report();self.assertEqual(data['coverage']['cloud_collector'],'partial');self.assertIsNone(data['overview']['running_workers']);self.assertIsNone(data['users'][0]['running_workers'])
 def test_offline_storage_and_stale_storage(self):
  item=snapshot(self.now,1,100);item['workers']=[];self.store.record_resources(item)
  self.assertEqual(self.report()['users'][0]['cloud']['storage_bytes']['value'],64*1024**2)
  item=snapshot(self.now+10,2,100);item['storage'][0]['stale']=True;self.now+=10;self.store.record_resources(item)
  self.assertIsNone(self.report()['users'][0]['cloud']['storage_bytes']['value'])
 def test_stale_snapshot_remains_marked(self):
  self.store.record_resources(snapshot(self.now,1,100));self.now+=121
  self.assertEqual(self.report()['freshness']['status'],'stale')
 def test_event_unknown_usage_and_no_body_allowed(self):
  self.store.enable_source('model');self.store.record_event(trusted_owner='M00099999',event=self.event())
  self.assertIsNone(self.report()['overview']['model']['input_tokens']['value'])
  with self.assertRaises(Invalid):self.store.record_event(trusted_owner='M00099999',event=self.event(prompt='sensitive'))
 def test_provider_usage_and_cache_are_exact(self):
  self.store.enable_source('model');event=self.event(input_tokens=25,output_tokens=7,cache_read_tokens=20)
  self.store.record_event(trusted_owner='M00099999',event=event);self.store.record_event(trusted_owner='M00099999',event=event)
  result=self.report()['overview']['model'];self.assertEqual(result['requests']['value'],1);self.assertEqual(result['input_tokens']['value'],25);self.assertEqual(result['cache_read_tokens']['value'],20);self.assertIsNone(result['cache_write_tokens']['value'])
 def test_event_owner_collision_rejected(self):
  self.store.enable_source('model');event=self.event();self.store.record_event(trusted_owner='M00099999',event=event)
  with self.assertRaises(Invalid):self.store.record_event(trusted_owner='M00088888',event=event)
 def test_observed_terminal_first_observation_preserved(self):
  self.store.enable_source('office');event=self.event('office');self.store.record_observed_event(trusted_owner='M00099999',event=event)
  self.store.record_observed_event(trusted_owner='M00099999',event={**event,'duration_ms':None,'completed_at':self.now+1})
  self.assertEqual(self.report()['overview']['office']['duration_p95_ms']['value'],10)
  with self.assertRaises(Invalid):self.store.record_observed_event(trusted_owner='M00099999',event={**event,'status':'failed'})
 def test_retention_cap_and_loss_are_visible(self):
  self.store.enable_source('model')
  for i in range(11):self.store.record_event(trusted_owner='M00099999',event=self.event(completed_at=self.now-i))
  result=self.report();self.assertEqual(result['overview']['model']['requests']['value'],10);self.assertTrue(any(a['code']=='EVENT_SAMPLES_INCOMPLETE' for a in result['alerts']));self.assertFalse(result['coverage']['window_complete']['model'])
 def test_explicit_queue_loss(self):
  self.store.enable_source('model');self.store.record_loss('model',self.now)
  self.assertTrue(any(a['scope']=='model' for a in self.report()['alerts']))
 def test_platform_trends_low_frequency_and_no_interpolation(self):
  self.store.record_resources(snapshot(self.now-90,1,100));self.store.record_resources(snapshot(self.now-80,2,110));self.store.record_resources(snapshot(self.now,3,150))
  series=self.report()['overview']['series'];self.assertEqual(len(series['points']),2);self.assertEqual(series['scope'],'PLATFORM_AGGREGATES_NO_USER_ATTRIBUTION')
 def test_multiple_service_instances_explicit(self):
  item=snapshot(self.now,1,100);item['services'].append({**item['services'][0],'instance_id':'c'*64});self.store.record_resources(item)
  service=self.report()['services'][0];self.assertEqual(service['state'],'multiple_instances');self.assertEqual(service['running_instances'],2)
 def test_query_cache_is_bounded_and_avoids_four_binding_reads(self):
  calls=[]
  async def lookup():calls.append(1);return await bindings()
  async def run():
   monitor=Monitor(self.store,lookup,clock=lambda:self.now)
   for _ in range(4):await monitor.report()
   self.assertEqual(len(calls),1);self.now+=5;await monitor.report();self.assertEqual(len(calls),2)
  asyncio.run(run())
 def test_sources_are_not_assumed_active_after_restart(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory).resolve()/'metadata.sqlite';store=MetadataStore(path,clock=lambda:self.now);store.enable_source('model');store.close()
   store=MetadataStore(path,clock=lambda:self.now);self.assertEqual(store.read('24h')[2],{});store.close()

class RouteTests(unittest.TestCase):
 def setUp(self):
  self.owner='M00096389';self.calls=[];self.store=MetadataStore(clock=lambda:NOW)
  async def auth(request):
   self.calls.append(request.url.path)
   if self.owner is None:raise Denied(401,'MONITOR_LOGIN_REQUIRED')
   return self.owner
  self.app=FastAPI();self.app.include_router(make_router(Monitor(self.store,bindings,clock=lambda:NOW),authenticate=auth,ui={'index.html':b'<html></html>','monitor.mjs':b'export {};','monitor.css':b'body{}'},clock=lambda:NOW));self.client=TestClient(self.app)
 def tearDown(self):self.client.close();self.store.close()
 def test_exact_six_user_grant(self):self.assertEqual(len(DEVELOPERS),6);self.assertNotIn('M00003307',DEVELOPERS)
 def test_all_authorized_have_read_access(self):
  for owner in DEVELOPERS:self.owner=owner;self.assertEqual(self.client.get('/v1/developer-monitor/access').status_code,200)
 def test_non_developer_denied_api_html_assets_and_header_cannot_grant(self):
  self.owner='M00003307'
  for path in ('/v1/developer-monitor/users','/maimi/developer/monitor','/maimi/developer/monitor/assets/monitor.mjs'):
   self.assertEqual(self.client.get(path,headers={'x-user-no':'M00096389','x-role':'developer'}).status_code,403)
 def test_auth_rechecked_after_revoke(self):
  self.assertEqual(self.client.get('/v1/developer-monitor/access').status_code,200);self.owner=None;self.assertEqual(self.client.get('/v1/developer-monitor/access').status_code,401)
 def test_access_does_not_query_binding_database(self):
  async def broken():raise AssertionError('must not read')
  app=FastAPI();app.include_router(make_router(Monitor(self.store,broken),authenticate=lambda r:asyncio.sleep(0,result='M00096389')))
  with TestClient(app) as client:self.assertEqual(client.get('/v1/developer-monitor/access').json()['allowed'],True)
 def test_no_ingestion_or_write_endpoint(self):
  self.assertEqual(self.client.post('/v1/developer-monitor/users',json={'user_no':'M00096389'}).status_code,405)
  self.assertEqual(self.client.post('/v1/developer-monitor/ingest').status_code,404)
 def test_query_whitelist_and_user_validation(self):
  for path in ('/overview?window=all','/users?window=1h&window=7d','/users?role=developer','/users/ANY'):
   self.assertEqual(self.client.get('/v1/developer-monitor'+path).status_code,400)
 def test_error_does_not_expose_user_or_trace(self):
  self.owner=None;result=self.client.get('/v1/developer-monitor/users');self.assertEqual(result.json(),{'schema':'maimi-developer-monitor-v1','error':{'code':'MONITOR_LOGIN_REQUIRED'}})
 def test_cross_site_and_refresh_limit(self):
  self.assertEqual(self.client.get('/v1/developer-monitor/access',headers={'sec-fetch-site':'cross-site'}).status_code,403)
  for _ in range(60):self.assertEqual(self.client.get('/v1/developer-monitor/access').status_code,200)
  self.assertEqual(self.client.get('/v1/developer-monitor/access').status_code,429)
 def test_private_security_headers(self):
  value=self.client.get('/maimi/developer/monitor');self.assertEqual(value.headers['cache-control'],'no-store, private');self.assertIn("frame-ancestors 'none'",value.headers['content-security-policy'])
 def test_empty_allowlist_denies(self):
  app=FastAPI();app.include_router(make_router(Monitor(self.store,bindings),allowed_users=frozenset(),authenticate=lambda r:asyncio.sleep(0,result='M00096389')))
  with TestClient(app) as client:self.assertEqual(client.get('/v1/developer-monitor/access').status_code,403)

if __name__=='__main__':unittest.main()
