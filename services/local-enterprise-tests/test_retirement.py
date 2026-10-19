import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

HERE=Path(__file__).resolve().parent
BASE=HERE.parent/'service-broker'
sys.path.insert(0,str(BASE))
from journal import Journal
from ssh_transport import Connections
from protocol import PROTOCOL,Rejected

class FakeConnection:
    def __init__(self):self.closed=False
    def close(self):self.closed=True
    async def wait_closed(self):pass
    def is_closed(self):return self.closed

class RetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'journal.sqlite'
        self.journal=Journal(self.path,clock=time.time)
        self.registry=SimpleNamespace(allowed=lambda *a:None)
        self.ssh_calls=0;self.created=[]
        async def connect(**kw):
            self.ssh_calls+=1;kw['client_factory']().verified=True
            c=FakeConnection();self.created.append(c);return c
        self.connect=connect;self.connections=Connections(self.registry,self.journal,connection_factory=connect)
        job=str(uuid4());self.scope=('M00096389',str(uuid4()),'syntheticGeneration')
        self.body={'protocol':PROTOCOL,'employee_no':self.scope[0],'scope':{'session_id':self.scope[1],'generation':self.scope[2],'invocation_id':'pi_'+'A'*43,'job_id':job,'expires_at':int(time.time())+60},'request_id':job,'fields':{'host':'10.4.240.14','port':22,'username':'root','password':'synthetic-only'}}
    async def asyncTearDown(self):
        await self.connections.close();self.journal.close();self.temp.cleanup()
    async def test_retire_ack_blocks_delayed_post_without_ssh(self):
        ack=await self.connections.retire(*self.scope);self.assertFalse(ack['connected'])
        with self.assertRaises(Rejected):await self.connections.open(copy.deepcopy(self.body))
        self.assertEqual(self.ssh_calls,0)
    async def test_ordinary_disconnect_still_allows_reconnection(self):
        await self.connections.disconnect(*self.scope)
        self.assertTrue((await self.connections.open(copy.deepcopy(self.body)))['connected'])
    async def test_retirement_survives_broker_restart(self):
        await self.connections.retire(*self.scope);await self.connections.close();self.journal.close()
        self.journal=Journal(self.path,clock=time.time);self.connections=Connections(self.registry,self.journal,connection_factory=self.connect)
        with self.assertRaises(Rejected):await self.connections.open(copy.deepcopy(self.body))
        self.assertEqual(self.ssh_calls,0)
    async def test_late_ssh_success_after_cancel_cannot_publish(self):
        started=asyncio.Event();release=asyncio.Event()
        async def delayed(**kw):
            started.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            return await self.connect(**kw)
        self.connections.connect=delayed
        task=asyncio.create_task(self.connections.open(copy.deepcopy(self.body)))
        await started.wait();await self.connections.retire(*self.scope);release.set()
        with self.assertRaises(Rejected):await task
        self.assertTrue(self.created[0].closed);self.assertFalse((await self.connections.status(*self.scope))['connected'])
    async def test_new_generation_independent_and_separate_reader_sees_fence(self):
        other=Journal(self.path,clock=time.time)
        try:
            other.retire_connection(*self.scope)
            with self.assertRaises(Rejected):await self.connections.open(copy.deepcopy(self.body))
            body=copy.deepcopy(self.body);body['scope']['generation']='newGeneration'
            self.assertTrue((await self.connections.open(body))['connected'])
        finally:other.close()

if __name__=='__main__':unittest.main()
