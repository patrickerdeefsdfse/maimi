"""Local renderer only. Never connects, reads TLS files, installs, or starts services."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
ROLE_NAMES = ("A1", "A2")
TEMPLATES = {
    'haproxy.cfg.in': 'haproxy.cfg',
    'keepalived.conf.in': 'keepalived.conf',
    'maimi-web-entry-direct.service.in': 'maimi-web-entry-direct.service',
    'maimi-web-vip.service.in': 'maimi-web-vip.service',
    'check_entry.py.in': 'check_entry.py',
}
GLOBAL_FACTS = (
    'vip_reserved_and_float_allowed', 'same_layer2_verified',
    'vrrp_and_gratuitous_arp_verified', 'vrid_unused_on_layer2_verified',
    'gateway_internal_traffic_policy_local_verified', 'both_ap_gateway_endpoints_verified',
    'ap_interconnect_18080_verified', 'private_adapter_port_18080_unused_verified',
    'public_ports_80_443_handover_reviewed',
)
ROLE_FACTS = (
    'interface_assignment_verified', 'tls_name_chain_key_expiry_verified',
    'system_trust_or_public_ca_verified', 'software_and_loadcredential_verified',
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    return (isinstance(value, str) and bool(re.fullmatch(r'/[A-Za-z0-9_./-]+', value))
            and '..' not in value.split('/') and '//' not in value and value != '/')


def validate(data):
    problems = []
    def need(condition, name):
        if not condition:
            problems.append(name)
    need(isinstance(data, dict), 'INPUT_OBJECT_REQUIRED')
    if not isinstance(data, dict):
        return problems
    need(data.get('schema') == 'maimi-direct-entry-input/v1', 'SCHEMA')
    domain = data.get('hostname')
    need(isinstance(domain, str) and len(domain) <= 253 and
         all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
             for label in domain.split('.')), 'HTTPS_HOSTNAME_REQUIRED')
    try:
        virtual_ip = ipaddress.IPv4Address(data.get('vip',''))
    except (ValueError,TypeError):
        return problems + ['VALID_VIP_REQUIRED']
    phase=data.get('phase','validated')
    need(phase in ('prepare_https','validated'),'PHASE')
    deferred={'vrrp_and_gratuitous_arp_verified','vrid_unused_on_layer2_verified'} if phase=='prepare_https' else set()
    for key in GLOBAL_FACTS:
        if key not in deferred:need(data.get(key) is True, key)
    vrid = data.get('vrid')
    need(type(vrid) is int and 1 <= vrid <= 255, 'VRID_REQUIRED_1_TO_255')
    try:
        gateway = ipaddress.IPv4Address(data.get('gateway_cluster_ip', ''))
        service_network = ipaddress.IPv4Network(data.get('service_cidr',''))
        need(gateway in service_network and gateway not in (service_network.network_address,service_network.broadcast_address),
             'GATEWAY_SERVICE_IP_IN_APPROVED_SERVICE_CIDR')
    except (ipaddress.AddressValueError, ValueError, TypeError):
        problems.append('GATEWAY_SERVICE_IP_REQUIRED')
    roles = data.get('roles')
    if not isinstance(roles, dict):
        return problems + ['ROLES_OBJECT_REQUIRED']
    if set(roles) != set(ROLE_NAMES):
        return problems + ['ONLY_A1_A2_ENTRY_ROLES']
    try:
        addresses = {k:str(ipaddress.IPv4Address(roles[k]['address'])) for k in ROLE_NAMES}
    except (TypeError,KeyError,ValueError):
        return problems + ['TWO_VALID_IPV4_addresses_REQUIRED']
    need(len(set(addresses.values()) | {str(virtual_ip)}) == 3, 'AP_AND_VIP_MUST_BE_DISTINCT')
    prefixes = []
    networks = []
    for role, address in addresses.items():
        item = roles.get(role)
        if not isinstance(item, dict):
            problems.append(role + ':ROLE_OBJECT_REQUIRED')
            continue
        need(item.get('address') == address, role + ':EXPECTED_DIRECT_ADDRESS')
        for key in ROLE_FACTS:
            if phase=='prepare_https' and key=='software_and_loadcredential_verified':continue
            need(item.get(key) is True, role + ':' + key)
        iface = item.get('interface')
        need(isinstance(iface, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}', iface)),
             role + ':VERIFIED_INTERFACE_REQUIRED')
        prefix = item.get('node_prefix')
        vip_prefix = item.get('vip_prefix')
        if type(prefix) is int and 1 <= prefix <= 30 and type(vip_prefix) is int and 1 <= vip_prefix <= 32:
            network = ipaddress.IPv4Network(address + '/' + str(prefix), strict=False)
            need(all(ipaddress.IPv4Address(ip) in network and
                     ipaddress.IPv4Address(ip) not in (network.network_address, network.broadcast_address)
                     for ip in (*addresses.values(), str(virtual_ip))),
                 role + ':AP_AND_VIP_ONLINK_IN_VERIFIED_NETWORK')
            need(vip_prefix in (prefix, 32), role + ':VIP_PREFIX_MUST_MATCH_NODE_OR_APPROVED_32')
            networks.append(str(network))
            prefixes.append(vip_prefix)
        else:
            problems.append(role + ':VERIFIED_NODE_AND_VIP_PREFIX_REQUIRED')
        for key in ('tls_pem_path', 'haproxy_binary', 'haproxy_library_dir', 'keepalived_binary'):
            need(safe_path(item.get(key)), role + ':' + key.upper() + '_REQUIRED_SAFE_ABSOLUTE_PATH')
        need(item.get('health_ca_file') is None or safe_path(item.get('health_ca_file')),
             role + ':CA_FILE_MUST_BE_NULL_OR_PUBLIC_CA_PATH')
    if len(networks) == 2:
        need(networks[0] == networks[1], 'AP_ONLINK_NETWORKS_MUST_MATCH')
        need(prefixes[0] == prefixes[1], 'VIP_PREFIX_MUST_MATCH_ON_BOTH_APS')
    return problems


def render(data):
    problems = validate(data)
    if problems:
        raise ValueError('UNVERIFIED_INPUT:' + ','.join(problems))
    result = {}
    addresses = {role:data['roles'][role]['address'] for role in ROLE_NAMES}
    for role, node_ip in addresses.items():
        item = data['roles'][role]
        values = {
            'ROLE': role, 'NODE_IP': node_ip, 'HOSTNAME':data['hostname'], 'APPLICATION_VIP':data['vip'],
            'A1_IP':addresses['A1'], 'A2_IP':addresses['A2'],
            'PEER_IP': addresses['A2' if role == 'A1' else 'A1'],
            'PRIORITY': '150' if role == 'A1' else '100',
            'GATEWAY_CLUSTER_IP': data['gateway_cluster_ip'],
            'A1_GATEWAY_BACKEND': data['gateway_cluster_ip']+':8080' if role=='A1' else addresses['A1']+':30080',
            'A2_GATEWAY_BACKEND': data['gateway_cluster_ip']+':8080' if role=='A2' else addresses['A2']+':30080',
            'INTERFACE': item['interface'], 'VIP_PREFIX': str(item['vip_prefix']),
            'VRID': str(data['vrid']), 'TLS_PEM_PATH': item['tls_pem_path'],
            'HAPROXY_BINARY': item['haproxy_binary'],
            'HAPROXY_LIBRARY_DIR': item['haproxy_library_dir'],
            'KEEPALIVED_BINARY': item['keepalived_binary'],
            'KEEPALIVED_LIBRARY_DIR': item.get('keepalived_library_dir',str(Path(item['keepalived_binary']).parent.parent/'lib/x86_64-linux-gnu')).replace('\\','/'),
            'CA_FILE_LITERAL': repr(item['health_ca_file']),
        }
        for source, destination in TEMPLATES.items():
            text = (ROOT / source).read_text(encoding='utf-8')
            for key, value in values.items():
                text = text.replace('{{' + key + '}}', value)
            if re.search(r'{{[^{}]+}}', text):
                raise ValueError('UNRESOLVED_TEMPLATE_TOKEN:' + source)
            if destination.endswith('.py'):
                compile(text, destination, 'exec')
            result[role + '/' + destination] = text.encode('utf-8')
    return result


def unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('DUPLICATE_JSON_KEY')
        out[key] = value
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'input.example.json')
    parser.add_argument('--output-directory', type=Path, help='Directory for rendered candidates')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-input', action='store_true')
    mode.add_argument('--output-label')
    args = parser.parse_args()
    input_path = args.input.resolve(strict=True)
    if input_path.suffix.lower() != '.json':
        raise ValueError('INPUT_MUST_BE_JSON')
    raw = input_path.read_bytes()
    if len(raw) > 32768:
        raise ValueError('INPUT_TOO_LARGE')
    data = json.loads(raw, object_pairs_hook=unique_pairs)
    problems = validate(data)
    report = {
        'schema': 'maimi-direct-entry-render/v1', 'status': 'INPUT_PENDING' if problems else 'INPUT_COMPLETE',
        'missing_or_invalid': problems, 'server_mutations': 0, 'network_requests': 0,
        'certificate_files_read': False, 'native_configuration_syntax_tested': False,
        'deployment_ready': False,
    }
    if args.check_input or problems:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if problems else 0
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,50}', args.output_label):
        raise ValueError('OUTPUT_LABEL_REQUIRED_SAFE_NAME')
    generated = (args.output_directory or Path.cwd() / 'generated').resolve()
    if generated.is_symlink() or (generated.exists() and generated.resolve() != generated):
        raise ValueError('GENERATED_DIRECTORY_LINK_REFUSED')
    target = generated / args.output_label
    if target.exists() or target.is_symlink():
        raise ValueError('OUTPUT_EXISTS_NO_OVERWRITE')
    files = render(data)
    target.mkdir(parents=True, exist_ok=False)
    for filename, body in files.items():
        path = target / filename
        path.parent.mkdir(exist_ok=True)
        with path.open('xb') as stream:
            stream.write(body)
    report.update({
        'status': 'CANDIDATES_RENDERED_NOT_INSTALLED', 'input_sha256': sha(raw),
        'output': str(target),
        'files': {name: {'bytes': len(body), 'sha256': sha(body)} for name, body in files.items()},
        'limitations': ['INPUT_ASSERTIONS_ARE_NOT_REMOTE_EVIDENCE',
                        'NO_NATIVE_HAPROXY_KEEPALIVED_OR_SYSTEMD_CHECK',
                        'NO_TLS_NAS_K3S_APP_OR_VIP_RUNTIME_TEST'],
    })
    with (target / 'manifest.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
