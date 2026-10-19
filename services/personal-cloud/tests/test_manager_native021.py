"""No Docker API or employee data: actual manager configuration/activation paths."""
from pathlib import Path
import asyncio
import importlib.util
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock

SOURCE=Path(__file__).with_name('manager.py')
if not SOURCE.is_file():SOURCE=Path(__file__).resolve().parents[1]/'manager.py'
OWNER='a'*32
IMAGE='sha256:'+'b'*64
RELEASE='c'*64


def load(**overrides):
    env={key:value for key,value in os.environ.items() if not key.startswith('PERSONAL_CLOUD_')}
    env.update(PERSONAL_CLOUD_WORKER_RUNTIME='0.2.1-rc.2',PERSONAL_CLOUD_WORKER_IMAGE=IMAGE,
        PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE='/opt/personal-cloud/runtime-r65/activation.json',
        PERSONAL_CLOUD_RUNTIME_RELEASE_SHA=RELEASE,
        PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR='/opt/personal-cloud/runtime-r65/owners')
    env.update(overrides)
    spec=importlib.util.spec_from_file_location('manager_fixture',SOURCE)
    module=importlib.util.module_from_spec(spec)
    with patch.dict(os.environ,env,clear=True): spec.loader.exec_module(module)
    module.image_id=IMAGE
    return module


