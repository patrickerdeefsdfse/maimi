import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4
import httpx
from fastapi import FastAPI
from starlette.requests import Request

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'gateway'))
from app import local_service as m

SID='11111111-1111-4111-8111-111111111111'
DEVICE='22222222-2222-4222-8222-222222222222'
HOST='33333333-3333-4333-8333-333333333333'
class Stream(httpx.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):yield json.dumps(self.value).encode()
def reply(status,value):return httpx.Response(status,stream=Stream(value),headers={'Content-Type':'application/json'})

class MemoryGrants:
    """Test double for storage only; production uses RedisGrants atomic scripts."""
    def __init__(self):self.rows={};self.slots={};self.locks=set()
    async def create(self,identity,native_session):
        m.require(m.wire.SID.fullmatch(native_session),'NATIVE_SESSION_INVALID',400)
        c,t=m.opaque(),m.opaque();slot=m.digest(identity.binding()+native_session)
        old=self.rows.get(self.slots.get(slot))
        row={'binding':identity.binding(),'employee':identity.employee,'native_session':native_session,'broker_session':str(uuid4()),
             'generation':m.opaque(),'expires_at':min(int(time.time())+600,identity.expires_at),'control_hash':m.digest(c),'tool_hash':m.digest(t),'slot':slot}
        self.rows[m.digest(c)]=self.rows[m.digest(t)]=row;self.slots[slot]=m.digest(c)
        return row,{'control_grant':c,'tool_grant':t,'expires_at':row['expires_at']},old
    async def read(self,token,identity,kind):
        row=self.rows.get(m.digest(token));m.require(row and row[kind+'_hash']==m.digest(token) and row['binding']==identity.binding() and row['expires_at']>time.time() and self.slots.get(row['slot'])==row['control_hash'],'SERVICE_GRANT_EXPIRED')
        return row
    async def revoke(self,row):
        self.rows.pop(row['control_hash'],None);self.rows.pop(row['tool_hash'],None)
        if self.slots.get(row['slot'])==row['control_hash']:self.slots.pop(row['slot'])
    async def lock(self,row):
        m.require(row['control_hash'] not in self.locks,'SERVICE_BUSY',409);self.locks.add(row['control_hash']);return 'lock'
    async def unlock(self,row,token):self.locks.discard(row['control_hash'])

class Fixture:
    def __init__(self):
        self.identity=m.Identity('M00096389','synthetic-iam',DEVICE,'synthetic-desktop-grant',int(time.time())+3600)
        self.store=MemoryGrants();self.connections={};self.jobs={};self.calls=[];self.revoked=False;self.running=False
        async def authenticate(service,request):
            m.require(not self.revoked,'DESKTOP_SESSION_EXPIRED',401)
            m.require(self.identity.employee in m.wire.USERS,'SERVICE_NOT_AUTHORIZED')
            return self.identity
        async def with_service(run):return await run(SimpleNamespace(store=SimpleNamespace(redis=None)))
        self.transport=httpx.MockTransport(self.broker)
        self.port=lambda:m.BrokerPort(configuration=lambda:m.wire.GatewayConfig('http://service-broker:8090','synthetic',tuple(m.wire.USERS)),read_secret=lambda *a:b'B'*43,transport=self.transport,sleep=lambda n:asyncio.sleep(.005))
        self.app=FastAPI();self.app.include_router(m.make_router(with_service=with_service,authenticate=authenticate,store_factory=lambda _:self.store,port_factory=self.port))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic')
    async def broker(self,req):
        body=json.loads(req.content) if req.content else None;self.calls.append((req.method,req.url.path,body))
        if req.url.path in ('/v1/connections','/v1/connection-grants'):
            if req.method=='POST':
                scope=body['scope'];key=(body['employee_no'],scope['session_id'],scope['generation'])
                assert body['fields']['username']=='root' and body['fields']['port']==22
                assert set(body['fields'])=={'host','port','username','password'}
                self.connections[key]=True
            else:
                q=req.url.params;key=(q['employee_no'],q['session_id'],q['generation'])
                if req.method=='DELETE':self.connections.pop(key,None)
            yes=key in self.connections
            return reply(200,{'protocol':m.wire.PROTOCOL,'connected':yes,'server':{'host_id':HOST,'label':'synthetic target','actions':list(m.wire.REMOTE_ACTIONS),'high_privilege':True} if yes else None})
        if req.url.path=='/v1/operations':
            scope=body['scope'];assert (body['employee_no'],scope['session_id'],scope['generation']) in self.connections
            op=str(uuid4());self.jobs[op]=body
            return reply(202,{'protocol':m.wire.PROTOCOL,'operation_id':op,'scope':scope,'expires_at':scope['expires_at'],'status':'queued'})
        op=req.url.path.rsplit('/',1)[-1];job=self.jobs[op];scope=job['scope'];status='cancelled' if req.method=='DELETE' else 'running' if self.running else 'completed'
        result={'host_id':HOST,'category':'environment','checks':[{'name':'version','status':'ok','output':'synthetic Linux','exit_code':0}],'truncated':False}
        return reply(200,{'protocol':m.wire.PROTOCOL,'operation_id':op,'scope':scope,'expires_at':scope['expires_at'],'status':status,'result':result if status=='completed' else None,'error_code':None})
    async def call(self,action,body,cap=None):
        return await self.client.post(m.PREFIX+'/'+action,json=body,headers={'x-maimi-service-grant':cap} if cap else {})
    async def grant(self):
        r=await self.call('grants',{'native_session_id':SID});assert r.status_code==200,r.text;return r.json()
    async def close(self):await self.client.aclose()

