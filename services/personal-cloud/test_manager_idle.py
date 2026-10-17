"""Synthetic admission/reaping tests; no company service, token, or data."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


def manager(overlay=''):
    spec=importlib.util.spec_from_file_location('idle_manager_test',Path(__file__).with_name('manager.py'))
    module=importlib.util.module_from_spec(spec)
    with patch.dict(os.environ,{'PERSONAL_CLOUD_RESERVED_OWNERS':'','PERSONAL_CLOUD_MAX_ACTIVE_HOSTS':'2',
            'PERSONAL_CLOUD_WORKER_OVERLAY_DIR':overlay,'PERSONAL_CLOUD_MIN_AVAILABLE_MIB':'0'},clear=False):
        spec.loader.exec_module(module)
    module.auth=lambda request:None
    return module


def record(touched=0):
    return {'container_id':'synthetic-worker','touched':touched,'expires_at':10000,
        'origin':'http://synthetic-worker:3084','route_token':'a'*43,
        'native_workspace_id':'synthetic','lease':'mcl_'+'b'*43}


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.m=manager()

    def test_reservation_and_general_fifo_have_independent_room(self):
        m=self.m;m.MAX_HOSTS=5;m.RESERVED_OWNERS=frozenset({'d'*32})
        m.records={str(i)*32:record() for i in range(4)}
        self.assertEqual(m.queue_admission('a'*32,'a',100)['position'],1)
        self.assertEqual(m.queue_admission('b'*32,'b',100)['position'],2)
        self.assertIsNone(m.queue_admission('d'*32,'developer',100))
        m.records['d'*32]=record();m.records.pop('0'*32)
        self.assertEqual(m.queue_admission('b'*32,'b',105)['position'],2)
        self.assertIsNone(m.queue_admission('a'*32,'a',105))
        m.records['a'*32]=record()
        self.assertEqual(m.queue_admission('b'*32,'b',105)['position'],1)

    async def test_memory_reserves_future_growth_and_missing_developer(self):
        m=self.m;m.MIN_AVAILABLE_MIB=2048;m.RESERVED_OWNERS=frozenset({'d'*32})
        m.records={str(i)*32:record() for i in range(3)}
        m.docker=AsyncMock(return_value={'memory_stats':{'stats':{'anon':256*1024*1024}}})
        with patch.object(Path,'read_text',return_value='MemAvailable: 8388608 kB\n'):
            self.assertFalse(await m.memory_admission('a'*32))
            self.assertTrue(await m.memory_admission('d'*32))
        self.assertEqual(m.docker.await_count,6)
        self.assertIn('/stats?stream=false',m.docker.call_args.args[1])

    async def test_memory_never_credits_cache_or_incomplete_statistics(self):
        m=self.m;m.MIN_AVAILABLE_MIB=2048;m.records={'a'*32:record()}
        with patch.object(Path,'read_text',return_value='MemAvailable: 33554432 kB\n'):
            for bad in ({'memory_stats':{'usage':1024,'stats':{'cache':1024}}},
                        {'memory_stats':{'stats':{'anon':True}}},
                        {'memory_stats':{'stats':{'rss':m.HOST_MEMORY_BYTES+1}}},{}):
                m.docker=AsyncMock(return_value=bad)
                self.assertFalse(await m.memory_admission('b'*32))
            m.docker=AsyncMock(return_value={'memory_stats':{'stats':{'rss':1}}})
            self.assertTrue(await m.memory_admission('b'*32))
        with patch.object(Path,'read_text',return_value='MemFree: 33554432 kB\n'):
            self.assertFalse(await m.memory_admission('b'*32))

    def test_memory_denial_keeps_queue_place(self):
        m=self.m
        self.assertEqual(m.queue_admission('a'*32,'a',100,memory_ok=False)['position'],1)
        self.assertEqual(m.queue_admission('b'*32,'b',100)['position'],2)
        self.assertIsNone(m.queue_admission('a'*32,'a',105))

    async def test_memory_uses_lower_before_after_sample_and_rejects_unknown(self):
        m=self.m;m.MIN_AVAILABLE_MIB=2048;m.docker=AsyncMock()
        high='MemAvailable: 8388608 kB\n';low='MemAvailable: 1048576 kB\n'
        for samples in ([high,low],[low,high],[high,'MemFree: 8388608 kB\n']):
            with patch.object(Path,'read_text',side_effect=samples):
                self.assertFalse(await m.memory_admission('a'*32))
        m.docker.assert_not_awaited()

    async def test_background_heartbeat_and_get_do_not_touch_activity(self):
        m=self.m;m.records['a'*32]=r=record(10)
        with patch.object(m.time,'time',return_value=100):
            await m.host('a'*32,None)
            await m.heartbeat('a'*32,m.HeartbeatBody(lease=r['lease'],expires_at=900,activity=False),None)
            self.assertEqual(r['touched'],10)
            await m.heartbeat('a'*32,m.HeartbeatBody(lease=r['lease'],expires_at=900,activity=True),None)
            self.assertEqual(r['touched'],100)
            r['touched']=20
            await m.host('a'*32,None,touch=True)
            self.assertEqual(r['touched'],100)

    async def test_busy_work_and_completion_each_restart_idle_window(self):
        m=self.m;m.records['a'*32]=r=record(10)
        m.worker_control=AsyncMock(return_value={'protocol':1,'busy':True,'draining':False})
        with patch.object(m.time,'time',return_value=1000):
            state=await m.host_activity('a'*32,None)
            self.assertEqual(state['idle_for_seconds'],0)
            self.assertTrue(state['busy'])
        m.worker_control.return_value={'protocol':1,'busy':False,'draining':False}
        with patch.object(m.time,'time',return_value=1200):
            state=await m.host_activity('a'*32,None)
            self.assertEqual(state['idle_for_seconds'],0)
        with patch.object(m.time,'time',return_value=1250):
            state=await m.host_activity('a'*32,None)
            self.assertEqual(state['idle_for_seconds'],50)
            self.assertEqual(r['touched'],10)

    async def test_long_task_then_complete_requires_a_full_silent_ten_minutes_before_release(self):
        m=self.m;m.IDLE=600;m.records['a'*32]=record(0);m.remove=AsyncMock()
        m.worker_control=AsyncMock(return_value={'protocol':1,'busy':True,'draining':False})
        for now in (601,1800,3600):
            with patch.object(m.time,'time',return_value=now):await m.reap_once()
            m.remove.assert_not_awaited()
        m.worker_control.return_value={'protocol':1,'busy':False,'draining':False}
        for now in (3610,4209,4210):
            with patch.object(m.time,'time',return_value=now):await m.reap_once()
            m.remove.assert_not_awaited()
        m.worker_control.side_effect=[{'protocol':1,'busy':False,'draining':False},
                                     {'protocol':1,'busy':False,'draining':True}]
        with patch.object(m.time,'time',return_value=4211):await m.reap_once()
        m.remove.assert_awaited_once();self.assertFalse(m.records)

    async def test_busy_and_uncertain_worker_never_removed(self):
        m=self.m;m.records['a'*32]=record();m.remove=AsyncMock()
        m.worker_control=AsyncMock(return_value={'protocol':1,'busy':True,'draining':False})
        with patch.object(m.time,'time',return_value=1000):await m.reap_once()
        m.remove.assert_not_awaited()
        m.worker_control.side_effect=RuntimeError('synthetic uncertainty')
        m.docker=AsyncMock(return_value={'Config':{'Labels':{m.LABEL:m.INSTANCE}},'State':{'Running':True}})
        with patch.object(m.time,'time',return_value=2000):
            await m.reap_once()
            self.assertIsNone((await m.host_activity('a'*32,None))['busy'])
        m.remove.assert_not_awaited();self.assertIn('a'*32,m.records)

    async def test_idle_requires_fence_and_ends_only_after_confirmed_delete(self):
        m=self.m;m.records['a'*32]=record();m.remove=AsyncMock()
        m.worker_control=AsyncMock(side_effect=[{'protocol':1,'busy':False,'draining':False},
                                              {'protocol':1,'busy':False,'draining':True}])
        with patch.object(m.time,'time',return_value=1000):await m.reap_once()
        m.remove.assert_awaited_once();self.assertFalse(m.records)

    async def test_new_work_found_at_fence_is_retained_and_gets_idle_window(self):
        m=self.m;m.records['a'*32]=r=record();m.remove=AsyncMock()
        m.worker_control=AsyncMock(side_effect=[{'protocol':1,'busy':False,'draining':False},
                                              {'protocol':1,'busy':True,'draining':False}])
        with patch.object(m.time,'time',return_value=1000):await m.reap_once()
        m.remove.assert_not_awaited();self.assertEqual(r['work_touched'],1000)

    async def test_delete_failure_resumes_admission(self):
        m=self.m;m.records['a'*32]=r=record();m.remove=AsyncMock(side_effect=RuntimeError('synthetic delete failure'))
        m.worker_control=AsyncMock(side_effect=[{'protocol':1,'busy':False,'draining':False},
            {'protocol':1,'busy':False,'draining':True},{'protocol':1,'busy':False,'draining':False}])
        with patch.object(m.time,'time',return_value=1000):await m.reap_once()
        self.assertIn('a'*32,m.records);self.assertNotIn('release_pending',r)
        self.assertEqual(m.worker_control.call_args.args[1],'idle-resume')

    async def test_lost_fence_reply_also_resumes(self):
        m=self.m;m.records['a'*32]=r=record();m.remove=AsyncMock()
        m.worker_control=AsyncMock(side_effect=[{'protocol':1,'busy':False,'draining':False},
            TimeoutError(),{'protocol':1,'busy':False,'draining':False}])
        with patch.object(m.time,'time',return_value=1000):await m.reap_once()
        m.remove.assert_not_awaited();self.assertNotIn('release_pending',r)

    async def test_missing_worker_does_not_hold_capacity_forever(self):
        m=self.m;m.records['a'*32]=record();m.remove=AsyncMock()
        m.worker_control=AsyncMock(side_effect=RuntimeError('worker absent'))
        m.docker=AsyncMock(return_value=None)
        await m.reap_once();self.assertFalse(m.records);m.remove.assert_not_awaited()

    async def test_touch_waits_for_manager_lock(self):
        m=self.m;m.records['a'*32]=r=record(10)
        async with m.lock:
            task=asyncio.create_task(m.host('a'*32,None,touch=True))
            await asyncio.sleep(0);self.assertFalse(task.done());self.assertEqual(r['touched'],10)
        with patch.object(m.time,'time',return_value=100):await task
        self.assertEqual(r['touched'],100)

    def test_worker_overlay_only_adds_fixed_readonly_sources(self):
        m=manager('/data/megmeet-web-files-idle-20260923-r18/public');m.image_id='synthetic-image'
        with patch.object(Path,'is_file',return_value=True),patch.object(Path,'is_symlink',return_value=False):
            config=m.container_config('a'*32,SimpleNamespace(catalog={},lease='mcl_'+'b'*43),'c'*43)
        binds=[x for x in config['HostConfig']['Mounts'] if x['Type']=='bind']
        self.assertEqual({x['Target'] for x in binds},{'/opt/personal-cloud/' + name for name in ('worker.mjs', 'idle-control.mjs', 'catalog.mjs', 'vision-control-plugin.mjs', 'vision-admission.mjs', 'task-authority.mjs', 'web-control-plugin.mjs', 'web-bridge.mjs')} | {m.BRAND_OVERLAY_TARGET})
        self.assertTrue(all(x['ReadOnly'] is True for x in binds))
        self.assertEqual({x['Source'] for x in binds},{m.WORKER_OVERLAY_DIR+'/'+name for name in (*m.WORKER_OVERLAY_FILES,m.BRAND_OVERLAY_FILE)})
        self.assertEqual(config['HostConfig']['Memory'],m.HOST_MEMORY_BYTES)

    def test_worker_overlay_directory_remains_a_fixed_release_family(self):
        for invalid in ('/tmp/public','/data/megmeet-web-files-idle-20260923-r18/public/../public',
                        '/data/megmeet-arbitrary-20260923-r18/public'):
            with self.assertRaisesRegex(RuntimeError,'invalid worker overlay directory'):manager(invalid)


if __name__=='__main__':unittest.main(verbosity=2)