class Native021Tests(unittest.TestCase):
    def test_isolated_latest_configuration_rejects_every_legacy_overlay(self):
        for key,value in {
            'PERSONAL_CLOUD_FAST_ENTRY_OVERLAY_DIR':'/data/megmeet-service-entry-r62-20261016/native',
            'PERSONAL_CLOUD_WORKER_OVERLAY_DIR':'/data/megmeet-dsh-cli-20260928-r27/public',
            'PERSONAL_CLOUD_HISTORY_OVERLAY_DIR':'/data/megmeet-dsh-history-20260928-r29/public',
            'PERSONAL_CLOUD_WORKFLOW_OVERLAY_DIR':'/data/megmeet-workflow-20260928-r30/public',
            'PERSONAL_CLOUD_RUNTIME_FOLLOWUP_OVERLAY_DIR':'/data/megmeet-runtime-followup-20260929-r35/public',
        }.items():
            with self.subTest(key=key),self.assertRaises(RuntimeError):load(**{key:value})

    def test_new_runtime_rejects_mutable_image_wrong_runtime_old_activation_and_missing_receipts(self):
        for changes in [dict(PERSONAL_CLOUD_WORKER_IMAGE='worker:latest'),
            dict(PERSONAL_CLOUD_WORKER_RUNTIME='0.2.1-rc.1'),
            dict(PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE='/opt/personal-cloud/runtime-r41/activation.json'),
            dict(PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR='')]:
            with self.subTest(changes=changes),self.assertRaises(RuntimeError):load(**changes)

    def test_new_activation_requires_exact_root_owned_receipt(self):
        m=load()
        with tempfile.TemporaryDirectory() as temp:
            f=Path(temp)/'activation.json';m.RUNTIME_ACTIVATION_FILE=str(f)
            payload={'schema':'megmeet-runtime-activation-r65-v1','runtime':m.NATIVE021_RUNTIME,
                'release_manifest_sha256':RELEASE,'image_id':IMAGE,'status':'ACTIVE',
                'history_admission':'OWNER_SNAPSHOT_RECEIPTS_REQUIRED','legacy_overlays':False}
            self.assertFalse(m.runtime_activated())
            f.write_text(json.dumps(payload));f.chmod(0o600)
            self.assertTrue(m.runtime_activated())
            for key,value in [('image_id','sha256:'+'f'*64),('runtime',m.REVIEWED_RUNTIME),
                              ('legacy_overlays',True),('history_admission','NONE')]:
                f.write_text(json.dumps({**payload,key:value}));self.assertFalse(m.runtime_activated())
            f.write_text(json.dumps(payload));f.chmod(0o666)
            self.assertFalse(m.runtime_activated())

    def test_old_r41_activation_and_configuration_remain_separate(self):
        m=load(PERSONAL_CLOUD_WORKER_RUNTIME='0.2.0-rc.2',PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR='',
            PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE='/opt/personal-cloud/runtime-r41/activation.json')
        with tempfile.TemporaryDirectory() as temp:
            f=Path(temp)/'activation.json';m.RUNTIME_ACTIVATION_FILE=str(f)
            f.write_text(json.dumps({'schema':'megmeet-runtime-activation-r41-v1','runtime':m.REVIEWED_RUNTIME,
                'release_manifest_sha256':RELEASE,'image_id':IMAGE,'status':'ACTIVE'}));f.chmod(0o600)
            self.assertTrue(m.runtime_activated())
            config=m.container_config(OWNER,SimpleNamespace(catalog={},lease='mcl_'+'L'*43),'R'*43)
            self.assertEqual(config['Labels']['com.megmeet.personal-cloud.runtime'],'0.2.0-rc.2')

    def test_owner_snapshot_binds_release_employee_both_volume_identities(self):
        m=load();name=m.owner_key(OWNER)
        state=(IMAGE,name+'-state','2026-10-01T00:00:00Z');files=(name+'-files','2026-10-01T00:00:01Z')
        data={'schema':'megmeet-runtime-owner-snapshot-r65-v1','status':'SNAPSHOT_VERIFIED','owner':OWNER,
            'manager_namespace':m.INSTANCE,'source_runtime':m.REVIEWED_RUNTIME,'target_runtime':m.NATIVE021_RUNTIME,
            'release_manifest_sha256':RELEASE,'image_id':IMAGE,
            'state_volume':{'name':state[1],'created_at':state[2]},'files_volume':{'name':files[0],'created_at':files[1]},
            'backup':{'state_sha256':'d'*64,'files_sha256':'e'*64,'verification_receipt_sha256':'f'*64}}
        with tempfile.TemporaryDirectory() as temp:
            m.NATIVE021_RECEIPTS_DIR=temp;f=Path(temp)/(OWNER+'.json')
            with self.assertRaises(m.HTTPException):m.require_native021_history_receipt(OWNER,state,files)
            f.write_text(json.dumps(data));f.chmod(0o600)
            m.require_native021_history_receipt(OWNER,state,files)
            for values in [(OWNER,(IMAGE,state[1],'RECREATED'),files),
                (OWNER,state,(files[0],'RECREATED')),('0'*32,state,files)]:
                with self.subTest(values=values),self.assertRaises(m.HTTPException):m.require_native021_history_receipt(*values)
            for key,value in [('status','SCHEMA_V4_ONLY'),('release_manifest_sha256','0'*64),('backup',{}),
                              ('source_runtime',m.NATIVE021_RUNTIME)]:
                f.write_text(json.dumps({**data,key:value}))
                with self.subTest(key=key),self.assertRaises(m.HTTPException):m.require_native021_history_receipt(OWNER,state,files)
            f.write_text(json.dumps(data));f.chmod(0o666)
            with self.assertRaises(m.HTTPException):m.require_native021_history_receipt(OWNER,state,files)

    def test_container_configuration_keeps_limits_and_named_volumes_without_source_overlays(self):
        m=load();m.runtime_activated=lambda:True
        body=SimpleNamespace(catalog={},lease='mcl_'+'L'*43)
        with self.assertRaises(m.HTTPException):m.container_config(OWNER,body,'R'*43)
        m.migration_ready[OWNER]=(IMAGE,m.owner_key(OWNER)+'-state','synthetic-created-at')
        config=m.container_config(OWNER,body,'R'*43);host=config['HostConfig']
        self.assertEqual(host['NanoCpus'],1_000_000_000);self.assertEqual(host['Memory'],1536*1024*1024)
        self.assertEqual(host['MemorySwap'],host['Memory']);self.assertEqual(host['PidsLimit'],512)
        self.assertTrue(host['Init']);self.assertTrue(host['ReadonlyRootfs']);self.assertEqual(config['User'],'1000:1000')
        self.assertEqual(host['Mounts'],[{'Type':'volume','Source':m.owner_key(OWNER)+'-state','Target':'/state'},
                                       {'Type':'volume','Source':m.owner_key(OWNER)+'-files','Target':'/workspace'}])
        m.FAST_ENTRY_OVERLAY_DIR=m.FAST_ENTRY_OVERLAY_PATH
        with self.assertRaises(m.HTTPException):m.container_config(OWNER,body,'R'*43)

    def test_old_history_helper_is_never_run_for_new_runtime(self):
        m=load();m.docker=AsyncMock(side_effect=AssertionError('Docker must not be called'))
        for action in ['migration-admit','migration-apply','migration-rollback','inventory','delete-session']:
            with self.subTest(action=action),self.assertRaises(m.HTTPException):
                asyncio.run(m.run_history_helper(OWNER,{'action':action}))
        m.docker.assert_not_called()

    def test_prepare_open_without_snapshot_never_creates_worker_or_runs_old_migration(self):
        m=load();m.runtime_activated=lambda:True;m.auth=lambda request:None
        m.memory_admission=AsyncMock(return_value=True)
        m.run_history_helper=AsyncMock(side_effect=AssertionError('legacy history path forbidden'))
        name=m.owner_key(OWNER);calls=[]
        async def docker(method,path,body=None,**kwargs):
            calls.append((method,path))
            if method=='POST' and path=='/volumes/create':return {}
            if method=='GET' and path.startswith('/volumes/'):
                return {'Name':path.split('/')[-1],'CreatedAt':'2026-10-01T00:00:00Z',
                    'Labels':{m.LABEL:m.INSTANCE,'com.megmeet.personal-cloud.owner':OWNER}}
            raise AssertionError('unexpected Docker mutation or request')
        m.docker=docker
        with tempfile.TemporaryDirectory() as temp:
            m.NATIVE021_RECEIPTS_DIR=temp
            body=m.OpenBody(lease='mcl_'+'L'*43,catalog={},expires_at=int(time.time())+500)
            with self.assertRaises(m.HTTPException) as result:
                asyncio.run(m.prepare_open(OWNER,body,object(),'test'))
            self.assertEqual(result.exception.detail,'personal_cloud_runtime_snapshot_required')
        self.assertEqual(m.migrations,{})
        self.assertEqual(m.starting,{})
        self.assertEqual(m.migration_ready,{})
        self.assertFalse(any('/containers/' in path for method,path in calls))
        m.run_history_helper.assert_not_called()


if __name__=='__main__':unittest.main()
