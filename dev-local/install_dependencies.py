"""Choose a local full dependency lock only when it matches the source input."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys

REQUIREMENTS_KEY = 'services/gateway/requirements.txt'
PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9][A-Za-z0-9_.+!-]*)$')


def target():
    return {'os': platform.system(), 'machine': platform.machine(), 'python': list(sys.version_info[:2])}


def pins(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError('dependency input must contain exact package==version pins only')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in result:
            raise ValueError('duplicate dependency pin: ' + name)
        result[name] = match[2]
    if not result:
        raise ValueError('dependency input is empty')
    return result


def validate_pins(source_text, installed_text):
    requested, installed = pins(source_text), pins(installed_text)
    if any(installed.get(name) != version for name, version in requested.items()):
        raise ValueError('installed package snapshot does not satisfy current source requirements')


def select_requirements(source, directory):
    lock = directory / 'requirements.installed.txt'
    manifest = directory / 'runtime-manifest.json'
    if not lock.exists() and not manifest.exists():
        pins(source.read_text(encoding='utf-8'))
        return source, False
    if not lock.is_file() or not manifest.is_file():
        raise ValueError('incomplete dependency lock: both lock file and manifest are required')
    metadata = json.loads(manifest.read_text(encoding='utf-8'))
    expected = metadata.get('source_sha256', {}).get(REQUIREMENTS_KEY)
    if expected != hashlib.sha256(source.read_bytes()).hexdigest():
        raise ValueError('source requirements changed; explicitly regenerate and verify the local dependency lock')
    if metadata.get('dependency_lock_sha256') != hashlib.sha256(lock.read_bytes()).hexdigest():
        raise ValueError('dependency lock checksum mismatch')
    if metadata.get('dependency_target') != target():
        raise ValueError('dependency lock belongs to another OS, architecture or Python minor version')
    validate_pins(source.read_text(encoding='utf-8'), lock.read_text(encoding='utf-8'))
    return lock, True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--locks', type=Path, required=True)
    args = parser.parse_args()
    selected, locked = select_requirements(args.source, args.locks)
    print('DEPENDENCY_INSTALL_MODE: ' + ('LOCAL_EXACT_LOCK' if locked else 'INITIAL_SOURCE_REQUIREMENTS'), flush=True)
    command = [sys.executable, '-m', 'pip', 'install', '--no-cache-dir']
    if locked:
        # All transitives are explicit, so the resolver must not add newer ones.
        command.append('--no-deps')
    subprocess.run([*command, '-r', str(selected)], check=True)
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)


if __name__ == '__main__':
    main()
