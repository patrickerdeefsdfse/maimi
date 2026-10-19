"""Read-only Linux/Docker sampler. No container exec, credentials or file bodies."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid

SCHEMA = 'maimi-observability-host-sample-v1'
NAMESPACE = 'megmeet-personal-server-v1'
PROJECT = 'megmeet-desktop-server'
DOCKER_CONFIG_DIRECTORY = '/opt/maimi-monitor/docker-empty-config'
SERVICES = frozenset({'gateway', 'personal-cloud-manager', 'office-service', 'service-broker',
                      'postgres', 'redis', 'downloads', 'retention-worker'})
MANAGED = 'com.megmeet.personal-cloud.manager'
OWNER = 'com.megmeet.personal-cloud.owner'
ID = re.compile(r'^[a-f0-9]{64}$')
OWNER_ID = re.compile(r'^[a-f0-9]{32}$')
FORMAT = ('{"id":{{json .Id}},"pid":{{json .State.Pid}},"state":{{json .State.Status}},'
          '"started":{{json .State.StartedAt}},"restarts":{{json .RestartCount}},'
          '"oom_killed":{{json .State.OOMKilled}},"health":{{with index .State "Health"}}{{json (index . "Status")}}{{else}}null{{end}},'
          '"manager":{{json (index .Config.Labels "com.megmeet.personal-cloud.manager")}},'
          '"owner":{{json (index .Config.Labels "com.megmeet.personal-cloud.owner")}},'
          '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
          '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
          '"network_mode":{{json (index .HostConfig "NetworkMode")}}}')
VOLUME_FORMAT = ('{"name":{{json .Name}},"created":{{json .CreatedAt}},"driver":{{json .Driver}},'
                 '"mountpoint":{{json .Mountpoint}},"has_options":{{if .Options}}true{{else}}false{{end}},'
                 '"manager":{{json (index .Labels "com.megmeet.personal-cloud.manager")}},'
                 '"owner":{{json (index .Labels "com.megmeet.personal-cloud.owner")}}}')


class Unavailable(Exception):
    pass


def integer(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise Unavailable('INVALID_COUNTER')
    return value


def bounded(path, cap=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unavailable('INVALID_KERNEL_FILE')
        value = os.read(fd, cap + 1)
        if len(value) > cap:
            raise Unavailable('KERNEL_FILE_LIMIT')
        return value.decode('ascii', errors='strict')
    finally:
        os.close(fd)


def optional(function, *args):
    try:
        return function(*args)
    except (OSError, ValueError, UnicodeError, Unavailable):
        return None


def value(path):
    text = bounded(path, 128).strip()
    return integer(int(text))


def limit(path):
    text = bounded(path, 128).strip()
    return (None, True) if text == 'max' else (integer(int(text)), False)


def keyed(path, allowed):
    result = {}
    for line in bounded(path).splitlines():
        key, number = line.split()
        if key in allowed:
            if key in result: raise Unavailable('DUPLICATE_COUNTER')
            result[key] = integer(int(number))
    return result


def process_start(pid):
    text = bounded(Path('/proc') / str(pid) / 'stat', 4096)
    end = text.rfind(')')
    if end < 0: raise Unavailable('PROCESS_IDENTITY')
    return integer(int(text[end + 2:].split()[19]))


def cgroup_directory(pid):
    matches = [line[3:] for line in bounded(Path('/proc') / str(pid) / 'cgroup', 8192).splitlines()
               if line.startswith('0::')]
    if len(matches) != 1: raise Unavailable('CGROUP_V2_REQUIRED')
    relative = Path(matches[0])
    if not relative.is_absolute() or '..' in relative.parts or len(str(relative)) > 1024:
        raise Unavailable('CGROUP_PATH')
    target = Path('/sys/fs/cgroup') / str(relative).lstrip('/')
    if target == Path('/sys/fs/cgroup') or any(p.is_symlink() for p in (target, *target.parents)):
        raise Unavailable('CGROUP_PATH')
    return target


def network(pid):
    result = dict(rx_bytes=0, tx_bytes=0, rx_packets=0, tx_packets=0)
    for line in bounded(Path('/proc') / str(pid) / 'net/dev', 65536).splitlines()[2:]:
        name, text = line.split(':', 1)
        values = text.split()
        if len(values) != 16: raise Unavailable('NETWORK_COUNTER_SHAPE')
        if name.strip() != 'lo':
            for key, index in (('rx_bytes', 0), ('rx_packets', 1), ('tx_bytes', 8), ('tx_packets', 9)):
                result[key] += integer(int(values[index]))
    return result


def io_counters(path):
    result = dict(read_bytes=0, write_bytes=0, read_operations=0, write_operations=0)
    names = {'rbytes': 'read_bytes', 'wbytes': 'write_bytes', 'rios': 'read_operations', 'wios': 'write_operations'}
    for line in bounded(path).splitlines():
        fields = line.split()
        if not re.fullmatch(r'[0-9]+:[0-9]+', fields[0]): raise Unavailable('IO_COUNTER_SHAPE')
        for pair in fields[1:]:
            key, raw = pair.split('=', 1)
            if key in names: result[names[key]] += integer(int(raw))
    return result


def empty_docker_config_directory():
    """No default Docker home/config fallback, and no reading config file bodies."""
    path = Path(DOCKER_CONFIG_DIRECTORY)
    if not path.is_absolute() or '..' in path.parts: raise Unavailable('DOCKER_CONFIG_DIRECTORY')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            entry = os.fstat(fd)
            # Root-owned sticky temp parents support only synthetic tests. The
            # production constant is fixed under /opt, not caller/environment input.
            if entry.st_uid != 0 or (entry.st_mode & 0o022 and not entry.st_mode & stat.S_ISVTX):
                raise Unavailable('DOCKER_CONFIG_PARENT')
        entry = os.fstat(fd)
        if entry.st_uid != 0 or entry.st_gid != 0 or stat.S_IMODE(entry.st_mode) != 0o555:
            raise Unavailable('DOCKER_CONFIG_MODE')
        with os.scandir(fd) as entries:
            if next(entries, None) is not None: raise Unavailable('DOCKER_CONFIG_NOT_EMPTY')
        return str(path)
    finally: os.close(fd)


class Docker:
    """Exact local socket; metadata-only command forms; bounded pipes/deadline."""
    @staticmethod
    def permitted(args):
        if args[:5] == ['inspect', '--type', 'container', '--format', FORMAT]:
            return bool(args[5:]) and len(args[5:]) <= 4096 and all(isinstance(v, str) and ID.fullmatch(v) for v in args[5:])
        if args[:4] == ['volume', 'inspect', '--format', VOLUME_FORMAT]:
            return bool(args[4:]) and len(args[4:]) <= 4096 and all(isinstance(v, str) and re.fullmatch(
                re.escape(NAMESPACE) + '-[a-f0-9]{32}-(state|files)', v) for v in args[4:])
        if args in (['info', '--format', '{{json .DockerRootDir}}'],
                    ['volume', 'ls', '--filter', 'label=' + MANAGED + '=' + NAMESPACE, '--format', '{{json .Name}}']):
            return True
        return any(args == ['ps', '-a', '--no-trunc', '--filter', 'label=' + label, '--format', '{{json .ID}}']
                   for label in (MANAGED + '=' + NAMESPACE, 'com.docker.compose.project=' + PROJECT))

    def run(self, args, deadline, cap=2 * 1024**2):
        if not self.permitted(args) or cap != 2 * 1024**2:
            raise Unavailable('COMMAND_SCOPE')
        remaining = min(4, deadline - time.monotonic())
        if remaining <= 0: raise Unavailable('SAMPLE_DEADLINE')
        config = empty_docker_config_directory()
        proc = subprocess.Popen(['docker', '--config', config, '--host', 'unix:///var/run/docker.sock', *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        selector = selectors.DefaultSelector(); buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
        expires = time.monotonic() + remaining
        try:
            for stream in buffers:
                os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                wait = expires - time.monotonic()
                if wait <= 0: raise Unavailable('SAMPLE_DEADLINE')
                for key, _ in selector.select(wait):
                    block = os.read(key.fd, 65536)
                    if not block: selector.unregister(key.fileobj); continue
                    buffers[key.fileobj].extend(block)
                    if sum(map(len, buffers.values())) > cap: raise Unavailable('METADATA_OUTPUT_LIMIT')
            proc.wait(timeout=max(.01, expires - time.monotonic()))
            if proc.returncode: raise Unavailable('DOCKER_METADATA_UNAVAILABLE')
            return bytes(buffers[proc.stdout])
        finally:
            selector.close()
            if proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            proc.wait(timeout=.25)
            for stream in buffers: stream.close()

    def rows(self, args, deadline, maximum=4096):
        rows = [line for line in self.run(args, deadline).splitlines() if line.strip()]
        if len(rows) > maximum: raise Unavailable('METADATA_ROW_LIMIT')
        return [json.loads(line) for line in rows]

    def inspect(self, ids, deadline):
        if any(not isinstance(v, str) or not ID.fullmatch(v) for v in ids): raise Unavailable('CONTAINER_ID')
        return self.rows(['inspect', '--type', 'container', '--format', FORMAT, *ids], deadline, len(ids)) if ids else []

    def catalog(self, deadline):
        ids = set()
        for label in (MANAGED + '=' + NAMESPACE, 'com.docker.compose.project=' + PROJECT):
            ids.update(self.rows(['ps', '-a', '--no-trunc', '--filter', 'label=' + label, '--format', '{{json .ID}}'], deadline))
        if len(ids) > 4096: raise Unavailable('CONTAINER_COUNT_LIMIT')
        return self.inspect(sorted(ids), deadline)


def classify(raw):
    if not isinstance(raw.get('id'), str) or not ID.fullmatch(raw['id']): raise Unavailable('CONTAINER_ID')
    owner = raw.get('owner')
    if raw.get('manager') == NAMESPACE and isinstance(owner, str) and OWNER_ID.fullmatch(owner):
        return 'cloud-worker', owner, NAMESPACE
    if raw.get('project') == PROJECT and isinstance(raw.get('service'), str) and raw['service'] in SERVICES:
        return raw['service'], None, None
    return None


def empty_metrics():
    return {'cpu': {'usage_usec': None, 'quota_cores': None, 'cores': None},
            'memory': {'current_bytes': None, 'max_bytes': None, 'limit_unlimited': None, 'events': None},
            'pids': {'current': None, 'max': None, 'limit_unlimited': None, 'events': None},
            'io': {'read_bytes': None, 'write_bytes': None, 'read_operations': None, 'write_operations': None,
                   'read_bytes_per_second': None, 'write_bytes_per_second': None},
            'network': {'rx_bytes': None, 'tx_bytes': None, 'rx_packets': None, 'tx_packets': None,
                        'rx_bytes_per_second': None, 'tx_bytes_per_second': None, 'shared_namespace': None}}


def quota(path):
    raw, period = bounded(path, 128).split()
    period = integer(int(period))
    if not period: raise Unavailable('CPU_PERIOD')
    return None if raw == 'max' else integer(int(raw)) / period


def container_sample(raw, now):
    kind = classify(raw)
    if kind is None: return None
    service, owner, namespace = kind
    started = raw.get('started')
    if not isinstance(started, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z', started):
        raise Unavailable('CONTAINER_START_TIME')
    row = dict(container_id=raw['id'], service_class=service, owner_ref=owner, managed_namespace=namespace,
               state=raw['state'] if raw.get('state') in ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead') else 'unknown',
               started_at=started, restart_count=integer(raw['restarts']), sampled_at=now,
               health=raw.get('health') if raw.get('health') in ('healthy','unhealthy','starting') else None,
               oom_killed=raw.get('oom_killed') if type(raw.get('oom_killed')) is bool else None,
               identity=None, sample_status='not_running', metrics=empty_metrics())
    if row['state'] != 'running': return row
    try:
        pid = integer(raw['pid'])
        if not pid: raise Unavailable('PROCESS_IDENTITY')
        start = process_start(pid); group = cgroup_directory(pid); before = group.stat()
        ns = optional(os.stat, Path('/proc') / str(pid) / 'ns/net')
        row['identity'] = dict(pid_start_ticks=start, cgroup_inode=before.st_ino, cgroup_device=before.st_dev,
                               netns_inode=ns.st_ino if ns else None)
        metrics = row['metrics']
        cpu = optional(keyed, group / 'cpu.stat', {'usage_usec'})
        metrics['cpu']['usage_usec'] = cpu.get('usage_usec') if cpu else None
        metrics['cpu']['quota_cores'] = optional(quota, group / 'cpu.max')
        for section, current_file, maximum in (('memory', 'memory.current', 'memory.max'), ('pids', 'pids.current', 'pids.max')):
            metrics[section]['current_bytes' if section == 'memory' else 'current'] = optional(value, group / current_file)
            maximum_value = optional(limit, group / maximum)
            if maximum_value is not None:
                metrics[section]['max_bytes' if section == 'memory' else 'max'], metrics[section]['limit_unlimited'] = maximum_value
            metrics[section]['events'] = optional(keyed, group / (section + '.events'),
                {'low', 'high', 'max', 'oom', 'oom_kill', 'oom_group_kill'} if section == 'memory' else {'max'})
        block = optional(io_counters, group / 'io.stat')
        if block is not None: metrics['io'].update(block)
        if raw.get('network_mode') != 'host':
            net = optional(network, pid)
            if net is not None: metrics['network'].update(net)
            if isinstance(raw.get('network_mode'), str) and raw['network_mode'].startswith('container:'):
                metrics['network']['shared_namespace'] = True
        else: metrics['network']['shared_namespace'] = True
        after = group.stat()
        if process_start(pid) != start or cgroup_directory(pid) != group or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise Unavailable('PROCESS_IDENTITY_CHANGED')
        row['sample_status'] = 'available' if all(metrics[k][f] is not None for k, f in
            (('cpu', 'usage_usec'), ('memory', 'current_bytes'), ('pids', 'current'))) else 'partial'
    except (OSError, ValueError, Unavailable, IndexError):
        row.update(identity=None, metrics=empty_metrics(), sample_status='unavailable')
    return row


def host_sample():
    try:
        lines = bounded('/proc/meminfo').splitlines(); memory = {}
        for line in lines:
            key, text = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable'):
                fields = text.split()
                if len(fields) != 2 or fields[1] != 'kB': raise Unavailable('MEMINFO_UNITS')
                memory[key] = integer(int(fields[0])) * 1024
    except (OSError, ValueError, Unavailable): memory = {}
    ticks = None
    try:
        fields = bounded('/proc/stat').splitlines()[0].split()
        if fields[0] != 'cpu' or len(fields) < 9: raise Unavailable('CPU_STAT')
        values = [integer(int(v)) for v in fields[1:9]]
        ticks = {'total': sum(values), 'idle': values[3] + values[4]}
    except (OSError, ValueError, Unavailable, IndexError): pass
    disks = {}
    for key, path in (('root', '/'), ('data', '/data')):
        fs = optional(os.statvfs, path)
        disks[key] = {'total_bytes': fs.f_blocks * fs.f_frsize if fs else None,
                      'available_bytes': fs.f_bavail * fs.f_frsize if fs else None}
    return {'cpu_count': os.cpu_count(), 'cpu_ticks': ticks, 'cpu_busy_percent': None,
            'memory_total_bytes': memory.get('MemTotal'), 'memory_available_bytes': memory.get('MemAvailable'), 'disks': disks}


def add_rates(current, previous):
    if previous is None: return
    dt = (current['monotonic_ns'] - previous['monotonic_ns']) / 1e9
    if not 0 < dt <= 300 or current['collector_instance'] != previous['collector_instance']: return
    a, b = current['host']['cpu_ticks'], previous['host']['cpu_ticks']
    if a and b and a['total'] > b['total'] and a['idle'] >= b['idle']:
        value = 100 * (1 - (a['idle'] - b['idle']) / (a['total'] - b['total']))
        if 0 <= value <= 100: current['host']['cpu_busy_percent'] = value
    old = {row['container_id']: row for row in previous['containers']}
    for row in current['containers']:
        before = old.get(row['container_id'])
        if (not before or row['identity'] is None or row['identity'] != before['identity'] or
                row['started_at'] != before['started_at'] or row['restart_count'] != before['restart_count']): continue
        for section, count, rate, scale in (('cpu', 'usage_usec', 'cores', 1e6),
                ('io', 'read_bytes', 'read_bytes_per_second', 1), ('io', 'write_bytes', 'write_bytes_per_second', 1),
                ('network', 'rx_bytes', 'rx_bytes_per_second', 1), ('network', 'tx_bytes', 'tx_bytes_per_second', 1)):
            current_count, old_count = row['metrics'][section][count], before['metrics'][section][count]
            if section == 'network' and (row['metrics']['network']['shared_namespace'] is not False
                    or before['metrics']['network']['shared_namespace'] is not False): continue
            if current_count is not None and old_count is not None and current_count >= old_count:
                row['metrics'][section][rate] = (current_count - old_count) / scale / dt


class Collector:
    def __init__(self, docker=None, storage=None):
        self.docker = docker or Docker(); self.storage = storage
        self.previous = None; self.sequence = 0; self.instance = uuid.uuid4().hex

    def sample(self, budget_seconds=10):
        if sys.platform != 'linux' or os.geteuid() != 0: raise Unavailable('LINUX_HOST_ROOT_REQUIRED')
        if not 1 <= budget_seconds <= 30: raise Unavailable('SAMPLE_BUDGET')
        started = time.monotonic(); now = time.time(); deadline = started + budget_seconds
        result = dict(schema=SCHEMA, collector_instance=self.instance, sample_id=uuid.uuid4().hex,
                      sequence=self.sequence, collected_at=now, monotonic_ns=time.monotonic_ns(),
                      stale_after_seconds=120, status='available', host=host_sample(), containers=[],
                      owner_storage=[], coverage={'docker': 'unknown', 'storage': 'not_enabled'})
        try:
            raw = self.docker.catalog(deadline)
            selected = [row for row in raw if classify(row) is not None]
            for row in selected:
                if time.monotonic() >= deadline: raise Unavailable('SAMPLE_DEADLINE')
                result['containers'].append(container_sample(row, now))
            final = self.docker.inspect([row['id'] for row in selected], deadline)
            stable = {row['id']: row for row in final}
            for index, source in enumerate(selected):
                if stable.get(source['id']) != source:
                    result['containers'][index].update(identity=None, metrics=empty_metrics(), sample_status='identity_changed')
            namespaces = {}
            for row in result['containers']:
                if row['identity'] and row['identity']['netns_inode'] is not None:
                    namespaces.setdefault(row['identity']['netns_inode'], []).append(row)
            for group in namespaces.values():
                for row in group:
                    net = row['metrics']['network']
                    if net['shared_namespace'] is not True: net['shared_namespace'] = len(group) > 1
            result['coverage']['docker'] = 'available'
            if self.storage is not None and time.monotonic() < deadline:
                try:
                    result['owner_storage'] = self.storage.sample(self.docker, deadline)
                    result['coverage']['storage'] = 'metadata_estimates'
                except (OSError, ValueError, Unavailable, subprocess.SubprocessError, KeyError):
                    result['coverage']['storage'] = 'unavailable'
        except (OSError, ValueError, Unavailable, subprocess.SubprocessError, KeyError):
            result['status'] = 'partial'; result['coverage']['docker'] = 'unavailable'
            result['containers'] = []
        add_rates(result, self.previous)
        self.sequence += 1; self.previous = json.loads(json.dumps(result, allow_nan=False))
        return result


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    if sys.argv[1:]: raise SystemExit('NO_ARGUMENTS_EXPECTED')
    print(json.dumps(Collector().sample(), separators=(',', ':'), allow_nan=False))
