import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock
from urllib.parse import unquote

import runtime_admission as admission
from runtime_admission import AdmissionRefused

OWNER='a'*32
IMAGE='sha256:'+'b'*64
RELEASE='c'*64
NS='maimi-synthetic'


def load_manager(**overrides):
    env={k:v for k,v in os.environ.items() if not k.startswith('PERSONAL_CLOUD_')}
    env.update(PERSONAL_CLOUD_WORKER_RUNTIME='0.2.1-rc.2',PERSONAL_CLOUD_WORKER_IMAGE=IMAGE,
        PERSONAL_CLOUD_RUNTIME_RELEASE_SHA=RELEASE,PERSONAL_CLOUD_MANAGER_NAMESPACE=NS,
        PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE='/opt/personal-cloud/runtime-r65/activation.json',
        PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR='/opt/personal-cloud/runtime-r65/owners',
        PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR='/opt/personal-cloud/runtime-r65/new-owners')
    env.update(overrides)
    spec=importlib.util.spec_from_file_location('manager_candidate',Path(__file__).with_name('manager.py'))
    m=importlib.util.module_from_spec(spec)
    with patch.dict(os.environ,env,clear=True):spec.loader.exec_module(m)
    m.image_id=IMAGE
    return m


class FakeDocker:
    def __init__(self):self.volumes={};self.calls=[];self.refs=[];self.helper_exit=0;self.mismatch=False
    async def __call__(self,method,path,body=None,**kw):
        self.calls.append((method,path,body))
        if path.startswith('/volumes/') and method=='GET':return self.volumes.get(path.split('/')[-1])
        if path=='/volumes/create':
            v={'Name':body['Name'],'Labels':dict(body['Labels']),'CreatedAt':'2026-10-19T00:00:00Z'}
            if self.mismatch:v['Labels'][admission.ADMISSION_LABEL]='other'
            self.volumes.setdefault(body['Name'],v);return v
        if method=='GET' and path.startswith('/containers/json'):return self.refs
        if path.startswith('/containers/create'):return {'Id':'e'*64}
        if '/wait?' in path:return {'StatusCode':self.helper_exit}
        if method in ('POST','DELETE') and path.startswith('/containers/'):return {}
        raise AssertionError((method,path))


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.new=self.root/'new';self.old=self.root/'old';self.new.mkdir(mode=0o700);self.old.mkdir(mode=0o700)
        self.docker=FakeDocker()
    def tearDown(self):self.temp.cleanup()
    def call(self):return asyncio.run(admission.admit_if_new(docker=self.docker,owner=OWNER,namespace=NS,
        image_id=IMAGE,release_sha=RELEASE,receipts_directory=self.new,snapshot_directory=self.old))
    def existing(self,kind):
        name=NS+'-'+OWNER+'-'+kind
        self.docker.volumes[name]={'Name':name,'CreatedAt':'existing','Labels':{admission.LABEL:NS,admission.OWNER_LABEL:OWNER}}
    def receipt_args(self):
        return dict(owner=OWNER,namespace=NS,image_id=IMAGE,release_sha=RELEASE,
            state={'name':NS+'-'+OWNER+'-state','created_at':'2026-10-19T00:00:00Z'},
            files={'name':NS+'-'+OWNER+'-files','created_at':'2026-10-19T00:00:00Z'})
    def test_new_owner_uses_two_empty_proofs_and_trusted_receipt(self):
        self.assertTrue(self.call());self.assertFalse((self.new/(OWNER+'.pending.json')).exists())
        self.assertTrue(admission.new_receipt_matches(self.new/(OWNER+'.json'),**self.receipt_args()))
        helpers=[b for m,p,b in self.docker.calls if '/containers/create' in p]
        self.assertEqual(len(helpers),2)
        self.assertTrue(all(b['HostConfig']['NetworkMode']=='none' and b['HostConfig']['ReadonlyRootfs'] for b in helpers))
        self.assertTrue(all(v['ReadOnly'] for v in helpers[1]['HostConfig']['Mounts']))
    def test_existing_volumes_are_not_inspected_or_reclassified_as_new(self):
        self.existing('state');self.existing('files');self.assertFalse(self.call())
        self.assertTrue(all(method=='GET' for method,_,_ in self.docker.calls))
        self.assertEqual(list(self.new.iterdir()),[])
    def test_partial_existing_volume_refused_without_creation(self):
        self.existing('state')
        with self.assertRaisesRegex(AdmissionRefused,'PARTIAL_EXISTING'):self.call()
        self.assertTrue(all(m=='GET' for m,_,_ in self.docker.calls))
    def test_preexisting_snapshot_or_pending_never_recreates_missing_data(self):
        for p in (self.old/(OWNER+'.json'),self.new/(OWNER+'.pending.json'),self.new/(OWNER+'.json')):
            p.write_text('{}')
            with self.assertRaisesRegex(AdmissionRefused,'MISSING_ADMITTED'):self.call()
            p.unlink()
    def test_failed_empty_proof_keeps_intent_without_admission(self):
        self.docker.helper_exit=21
        with self.assertRaisesRegex(AdmissionRefused,'EMPTY_VOLUME'):self.call()
        self.assertTrue((self.new/(OWNER+'.pending.json')).exists())
        self.assertFalse((self.new/(OWNER+'.json')).exists())
        self.assertTrue(any(m=='DELETE' for m,_,_ in self.docker.calls))
    def test_racing_volume_provenance_refused(self):
        self.docker.mismatch=True
        with self.assertRaisesRegex(AdmissionRefused,'CREATION_RACE'):self.call()
    def test_any_volume_reference_refused(self):
        self.docker.refs=[{'Id':'other'}]
        with self.assertRaisesRegex(AdmissionRefused,'NEW_VOLUME_REFERENCED'):self.call()
    def test_receipt_wrong_owner_release_and_recreated_volume_refused(self):
        self.call();path=self.new/(OWNER+'.json');args=self.receipt_args()
        for key,value in [('owner','d'*32),('release_sha','d'*64),('image_id','sha256:'+'d'*64),
                ('state',{**args['state'],'created_at':'later'})]:
            self.assertFalse(admission.new_receipt_matches(path,**{**args,key:value}))
        path.chmod(0o666);self.assertFalse(admission.new_receipt_matches(path,**args))
    def test_symlink_directory_refused(self):
        link=self.root/'link';link.symlink_to(self.new,target_is_directory=True)
        self.new=link
        with self.assertRaisesRegex(AdmissionRefused,'UNSAFE_ADMIN_DIRECTORY'):self.call()
    def test_large_bulk_receipt_requires_explicit_bounded_limit(self):
        path=self.root/'bulk.json';data={'owners':'x'*20000};admission.write_receipt(path,data)
        with self.assertRaises(AdmissionRefused):admission.read_receipt(path)
        self.assertEqual(admission.read_receipt(path,maximum_bytes=4*1024*1024),data)
        with self.assertRaises(AdmissionRefused):admission.read_receipt(path,maximum_bytes=8*1024*1024)
    def test_native_workspace_wipe_is_refused_before_any_docker_call(self):
        m=load_manager();m.auth=lambda _:None;m.require_runtime_activation=lambda:None;m.docker=AsyncMock()
        with self.assertRaises(m.HTTPException) as caught:
            asyncio.run(m.storage(OWNER,m.StorageBody(action='wipe-workspace'),None))
        self.assertEqual(caught.exception.detail,'personal_cloud_native021_maintenance_unreviewed')
        m.docker.assert_not_called()
    def test_real_manager_accepts_new_receipt_but_not_preexisting_empty_owner(self):
        self.call();m=load_manager();m.NATIVE021_NEW_OWNERS_DIR=str(self.new);m.NATIVE021_RECEIPTS_DIR=str(self.old)
        args=self.receipt_args()
        m.require_native021_history_receipt(OWNER,(IMAGE,args['state']['name'],args['state']['created_at']),
            (args['files']['name'],args['files']['created_at']))
        with self.assertRaises(m.HTTPException):m.require_native021_history_receipt('d'*32,
            (IMAGE,NS+'-'+'d'*32+'-state','existing'),(NS+'-'+'d'*32+'-files','existing'))


