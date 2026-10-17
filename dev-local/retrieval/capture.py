"""Lock only the two installed local retrieval images, never service secrets."""
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent


def command(*args):
    return subprocess.check_output(args, text=True, cwd=HERE).strip()


def main():
    entries, lines = {}, ['# Local retrieval images locked by actual registry digest.']
    for service, variable, tag in [
        ('milvus', 'LOCAL_MILVUS_IMAGE', 'milvusdb/milvus:v2.6.24'),
        ('elasticsearch', 'LOCAL_ELASTIC_IMAGE', 'docker.elastic.co/elasticsearch/elasticsearch:8.19.21'),
    ]:
        digests = json.loads(command('docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', tag))
        if not digests:
            raise RuntimeError('missing registry digest: ' + service)
        expected = command('docker', 'image', 'inspect', '--platform', 'linux/amd64', '--format', '{{.Id}}', tag)
        container = f'megmeet-dev-retrieval-{service}-1'
        running_ref = command('docker', 'inspect', '--format', '{{.Image}}', container)
        actual = command('docker', 'image', 'inspect', '--platform', 'linux/amd64', '--format', '{{.Id}}', running_ref)
        if actual != expected:
            raise RuntimeError('running platform differs: ' + service)
        entries[service] = {'tag': tag, 'digest': digests[0], 'platform_image_id': actual}
        lines.append(f'{variable}={digests[0]}')
    (HERE / 'images.env').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    manifest = {
        'scope': 'local synthetic development, not production sizing',
        'images': entries,
        'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(HERE.iterdir()) if p.suffix in ('.py', '.yaml', '.sh', '.env')},
        'production_deployed': False,
    }
    (HERE / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print('RETRIEVAL_IMAGE_LOCK: PASS')


if __name__ == '__main__':
    main()
