"""Offline operator-preflight checks; all Docker, disk, port and TLS I/O is mocked."""
from contextlib import ExitStack, contextmanager
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location(
    'server_preflight', Path(__file__).with_name('server-preflight.py'))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

GiB = 1024 ** 3
PROBE_ERROR = {'dns': False, 'tls_verified': False, 'timeout_or_probe_error': True}


@contextmanager
def synthetic_server(*, free_bytes=20 * GiB, nginx_state='running', tls_rows=None, api_range='1.51 1.24'):
    """Reject unexpected commands, file reads and real connections in these tests."""
    commands = {
        ('docker', 'version', '--format', '{{.Server.Version}}'): (0, '28.5.1'),
        ('docker', 'version', '--format', '{{.Server.APIVersion}} {{.Server.MinAPIVersion}}'): (0, api_range),
        ('docker', 'compose', 'version', '--short'): (0, '2.40.0'),
        ('docker', 'image', 'inspect', '--help'): (0, '--platform'),
        ('docker', 'compose', 'up', '--help'): (0, '--wait-timeout'),
        ('docker', 'inspect', '--format', '{{.State.Status}}', 'nginx-https'): (0, nginx_state),
        ('docker', 'inspect', '--format',
         '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}',
         'nginx-https'): (0, 'common-net'),
    }

    def metadata_only(*args):
        if args not in commands:
            raise AssertionError(f'unexpected command: {args}')
        return commands[args]

    def inventory_path(name):
        value = MagicMock()
        if name == '/proc/meminfo':
            value.read_text.return_value = (
                'MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n'
                'SwapTotal: 0 kB\nSwapFree: 0 kB\n')
        elif name == '/data':
            value.is_dir.return_value = True
        else:
            raise AssertionError(f'unexpected file read: {name}')
        return value

    with ExitStack() as stack:
        stack.enter_context(patch.object(preflight, 'run', side_effect=metadata_only))
        stack.enter_context(patch.object(preflight, 'Path', side_effect=inventory_path))
        stack.enter_context(patch.object(preflight.platform, 'system', return_value='Linux'))
        stack.enter_context(patch.object(preflight.platform, 'machine', return_value='x86_64'))
        stack.enter_context(patch.object(preflight.sys, 'version_info', (3, 12)))
        stack.enter_context(patch.object(preflight.shutil, 'disk_usage',
                                         return_value=SimpleNamespace(free=free_bytes)))
        stack.enter_context(patch.object(preflight.socket, 'socket', return_value=MagicMock()))
        for name in ('getaddrinfo', 'create_connection'):
            stack.enter_context(patch.object(preflight.socket, name,
                                             side_effect=AssertionError('real network forbidden')))
        process = stack.enter_context(patch.object(preflight.subprocess, 'run',
                                      side_effect=AssertionError('real subprocess forbidden')))
        probe = stack.enter_context(patch.object(preflight, 'bounded_tls_probe',
                side_effect=(lambda host: tls_rows[host]) if tls_rows is not None
                else AssertionError('default inventory must not probe TLS')))
        yield probe, process