class RestoreManagerTests(unittest.TestCase):
    def test_restored_mapping_is_consumed_in_actual_worker_mounts(self):
        m=load_manager(PERSONAL_CLOUD_WORKER_RUNTIME='0.2.0-rc.2',PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR='',
            PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR='',PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE='/opt/personal-cloud/runtime-r41/activation.json',
            PERSONAL_CLOUD_RESTORE_BINDINGS_FILE='/opt/personal-cloud/runtime-r65/restore.json')
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);f=root/'restore.json';m.RESTORE_BINDINGS_FILE=str(f);m.runtime_activated=lambda:True
            pair={k:{'name':NS+'-'+OWNER+'-restore-'+'e'*16+'-'+k,'created_at':'synthetic'} for k in ('state','files')}
            data=dict(schema='megmeet-runtime-restore-bindings-r65-v1',status='RESTORE_VERIFIED',runtime='0.2.0-rc.2',
                manager_namespace=NS,image_id=IMAGE,release_manifest_sha256=RELEASE,owners={OWNER:pair})
            admission.write_receipt(f,data)
            from types import SimpleNamespace
            config=m.container_config(OWNER,SimpleNamespace(catalog={},lease='mcl_'+'L'*43),'R'*43)
            self.assertEqual([v['Source'] for v in config['HostConfig']['Mounts']],[pair['state']['name'],pair['files']['name']])
            with self.assertRaises(m.HTTPException):m.volume_names('f'*32)
            f.write_text(json.dumps({**data,'image_id':'sha256:'+'f'*64}))
            with self.assertRaises(m.HTTPException):m.volume_names(OWNER)

if __name__=='__main__':unittest.main()
