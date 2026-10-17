"""Runs only inside the synthetic, network-none Office candidate container."""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time


def main():
    expected = json.loads(Path('/probe-expected.json').read_text())
    for name, digest in expected.items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Candidate source differs: ' + name)
    print('CANDIDATE_OFFICE_SOURCE: MATCH', flush=True)
    report = {'image_scope': 'offline synthetic container, fixed image id', 'stages': []}
    commands = [
        ('engine-formats', [sys.executable, '-I', '/opt/office/test_engine.py']),
        ('native-live-office', ['/usr/local/bin/node', '/probe/probe-native-host.mjs']),
    ]
    env = dict(os.environ, OFFICE_PROBE_RUNTIME='/opt/dsh', OFFICE_PROBE_ENGINE='/opt/office',
               OFFICE_PROBE_PYTHON='/opt/office/venv/bin/python', OFFICE_PROBE_NODE='/usr/local/bin/node',
               OFFICE_PROBE_GUARD='/probe-network-guard.mjs', OFFICE_PROBE_FULL_FORMATS='1')
    failed = False
    for name, argv in commands:
        print('LINUX_OFFICE_STAGE:', name, flush=True)
        start = time.monotonic()
        try:
            run = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=240)
            print(run.stdout[-20000:], end='', flush=True)
            print(run.stderr[-20000:], end='', flush=True)
            success = run.returncode == 0 and 'skipped' not in run.stderr.lower()
            row = {'name': name, 'exit_code': run.returncode, 'seconds': round(time.monotonic()-start, 3), 'ok': success}
        except subprocess.TimeoutExpired:
            row = {'name': name, 'seconds': round(time.monotonic()-start, 3), 'ok': False, 'error': 'bounded-timeout'}
        report['stages'].append(row)
        failed |= not row['ok']
        print('LINUX_OFFICE_STAGE_RESULT:', json.dumps(row), flush=True)
    report['max_child_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    for name in ('memory.peak', 'memory.current', 'memory.events', 'memory.max', 'memory.swap.max'):
        path = Path('/sys/fs/cgroup') / name
        if path.is_file():
            report[name] = path.read_text().strip()
    report['ok'] = not failed
    print('LINUX_OFFICE_REPORT:', json.dumps(report), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