class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.f=Fixture()
    async def asyncTearDown(self):await self.f.close()
    async def test_connect_and_real_wire_inspection_no_cloud_host(self):
        g=await self.f.grant();response=await self.f.call('connect',{'ip':'10.4.240.14','password':'synthetic-password'},g['control_grant'])
        self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['connected'])
        r=await self.f.call('invoke',{'action':'inspect_server','arguments':{'host_id':HOST}},g['tool_grant'])
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['checks'][0]['output'],'synthetic Linux')
        self.assertTrue(all('personal-cloud' not in path for _,path,_ in self.f.calls))
        row=next(iter(self.f.store.rows.values()));self.assertNotEqual(row['broker_session'],SID);self.assertNotIn('password',json.dumps(row))
    async def test_model_tool_grant_cannot_connect_and_control_grant_cannot_invoke(self):
        g=await self.f.grant()
        self.assertEqual((await self.f.call('connect',{'ip':'10.4.240.14','password':'private-fixture-value'},g['tool_grant'])).status_code,403)
        self.assertEqual((await self.f.call('invoke',{'action':'status','arguments':{}},g['control_grant'])).status_code,403)
        self.assertEqual(self.f.calls,[])
    async def test_forbidden_employee_and_caller_supplied_identity(self):
        self.f.identity=m.Identity('M00003308','synthetic',DEVICE,'token',int(time.time())+60)
        self.assertEqual((await self.f.call('grants',{'native_session_id':SID})).status_code,403)
        self.f.identity=m.Identity('M00096389','synthetic',DEVICE,'token',int(time.time())+60)
        self.assertEqual((await self.f.call('grants',{'native_session_id':SID,'employee':'M00003307'})).status_code,400)
    async def test_device_and_iam_change_reject_existing_grant(self):
        g=await self.f.grant();old=self.f.identity
        for identity in [m.Identity(old.employee,old.iam_session,str(uuid4()),old.desktop_grant_hash,old.expires_at),m.Identity(old.employee,'another-iam',old.instance,old.desktop_grant_hash,old.expires_at)]:
            self.f.identity=identity
            self.assertEqual((await self.f.call('invoke',{'action':'status','arguments':{}},g['tool_grant'])).status_code,403)
    async def test_replacement_fences_and_disconnects_prior_generation(self):
        a=await self.f.grant();await self.f.call('connect',{'ip':'10.4.240.14','password':'private-fixture-value'},a['control_grant'])
        b=await self.f.grant();self.assertEqual(len(self.f.connections),0)
        self.assertEqual((await self.f.call('invoke',{'action':'status','arguments':{}},a['tool_grant'])).status_code,403)
        self.assertFalse((await self.f.call('invoke',{'action':'status','arguments':{}},b['tool_grant'])).json()['connected'])
    async def test_unknown_model_action_and_password_fields_rejected(self):
        g=await self.f.grant()
        self.assertEqual((await self.f.call('invoke',{'action':'secure_connect','arguments':{}},g['tool_grant'])).status_code,400)
        self.assertEqual((await self.f.call('invoke',{'action':'status','arguments':{'password':'private-fixture-value'}},g['tool_grant'])).status_code,400)
        self.assertEqual((await self.f.call('connect',{'ip':'127.0.0.1','password':'private-fixture-value'},g['control_grant'])).status_code,400)
    async def test_revoke_disconnect_ack_and_expired_grant(self):
        g=await self.f.grant();await self.f.call('connect',{'ip':'10.4.240.14','password':'private-fixture-value'},g['control_grant'])
        r=await self.f.call('revoke',{},g['control_grant']);self.assertTrue(r.json()['disconnect_confirmed']);self.assertEqual(len(self.f.connections),0)
        self.assertEqual((await self.f.call('invoke',{'action':'status','arguments':{}},g['tool_grant'])).status_code,403)
    async def test_iam_revocation_cancels_running_broker_job(self):
        g=await self.f.grant();await self.f.call('connect',{'ip':'10.4.240.14','password':'private-fixture-value'},g['control_grant']);self.f.running=True
        task=asyncio.create_task(self.f.call('invoke',{'action':'inspect_server','arguments':{'host_id':HOST}},g['tool_grant']))
        async with asyncio.timeout(3):
            while not self.f.jobs:await asyncio.sleep(.01)
        self.f.revoked=True;r=await task
        self.assertNotEqual(r.status_code,200)
        self.assertTrue(any(method=='DELETE' and path.startswith('/v1/operations/') for method,path,_ in self.f.calls))

class DesktopIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_desktop_identity_reader_uses_server_grant_device_and_iam(self):
        from app import desktop_auth
        class Grants:
            async def read_grant(self,token):
                assert token=='mdt_'+'A'*43
                return {'sid':'real-store-fixture-sid','instance':DEVICE,'expires_at':int(time.time())+60}
        async def authenticate(sid):
            self.assertEqual(sid,'real-store-fixture-sid');return {'user_no':'M00096389'}
        service=SimpleNamespace(desktop_store=Grants(),authenticate=authenticate)
        request=Request({'type':'http','method':'POST','path':m.PREFIX+'/grants','headers':[(b'authorization',('Bearer mdt_'+'A'*43).encode())]})
        identity=await m.identity_for(service,request)
        self.assertEqual(identity.employee,'M00096389');self.assertEqual(identity.instance,DEVICE)
        self.assertEqual(identity.iam_session,'real-store-fixture-sid');self.assertNotIn('mdt_',repr(identity))
    async def test_origin_cookie_only_duplicate_header_and_unknown_employee_denied(self):
        class Grants:
            async def read_grant(self,token):return {'sid':'synthetic','instance':DEVICE,'expires_at':int(time.time())+60}
        async def authenticate(sid):return {'user_no':'M00003308'}
        service=SimpleNamespace(desktop_store=Grants(),authenticate=authenticate)
        auth=(b'authorization',('Bearer mdt_'+'A'*43).encode())
        for headers in [[],[auth,auth],[auth,(b'origin',b'https://platform-ai.megmeet.com')],[auth]]:
            request=Request({'type':'http','method':'POST','path':m.PREFIX+'/grants','headers':headers})
            with self.assertRaises(m.Denied):await m.identity_for(service,request)

if __name__=='__main__':unittest.main()
