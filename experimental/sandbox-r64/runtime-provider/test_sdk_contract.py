"""Real pinned SDK through synthetic HTTP transport; no Docker or network."""
import json
import asyncio
import unittest
from copy import deepcopy
import httpx
from provider import Provider, ProviderError, Profile, REQUIRED_NODE_FACTS, PrivateTransport

TASK = '11111111-2222-3333-4444-555555555555'
IMAGE = 'example.invalid/office@sha256:' + 'a'*64
KEY = 'synthetic-not-a-real-api-key-' + 'x'*32
SID = 'synthetic-sandbox'


class Guard:
    def __init__(self): self.checked=[]; self.absent=True; self.facts={k:True for k in REQUIRED_NODE_FACTS}; self.fail_snapshot=False
    async def probe(self): return self.facts
    async def verify_sandbox(self,*args): self.checked.append(args); return 'b'*64
    async def verify_task_absent(self,*args): return self.absent
    async def snapshot_output(self,*args):
        if self.fail_snapshot: raise ProviderError('SNAPSHOT_RESUME_UNCONFIRMED')
        return b'synthetic-output'


class API:
    def __init__(self):
        self.calls=[]; self.exists=False; self.fail_create=False; self.fail_delete=False
        self.huge_list=False; self.bad_owner=False; self.bad_endpoint=False; self.incomplete_command=False
    def info(self):
        return {'id':SID,'status':{'state':'Running'},'entrypoint':['sleep','infinity'],
          'createdAt':'2026-10-17T00:00:00Z','image':{'uri':IMAGE},
          'metadata':{'maimi_task_id':TASK,'maimi_controller':'other' if self.bad_owner else 'test-controller','maimi_profile':'office'}}
    def handler(self, req):
        self.calls.append((req.method,req.url.path,bytes(req.content),dict(req.headers)))
        if req.url.host != '127.0.0.1': raise AssertionError('external request')
        if req.headers.get('open-sandbox-api-key') != KEY: raise AssertionError('missing internal authentication')
        path=req.url.path
        if path=='/v1/sandboxes' and req.method=='GET':
            return httpx.Response(200,json={'items':[self.info()] if self.exists else [],
              'pagination':{'page':1,'pageSize':25,'totalItems':101 if self.huge_list else int(self.exists),'totalPages':1,'hasNextPage':False}})
        if path=='/v1/sandboxes' and req.method=='POST':
            self.exists=True
            if self.fail_create: raise httpx.ReadTimeout('synthetic lost ack')
            return httpx.Response(202,json=self.info())
        if path==f'/v1/sandboxes/{SID}' and req.method=='GET':
            return httpx.Response(200,json=self.info()) if self.exists else httpx.Response(404,json={'code':'not_found','message':'synthetic'})
        if path==f'/v1/sandboxes/{SID}' and req.method=='DELETE':
            if self.fail_delete: raise httpx.ReadTimeout('synthetic lost ack')
            self.exists=False; return httpx.Response(204)
        if '/endpoints/' in path:
            port=path.rsplit('/',1)[-1]
            return httpx.Response(200,json={'endpoint':('attacker.invalid:80' if self.bad_endpoint else f'127.0.0.1:18080/v1/sandboxes/{SID}/proxy/{port}'),'headers':{}})
        if path.endswith('/ping'): return httpx.Response(200)
        if path.endswith('/files/upload'): return httpx.Response(200)
        if path.endswith('/command') and req.method=='POST':
            events=[{'type':'init','text':'exec-1','timestamp':1},{'type':'stdout','text':'hello world','timestamp':2}]
            if not self.incomplete_command: events.append({'type':'execution_complete','timestamp':3,'execution_time':1})
            return httpx.Response(200,content=''.join('data: '+json.dumps(e)+'\n\n' for e in events),headers={'content-type':'text/event-stream'})
        if path.endswith('/command') and req.method=='DELETE': return httpx.Response(200)
        raise AssertionError('unexpected synthetic path '+req.method+' '+path)


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.api=API(); self.guard=Guard()
    def provider(self,enabled=True):
        return Provider(endpoint='http://127.0.0.1:18080',api_key=KEY,namespace='test-controller',profiles={'office':Profile(IMAGE)},node_guard=self.guard,enabled=enabled,transport_factory=lambda:httpx.MockTransport(self.api.handler))
    async def test_disabled_and_missing_policy_do_not_send_requests(self):
        with self.assertRaisesRegex(ProviderError,'NODE_POLICY_NOT_VERIFIED'): await self.provider(False).create(TASK,'office',{'maimi_task_id':TASK})
        self.guard.facts['runsc_registered']=False
        with self.assertRaisesRegex(ProviderError,'NODE_POLICY_NOT_VERIFIED'): await self.provider().create(TASK,'office',{'maimi_task_id':TASK})
        self.assertEqual(self.api.calls,[])
    async def test_activation_and_resource_types_are_strict(self):
        for flag in ('false', 1, None):
            with self.assertRaisesRegex(ProviderError,'INVALID_ADMIN_CONFIGURATION'): self.provider(flag)
        for values in ({'cpu_millis':True}, {'memory_mib':2048.0}, {'lifetime_seconds':False}):
            with self.assertRaisesRegex(ProviderError,'INVALID_ADMIN_PROFILE'): Profile(IMAGE,**values).validate()
        self.assertEqual(self.api.calls,[])
    async def test_connection_total_deadline_is_bounded(self):
        with self.assertRaises(TimeoutError):
            async with self.provider()._connection(seconds=0.02):
                await asyncio.sleep(1)
        self.assertEqual(self.api.calls,[])
    async def test_real_sdk_create_scope_image_resources_and_proxy(self):
        sid=await self.provider().create(TASK,'office',{'maimi_task_id':TASK})
        self.assertEqual(sid,SID)
        posts=[x for x in self.api.calls if x[:2]==('POST','/v1/sandboxes')]
        self.assertEqual(len(posts),1); body=json.loads(posts[0][2])
        self.assertEqual(body['image']['uri'],IMAGE)
        self.assertEqual(body['resourceLimits'],{'cpu':'1.0','memory':'2048Mi'})
        self.assertEqual(body['metadata']['maimi_task_id'],TASK)
        self.assertNotIn(KEY,json.dumps(body))
        self.assertTrue(self.guard.checked)
    async def test_lost_create_ack_never_reposts(self):
        self.api.fail_create=True
        with self.assertRaisesRegex(ProviderError,'CREATE_OUTCOME_UNKNOWN'): await self.provider().create(TASK,'office',{'maimi_task_id':TASK})
        self.assertEqual(sum(m=='POST' and p=='/v1/sandboxes' for m,p,_,_ in self.api.calls),1)
        self.assertTrue(self.api.exists)
    async def test_existing_task_never_creates_again(self):
        self.api.exists=True
        with self.assertRaisesRegex(ProviderError,'TASK_ALREADY_HAS_SANDBOX'): await self.provider().create(TASK,'office',{'maimi_task_id':TASK})
        self.assertFalse(any(m=='POST' for m,_,_,_ in self.api.calls))
    async def test_real_sdk_upload_permissions_and_not_credential_payload(self):
        self.api.exists=True
        await self.provider().write_input(SID,'/workspace/inputs/input-000',b'synthetic bytes')
        uploads=[x for x in self.api.calls if x[1].endswith('/files/upload')]
        self.assertEqual(len(uploads),1)
        self.assertIn(b'"mode": 444',uploads[0][2]); self.assertNotIn(KEY.encode(),uploads[0][2])
    async def test_real_sdk_command_stream_capped_and_not_replayed(self):
        self.api.exists=True
        result=await self.provider().execute(SID,'printf hello',5,5)
        self.assertEqual(result,{'exit_code':0,'stdout':'hello','stderr':'','truncated':True})
        self.assertEqual(sum(m=='POST' and path.endswith('/command') for m,path,_,_ in self.api.calls),1)
    async def test_missing_command_complete_is_unknown(self):
        self.api.exists=True; self.api.incomplete_command=True
        with self.assertRaisesRegex(ProviderError,'COMMAND_OUTCOME_UNKNOWN_NO_RETRY'): await self.provider().execute(SID,'synthetic command',5,100)
        self.assertEqual(sum(m=='POST' and path.endswith('/command') for m,path,_,_ in self.api.calls),1)
        self.assertEqual(sum(m=='DELETE' and path.endswith('/command') for m,path,_,_ in self.api.calls),1)
    async def test_destroy_remains_available_disabled_and_requires_node_absence(self):
        self.api.exists=True; self.guard.absent=False
        with self.assertRaisesRegex(ProviderError,'DESTROY_OUTCOME_UNKNOWN'): await self.provider(False).destroy_task(TASK,SID)
        self.assertEqual(sum(m=='DELETE' for m,_,_,_ in self.api.calls),1)
    async def test_lost_destroy_ack_not_retried(self):
        self.api.exists=True; self.api.fail_delete=True
        with self.assertRaisesRegex(ProviderError,'DESTROY_OUTCOME_UNKNOWN'): await self.provider().destroy_task(TASK,SID)
        self.assertEqual(sum(m=='DELETE' for m,_,_,_ in self.api.calls),1)
    async def test_bounded_metadata_listing_and_wrong_owner(self):
        self.api.huge_list=True
        with self.assertRaisesRegex(ProviderError,'TASK_LOOKUP_UNBOUNDED'): await self.provider().create(TASK,'office',{'maimi_task_id':TASK})
        self.api.huge_list=False; self.api.exists=True; self.api.bad_owner=True
        with self.assertRaisesRegex(ProviderError,'DESTROY_OUTCOME_UNKNOWN'): await self.provider().destroy_task(TASK,SID)
        self.assertFalse(any(m in ('POST','DELETE') for m,_,_,_ in self.api.calls))
    async def test_snapshot_resume_uncertain_never_returns_file(self):
        self.api.exists=True; self.guard.fail_snapshot=True
        with self.assertRaisesRegex(ProviderError,'OUTPUT_SNAPSHOT_UNCONFIRMED'): await self.provider().read_output(SID,'/workspace/outputs/a.txt',1024)
    async def test_external_endpoint_rejected_before_send(self):
        self.api.exists=True; self.api.bad_endpoint=True
        with self.assertRaises(ProviderError): await self.provider().write_input(SID,'/workspace/inputs/a',b'a')
        self.assertFalse(any('/files/' in path for _,path,_,_ in self.api.calls))
    async def test_transport_redirect_and_encoding_rejected(self):
        for response in (httpx.Response(302,headers={'location':'https://outside.invalid'}),httpx.Response(200,headers={'content-encoding':'gzip'})):
            transport=PrivateTransport('http://127.0.0.1:18080',httpx.MockTransport(lambda _:response))
            with self.assertRaisesRegex(ProviderError,'UNSUPPORTED_UPSTREAM_RESPONSE'):
                await transport.handle_async_request(httpx.Request('GET','http://127.0.0.1:18080/v1/sandboxes'))


if __name__=='__main__': unittest.main(verbosity=2)
