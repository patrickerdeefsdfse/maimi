#!/usr/bin/env python3
"""Read-only deployment inventory: no credentials, service changes or data rows."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import os
import re

OUTBOUND_HOSTS = ('iam.megmeet.com', 'tokenkey.megmeet.com')
PUBLIC_ENTRY_HOST = 'platform-ai.megmeet.com'
HOSTS = (*OUTBOUND_HOSTS, PUBLIC_ENTRY_HOST)


def tls_probe(hostname):
    row = {'dns': False, 'tls_verified': False}
    try:
        socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        row['dns'] = True
        with socket.create_connection((hostname, 443), timeout=6) as tcp:
            with ssl.create_default_context().wrap_socket(tcp, server_hostname=hostname):
                row['tls_verified'] = True
    except (OSError, ssl.SSLError):
        pass
    return row


def bounded_tls_probe(hostname):
    try:
        process = subprocess.run([sys.executable, str(Path(__file__).absolute()), '--tls-probe', hostname],
                                 capture_output=True, text=True, timeout=12)
        data = json.loads(process.stdout) if process.returncode == 0 else None
        if isinstance(data, dict) and set(data) == {'dns', 'tls_verified'} and all(type(v) is bool for v in data.values()):
            return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {'dns': False, 'tls_verified': False, 'timeout_or_probe_error': True}


def run(*args):
    environment = dict(os.environ, DOCKER_HOST='unix:///var/run/docker.sock')
    for name in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(name, None)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15, env=environment)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ''


def own_gateway():
    code, output = run('docker', 'ps', '--filter', 'label=com.docker.compose.project=megmeet-desktop-server',
                       '--filter', 'label=com.docker.compose.service=gateway', '--format', '{{.Ports}}')
    return code == 0 and any('127.0.0.1:18091->8080/tcp' in line for line in output.splitlines())


def inspect(*, network=False):
    report = {'scope': 'read-only; no credentials; no service changes', 'checks': {}, 'warnings': []}
    checks, warnings = report['checks'], report['warnings']
    checks['linux_amd64'] = platform.system() == 'Linux' and platform.machine() in ('x86_64', 'amd64')
    checks['python_3_10_or_newer'] = sys.version_info >= (3, 10)
    report['cpu_count'] = os.cpu_count()
    memory = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                memory[key + '_MiB'] = int(value.split()[0]) // 1024
    except (OSError, ValueError):
        warnings.append('memory_information_unavailable')
    report['memory'] = memory
    if memory.get('MemAvailable_MiB', 0) < 2500:
        warnings.append('less_than_2500_MiB_available; check_capacity_before_two_cloud_hosts')
    if Path('/data').is_dir():
        free_bytes = shutil.disk_usage('/data').free
        report['data_free_GiB'] = round(free_bytes / (1024 ** 3), 2)
        checks['data_free_at_least_10_GiB_for_pilot'] = free_bytes >= 10 * 1024 ** 3
    else:
        checks['data_directory_exists'] = False
    code, version = run('docker', 'version', '--format', '{{.Server.Version}}')
    checks['docker_available'] = code == 0 and bool(version)
    report['docker_version'] = version if code == 0 else 'unavailable'
    code, api_range = run('docker', 'version', '--format', '{{.Server.APIVersion}} {{.Server.MinAPIVersion}}')
    parts = api_range.split()
    valid_range = code == 0 and len(parts) == 2 and all(re.fullmatch(r'\d+\.\d+', value) for value in parts)
    checks['docker_engine_supports_manager_api_1_47'] = False
    if valid_range:
        maximum, minimum = (tuple(map(int, value.split('.'))) for value in parts)
        checks['docker_engine_supports_manager_api_1_47'] = minimum <= (1, 47) <= maximum
        report['docker_api_range'] = {'minimum': parts[1], 'maximum': parts[0]}
    else:
        report['docker_api_range'] = 'unavailable'
    code, compose = run('docker', 'compose', 'version', '--short')
    checks['compose_available'] = code == 0 and bool(compose)
    report['compose_version'] = compose if code == 0 else 'unavailable'
    code, help_text = run('docker', 'image', 'inspect', '--help')
    checks['docker_platform_image_inspection'] = code == 0 and '--platform' in help_text
    code, help_text = run('docker', 'compose', 'up', '--help')
    checks['compose_wait_supported'] = code == 0 and '--wait-timeout' in help_text
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 18091))
        report['gateway_port_18091'] = 'free'
        checks['gateway_port_available_or_owned'] = True
    except OSError:
        owned = own_gateway()
        report['gateway_port_18091'] = 'owned_gateway' if owned else 'occupied_unknown'
        checks['gateway_port_available_or_owned'] = owned
    code, state = run('docker', 'inspect', '--format', '{{.State.Status}}', 'nginx-https')
    report['known_nginx_container'] = state if code == 0 else 'not_found'
    if code == 0:
        checks['known_container_nginx_running'] = state == 'running'
        code, memberships = run('docker', 'inspect', '--format',
                                '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}', 'nginx-https')
        checks['container_nginx_common_net'] = code == 0 and 'common-net' in memberships.split()
    else:
        warnings.append('confirm_host_nginx_or_other_https_proxy_before_activation')
    if network:
        # No HTTP requests, tokens or user records. Only DNS and verified TLS.
        # The public callback is reached by the employee's browser. Server
        # hairpin access to that origin is diagnostic, not a backend dependency.
        report['dns_tls'] = {}
        report['required_server_outbound_hosts'] = list(OUTBOUND_HOSTS)
        for hostname in HOSTS:
            row = bounded_tls_probe(hostname)
            report['dns_tls'][hostname] = row
            if hostname in OUTBOUND_HOSTS:
                checks['dns_tls_' + hostname] = row['dns'] and row['tls_verified']
            elif not (row['dns'] and row['tls_verified']):
                warnings.append('public_entry_not_reachable_from_server; '
                                'advisory_only; verify_employee_browser_ingress_separately')
    else:
        warnings.append('server_dns_tls_not_checked; add_--network_on_company_server')
    report['browser_public_entry_verified'] = False
    report['container_outbound_verified'] = False
    report['configuration_values_checked'] = False
    report['iam_login_and_tokenkey_verified'] = False
    report['result'] = 'PREFLIGHT_PASS_NOT_LOGIN_ACCEPTANCE' if all(checks.values()) else 'STOP_CHECK_FAILED'
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', action='store_true', help='check fixed company DNS/TLS only; no HTTP authentication')
    parser.add_argument('--tls-probe', choices=HOSTS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.tls_probe:
        print(json.dumps(tls_probe(args.tls_probe)))
        raise SystemExit(0)
    result = inspect(network=args.network)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(result['checks'].values()) else 1)
