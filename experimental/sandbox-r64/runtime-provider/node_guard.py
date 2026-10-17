"""Privileged same-host guard. Only bounded Docker projections, never full inspect.

This process belongs on the execution node, outside employee workers. The Docker
socket is admin authority and must not be mounted into the DSH or task container.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import PurePosixPath
import re
import tarfile

from provider import ProviderError, IDENT, TASK, REQUIRED_NODE_FACTS

NETWORK = 'maimi-sandbox-r64'
BRIDGE = 'maimi-sbx-r64'
TMPFS = {
    '/workspace': 'rw,nosuid,nodev,size=536870912,mode=1777',
    '/tmp': 'rw,nosuid,nodev,size=134217728,mode=1777',
    '/opt/opensandbox': 'rw,nosuid,nodev,size=134217728,mode=0755',
    '/run': 'rw,nosuid,nodev,noexec,size=16777216,mode=0755',
}

# Exact keys only: no Config.Env, command lines, host mount sources, or arbitrary labels.
PROJECTION = r'''{"id":{{json .Id}},"image":{{json .Config.Image}},"runtime":{{json .HostConfig.Runtime}},"network":{{json .HostConfig.NetworkMode}},"networks":[{{range $i,$n := .NetworkSettings.Networks}}{{if $i}}{{end}}{{json $n.NetworkID}},{{end}}null],"readonly":{{json .HostConfig.ReadonlyRootfs}},"privileged":{{json .HostConfig.Privileged}},"cap_add":{{json .HostConfig.CapAdd}},"cap_drop":{{json .HostConfig.CapDrop}},"security":{{json .HostConfig.SecurityOpt}},"pids":{{json .HostConfig.PidsLimit}},"memory":{{json .HostConfig.Memory}},"cpu":{{json .HostConfig.NanoCpus}},"pid_mode":{{json .HostConfig.PidMode}},"ipc_mode":{{json .HostConfig.IpcMode}},"devices":{{json .HostConfig.Devices}},"tmpfs":{{json .HostConfig.Tmpfs}},"mounts":{{json .Mounts}},"ports":{{json .HostConfig.PortBindings}},"running":{{json .State.Running}},"paused":{{json .State.Paused}},"task":{{json (index .Config.Labels "maimi_task_id")}},"controller":{{json (index .Config.Labels "maimi_controller")}},"sandbox_id":{{json (index .Config.Labels "opensandbox.io/id")}},"execd_init":{{range .Config.Env}}{{if eq . "EXECD_INIT=1"}}true{{end}}{{end}}}'''
# Mount projection must not include Source (host paths) or arbitrary options.
PROJECTION = PROJECTION.replace('{{json .Mounts}}', '[{{range .Mounts}}{"type":{{json .Type}},"destination":{{json .Destination}},"rw":{{json .RW}}},{{end}}null]')
PROJECTION = ('{{$hasInit := false}}{{range .Config.Env}}{{if eq . "EXECD_INIT=1"}}{{$hasInit = true}}{{end}}{{end}}'
              + PROJECTION.replace('{{range .Config.Env}}{{if eq . "EXECD_INIT=1"}}true{{end}}{{end}}', '{{json $hasInit}}'))
# Docker Engines may omit optional HostConfig keys. Missing is data, not a Go
# template evaluation failure; the validator still rejects unsafe/missing limits.
PROJECTION = re.sub(r'\.HostConfig\.([A-Za-z]+)', r'(index .HostConfig "\1")', PROJECTION)


async def run(argv, *, limit=65536, timeout=15):
    """Bound bytes while reading, kill on overrun; never relay stderr."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'HOME': '/nonexistent'},
    )
    chunks, total = [], 0
    try:
        async with asyncio.timeout(timeout):
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ProviderError('NODE_OUTPUT_TOO_LARGE')
                chunks.append(chunk)
            code = await process.wait()
        if code != 0:
            raise ProviderError('NODE_COMMAND_FAILED')
        return b''.join(chunks)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise ProviderError('NODE_COMMAND_UNCONFIRMED') from None


