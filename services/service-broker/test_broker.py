"""Only synthetic passwords, keys, files and loopback SSH; no company network."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import asyncssh
import httpx
from journal import Journal
from main import make_app,configure_process
from protocol import PROTOCOL,Rejected,arguments,connection_request,decode,request
from registry import Registry,load_registry
from service import Broker

configure_process()
TOKEN='synthetic_gateway_token_'+('a'*32)
PASSWORD='synthetic_ssh_password_only'
USER='M00003307'
SESSION=str(uuid4())

def connection_body(*,employee=USER,session=SESSION,generation='stable_1'):
    job=str(uuid4())
    return {'protocol':PROTOCOL,'employee_no':employee,'scope':{'session_id':session,'invocation_id':'pi_'+'a'*43,'job_id':job,'generation':generation,'expires_at':int(time.time())+60},'request_id':job,'fields':{'host':'10.4.240.14','port':22,'username':'root','password':PASSWORD}}
def operation(host,command='normal',*,template=None,action='run_command',args=None):
    value=connection_body() if template is None else deepcopy(template);value.pop('fields',None)
    job=str(uuid4());value['request_id']=job;value['scope']['job_id']=job
    value['action']=action;value['arguments']=args or ({'host_id':host,'command':command} if action=='run_command' else {})
    return value
async def terminal(broker,ack):
    for _ in range(500):
        value=await broker.get(ack['operation_id'],USER,ack['scope']['job_id'])
        if value['status'] in {'completed','failed','cancelled','unknown'}:return value
        await asyncio.sleep(.01)
    raise AssertionError('synthetic job did not finish')

class ProtocolTests(unittest.TestCase):
    def test_minimal_registry_has_no_ssh_secret_or_host_configuration(self):
        config={'schema':'maimi-service-broker-v2','enabled':True,'policy_generation':1,'token_file':'/run/secrets/token','allowed_users':[USER]}
        def private(path,size):return TOKEN.encode() if path.endswith('/token') else json.dumps(config).encode()
        with patch('registry.private_read',private):
            value=load_registry();self.assertEqual(value.allowed_users,(USER,));self.assertFalse(hasattr(value,'host'))
            config['host']={'password':'forbidden'}
            with self.assertRaises(Rejected):load_registry()
    def test_private_ipv4_and_strict_schema(self):
        for address in ('10.4.240.14','172.16.0.1','172.31.255.254','192.168.1.1'):
            value=connection_body();value['fields']['host']=address;self.assertEqual(connection_request(value)['fields']['host'],address)
    def test_reject_nonprivate_addresses_names_url_and_ambiguous_literals(self):
        for address in ('127.0.0.1','169.254.169.254','localhost','8.8.8.8','172.32.0.1','::1','10.004.1.1','10.0.0.1:22','https://10.1.1.1','0.0.0.0'):
            value=connection_body();value['fields']['host']=address
            with self.subTest(address=address),self.assertRaises(Rejected):connection_request(value)
    def test_three_employee_acl(self):
        value=connection_body(employee='M00092946')
        with self.assertRaises(Rejected):connection_request(value)
    def test_bounded_port_username_password(self):
        for key,bad in (('port',True),('port','22'),('port',0),('port',65536),('username','root;ls'),('username','-root'),('password',''),('password','x\n'),('password','x'*4097)):
            value=connection_body();value['fields'][key]=bad
            with self.subTest(key=key,bad=str(bad)[:8]),self.assertRaises(Rejected):connection_request(value)
    def test_password_only_accepted_on_human_control(self):
        value=operation(str(uuid4()));value['arguments']['password']=PASSWORD
        with self.assertRaises(Rejected):request(value)
        value=connection_body();value['fields']['private_key']='x'
        with self.assertRaises(Rejected):connection_request(value)
    def test_opaque_uuid_host_only(self):
        for host in ('server-10-4-240-14','10.4.240.14',str(uuid4()).upper()):
            with self.assertRaises(Rejected):arguments('run_command',{'host_id':host,'command':'x'})
    def test_file_guards_and_limits(self):
        for path in ('/etc/passwd','../outside','a/../b','a//b','.ssh/key','.env','x.pem','id_rsa','a\\b'):
            with self.subTest(path=path),self.assertRaises(Rejected):arguments('read_file',{'host_id':str(uuid4()),'relative_path':path})
        with self.assertRaises(Rejected):arguments('write_file',{'host_id':str(uuid4()),'relative_path':'a','content':'x','expected_sha256':'ANY'})
        with self.assertRaises(Rejected):arguments('run_command',{'host_id':str(uuid4()),'command':'x','timeout_seconds':301})
    def test_strict_json_and_scope(self):
        with self.assertRaises(Rejected):decode(b'{"x":1,"x":2}')
        value=connection_body();value['scope']['expires_at']=int(time.time())+1000
        with self.assertRaises(Rejected):connection_request(value)

class LoopbackSSHTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name);os.chmod(self.path,0o700)
        self.remote=self.path/'synthetic_home';self.remote.mkdir();self.commands=[];self.auth=[];self.options=[];self.gate=asyncio.Event()
        self.hostkey=asyncssh.generate_private_key('ssh-ed25519');self.registry=Registry(1,TOKEN)
        outer=self
        class Server(asyncssh.SSHServer):
            def begin_auth(self,username):return True
            def password_auth_supported(self):return True
            def validate_password(self,username,password):
                outer.auth.append((username,password,outer.broker.store.db.execute('SELECT count(*) FROM public_host_keys').fetchone()[0]))
                return username in {'root','dev'} and password==PASSWORD
        async def handler(process):
            self.commands.append(process.command)
            if process.command.startswith('/usr/bin/python3'):
                if not sys.platform.startswith('linux'):process.exit(127);return
                from remote_program import PROGRAM
                value=json.loads(await process.stdin.read());value['root']=str(self.remote)
                child=await asyncio.create_subprocess_exec(sys.executable,'-I','-B','-S','-c',PROGRAM,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await child.communicate(json.dumps(value).encode());process.stdout.write(out);process.stderr.write(err);process.exit(child.returncode);return
            command=shlex.split(process.command)[2].split(' && ',2)[-1] if process.command.startswith('/bin/sh -c ') else 'inspect'
            if command.startswith('exec /bin/sh -c '):command=shlex.split(command)[3]
            if command=='stall':
                try:await self.gate.wait()
                except asyncio.CancelledError:return
            if command=='large':process.stdout.write(b'x'*70000)
            elif command=='secret':process.stdout.write((PASSWORD+' '+TOKEN).encode())
            elif command=='stderr':process.stderr.write(b'synthetic stderr')
            else:process.stdout.write(b'synthetic remote stdout')
            process.exit(0)
        self.server=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[self.hostkey],process_factory=handler,encoding=None)
        self.port=self.server.get_port()
        async def factory(**options):
            self.options.append(dict(options));options['host']='127.0.0.1';options['port']=self.port
            return await asyncssh.connect(**options)
        self.factory=factory;self.broker=Broker(self.registry,self.path/'state.sqlite3',connection_factory=factory)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(self.broker)),base_url='http://service-broker',headers={'Authorization':'Bearer '+TOKEN})
    async def asyncTearDown(self):
        self.gate.set();await self.client.aclose();await self.broker.close();self.server.close();await self.server.wait_closed()
    async def connect(self,body=None):
        self.connect_body=body or connection_body();reply=await self.broker.connections.open(self.connect_body);self.host=reply['server']['host_id'];return reply
    async def execute(self,command='normal',**kwargs):
        ack=await self.broker.submit(operation(self.host,command,**kwargs));return await terminal(self.broker,ack)
    def scopequery(self,body=None):
        body=body or self.connect_body
        return {'employee_no':body['employee_no'],'session_id':body['scope']['session_id'],'generation':body['scope']['generation']}
    async def test_connect_auth_only_pins_before_password_and_no_target_commands(self):
        reply=await self.connect();self.assertTrue(reply['connected']);self.assertEqual(len(self.auth),1);self.assertEqual(self.auth[0][2],1);self.assertEqual(self.commands,[])
        opts=self.options[0];self.assertEqual(opts['known_hosts'],());self.assertEqual(opts['config'],[]);self.assertIsNone(opts['agent_path']);self.assertFalse(opts['public_key_auth']);self.assertFalse(opts['kbdint_auth']);self.assertEqual(opts['x509_trusted_cert_paths'],[])
        self.assertNotIn(PASSWORD,json.dumps(reply));self.assertNotIn(TOKEN,json.dumps(reply))
    async def test_cross_job_invocation_same_stable_generation_uses_same_ssh(self):
        await self.connect();a=await self.execute();value=operation(self.host);value['scope']['invocation_id']='pi_'+'b'*43
        b=await terminal(self.broker,await self.broker.submit(value));self.assertEqual((a['status'],b['status']),('completed','completed'));self.assertEqual(len(self.auth),1)
    async def test_employee_session_generation_isolation(self):
        await self.connect()
        for body in (connection_body(employee='M00001069'),connection_body(session=str(uuid4())),connection_body(generation='stable_2')):
            reply=await self.broker.connections.status(**self.scopequery(body));self.assertFalse(reply['connected'])
            with self.assertRaises(Rejected):await self.broker.submit(operation(self.host,template=body))
        self.assertTrue((await self.broker.connections.status(**self.scopequery()))['connected'])
    async def test_stale_generation_get_and_delete_cannot_remove_new_connection(self):
        await self.connect();old=self.connect_body
        await self.connect(connection_body(generation='stable_new'))
        self.assertFalse((await self.broker.connections.status(**self.scopequery(old)))['connected'])
        self.assertFalse((await self.broker.connections.disconnect(**self.scopequery(old)))['connected'])
        self.assertTrue((await self.broker.connections.status(**self.scopequery()))['connected'])
    async def test_new_generation_replaces_old_handle_and_clears_password(self):
        await self.connect();old=self.broker.connections.sessions[(USER,SESSION)];oldid=self.host
        await self.connect(connection_body(generation='stable_new'));self.assertEqual(old.password,'');self.assertIsNone(old.connection);self.assertNotEqual(self.host,oldid)
        with self.assertRaises(Rejected):await self.broker.submit(operation(oldid,template=self.connect_body))
    async def test_changed_host_key_rejected_before_password_in_new_session(self):
        await self.connect();await self.broker.connections.disconnect(**self.scopequery());authcount=len(self.auth)
        wrong=asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode().strip()
        self.broker.store.db.execute('UPDATE public_host_keys SET public_key=?',(wrong,))
        with self.assertRaises(Rejected) as cm:await self.connect(connection_body())
        self.assertEqual(cm.exception.code,'SSH_HOST_KEY_REJECTED');self.assertEqual(len(self.auth),authcount)
    async def test_failed_password_bounded_one_attempt_and_not_in_database(self):
        value=connection_body();value['fields']['password']='wrong_synthetic_password'
        with self.assertRaises(Rejected) as cm:await self.connect(value)
        self.assertEqual(cm.exception.code,'SSH_AUTH_FAILED');self.assertEqual(len(self.options),1);self.assertEqual(len(self.auth),1);self.assertFalse(self.commands)
        with self.assertRaises(Rejected):await self.connect(value)
        self.assertEqual(len(self.options),1);self.assertNotIn(b'wrong_synthetic_password',(self.path/'state.sqlite3').read_bytes())
    async def test_connect_lost_ack_duplicate_does_not_reauthenticate(self):
        await self.connect();reply=await self.broker.connections.open(deepcopy(self.connect_body));self.assertEqual(reply['server']['host_id'],self.host);self.assertEqual(len(self.options),1)
        value=deepcopy(self.connect_body);value['fields']['password']='changed_synthetic_password'
        with self.assertRaises(Rejected):await self.broker.connections.open(value)
    async def test_idle_expiry_and_status_poll_does_not_extend(self):
        await self.connect();session=self.broker.connections.sessions[(USER,SESSION)];before=session.last_used
        await self.broker.connections.status(**self.scopequery());self.assertEqual(before,session.last_used)
        session.last_used-=7201;await self.broker.connections.expire();self.assertEqual(session.password,'');self.assertFalse((await self.broker.connections.status(**self.scopequery()))['connected'])
    async def test_disconnect_clears_session_without_touching_other_employee(self):
        await self.connect();first=self.broker.connections.sessions[(USER,SESSION)]
        other=connection_body(employee='M00001069');await self.broker.connections.open(other)
        await self.broker.connections.disconnect(**self.scopequery());self.assertEqual(first.password,'');self.assertTrue((await self.broker.connections.status(**self.scopequery(other)))['connected'])
    async def test_normal_stdout_stderr_no_python_prerequisite(self):
        await self.connect();result=await self.execute('stderr');self.assertEqual(result['status'],'completed');self.assertEqual(result['result']['output'],'synthetic stderr');self.assertTrue(self.commands[0].startswith('/bin/sh -c '));self.assertIn('"$HOME"',self.commands[0]);self.assertFalse(result['result']['sandboxed'])
    async def test_secret_echo_redacted_and_never_journaled(self):
        await self.connect();result=await self.execute('secret');self.assertEqual(result['status'],'completed');self.assertIn('REDACTED',result['result']['output']);data=(self.path/'state.sqlite3').read_bytes();self.assertNotIn(PASSWORD.encode(),data);self.assertNotIn(TOKEN.encode(),data)
    async def test_cannot_submit_known_password_in_operation(self):
        await self.connect()
        with self.assertRaises(Rejected):await self.execute('echo '+PASSWORD)
        self.assertFalse(self.commands)
    async def test_exact_idempotent_command_no_replay_or_blind_conflict(self):
        await self.connect();value=operation(self.host);ack=await self.broker.submit(value);await terminal(self.broker,ack)
        duplicate=await self.broker.submit(deepcopy(value));self.assertEqual(ack,duplicate);self.assertEqual(len(self.commands),1)
        value['arguments']['command']='other'
        with self.assertRaises(Rejected):await self.broker.submit(value)
    async def test_cancel_dispatched_command_unknown_and_revokes_connection(self):
        await self.connect();ack=await self.broker.submit(operation(self.host,'stall'))
        while not self.commands:await asyncio.sleep(.01)
        await self.broker.cancel(ack['operation_id'],USER,ack['scope']['job_id']);result=await terminal(self.broker,ack)
        self.assertEqual(result['status'],'unknown');self.assertIsNone(result['result']);self.assertFalse((await self.broker.connections.status(**self.scopequery()))['connected'])
    async def test_output_limit_unknown_and_not_success(self):
        await self.connect();result=await self.execute('large');self.assertEqual(result['status'],'unknown');self.assertEqual(result['error_code'],'OUTPUT_LIMIT');self.assertIsNone(result['result'])
    async def test_timeout_unknown(self):
        await self.connect();result=await self.execute(args={'host_id':self.host,'command':'stall','timeout_seconds':1});self.assertEqual(result['status'],'unknown');self.assertEqual(result['error_code'],'OPERATION_TIMEOUT')
    async def test_http_control_requires_bearer_before_body(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(self.broker)),base_url='http://service-broker') as client:
            result=await client.post('/v1/connections',json=connection_body());self.assertEqual(result.status_code,403);self.assertEqual(result.json(),{'error_code':'NOT_AUTHORIZED'})
        self.assertFalse(self.auth)
    async def test_http_control_closed_shapes_status_delete_and_no_store(self):
        body=connection_body();result=await self.client.post('/v1/connections',json=body);self.assertEqual(result.status_code,200);self.assertEqual(set(result.json()),{'protocol','connected','server'});self.assertEqual(result.headers['cache-control'],'no-store')
        query=self.scopequery(body);self.assertTrue((await self.client.get('/v1/connections',params=query)).json()['connected']);self.assertFalse((await self.client.delete('/v1/connections',params=query)).json()['connected'])
        result=await self.client.get('/v1/connections',params={**query,'password':'never'});self.assertEqual(result.status_code,400)
    async def test_failed_control_response_contains_no_password_or_address(self):
        value=connection_body();value['fields']['password']='wrong_synthetic_password'
        result=await self.client.post('/v1/connections',json=value);self.assertEqual(result.status_code,403);self.assertEqual(result.json(),{'error_code':'SSH_AUTH_FAILED'})
    async def test_duplicate_or_oversized_http_body_rejected_without_authentication(self):
        result=await self.client.post('/v1/connections',content=b'{"protocol":1,"protocol":2}',headers={'Content-Type':'application/json'})
        self.assertEqual(result.status_code,400)
        result=await self.client.post('/v1/connections',content=b'x'*16385,headers={'Content-Type':'application/json'});self.assertEqual(result.status_code,400);self.assertFalse(self.auth)
    async def test_queued_cancel_before_first_instruction_no_execution(self):
        await self.connect();ack=await self.broker.submit(operation(self.host));await self.broker.cancel(ack['operation_id'],USER,ack['scope']['job_id']);result=await terminal(self.broker,ack)
        self.assertEqual(result['status'],'cancelled');self.assertFalse(self.commands);self.assertTrue((await self.broker.connections.status(**self.scopequery()))['connected'])
    async def test_other_operation_replay_keeps_unrelated_active_job(self):
        await self.connect();ack=await self.broker.submit(operation(self.host,'stall'))
        while not self.commands:await asyncio.sleep(.01)
        other=connection_body(employee='M00001069',session=str(uuid4()));await self.broker.connections.open(other)
        result=await self.broker.get(ack['operation_id'],USER,ack['scope']['job_id']);self.assertEqual(result['status'],'running');self.gate.set();self.assertEqual((await terminal(self.broker,ack))['status'],'completed')
    async def test_connect_replacement_rejected_during_running_command(self):
        await self.connect();ack=await self.broker.submit(operation(self.host,'stall'))
        while not self.commands:await asyncio.sleep(.01)
        with self.assertRaises(Rejected) as cm:await self.broker.connections.open(connection_body())
        self.assertEqual(cm.exception.code,'CONNECTION_BUSY');self.gate.set();await terminal(self.broker,ack)
    async def test_read_missing_python_is_known_failure_keeps_shell_connection(self):
        await self.connect()
        original=self.broker.transport.command
        async def missing(connection,command,input_bytes,execution,**kwargs):
            if command.startswith('/usr/bin/python3'):execution.dispatched=True;return 127,b'not installed'
            return await original(connection,command,input_bytes,execution,**kwargs)
        self.broker.transport.command=missing
        ack=await self.broker.submit(operation(self.host,action='read_file',args={'host_id':self.host,'relative_path':'x.py'}));result=await terminal(self.broker,ack)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error_code'],'PYTHON_UNAVAILABLE');self.assertEqual((await self.execute())['status'],'completed')
    async def test_restart_clears_connections_preserves_pins_and_unknown_receipt(self):
        await self.connect();ack=await self.broker.submit(operation(self.host,'stall'))
        while not self.commands:await asyncio.sleep(.01)
        await self.broker.close();self.broker=Broker(self.registry,self.path/'state.sqlite3',connection_factory=self.factory)
        self.assertFalse((await self.broker.connections.status(**self.scopequery()))['connected']);self.assertEqual(self.broker.store.db.execute('SELECT count(*) FROM public_host_keys').fetchone()[0],1)
        result=await self.broker.get(ack['operation_id'],USER,ack['scope']['job_id']);self.assertEqual(result['status'],'unknown');self.assertIsNone(result['result'])
    async def test_disconnect_fences_pending_authentication_even_if_cancel_delayed(self):
        original=self.broker.connections.connect;entered=asyncio.Event();release=asyncio.Event()
        async def delayed(**options):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:pass
            return await original(**options)
        self.broker.connections.connect=delayed;body=connection_body();task=asyncio.create_task(self.broker.connections.open(body));await entered.wait()
        await self.broker.connections.disconnect(**self.scopequery(body));release.set()
        with self.assertRaises(Rejected):await task
        self.assertFalse((await self.broker.connections.status(**self.scopequery(body)))['connected'])
    async def test_stale_delete_does_not_fence_new_generation_connecting(self):
        original=self.broker.connections.connect;entered=asyncio.Event();release=asyncio.Event()
        async def delayed(**options):entered.set();await release.wait();return await original(**options)
        self.broker.connections.connect=delayed;new=connection_body(generation='new');task=asyncio.create_task(self.broker.connections.open(new));await entered.wait()
        await self.broker.connections.disconnect(**self.scopequery(connection_body(generation='old')));release.set();self.assertTrue((await task)['connected'])
    async def test_result_after_disconnect_is_discarded_not_persisted(self):
        await self.connect();started=asyncio.Event();release=asyncio.Event()
        class Delayed:
            async def execute(inner,session,action,args,execution):
                execution.dispatched=True;started.set();await release.wait()
                return {'output':PASSWORD}
        self.broker.transport=Delayed();ack=await self.broker.submit(operation(self.host));await started.wait();await self.broker.connections.disconnect(**self.scopequery());release.set()
        result=await terminal(self.broker,ack);self.assertEqual(result['status'],'unknown');self.assertIsNone(result['result']);self.assertNotIn(PASSWORD.encode(),(self.path/'state.sqlite3').read_bytes())
    @unittest.skipUnless(sys.platform.startswith('linux'),'requires real Linux openat helper')
    async def test_authenticated_file_create_read_update_conflict_and_escape(self):
        await self.connect();base={'host_id':self.host,'relative_path':'source.py'}
        value=operation(self.host,action='write_file',args={**base,'content':'first','expected_sha256':'MISSING'});created=await terminal(self.broker,await self.broker.submit(value));self.assertEqual(created['status'],'completed')
        read=await terminal(self.broker,await self.broker.submit(operation(self.host,action='read_file',args=base)));self.assertEqual(read['result']['content'],'first')
        updated=await terminal(self.broker,await self.broker.submit(operation(self.host,action='write_file',args={**base,'content':'second','expected_sha256':read['result']['sha256']})));self.assertEqual(updated['status'],'completed')
        stale=await terminal(self.broker,await self.broker.submit(operation(self.host,action='write_file',args={**base,'content':'third','expected_sha256':read['result']['sha256']})));self.assertEqual(stale['error_code'],'FILE_CHANGED');self.assertEqual((self.remote/'source.py').read_text(),'second')

if __name__=='__main__':unittest.main()
