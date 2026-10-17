"""Fixed owner volume deletion. Synthetic Docker only; no real volumes."""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from test_manager_idle import manager, record


class RetentionManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m=manager();self.owner='a'*32;self.calls=[]
        self.volumes={self.m.owner_key(self.owner)+'-'+suffix:{'Name':self.m.owner_key(self.owner)+'-'+suffix,
            'Labels':{self.m.LABEL:self.m.INSTANCE,'com.megmeet.personal-cloud.owner':self.owner}}
            for suffix in ('state','files')}
        self.references=[]
        async def docker(method,path,body=None,**kwargs):
            self.calls.append((method,path))
            if path.startswith('/volumes/'):
                name=path.removeprefix('/volumes/')
                if method=='GET':return self.volumes.get(name)
                if method=='DELETE':self.volumes.pop(name,None);return
            if path.startswith('/containers/json?all=true&filters='):return self.references
            raise AssertionError((method,path))
        self.m.docker=docker

    async def wipe(self):
        return await self.m.storage(self.owner,self.m.StorageBody(action='wipe-workspace'),None)

    async def test_two_exact_owned_volumes_preflight_before_mutation_and_idempotence(self):
        result=await self.wipe();self.assertEqual(result['result'],{'status':'deleted','scope':'workspace','stateDeleted':True,'filesDeleted':True})
        self.assertEqual([x[0] for x in self.calls],['GET','GET','GET','GET','DELETE','DELETE'])
        self.assertTrue(all('force=' not in p for _,p in self.calls));self.assertEqual(self.volumes,{})
        self.calls.clear();await self.wipe();self.assertEqual([x[0] for x in self.calls],['GET','GET'])

    async def test_any_running_or_stopped_reference_and_live_record_refuse_without_deletion(self):
        for status in ('running','exited'):
            self.references=[{'State':status}]
            with self.assertRaises(self.m.HTTPException) as e:await self.wipe()
            self.assertEqual(e.exception.status_code,409)
            self.assertFalse(any(m=='DELETE' for m,_ in self.calls))
        self.calls.clear();self.references=[];self.m.records[self.owner]=record()
        with self.assertRaises(self.m.HTTPException) as e:await self.wipe()
        self.assertEqual(e.exception.status_code,409);self.assertEqual(self.calls,[])

    async def test_foreign_second_volume_aborts_before_first_delete(self):
        self.volumes[self.m.owner_key(self.owner)+'-files']['Labels']['com.megmeet.personal-cloud.owner']='b'*32
        with self.assertRaises(self.m.HTTPException) as e:await self.wipe()
        self.assertEqual(e.exception.status_code,503);self.assertFalse(any(m=='DELETE' for m,_ in self.calls))
        self.assertEqual(len(self.volumes),2)

    async def test_partial_delete_failure_has_no_success_then_retry_finishes(self):
        actual=self.m.docker
        async def failing(method,path,body=None,**kwargs):
            if method=='DELETE' and path.endswith('-files'):raise self.m.HTTPException(503,'synthetic')
            return await actual(method,path,body,**kwargs)
        self.m.docker=failing
        with self.assertRaises(self.m.HTTPException) as e:await self.wipe()
        self.assertEqual(e.exception.status_code,503);self.assertEqual(len(self.volumes),1)
        self.m.docker=actual;await self.wipe();self.assertEqual(self.volumes,{})

    async def test_operation_uses_same_admission_lock_and_no_caller_path(self):
        async with self.m.lock:
            task=asyncio.create_task(self.wipe());await asyncio.sleep(0)
            self.assertFalse(task.done());self.assertEqual(self.calls,[])
        await task
        with self.assertRaises(Exception):self.m.StorageBody(action='wipe-workspace',path='/data')
        with self.assertRaises(self.m.HTTPException):
            await self.m.storage(self.owner,self.m.StorageBody(action='wipe-workspace',sessionId='x'),None)

    async def test_absent_session_state_is_idempotent_and_arguments_checked_first(self):
        self.volumes.clear()
        result=await self.m.storage(self.owner,self.m.StorageBody(action='delete-session',sessionId='session-123'),None)
        self.assertEqual(result['result']['alreadyAbsent'],True)
        for session in (None,'../files','a/b'):
            with self.assertRaises(self.m.HTTPException):
                await self.m.storage(self.owner,self.m.StorageBody(action='delete-session',sessionId=session),None)

    async def test_new_release_helper_binds_only_two_maintenance_sources_and_owned_state(self):
        m=manager('/data/megmeet-retention-network-20260923-r19/public');m.image_id='synthetic-image';configs=[]
        async def docker(method,path,body=None,**kwargs):
            if path.startswith('/volumes/'):return next(iter(self.volumes.values()))
            if path.startswith('/containers/json?all=true'):return []
            if path.startswith('/containers/create?'):configs.append(body);return {'Id':'synthetic-maintenance'}
            if path.endswith('/start'):return
            raise AssertionError((method,path))
        m.docker=docker;m.remove=AsyncMock()
        class Response:
            status_code=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return
            async def aiter_bytes(self):yield b'{"result":{"sessionIds":[],"complete":true}}'
        class Client(Response):
            async def get(self,*args,**kwargs):return Response()
            def stream(self,*args,**kwargs):return Response()
        with patch.object(Path,'is_file',return_value=True),patch.object(Path,'is_symlink',return_value=False),patch.object(m.httpx,'AsyncClient',return_value=Client()):
            self.assertEqual((await m.storage(self.owner,m.StorageBody(action='archived-sessions'),None))['result']['complete'],True)
        mounts=configs[0]['HostConfig']['Mounts'];binds=[x for x in mounts if x['Type']=='bind']
        self.assertEqual({x['Target'] for x in binds},{'/opt/personal-cloud/history-native.mjs','/opt/personal-cloud/history-server.mjs'})
        self.assertTrue(all(x['ReadOnly'] for x in binds))
        self.assertEqual([x['Target'] for x in mounts if x['Type']=='volume'],['/state'])
        m.remove.assert_awaited_once()


if __name__=='__main__':unittest.main(verbosity=2)