def validate_container(value, sandbox_id, task, profile, namespace, network_id, paused=False):
    expected = {
        'image': profile.image, 'runtime': 'runsc', 'network': NETWORK,
        'readonly': True, 'privileged': False, 'pids': profile.pids,
        'memory': profile.memory_mib * 1024 * 1024, 'cpu': profile.cpu_millis * 1000000,
        'running': True, 'paused': paused, 'task': task, 'controller': namespace,
        'sandbox_id': sandbox_id, 'execd_init': True,
    }
    if any(value.get(k) != v for k, v in expected.items()):
        raise ProviderError('SANDBOX_BOUNDARY_CHANGED')
    if not re.fullmatch(r'[a-f0-9]{64}', value.get('id', '')):
        raise ProviderError('SANDBOX_IDENTITY_INVALID')
    if value.get('cap_add') not in (None, []) or value.get('cap_drop') != ['ALL']:
        raise ProviderError('SANDBOX_CAPABILITIES_INVALID')
    if value.get('security') != ['no-new-privileges=true']:
        raise ProviderError('SANDBOX_SECURITY_INVALID')
    if value.get('pid_mode') not in ('', None) or value.get('ipc_mode') not in ('private', ''):
        raise ProviderError('SANDBOX_NAMESPACE_INVALID')
    if value.get('devices') not in (None, []) or value.get('networks') != [network_id, None]:
        raise ProviderError('SANDBOX_NETWORK_INVALID')
    if value.get('tmpfs') != TMPFS:
        raise ProviderError('SANDBOX_STORAGE_INVALID')
    mounts = [m for m in value.get('mounts', []) if m is not None]
    if any(m.get('type') != 'tmpfs' or m.get('destination') not in TMPFS for m in mounts):
        raise ProviderError('SANDBOX_HOST_MOUNT_REJECTED')
    ports = value.get('ports')
    if not isinstance(ports, dict) or set(ports) != {'44772/tcp', '8080/tcp'}:
        raise ProviderError('SANDBOX_PORTS_INVALID')
    if any(not isinstance(v, list) or len(v) != 1 or v[0].get('HostIp') != '127.0.0.1'
           or not str(v[0].get('HostPort', '')).isdigit() for v in ports.values()):
        raise ProviderError('SANDBOX_PORT_EXPOSURE_REJECTED')
    return value['id']


def validate_network(value):
    if (value.get('Name') != NETWORK or value.get('Driver') != 'bridge'
        or value.get('Internal') is not True or value.get('EnableIPv6') is not False
        or value.get('Options') != {'com.docker.network.bridge.name': BRIDGE,
                                   'com.docker.network.bridge.enable_icc': 'false'}
        or not re.fullmatch(r'[a-f0-9]{64}', value.get('Id', ''))):
        raise ProviderError('EXECUTION_NETWORK_NOT_ISOLATED')
    return value['Id']


def expected_firewall_rules():
    iif = {'match': {'op': '==', 'left': {'meta': {'key': 'iifname'}}, 'right': BRIDGE}}
    established = {'match': {'op': 'in', 'left': {'ct': {'key': 'state'}},
                              'right': {'set': ['established', 'related']}}}
    return {
        'input': [[iif, established, {'accept': None}], [iif, {'drop': None}]],
        'forward': [[iif, {'drop': None}]],
    }


def validate_firewall(raw):
    # Dedicated table only. Reject added rules/accepts and absent hooks; no names
    # or raw rules from unrelated company firewall tables are ever requested.
    entries = raw.get('nftables', [])
    chains, rules = {}, {'input': [], 'forward': []}
    for entry in entries:
        if 'metainfo' in entry or 'table' in entry:
            continue
        if 'chain' in entry:
            c = entry['chain']
            if c.get('family') != 'inet' or c.get('table') != 'maimi_sandbox_r64' or c.get('name') not in rules:
                raise ProviderError('SANDBOX_FIREWALL_INVALID')
            if (c.get('type'), c.get('hook'), c.get('prio'), c.get('policy')) != ('filter', c['name'], -200, 'accept'):
                raise ProviderError('SANDBOX_FIREWALL_INVALID')
            chains[c['name']] = True
        elif 'rule' in entry:
            r = entry['rule']
            if r.get('family') != 'inet' or r.get('table') != 'maimi_sandbox_r64' or r.get('chain') not in rules:
                raise ProviderError('SANDBOX_FIREWALL_INVALID')
            rules[r['chain']].append(r.get('expr'))
        else:
            raise ProviderError('SANDBOX_FIREWALL_INVALID')
    if set(chains) != set(rules) or rules != expected_firewall_rules():
        raise ProviderError('SANDBOX_FIREWALL_INVALID')


