"""Opt-in isolated local Docker test, no ports or company endpoints."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'gateway'))
from app import local_service as m

def docker_command():
    # Explicit JSON argv supports local Docker or an opted-in WSL distribution.
    value=json.loads(os.getenv('MAIMI_TEST_DOCKER_COMMAND','["docker"]'))
    if not isinstance(value,list) or not value or not all(isinstance(x,str) and x for x in value):
        raise ValueError('INVALID_TEST_DOCKER_COMMAND')
    return value

class DockerRedis:
    def __init__(self,name):self.name=name
    async def command(self,*args):
        def run():
            result=subprocess.run(docker_command()+['exec',self.name,'redis-cli','--json',*map(str,args)],capture_output=True,timeout=15)
            if result.returncode:raise RuntimeError('LOCAL_REDIS_COMMAND_FAILED')
            return json.loads(result.stdout.decode().strip())
        return await asyncio.to_thread(run)
    async def eval(self,script,n,*args):return await self.command('EVAL',script,n,*args)
    async def get(self,key):return await self.command('GET',key)
    async def set(self,key,value,nx=False,ex=None):
        return await self.command('SET',key,value,*(['NX'] if nx else []),*(['EX',ex] if ex else []))

@unittest.skipUnless(os.getenv('MAIMI_LOCAL_REDIS_TEST')=='1','requires explicit local Docker test')
class RealRedisTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_grants_and_lock_cas_against_redis7(self):
        name='maimi-r65-grants-'+uuid4().hex[:12]
        run=lambda *args:subprocess.run(docker_command()+list(args),capture_output=True,timeout=30,check=True)
        run('image','inspect','redis:7')  # Never implicitly pull from a registry.
        run('run','--pull','never','--detach','--name',name,'--network','none','--memory','128m','--pids-limit','32','--read-only','--tmpfs','/data:rw,noexec,nosuid,size=16m','redis:7','redis-server','--save','','--appendonly','no')
        try:
            redis=DockerRedis(name)
            for _ in range(20):
                try:
                    if await redis.command('PING')=='PONG':break
                except Exception:pass
                await asyncio.sleep(.1)
            store=m.RedisGrants(redis);identity=m.Identity('M00096389','synthetic-iam',str(uuid4()),'synthetic-token-hash',int(time.time())+600)
            sid=str(uuid4());a,ta,prior=await store.create(identity,sid);self.assertIsNone(prior)
            self.assertEqual(await store.read(ta['control_grant'],identity,'control'),a)
            with self.assertRaises(m.Denied):await store.read(ta['tool_grant'],identity,'control')
            b,tb,prior=await store.create(identity,sid);self.assertEqual(prior,a)
            with self.assertRaises(m.Denied):await store.read(ta['control_grant'],identity,'control')
            await store.revoke(a);self.assertEqual(await store.read(tb['tool_grant'],identity,'tool'),b)
            lock=await store.lock(b)
            with self.assertRaises(m.Denied):await store.lock(b)
            await store.unlock(b,'wrong-lock')
            with self.assertRaises(m.Denied):await store.lock(b)
            await store.unlock(b,lock);lock=await store.lock(b);await store.unlock(b,lock)
            changed=m.Identity(identity.employee,identity.iam_session,str(uuid4()),identity.desktop_grant_hash,identity.expires_at)
            with self.assertRaises(m.Denied):await store.read(tb['tool_grant'],changed,'tool')
            keys=await redis.command('KEYS',store.PREFIX+'*')
            for key in keys:
                value=await redis.get(key)
                self.assertNotIn(ta['control_grant'],str(value));self.assertNotIn(tb['tool_grant'],str(value))
            await store.revoke(b)
            with self.assertRaises(m.Denied):await store.read(tb['tool_grant'],identity,'tool')
        finally:run('rm','--force',name)

if __name__=='__main__':unittest.main()