class InventoryTests(unittest.TestCase):
    def test_manager_api_must_be_in_engine_supported_range_despite_new_cli(self):
        for api_range, supported in (
            ('1.46 1.24', False), ('1.47 1.24', True), ('1.51 1.47', True),
            ('1.51 1.48', False), ('invalid', False), ('1.51', False),
        ):
            with self.subTest(api_range=api_range), synthetic_server(api_range=api_range):
                report = preflight.inspect()
            self.assertTrue(report['checks']['docker_platform_image_inspection'])
            self.assertEqual(report['checks']['docker_engine_supports_manager_api_1_47'], supported)
            self.assertEqual(report['result'], 'PREFLIGHT_PASS_NOT_LOGIN_ACCEPTANCE' if supported else 'STOP_CHECK_FAILED')

    def test_default_is_metadata_only_and_does_not_claim_login_acceptance(self):
        with synthetic_server() as (probe, process):
            report = preflight.inspect()
        probe.assert_not_called()
        process.assert_not_called()
        self.assertNotIn('dns_tls', report)
        self.assertFalse(report['configuration_values_checked'])
        self.assertFalse(report['iam_login_and_tokenkey_verified'])
        self.assertEqual(report['result'], 'PREFLIGHT_PASS_NOT_LOGIN_ACCEPTANCE')

    def test_disk_threshold_uses_raw_bytes_even_when_display_rounds_up(self):
        for free_bytes in (int(9.96 * GiB), 10 * GiB - 1):
            with self.subTest(free_bytes=free_bytes), synthetic_server(free_bytes=free_bytes):
                report = preflight.inspect()
            self.assertFalse(report['checks']['data_free_at_least_10_GiB_for_pilot'])
            self.assertEqual(report['result'], 'STOP_CHECK_FAILED')
        with synthetic_server(free_bytes=10 * GiB):
            report = preflight.inspect()
        self.assertTrue(report['checks']['data_free_at_least_10_GiB_for_pilot'])

    def test_exited_nginx_fails_even_with_common_network_membership(self):
        with synthetic_server(nginx_state='exited'):
            report = preflight.inspect()
        self.assertTrue(report['checks']['container_nginx_common_net'])
        self.assertFalse(report['checks']['known_container_nginx_running'])
        self.assertEqual(report['result'], 'STOP_CHECK_FAILED')

    def test_network_inventory_uses_only_fixed_hosts_and_both_results(self):
        rows = {host: {'dns': True, 'tls_verified': True} for host in preflight.HOSTS}
        rows[preflight.HOSTS[1]] = {'dns': True, 'tls_verified': False}
        with synthetic_server(tls_rows=rows) as (probe, _):
            report = preflight.inspect(network=True)
        self.assertEqual([call.args[0] for call in probe.call_args_list], list(preflight.HOSTS))
        self.assertEqual(report['dns_tls'], rows)
        self.assertFalse(report['checks']['dns_tls_' + preflight.HOSTS[1]])
        self.assertEqual(report['result'], 'STOP_CHECK_FAILED')

    def test_child_timeout_result_fails_overall_preflight(self):
        rows = {host: dict(PROBE_ERROR) for host in preflight.HOSTS}
        with synthetic_server(tls_rows=rows):
            report = preflight.inspect(network=True)
        self.assertEqual(report['result'], 'STOP_CHECK_FAILED')
        self.assertTrue(all(row['timeout_or_probe_error'] for row in report['dns_tls'].values()))

    def test_public_entry_hairpin_failure_does_not_block_backend_preflight(self):
        for public_row in ({'dns': False, 'tls_verified': False},
                           {'dns': True, 'tls_verified': False}, PROBE_ERROR):
            rows = {host: {'dns': True, 'tls_verified': True} for host in preflight.HOSTS}
            rows[preflight.PUBLIC_ENTRY_HOST] = dict(public_row)
            with self.subTest(public_row=public_row), synthetic_server(tls_rows=rows):
                report = preflight.inspect(network=True)
            self.assertEqual(report['result'], 'PREFLIGHT_PASS_NOT_LOGIN_ACCEPTANCE')
            self.assertEqual(report['dns_tls'][preflight.PUBLIC_ENTRY_HOST], public_row)
            self.assertNotIn('dns_tls_' + preflight.PUBLIC_ENTRY_HOST, report['checks'])
            self.assertTrue(any('advisory_only' in warning for warning in report['warnings']))
            self.assertFalse(report['browser_public_entry_verified'])
            self.assertFalse(report['container_outbound_verified'])
            self.assertFalse(report['iam_login_and_tokenkey_verified'])

    def test_browser_origin_success_never_masks_required_backend_failure(self):
        for host in ('iam.megmeet.com', 'tokenkey.megmeet.com'):
            rows = {name: {'dns': True, 'tls_verified': True} for name in preflight.HOSTS}
            rows[host] = {'dns': True, 'tls_verified': False}
            with self.subTest(host=host), synthetic_server(tls_rows=rows):
                report = preflight.inspect(network=True)
            self.assertEqual(report['result'], 'STOP_CHECK_FAILED')
            self.assertFalse(report['checks']['dns_tls_' + host])

    def test_server_tls_success_does_not_claim_browser_container_or_login_verified(self):
        rows = {host: {'dns': True, 'tls_verified': True} for host in preflight.HOSTS}
        with synthetic_server(tls_rows=rows):
            report = preflight.inspect(network=True)
        self.assertEqual(report['result'], 'PREFLIGHT_PASS_NOT_LOGIN_ACCEPTANCE')
        self.assertFalse(report['browser_public_entry_verified'])
        self.assertFalse(report['container_outbound_verified'])
        self.assertFalse(report['iam_login_and_tokenkey_verified'])


class BoundedProbeTests(unittest.TestCase):
    def test_child_timeout_is_bounded_and_reported_without_exception_detail(self):
        with patch.object(preflight.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired(['synthetic'], 12)) as process:
            result = preflight.bounded_tls_probe(preflight.HOSTS[0])
        self.assertEqual(result, PROBE_ERROR)
        args, kwargs = process.call_args
        self.assertEqual(args[0][-2:], ['--tls-probe', preflight.HOSTS[0]])
        self.assertEqual(kwargs['timeout'], 12)
        self.assertTrue(kwargs['capture_output'])

    def test_well_formed_boolean_rows_are_preserved(self):
        for row in ({'dns': True, 'tls_verified': True},
                    {'dns': True, 'tls_verified': False},
                    {'dns': False, 'tls_verified': False}):
            with self.subTest(row=row), patch.object(preflight.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0, stdout=json.dumps(row))):
                self.assertEqual(preflight.bounded_tls_probe(preflight.HOSTS[0]), row)

    def test_malformed_or_nonzero_child_rows_fail_closed(self):
        for code, body in (
            (1, '{"dns":true,"tls_verified":true}'),
            (0, 'not-json'), (0, '[]'), (0, '{"dns":true}'),
            (0, '{"dns":1,"tls_verified":true}'),
            (0, '{"dns":true,"tls_verified":true,"unexpected":"omitted"}'),
        ):
            with self.subTest(code=code, body=body), patch.object(preflight.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=code, stdout=body)):
                self.assertEqual(preflight.bounded_tls_probe(preflight.HOSTS[0]), PROBE_ERROR)


if __name__ == '__main__':
    unittest.main()