class LinuxNodeGuard:
    def __init__(self, *, server_policy_check, capacity_check, snapshot_acceptance_check, runner=run):
        self.server_policy_check = server_policy_check
        self.capacity_check = capacity_check
        self.snapshot_acceptance_check = snapshot_acceptance_check
        self.run = runner

    async def network(self):
        fmt = '{"Name":{{json .Name}},"Id":{{json .Id}},"Driver":{{json .Driver}},"Internal":{{json .Internal}},"EnableIPv6":{{json .EnableIPv6}},"Options":{{json .Options}}}'
        raw = await self.run(['docker', 'network', 'inspect', '--format', fmt, NETWORK])
        return validate_network(json.loads(raw))

    async def probe(self):
        facts = {k: False for k in REQUIRED_NODE_FACTS}
        try:
            runtimes = json.loads(await self.run(['docker', 'info', '--format', '{{json .Runtimes}}']))
            facts['runsc_registered'] = 'runsc' in runtimes
        except Exception:
            pass
        try:
            await self.network()
            facts['internal_network_verified'] = True
            facts['peer_traffic_blocked'] = True
            validate_firewall(json.loads(await self.run(['nft', '-j', 'list', 'table', 'inet', 'maimi_sandbox_r64'])))
            facts['host_traffic_blocked'] = True
        except Exception:
            pass
        try:
            policy = await self.server_policy_check()
            if policy.get('policy') == 'maimi-osb-1.1.1-r64-v1' and policy.get('verified') is True:
                for k in ('server_policy_pinned', 'rootfs_readonly_enforced', 'bounded_tmpfs_enforced',
                          'no_host_business_mounts', 'api_private_and_authenticated'):
                    facts[k] = True
        except Exception:
            pass
        try:
            facts['capacity_reserved'] = await self.capacity_check() is True
            # Must be backed by real runsc snapshot/cleanup/isolation acceptance
            # tied to current binaries/config. A mock pass is not accepted proof.
            facts['snapshot_supported'] = await self.snapshot_acceptance_check() is True
        except Exception:
            pass
        return facts

    async def _projection(self, sandbox_id):
        if not isinstance(sandbox_id, str) or not IDENT.fullmatch(sandbox_id):
            raise ProviderError('INVALID_SANDBOX_ID')
        return json.loads(await self.run(['docker', 'inspect', '--type', 'container', '--format', PROJECTION,
                                          'sandbox-' + sandbox_id]))

    async def verify_sandbox(self, sandbox_id, task, profile, namespace, paused=False):
        if not TASK.fullmatch(task) or not IDENT.fullmatch(namespace):
            raise ProviderError('INVALID_TASK_SCOPE')
        value = await self._projection(sandbox_id)
        return validate_container(value, sandbox_id, task, profile, namespace, await self.network(), paused)

    async def verify_task_absent(self, task, namespace):
        if not TASK.fullmatch(task) or not IDENT.fullmatch(namespace):
            raise ProviderError('INVALID_TASK_SCOPE')
        raw = await self.run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=maimi_task_id=' + task,
                              '--filter', 'label=maimi_controller=' + namespace], limit=8192)
        return not raw.strip()

    async def snapshot_output(self, sandbox_id, task, profile, namespace, path, max_bytes):
        container_id = await self.verify_sandbox(sandbox_id, task, profile, namespace)
        pause_attempted = False
        try:
            pause_attempted = True
            await self.run(['docker', 'pause', container_id])
            if await self.verify_sandbox(sandbox_id, task, profile, namespace, paused=True) != container_id:
                raise ProviderError('SANDBOX_CHANGED_DURING_SNAPSHOT')
            raw = await self.run(['docker', 'cp', container_id + ':' + path, '-'],
                                 limit=max_bytes + 65536, timeout=30)
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
                members = archive.getmembers()
                if len(members) != 1:
                    raise ProviderError('SNAPSHOT_NOT_ONE_FILE')
                item = members[0]
                if (not item.isfile() or item.linkname or item.name != PurePosixPath(path).name
                    or item.size > max_bytes or item.size < 0 or item.pax_headers):
                    raise ProviderError('SNAPSHOT_FILE_REJECTED')
                data = archive.extractfile(item).read(max_bytes + 1)
                if len(data) != item.size:
                    raise ProviderError('SNAPSHOT_SIZE_CHANGED')
            if await self.verify_sandbox(sandbox_id, task, profile, namespace, paused=True) != container_id:
                raise ProviderError('SANDBOX_CHANGED_DURING_SNAPSHOT')
            return data
        finally:
            # A lost pause ACK is uncertain too. Try to resume only the exact
            # owned ID; any uncertainty propagates, retaining the task budget.
            if pause_attempted:
                value = await self._projection(sandbox_id)
                if value.get('id') != container_id or value.get('task') != task or value.get('controller') != namespace:
                    raise ProviderError('SNAPSHOT_RESUME_UNCONFIRMED')
                if value.get('paused') is True:
                    await self.run(['docker', 'unpause', container_id])
                if await self.verify_sandbox(sandbox_id, task, profile, namespace) != container_id:
                    raise ProviderError('SNAPSHOT_RESUME_UNCONFIRMED')
