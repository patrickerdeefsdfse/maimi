import json
import sys
import time
import uuid
import unittest
from pathlib import Path
sys.path.insert(0,"/proof")
from httpx import ASGITransport, AsyncClient
from magic_ingress import create_app, Handler, PATH, signature
from test_identity_pg import transaction
from identity_store import IdentityStore
from routing import Channel


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel=Channel("dingtalk","robot","corp")
        self.now=int(time.time()); self.key=b'f'*32; self.store=IdentityStore(b'i'*32)
        self.calls=[]
        async def iam(owner):
            self.calls.append(owner)
            return owner=='M92000001'
        handler=Handler(self.store,b'r'*32,transaction,iam)
        self.client=AsyncClient(transport=ASGITransport(app=create_app(channel=self.channel,
                                forward_key=self.key,handle=handler)),base_url="https://relay.invalid")
        with transaction() as db:
            db.execute("TRUNCATE maimi_im.bind_attempt,maimi_im.binding,maimi_im.receipt")
            ticket,code=self.store.begin(db,self.channel,"M92000001",self.now-2)
            proof=self.store.claim(db,self.channel,"staff",code,self.now-1)
            self.store.confirm(db,self.channel,"M92000001",ticket,proof,self.now-1)
        self.sample=dict(robotCode="robot",senderCorpId="corp",chatbotCorpId="corp",senderStaffId="staff",
                         conversationType="1",conversationId="dialog",msgId="event",msgtype="text",
                         createAt=self.now*1000,text=dict(content="状态"))

    async def asyncTearDown(self):
        await self.client.aclose()

    async def submit(self, payload, *, header_age=0, tamper=False):
        raw=json.dumps(payload,ensure_ascii=False).encode(); ts=str(self.now-header_age); nonce=str(uuid.uuid4())
        headers={'Content-Type':'application/json','X-Maimi-Time':ts,'X-Maimi-Nonce':nonce,
                 'X-Maimi-Sign':signature(self.key,ts,nonce,raw)}
        return await self.client.post(PATH,headers=headers,content=raw+(b' ' if tamper else b''))

    async def test_status_once_and_duplicates(self):
        a=await self.submit(self.sample); self.assertEqual(a.status_code,200,a.text)
        self.assertEqual(a.json()['state'],'answered'); self.assertNotIn('M92000001',a.text)
        b=await self.submit(self.sample); self.assertEqual(b.json()['state'],'duplicate')
        self.assertEqual(b.json()['reply'],'')
        with transaction() as db:
            self.assertEqual(db.execute("SELECT state FROM maimi_im.receipt").fetchall(),[('finished',)])

    async def test_auth_tenant_age_and_sender(self):
        self.assertEqual((await self.submit(self.sample,tamper=True)).status_code,403)
        self.assertEqual((await self.submit(self.sample,header_age=90)).status_code,403)
        for fields in ({'senderCorpId':'wrong'},{'robotCode':'wrong'},{'conversationType':'2'},{'createAt':100}):
            self.assertEqual((await self.submit(self.sample|fields)).status_code,403)
        self.assertEqual((await self.submit(self.sample|{'senderStaffId':'unknown'})).json()['state'],'binding_required')
        self.assertEqual(self.calls,[])
    async def test_unconfigured_model_fails_without_consuming_message(self):
        r=await self.submit(self.sample|{'text':{'content':'hello'}})
        self.assertEqual(r.status_code,503); self.assertEqual(r.json()['state'],'dispatch_not_configured')
        with transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maimi_im.receipt").fetchone(),(0,))

if __name__ == '__main__':
    with transaction() as db:
        db.execute(Path('/proof/identity-schema.sql').read_text())
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IngressTests))
    Path('/proof/magic-ingress-validation.json').write_text(json.dumps(dict(passed=result.wasSuccessful(),tests=result.testsRun,external_messages=0,db='isolated PG16', real_iam=False,server_deployed=False),indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)

