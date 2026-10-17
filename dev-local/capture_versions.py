"""Capture local image digests, installed Python packages and source fingerprints.

This reads only the specifically named development images/containers and source
files. It never reads .env, application tables or container environment values.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from install_dependencies import validate_pins

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent


def command(*parts):
    return subprocess.check_output(parts, cwd=HERE, text=True).strip()


def matching_repository_digest(reference, digests):
    # docker tag/load may add company-local aliases before the official origin.
    # Such an alias is not a pullable repository; preserve the requested source.
    repository = reference.rsplit(':', 1)[0]
    def canonical(value):
        return value.removeprefix('docker.io/library/').removeprefix('docker.io/')
    matches = [value for value in digests if '@sha256:' in value
               and canonical(value.split('@', 1)[0]) == canonical(repository)]
    if len(matches) != 1:
        raise RuntimeError('base image lacks one unambiguous source repository digest: ' + reference)
    return matches[0]


def describe_running_image(container, reference, inspect_command=command):
    """Compare platform contents, not mutable multi-platform attestation indexes."""
    running_ref = inspect_command('docker', 'inspect', '--format', '{{.Image}}', container)
    descriptor_id = inspect_command('docker', 'image', 'inspect', '--format', '{{.Id}}', reference)
    running_platform = inspect_command('docker', 'image', 'inspect', '--platform', 'linux/amd64',
                                       '--format', '{{.Id}}', running_ref)
    tagged_platform = inspect_command('docker', 'image', 'inspect', '--platform', 'linux/amd64',
                                      '--format', '{{.Id}}', reference)
    if running_platform != tagged_platform:
        raise RuntimeError('running local service platform image differs from the image being captured: ' + reference)
    return {
        'reference': reference,
        'platform': 'linux/amd64',
        'container_image_reference': running_ref,
        'running_platform_image_id': running_platform,
        'tag_descriptor_id': descriptor_id,
        'tag_platform_image_id': tagged_platform,
    }


def main():
    images = {}
    for variable, reference in (
        ('LOCAL_POSTGRES_IMAGE', 'postgres:16'),
        ('LOCAL_REDIS_IMAGE', 'redis:7'),
        ('LOCAL_PYTHON_IMAGE', 'python:3.12-slim'),
        ('LOCAL_NGINX_IMAGE', 'nginx:alpine'),
        ('LOCAL_DSH_NODE_IMAGE', 'node:22.20.0-bookworm-slim'),
    ):
        digests = json.loads(command('docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', reference))
        if not digests:
            raise RuntimeError('base image has no repository digest: ' + reference)
        images[variable] = matching_repository_digest(reference, digests)
    compose = ('docker', 'compose', '-p', 'megmeet-dev-local', '--env-file', 'defaults.env', '-f', 'compose.yaml')
    service_images = {}
    for service, reference in (
        ('gateway', 'megmeet-dev-gateway:local'),
        ('iam-directory', 'megmeet-dev-gateway:local'),
        ('mock-company', 'megmeet-dev-mock:local'),
        ('postgres', 'postgres:16'),
        ('redis', 'redis:7'),
        ('edge', 'nginx:alpine'),
        ('dsh', 'megmeet-dev-dsh:0.1.6-alpha.2-upstream-ui'),
    ):
        container = command(*compose, 'ps', '-q', service)
        if not container or '\n' in container:
            raise RuntimeError('expected one running local service: ' + service)
        service_images[service] = describe_running_image(container, reference)
    app_images = {
        service_images[service]['reference']: service_images[service]['tag_platform_image_id']
        for service in ('gateway', 'mock-company', 'dsh')
    }
    packages = command(*compose, 'exec', '-T', 'gateway', 'python', '-m', 'pip', 'freeze')
    version = command(*compose, 'exec', '-T', 'gateway', 'python', '--version')
    dependency_target = json.loads(command(*compose, 'exec', '-T', 'gateway', 'python', '-c',
        'import json,platform,sys; print(json.dumps({"os":platform.system(),"machine":platform.machine(),"python":list(sys.version_info[:2])}))'))
    requirements = SOURCE / 'services/gateway/requirements.txt'
    validate_pins(requirements.read_text(encoding='utf-8'), packages)
    package_bytes = (packages + '\n').encode('utf-8')
    files = [SOURCE / 'services/gateway/requirements.txt']
    files += sorted((SOURCE / 'services/gateway/app').glob('*.py'))
    files += sorted((SOURCE / 'db/migrations').glob('*.sql'))
    files += [path for path in sorted((HERE / 'dsh').iterdir()) if path.is_file()]
    files += [path for path in sorted((HERE / 'dsh' / 'assets').iterdir()) if path.is_file()]
    manifest = {
        'scope': 'local synthetic stack only; production image equality NOT VERIFIED',
        'base_images': images, 'application_image_ids': app_images, 'python': version,
        'service_image_identity': service_images,
        'dependency_target': dependency_target,
        'dependency_lock_sha256': hashlib.sha256(package_bytes).hexdigest(),
        'source_sha256': {
            path.relative_to(SOURCE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        },
        'native_dsh_runtime': {
            'role': 'legacy Linux development probe; current employee desktop runs the separately locked Windows runtime',
            'version': '0.1.6-alpha.2', 'channel': 'official prerelease; exact npm lock',
            'source': 'https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.6-alpha.2',
            'actual_cli_version': command(*compose, 'exec', '-T', 'dsh', 'dsh', '--version'),
            'local_presentation_overlay': 'MEGMEET supplied images/title; Preview and internal-testing notice disabled with SHA256 guards',
            'release_manifest': json.loads(command(*compose, 'exec', '-T', 'dsh', 'cat', '/opt/megmeet/release-manifest.json')),
            'model_transport': 'chat-completions; synthetic local fixture only',
            'production_upgrade': 'NOT_DEPLOYED',
        },
    }
    desktop_root = SOURCE / 'desktop-local'
    desktop_runtime = desktop_root / 'runtime'
    desktop_manifests = {}
    for name in ('runtime-manifest.json', 'release-manifest.json', 'brand-adaptation.json'):
        path = desktop_runtime / name
        if not path.is_file():
            raise RuntimeError('current Windows desktop runtime manifest is missing: ' + name)
        desktop_manifests[name] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'content': json.loads(path.read_text(encoding='utf-8-sig')),
        }
    manifest['windows_desktop_runtime'] = {
        'role': 'current native employee desktop development runtime',
        'manifests': desktop_manifests,
        'package_version': json.loads((desktop_root / 'package.json').read_text(encoding='utf-8'))['version'],
        'company_sso_and_personal_key': 'SYNTHETIC_LOCAL_ONLY',
        'production_deployment': 'NOT_DEPLOYED',
    }
    output = HERE / 'locks'
    output.mkdir(exist_ok=True)
    (output / 'images.env').write_text(
        'LOCAL_GATEWAY_PORT=28090\n' + ''.join(f'{key}={value}\n' for key, value in images.items()), encoding='utf-8')
    (output / 'requirements.installed.txt').write_bytes(package_bytes)
    (output / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('LOCAL_RUNTIME_VERSIONS_CAPTURED: PASS')
    print('PRODUCTION_IMAGE_EQUALITY: NOT_VERIFIED')


if __name__ == '__main__':
    main()
