"""Real r105 auth/lease/model broker and PG, with synthetic company identity/keys/upstream."""
import hashlib
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,'/proof')
sys.path.insert(0,'/app')
import httpx
from app import iam_oauth as iam
from app import personal_model_proxy as broker
from routing import Channel
from identity_store import IdentityStore
from magic_ingress import create_app, Handler, signature, PATH
from qa_delegation import ModelDelegation, install_browser_routes, PREFIX, SCOPE
from qa_jobs import Jobs
from test_identity_pg import transaction

ORIGIN="https://qa.invalid"
class KV:
    def __init__(self): self.data={}
    async def get(self,k): return self.data.get(k)
    async def set(self,k,v,ex=None,nx=False):
        if not nx or k not in self.data: self.data[k]=v; return True
        return False

class Body(httpx.AsyncByteStream):
    def __init__(self,res): self.raw=json.dumps(res).encode()
    async def __aiter__(self):
        yield self.raw

class DelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ.update(AUTH_MODE="iam_direct")
        self.channel=Channel("dingtalk","bot","corp"); self.store=IdentityStore(b'i'*32)
        now=int(time.time())
        self.sessions={('QA-'+str(n)).ljust(43,'a'):dict(user_no=f'M{n:08d}',account_no=f'fixture-{n}',display_name='',organization_codes=['QA']) for n in (1,2)}
        self.sids=list(self.sessions)
        async def authenticate(sid): return self.sessions.get(sid)
        async def deadline(sid,owner,account):
            user=self.sessions.get(sid)
            if not user or user['user_no']!=owner or user['account_no']!=account: raise iam.IAMFailure(401,'expired')
            return time.time()+3600
        self.iam=SimpleNamespace(store=SimpleNamespace(redis=KV()),authenticate=authenticate,session_deadline=deadline)
        async def with_service(callback): return await callback(self.iam)
        self.patches=[patch.object(iam,'_with_service',with_service)]
        self.owners=[]; self.upstream=[]
        class Provider:
            async def resolve(inner,actor):
                self.owners.append(actor.user_no)
                return broker.PersonalCredential(actor.user_no,'QA-PERSONAL-KEY-'+actor.user_no,time.time()+600)
        async def model(req):
            self.upstream.append((req.headers['authorization'],json.loads(req.content)['messages']))
            return httpx.Response(200,headers={'Content-Type':'application/json'},
                 stream=Body({'model':broker.DEFAULT_MODEL,'choices':[{'message':{'content':'QA response'}}]}))
        real=broker.PersonalModelBroker(Provider(),"https://model.qa.invalid/v1",httpx.MockTransport(model))
        self.patches.append(patch.object(broker,'get_broker',lambda:real))
        for p in self.patches:p.start()
        with transaction() as db:
            db.execute("TRUNCATE maimi_im.question_job,maimi_im.model_consent,maimi_im.binding,maimi_im.bind_attempt,maimi_im.receipt")
        self.delegation=ModelDelegation(self.store,self.channel,transaction,b'g'*32,broker.DEFAULT_MODEL)
        self.jobs=Jobs(transaction,b'j'*32,self.delegation.dispatch)
        app=create_app(channel=self.channel,forward_key=b'f'*32,handle=Handler(self.store,b'r'*32,transaction,None,self.delegation.dispatch,self.delegation,self.jobs))
        install_browser_routes(app,self.delegation,ORIGIN)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN)

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):p.stop()

    async def employee(self,sid,suffix,json_body,origin=ORIGIN):
        return await self.client.post(PREFIX+suffix,json=json_body,headers={
          'Cookie':iam.SESSION_COOKIE+'='+sid,'Origin':origin,'Sec-Fetch-Site':'same-origin'})
    async def send(self,peer,text,msgid=None):
        at=int(time.time())
        raw=json.dumps(dict(robotCode='bot',senderCorpId='corp',chatbotCorpId='corp',senderStaffId=peer,
                conversationType='1',conversationId='chat-'+peer,msgId=msgid or str(uuid.uuid4()),
                msgtype='text',createAt=at*1000,text={'content':text}),ensure_ascii=False).encode()
        ts=str(at);nonce=str(uuid.uuid4())
        return await self.client.post(PATH,content=raw,headers={'Content-Type':'application/json',
          'X-Maimi-Time':ts,'X-Maimi-Nonce':nonce,'X-Maimi-Sign':signature(b'f'*32,ts,nonce,raw)})
    async def pair(self,n):
        start=await self.employee(self.sids[n-1],'/pair/start',{})
        self.assertEqual(start.status_code,200,start.text)
        start=start.json()
        claim=await self.send('peer'+str(n),start['command'].removeprefix('麦米 '))
        self.assertEqual(claim.json()['state'],'pair_claimed')
        proof=claim.json()['reply'].split('：')[1]
        wrong=await self.employee(self.sids[2-n],'/pair/confirm',dict(ticket=start['ticket'],proof=proof))
        self.assertEqual(wrong.status_code,403)
        done=await self.employee(self.sids[n-1],'/pair/confirm',dict(ticket=start['ticket'],proof=proof))
        self.assertEqual(done.json(),{'paired':True})
    async def allow(self,n):
        consent=await self.employee(self.sids[n-1],'/consent',dict(scope=SCOPE))
        self.assertEqual(consent.status_code,200,consent.text)
        self.assertLessEqual(consent.json()['expires_at'],time.time()+900)
        self.assertNotIn('mcl_',consent.text)

    async def test_two_employees_distinct_credentials_and_replay(self):
        for n in (1,2):
            await self.pair(n)
            self.assertEqual((await self.send('peer'+str(n),'hello')).json()['state'],'consent_required')
            await self.allow(n)
        for n in (1,2):
            event='event'+str(n)
            accepted=(await self.send('peer'+str(n),'hello',event)).json()
            self.assertEqual(accepted['state'],'queued')
            ticket=accepted['reply'].split('结果 ')[-1].splitlines()[0]
            self.assertEqual((await self.send('peer'+str(n),'hello',event)).json()['state'],'duplicate')
            self.assertCountEqual(await asyncio.gather(self.jobs.work_once(),self.jobs.work_once()),[True,False])
            result=(await self.send('peer'+str(n),'结果 '+ticket)).json()
            self.assertIn("QA response",result['reply'])
            foreign=(await self.send('peer'+str(3-n),'结果 '+ticket)).json()
            self.assertNotIn("QA response",foreign['reply'])
        self.assertEqual(self.owners,['M92000001','M92000002'])
        self.assertEqual([h for h,_ in self.upstream],['Bearer QA-PERSONAL-KEY-M92000001','Bearer QA-PERSONAL-KEY-M92000002'])
        self.assertTrue(all(msgs==[{'role':'user','content':'hello'}] for _,msgs in self.upstream))
    async def test_crash_does_not_redrive_and_results_expire(self):
        await self.pair(1);await self.allow(1)
        accepted=(await self.send('peer1','hello')).json()
        ticket=accepted['reply'].split('结果 ')[-1].splitlines()[0]
        claim=await asyncio.to_thread(self.jobs.claim,int(time.time()))
        self.assertEqual(str(claim[0]),ticket)
        self.assertFalse(await self.jobs.work_once())
        with transaction() as db:
            self.jobs.cleanup(db,int(time.time())+140)
        outcome=(await self.send('peer1','结果 '+ticket)).json()['reply']
        self.assertIn('结果待核对',outcome);self.assertFalse(await self.jobs.work_once())
        with transaction() as db:
            self.jobs.cleanup(db,int(time.time())+1802)
        expired=(await self.send('peer1','结果 '+ticket)).json()['reply']
        self.assertIn('已过期',expired);self.assertEqual(self.upstream,[])
    async def test_bounded_admission_queue_read_and_cancel_without_call(self):
        await self.pair(1);await self.allow(1)
        for n in range(2):self.assertEqual((await self.send('peer1','hello'+str(n))).json()['state'],'queued')
        self.assertIn('已满',(await self.send('peer1','hello3')).json()['reply'])
        tasks=(await self.send('peer1','任务')).json()['reply'];self.assertEqual(tasks.count('等待中'),2)
        self.assertEqual((await self.employee(self.sids[0],'/revoke',{})).json(),{'revoked':True})
        with transaction() as db:
            self.jobs.cleanup(db,int(time.time()))
        self.assertFalse(await self.jobs.work_once());self.assertEqual(self.upstream,[])
    async def test_logout_revoke_and_account_mismatch(self):
        await self.pair(1); await self.allow(1)
        # Even swapping a Redis record to another employee cannot authorize this chat.
        leasekeys=[k for k in self.iam.store.redis.data if ':lease:' in k]
        key=leasekeys[0];orig=self.iam.store.redis.data[key];record=json.loads(orig)
        record['sid']=self.sids[1]; record['user_no']='M92000002'; record['account_no']='fixture-2'
        self.iam.store.redis.data[key]=json.dumps(record)
        self.assertEqual((await self.send('peer1','hello')).json()['state'],'consent_required')
        self.iam.store.redis.data[key]=orig
        self.sessions.pop(self.sids[0])
        self.assertEqual((await self.send('peer1','hello')).json()['state'],'consent_required')
        self.assertEqual(self.upstream,[])
        await self.pair(2);await self.allow(2)
        self.assertEqual((await self.employee(self.sids[1],'/revoke',{})).json(),{'revoked':True})
        self.assertEqual((await self.send('peer2','hello')).json()['state'],'binding_required')
        self.assertEqual(self.upstream,[])
    async def test_bad_origin_scope_and_expiry(self):
        for file in ("","pair.css","pair.js"):
            r=await self.client.get('/maimi/im/dingtalk/'+file)
            self.assertEqual(r.status_code,200)
            self.assertIn("frame-ancestors 'none'",r.headers["content-security-policy"])
        await self.pair(1)
        self.assertEqual((await self.employee(self.sids[0],'/consent',{'scope':'all'})).status_code,403)
        self.assertEqual((await self.employee(self.sids[0],'/consent',{'scope':SCOPE},origin='https://other.invalid')).status_code,403)
        await self.allow(1)
        with transaction() as db:
            raw=db.execute("SELECT lease_ciphertext FROM maimi_im.model_consent").fetchone()[0]
            self.assertNotIn(b'mcl_',bytes(raw))
            db.execute("UPDATE maimi_im.model_consent SET expires_at=%s",(int(time.time())-1,))
        self.assertEqual((await self.send('peer1','hello')).json()['state'],'consent_required')
        self.assertEqual(self.upstream,[])

if __name__=='__main__':
    with transaction() as db:
        for f in ['identity-schema.sql','delegation-schema.sql','jobs-schema.sql']:db.execute(Path('/proof',f).read_text())
    r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DelegationTests))
    Path('/proof/delegation-validation.json').write_text(json.dumps(dict(success=r.wasSuccessful(),tests=r.testsRun,
        qa_database='isolated PostgreSQL 16',real_methods=['require_trusted_actor','issue_lease','lease_actor','validate_trusted_actor','cloud_chat_complete','PersonalModelBroker.complete'],
        synthetic_boundaries=['IAM session fixture','Redis fixture','personal credential provider','model HTTP upstream'],production_data_accessed=False),indent=2))
    raise SystemExit(0 if r.wasSuccessful() else 1)
