"""Synthetic local preparation checks; no company requests or service startup."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('server_configure', Path(__file__).with_name('configure.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RunningGatewayOwnershipTests(unittest.TestCase):
    def test_exact_owned_running_loopback_gateway_only(self):
        valid={'running':True,'labels':{'com.docker.compose.project':'megmeet-desktop-server',
               'com.docker.compose.service':'gateway'},
               'ports':{'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18091'}]}}
        def accepted(value):
            with patch.object(module.subprocess,'run',side_effect=[
                SimpleNamespace(returncode=0,stdout='a'*64+'\n'),
                SimpleNamespace(returncode=0,stdout=json.dumps(value))]):
                return module.owned_running_gateway()
        self.assertTrue(accepted(valid))
        for bad in ({**valid,'running':False},{**valid,'labels':{}},
                    {**valid,'labels':{**valid['labels'],'com.docker.compose.project':'foreign'}},
                    {**valid,'ports':{'8080/tcp':[{'HostIp':'0.0.0.0','HostPort':'18091'}]}},
                    {**valid,'ports':{'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18090'}]}}):
            self.assertFalse(accepted(bad))
        with patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='a'*64+'\n'+'b'*64)):
            self.assertFalse(module.owned_running_gateway())


@unittest.skipUnless(os.name == 'posix' and shutil.which('docker'), 'Compose expansion requires Linux Docker CLI')
class ExpandedComposeTests(unittest.TestCase):
    def test_tmpfs_and_private_network_contract_in_expanded_config(self):
        # `config --quiet` accepts flow lists accidentally split at commas;
        # inspect normalized values before the Docker engine ever sees them.
        base=Path(__file__).parent
        env={'PATH':os.environ['PATH'],'DOCKER_HOST':'unix:///var/run/docker.sock',
             'POSTGRES_PASSWORD':'s'*64,'IAM_CLIENT_ID':'synthetic-test'}
        for name in module.FILES:env[name]='/tmp/synthetic-not-read/'+name
        args=['docker','compose','--env-file','/dev/null','-f',str(base/'compose.yaml'),
              '-f',str(base/'personal-cloud.compose.yaml'),'-f',str(base/'compose.nginx-network.yaml'),
              'config','--format','json']
        result=subprocess.run(args,capture_output=True,text=True,env=env,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        config=json.loads(result.stdout)
        for name,service in config['services'].items():
            if 'tmpfs' not in service:continue
            self.assertEqual(len(service['tmpfs']),1,name)
            self.assertTrue(service['tmpfs'][0].startswith('/tmp:'),name)
            self.assertIn(',nosuid,noexec,',service['tmpfs'][0],name)
        gateway=config['services']['gateway']
        self.assertEqual(gateway['environment']['IAM_IDENTITY_MODE'], 'sso_tokenkey')
        self.assertEqual(gateway['environment']['IAM_BASE_URL'], 'https://iam.megmeet.com')
        self.assertEqual(gateway['environment']['IAM_AUTHORIZATION_BASE_URL'], 'https://iam.megmeet.com')
        self.assertFalse(any('iam-test.megmeet.com' in host for host in gateway.get('extra_hosts', [])))
        self.assertNotIn('iam-directory', config['services'])
        self.assertNotIn('iam-directory', gateway.get('depends_on', {}))
        self.assertNotIn('iam-directory', config.get('volumes', {}))
        self.assertFalse(any(name.startswith('IAM_DIRECTORY_') for name in gateway['environment']))
        self.assertFalse(any('iam-directory' in mount.get('source', '') or '/var/lib/megmeet/iam' == mount.get('target')
                             for mount in gateway.get('volumes', [])))
        self.assertEqual(set(gateway['networks']),{'data','outbound','cloud-workers','common-net'})
        manager=config['services']['personal-cloud-manager']
        self.assertEqual(set(manager['networks']),{'cloud-workers'})
        self.assertTrue(config['networks']['cloud-workers']['internal'])
        self.assertEqual(manager['environment']['PERSONAL_CLOUD_WORKER_NETWORK'],config['networks']['cloud-workers']['name'])


@unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'Linux permission check requires root')
class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='megmeet-config-test-')
        self.directory = Path(self.temporary.name)
        self.config = {
            'POSTGRES_DB': 'megmeet_ai', 'POSTGRES_USER': 'megmeet', 'POSTGRES_PASSWORD': 'a' * 64,
            'IAM_BASE_URL': 'https://iam.megmeet.com',
            'IAM_AUTHORIZATION_BASE_URL': 'https://iam.megmeet.com',
            'IAM_CLIENT_ID': 'synthetic-client',
            **{name: str(self.directory / 'private' / filename) for name, filename in module.FILES.items()},
        }
        self.values = {name: 'synthetic-only' for name in module.FILES}
        self.values['IAM_TOKEN_ENCRYPTION_KEY_SOURCE'] = base64.urlsafe_b64encode(b's' * 32).decode()
        self.values['PERSONAL_CLOUD_MANAGER_SECRET_SOURCE'] = 'synthetic-manager-only-'+'x'*32

    def tearDown(self):
        self.temporary.cleanup()

    def test_complete_configuration_and_container_readable_permissions(self):
        module.validate(self.config, self.values)
        module.commit(self.directory, self.config, self.values)
        module.check(self.directory, check_port=False, check_compose=False)
        for name in module.FILES:
            metadata = Path(self.config[name]).stat()
            self.assertEqual(metadata.st_gid, 10001)
            self.assertEqual(metadata.st_mode & 0o777, 0o440)
        self.assertEqual((self.directory / '.env').stat().st_mode & 0o777, 0o600)

    def test_occupied_port_allow_running_still_rejects_foreign_owner(self):
        module.commit(self.directory,self.config,self.values)
        socket=MagicMock()
        socket.__enter__.return_value.bind.side_effect=OSError('synthetic port occupied')
        with patch.object(module.socket,'socket',return_value=socket),patch.object(module,'owned_running_gateway',return_value=False):
            with self.assertRaises(ValueError):module.check(self.directory,check_compose=False,allow_running=True)
        with patch.object(module.socket,'socket',return_value=socket),patch.object(module,'owned_running_gateway',return_value=True):
            module.check(self.directory,check_compose=False,allow_running=True)
            with self.assertRaises(ValueError):module.check(self.directory,check_compose=False)

    def test_reconfigure_preserves_existing_fernet_and_database_password(self):
        module.commit(self.directory, self.config, self.values)
        before = Path(self.config['IAM_TOKEN_ENCRYPTION_KEY_SOURCE']).read_bytes()
        with patch.object(module.sys.stdin, 'isatty', return_value=True), patch.object(module, 'prompt', side_effect=lambda label, default='', **kw: default), patch.object(module, 'compose_check'), contextlib.redirect_stdout(io.StringIO()):
            module.configure(self.directory, self.directory / 'unused-private')
        self.assertEqual(Path(self.config['IAM_TOKEN_ENCRYPTION_KEY_SOURCE']).read_bytes(), before)
        self.assertEqual(module.read_config(self.directory / '.env')['POSTGRES_PASSWORD'], 'a' * 64)

    def test_fresh_wizard_needs_only_sso_and_tokenkey_for_selected_environment(self):
        for origin in ('https://iam.megmeet.com',):
            with self.subTest(origin=origin):
                directory = self.directory / origin.split('//')[1]
                directory.mkdir()
                calls = []
                def answer(label, default='', **kwargs):
                    calls.append((label, kwargs))
                    if label == 'IAM地址': return origin
                    if label == 'IAM client_id': return 'synthetic-client'
                    if kwargs.get('hidden'): return 'synthetic-secret-only'
                    return default
                with patch.object(module.sys.stdin, 'isatty', return_value=True), \
                     patch.object(module, 'prompt', side_effect=answer), \
                     patch.object(module.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]), \
                     patch.object(module.socket, 'socket', return_value=MagicMock()), \
                     patch.object(module, 'check'), contextlib.redirect_stdout(io.StringIO()):
                    module.configure(directory, directory / 'private')
                saved = module.read_config(directory / '.env')
                self.assertEqual(saved['IAM_BASE_URL'], origin)
                self.assertEqual(saved['IAM_AUTHORIZATION_BASE_URL'], origin)
                self.assertFalse(set(module.LEGACY_FIELDS) & set(saved))
                self.assertEqual([label for label, _ in calls],
                                 ['IAM地址', 'IAM client_id', 'IAM client_secret', 'TokenKey服务 X-API-Key'])
                for label, options in calls:
                    if label != 'IAM地址':
                        self.assertTrue(options.get('hidden'), label)
                self.assertFalse((directory / 'private' / 'iam-directory-api-secret').exists())
                module.check(directory, check_port=False, check_compose=False)

    def test_test_iam_cannot_pass_company_deployment_check(self):
        with self.assertRaisesRegex(ValueError, 'IAM 正式服'):
            module.validate({**self.config, 'IAM_BASE_URL':'https://iam-test.megmeet.com',
                             'IAM_AUTHORIZATION_BASE_URL':'https://iam-test.megmeet.com'}, self.values)

    def test_switching_to_production_requires_new_application_credentials(self):
        previous = {**self.config, 'IAM_BASE_URL':'https://iam-test.megmeet.com',
                    'IAM_AUTHORIZATION_BASE_URL':'https://iam-test.megmeet.com',
                    'IAM_CLIENT_ID':'synthetic-old-test-client'}
        module.commit(self.directory, previous, self.values)
        encryption = Path(self.config['IAM_TOKEN_ENCRYPTION_KEY_SOURCE']).read_bytes()
        seen = {}
        def answer(label, default='', **kwargs):
            seen[label] = default
            return {'IAM client_id':'synthetic-production-client',
                    'IAM client_secret':'synthetic-production-secret'}.get(label, default)
        with patch.object(module.sys.stdin,'isatty',return_value=True), \
             patch.object(module,'prompt',side_effect=answer), \
             patch.object(module,'compose_check'), contextlib.redirect_stdout(io.StringIO()):
            module.configure(self.directory,self.directory/'unused-private')
        saved = module.read_config(self.directory/'.env')
        self.assertEqual(saved['IAM_BASE_URL'], 'https://iam.megmeet.com')
        self.assertEqual(seen['IAM client_id'], '')
        self.assertEqual(seen['IAM client_secret'], '')
        self.assertEqual(saved['IAM_CLIENT_ID'], 'synthetic-production-client')
        self.assertEqual(Path(saved['IAM_CLIENT_SECRET_SOURCE']).read_text().strip(), 'synthetic-production-secret')
        self.assertEqual(Path(saved['IAM_TOKEN_ENCRYPTION_KEY_SOURCE']).read_bytes(), encryption)
        self.assertEqual(saved['POSTGRES_PASSWORD'],previous['POSTGRES_PASSWORD'])

    def test_legacy_directory_fields_preserved_without_reading_or_rewriting_secret(self):
        legacy_secret = self.directory / 'legacy-directory-secret'
        legacy_secret.write_bytes(b'old-directory-secret\n')
        legacy_secret.chmod(0o600)
        legacy = {'IAM_DIRECTORY_ORG_URL': 'https://example.invalid/org',
                  'IAM_DIRECTORY_ACCOUNT_URL': 'https://example.invalid/account',
                  'IAM_DIRECTORY_API_KEY': 'synthetic-directory',
                  'IAM_DIRECTORY_SECRET_SOURCE': str(legacy_secret)}
        config = {**self.config, **legacy}
        module.validate(config, self.values)
        module.commit(self.directory, config, self.values)
        before_stat = legacy_secret.stat()
        read_value = module.read_value
        def read_required_only(path):
            self.assertNotEqual(path, legacy_secret)
            return read_value(path)
        with patch.object(module.sys.stdin, 'isatty', return_value=True), \
             patch.object(module, 'prompt', side_effect=lambda label, default='', **kw: default), \
             patch.object(module, 'read_value', side_effect=read_required_only), \
             patch.object(module, 'compose_check'), contextlib.redirect_stdout(io.StringIO()):
            module.configure(self.directory, self.directory / 'unused-private')
        saved = module.read_config(self.directory / '.env')
        self.assertEqual({name: saved[name] for name in module.LEGACY_FIELDS}, legacy)
        self.assertEqual(legacy_secret.read_bytes(), b'old-directory-secret\n')
        after_stat = legacy_secret.stat()
        self.assertEqual((after_stat.st_mode, after_stat.st_uid, after_stat.st_gid, after_stat.st_mtime_ns),
                         (before_stat.st_mode, before_stat.st_uid, before_stat.st_gid, before_stat.st_mtime_ns))

    def test_missing_legacy_directory_secret_is_not_required(self):
        config = {**self.config, 'IAM_DIRECTORY_SECRET_SOURCE': str(self.directory / 'not-present')}
        module.validate(config, self.values)
        module.commit(self.directory, config, self.values)
        saved = module.check(self.directory, check_port=False, check_compose=False)
        self.assertEqual(saved['IAM_DIRECTORY_SECRET_SOURCE'], config['IAM_DIRECTORY_SECRET_SOURCE'])
        self.assertFalse(Path(config['IAM_DIRECTORY_SECRET_SOURCE']).exists())

    def test_active_secret_cannot_overwrite_legacy_directory_secret(self):
        config = {**self.config, 'IAM_DIRECTORY_SECRET_SOURCE': self.config['IAM_CLIENT_SECRET_SOURCE']}
        with self.assertRaises(ValueError):
            module.validate(config, self.values)

    def test_missing_inputs_do_not_commit_or_start(self):
        with patch.object(module.sys.stdin, 'isatty', return_value=True), patch.object(module, 'prompt', return_value=''), patch.object(module.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]), patch.object(module, 'commit') as commit:
            with self.assertRaises(ValueError):
                module.configure(self.directory, self.directory / 'private')
            commit.assert_not_called()
        self.assertFalse((self.directory / '.env').exists())

    def test_invalid_existing_fernet_not_replaced(self):
        module.commit(self.directory, self.config, self.values)
        key = Path(self.config['IAM_TOKEN_ENCRYPTION_KEY_SOURCE'])
        key.write_text('')
        with patch.object(module.sys.stdin, 'isatty', return_value=True), patch.object(module, 'prompt', side_effect=lambda label, default='', **kw: default):
            with self.assertRaises(ValueError):
                module.configure(self.directory, self.directory / 'private')
        self.assertEqual(key.read_text(), '')

    def test_symlink_secret_rejected(self):
        target = self.directory / 'linked'
        target.symlink_to(self.directory / 'missing')
        bad = {**self.config, 'IAM_CLIENT_SECRET_SOURCE': str(target)}
        with self.assertRaises(ValueError):
            module.validate(bad, self.values)

    def test_partial_write_failure_restores_old_values(self):
        module.commit(self.directory, self.config, self.values)
        old = {path: path.read_bytes() for path in [self.directory / '.env', *(Path(self.config[name]) for name in module.FILES)]}
        real_replace = module.os.replace
        count = 0
        def fail_second(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic write failure')
            return real_replace(source, target)
        values = {**self.values, 'IAM_CLIENT_SECRET_SOURCE': 'changed-synthetic-only'}
        with patch.object(module.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                module.commit(self.directory, self.config, values)
        for path, content in old.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.directory / '.configuration-in-progress').exists())

    def test_incomplete_commit_marker_blocks_start(self):
        (self.directory / '.configuration-in-progress').write_text('synthetic')
        with self.assertRaises(ValueError):
            module.check(self.directory, check_port=False, check_compose=False)

    def test_world_readable_secret_rejected(self):
        module.commit(self.directory, self.config, self.values)
        Path(self.config['IAM_CLIENT_SECRET_SOURCE']).chmod(0o444)
        with self.assertRaises(ValueError):
            module.check(self.directory, check_port=False, check_compose=False)

    def test_directory_url_cannot_inject_dotenv_or_contain_control_characters(self):
        for value in ("https://example.invalid/a'b", 'https://example.invalid/a\\b', 'https://example.invalid/a b', 'https://example.invalid/a\tb'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate({**self.config, 'IAM_DIRECTORY_ORG_URL': value}, self.values)

    def test_database_names_cannot_change_database_url_authority(self):
        for field in ('POSTGRES_USER', 'POSTGRES_DB'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.validate({**self.config, field: 'invalid:password'}, self.values)

    def test_compose_uses_verified_file_not_exported_old_configuration(self):
        with patch.dict(module.os.environ, {**{name: 'old-exported-value' for name in module.ALL_FIELDS}, 'DOCKER_CONTEXT': 'remote'}):
            environment = module.docker_env()
        for name in module.ALL_FIELDS:
            self.assertNotIn(name, environment)
        self.assertNotIn('DOCKER_CONTEXT', environment)
        self.assertEqual(environment['DOCKER_HOST'], 'unix:///var/run/docker.sock')

    def test_cloud_limit_defaults_are_written_and_custom_values_preserved(self):
        module.commit(self.directory,self.config,self.values)
        config=module.read_config(self.directory / '.env')
        for name,bounds in module.LIMITS.items():
            self.assertEqual(config[name],str(bounds[0]))
        config['CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES']='4096'
        config['CLOUD_TASK_EMPLOYEE_LIMIT']='1'
        module.validate(config,self.values)
        module.commit(self.directory,config,self.values)
        current=module.read_config(self.directory / '.env')
        self.assertEqual(current['CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES'],'4096')
        self.assertEqual(current['CLOUD_TASK_EMPLOYEE_LIMIT'],'1')

    def test_cloud_limits_reject_bad_range_and_dotenv_injection(self):
        for name,(_default,minimum,maximum) in module.LIMITS.items():
            for value in (str(minimum-1),str(maximum+1),'NaN',"1'\nBAD=x"):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    module.validate({**self.config,name:value},self.values)


if __name__ == '__main__':
    unittest.main()
