"""Synthetic node projections plus pinned upstream hook invocation; no Docker."""
import io
import json
import tarfile
import unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import patch

import node_guard as n
import server_policy as s
from provider import Profile, ProviderError, REQUIRED_NODE_FACTS

TASK='11111111-2222-3333-4444-555555555555'; SID='synthetic'; CID='b'*64; NET='c'*64
PROFILE=Profile('example.invalid/image@sha256:'+'a'*64)


def container(paused=False):
    return {'id':CID,'image':PROFILE.image,'runtime':'runsc','network':n.NETWORK,
      'networks':[NET,None],'readonly':True,'privileged':False,'cap_add':None,'cap_drop':['ALL'],
      'security':['no-new-privileges=true'],'pids':512,'memory':2048*1024*1024,'cpu':1000000000,
      'pid_mode':'','ipc_mode':'private','devices':[],'tmpfs':dict(n.TMPFS),
      'mounts':[{'type':'tmpfs','destination':key,'rw':True} for key in n.TMPFS]+[None],
      'ports':{'44772/tcp':[{'HostIp':'127.0.0.1','HostPort':'41001'}],
               '8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'41002'}]},
      'running':True,'paused':paused,'task':TASK,'controller':'controller',
      'sandbox_id':SID,'execd_init':True}


def network():
    return {'Name':n.NETWORK,'Id':NET,'Driver':'bridge','Internal':True,'EnableIPv6':False,
      'Options':{'com.docker.network.bridge.name':n.BRIDGE,'com.docker.network.bridge.enable_icc':'false'}}


def firewall():
    entries=[{'table':{'family':'inet','name':'maimi_sandbox_r64'}}]
    for chain,rules in n.expected_firewall_rules().items():
        entries.append({'chain':{'family':'inet','table':'maimi_sandbox_r64','name':chain,'type':'filter','hook':chain,'prio':-200,'policy':'accept'}})
        entries += [{'rule':{'family':'inet','table':'maimi_sandbox_r64','chain':chain,'expr':expr}} for expr in rules]
    return {'nftables':entries}


def config():
    return NS(runtime=NS(type='docker',execd_run_as_init=True),
      secure_runtime=NS(type='gvisor',docker_runtime='runsc'),
      docker=NS(network_mode=n.NETWORK,publish_host='127.0.0.1',drop_capabilities=['ALL'],
                no_new_privileges=True,pids_limit=512,sandbox_env={},sandbox_binds=[],apparmor_profile=None,seccomp_profile=None),
      server=NS(host='127.0.0.1',api_key='synthetic-'+'x'*40))


def hostconfig():
    return {'network_mode':n.NETWORK,'runtime':'runsc','cap_drop':['ALL'],
            'security_opt':['no-new-privileges=true'],'pids_limit':512,
            'mem_limit':2048*1024*1024,'nano_cpus':1000000000}


class PolicyTests(unittest.TestCase):
    def test_container_strict_outer_boundary(self):
        self.assertEqual(n.validate_container(container(),SID,TASK,PROFILE,'controller',NET),CID)
        for key,value in [('runtime','runc'),('readonly',False),('privileged',True),('pids',None),
          ('execd_init',False),('network','host'),('cap_add',['SYS_ADMIN']),('devices',[{}]),
          ('networks',[NET,'foreign',None]),('task','another-task'),('security',[])]:
            v=container();v[key]=value
            with self.subTest(key=key),self.assertRaises(ProviderError): n.validate_container(v,SID,TASK,PROFILE,'controller',NET)
    def test_host_mount_or_public_port_rejected(self):
        for kind in ('bind','volume'):
            v=container();v['mounts'].append({'type':kind,'destination':'/workspace','rw':True})
            with self.assertRaises(ProviderError):n.validate_container(v,SID,TASK,PROFILE,'controller',NET)
        v=container();v['ports']['8080/tcp'][0]['HostIp']='0.0.0.0'
        with self.assertRaises(ProviderError):n.validate_container(v,SID,TASK,PROFILE,'controller',NET)
    def test_network_and_firewall_exact_rules(self):
        self.assertEqual(n.validate_network(network()),NET);n.validate_firewall(firewall())
        v=network();v['Internal']=False
        with self.assertRaises(ProviderError):n.validate_network(v)
        v=firewall();v['nftables'].pop()
        with self.assertRaises(ProviderError):n.validate_firewall(v)
        v=firewall();v['nftables'][-1]['rule']['expr']=[{'accept':None}]
        with self.assertRaises(ProviderError):n.validate_firewall(v)
    def test_policy_requires_private_api_and_rejects_unsafe_config(self):
        s.validate_config(config())
        for group,key,value in [('server','host','0.0.0.0'),('runtime','execd_run_as_init',False),
          ('secure_runtime','docker_runtime','runc'),('docker','sandbox_binds',['/etc:/etc']),
          ('docker','sandbox_env',{'KEY':'secret'}),('docker','pids_limit',0)]:
            v=config();setattr(getattr(v,group),key,value)
            with self.subTest(key=key),self.assertRaises(RuntimeError):s.validate_config(v)
    def test_storage_limits_and_docker_logs_enforced(self):
        v=s.enforce_host_config(hostconfig())
        self.assertTrue(v['read_only']);self.assertEqual(v['tmpfs'],n.TMPFS)
        self.assertEqual(v['memswap_limit'],v['mem_limit']);self.assertEqual(v['log_config']['Config']['max-file'],'2')
        for key,value in [('runtime','runc'),('binds',{'/etc':{}}),('privileged',True),('cap_add',['SYS_ADMIN']),('pid_mode','host')]:
            v=hostconfig();v[key]=value
            with self.assertRaises(RuntimeError):s.enforce_host_config(v)
    def test_actual_pinned_upstream_hook_reaches_host_config(self):
        from opensandbox_server.services.docker.container_ops import DockerContainerOpsMixin
        old=DockerContainerOpsMixin._create_and_start_container
        captures=[]
        class Reached(Exception):pass
        def create_host_config(**kw):captures.append(kw);raise Reached()
        instance=NS(app_config=config(),docker_client=NS(api=NS(create_host_config=create_host_config)))
        try:
            self.assertEqual(s.install(config()),{'policy':s.POLICY,'verified':True})
            with self.assertRaises(Reached):
                DockerContainerOpsMixin._create_and_start_container(instance,SID,PROFILE.image,
                  ['/bin/sh','-c','sleep infinity'],{},[],hostconfig(),['44772','8080'],None)
            self.assertTrue(captures[0]['read_only']);self.assertEqual(captures[0]['tmpfs'],n.TMPFS)
            self.assertEqual(captures[0]['runtime'],'runsc')
            with self.assertRaisesRegex(RuntimeError,'ALREADY_INSTALLED'):s.install(config())
            with self.assertRaisesRegex(RuntimeError,'UNCONFINED_REQUEST'):
                DockerContainerOpsMixin._create_and_start_container(instance,SID,PROFILE.image,
                  ['/bin/sh'],{},[],hostconfig(),[],None,True)
        finally:
            DockerContainerOpsMixin._create_and_start_container=old
            if hasattr(DockerContainerOpsMixin,'_maimi_policy'):delattr(DockerContainerOpsMixin,'_maimi_policy')
    def test_upstream_drift_rejected_before_hook_install(self):
        from opensandbox_server.services.docker.container_ops import DockerContainerOpsMixin
        original=DockerContainerOpsMixin._create_and_start_container
        read=s.Path.read_text
        def altered(path,*args,**kwargs):
            text=read(path,*args,**kwargs)
            if path.name=='upstream-pins.json':
                value=json.loads(text)
                value['server_normalized_sha256']['container_ops.py']='0'*64
                return json.dumps(value)
            return text
        with patch.object(s.Path,'read_text',altered):
            with self.assertRaisesRegex(RuntimeError,'UPSTREAM_SOURCE_CHANGED'):s.install(config())
        self.assertIs(DockerContainerOpsMixin._create_and_start_container,original)
        self.assertFalse(hasattr(DockerContainerOpsMixin,'_maimi_policy'))


def archive(kind='file'):
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w',format=tarfile.USTAR_FORMAT) as t:
        item=tarfile.TarInfo('a.txt')
        if kind=='link':item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd';t.addfile(item)
        else:item.size=7;t.addfile(item,io.BytesIO(b'content'))
    return output.getvalue()


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.paused=False;self.calls=[];self.mode='ok'
    async def runner(self,args,**kw):
        self.calls.append(args)
        if args[1]=='inspect':return json.dumps(container(self.paused)).encode()
        if args[1]=='network':return json.dumps(network()).encode()
        if args[1]=='pause':
            self.paused=True
            if self.mode=='lost_pause':raise ProviderError('NODE_COMMAND_UNCONFIRMED')
            return CID.encode()
        if args[1]=='unpause':
            if self.mode=='lost_resume':raise ProviderError('NODE_COMMAND_UNCONFIRMED')
            self.paused=False;return CID.encode()
        if args[1]=='cp':return archive('link' if self.mode=='link' else 'file')
        raise AssertionError('unexpected node action')
    def guard(self):
        async def policy():return {'policy':s.POLICY,'verified':True}
        async def true():return True
        return n.LinuxNodeGuard(server_policy_check=policy,capacity_check=true,snapshot_acceptance_check=true,runner=self.runner)
    async def read(self):return await self.guard().snapshot_output(SID,TASK,PROFILE,'controller','/workspace/outputs/a.txt',100)
    async def test_paused_single_file_snapshot_and_resume(self):
        self.assertEqual(await self.read(),b'content');self.assertFalse(self.paused)
        self.assertEqual(sum(a[1]=='pause' for a in self.calls),1);self.assertEqual(sum(a[1]=='unpause' for a in self.calls),1)
    async def test_symlink_export_rejected_and_resumed(self):
        self.mode='link'
        with self.assertRaisesRegex(ProviderError,'SNAPSHOT_FILE_REJECTED'):await self.read()
        self.assertFalse(self.paused)
    async def test_lost_pause_ack_never_returns_data_even_if_resumed(self):
        self.mode='lost_pause'
        with self.assertRaises(ProviderError):await self.read()
        self.assertFalse(self.paused);self.assertFalse(any(a[1]=='cp' for a in self.calls))
    async def test_lost_resume_ack_never_returns_snapshot(self):
        self.mode='lost_resume'
        with self.assertRaises(ProviderError):await self.read()
        self.assertTrue(self.paused)
        self.assertEqual(sum(a[1]=='unpause' for a in self.calls),1)
    async def test_missing_runtime_acceptance_keeps_not_ready(self):
        async def runner(args,**kw):
            if args[0]=='nft':return json.dumps(firewall()).encode()
            if args[1]=='info':return b'{"runsc":{}}'
            if args[1]=='network':return json.dumps(network()).encode()
            raise AssertionError()
        async def policy():return {'policy':s.POLICY,'verified':True}
        async def true():return True
        async def false():return False
        guard=n.LinuxNodeGuard(server_policy_check=policy,capacity_check=true,snapshot_acceptance_check=false,runner=runner)
        facts=await guard.probe()
        self.assertFalse(facts['snapshot_supported']);self.assertFalse(all(facts.values()))


if __name__=='__main__':unittest.main(verbosity=2)
