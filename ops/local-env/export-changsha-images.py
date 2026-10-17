"""Export only verified local development image contents; no company connection."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'releases' / 'changsha-personal-cloud-20260918-r9'
WORKER_SOURCE = 'megmeet-personal-dsh:office-candidate'
ENV = dict(os.environ, DOCKER_HOST='unix:///var/run/docker.sock')
ENV.pop('DOCKER_CONTEXT', None)

def docker(*args):
    return subprocess.check_output(['docker', *args], env=ENV, text=True).strip()

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

if os.getenv('WSL_DISTRO_NAME') != 'MegmeetDev':
    raise SystemExit('This export is only for the local MegmeetDev build environment')
DEST.mkdir(parents=True, exist_ok=True)
image_app = json.loads(docker('run', '--rm', '--network', 'none', '--entrypoint', 'python',
    'megmeet-dev-gateway:local', '-c',
    'from pathlib import Path; import hashlib,json; print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("/app/app").glob("*.py")}))'))
source_app = {p.name: digest(p) for p in (ROOT / 'services/gateway/app').glob('*.py')}
if image_app != source_app:
    raise SystemExit('Image application source differs from the reviewed workspace')
print('SERVER_IMAGE_SOURCE: MATCH', flush=True)
runtime_sources = {}
for image, entries in (
    ('megmeet-personal-cloud-manager:local', {
        'services/personal-cloud/manager.py': '/opt/personal-cloud/manager.py'}),
    (WORKER_SOURCE, {
        'services/personal-cloud/worker.mjs': '/opt/personal-cloud/worker.mjs',
        'services/personal-cloud/catalog.mjs': '/opt/personal-cloud/catalog.mjs',
        'services/personal-cloud/history-native.mjs': '/opt/personal-cloud/history-native.mjs',
        'services/personal-cloud/history-server.mjs': '/opt/personal-cloud/history-server.mjs',
        'services/personal-cloud/guard-runtime.mjs': '/opt/personal-cloud/guard-runtime.mjs',
        'dev-local/dsh/enterprise-native.mjs': '/opt/megmeet/enterprise-native.mjs',
        'dev-local/dsh/community-plugins-guard.mjs': '/opt/dsh/node_modules/@dshp/file-change-viewer/megmeet-guarded-host.mjs',
        'dev-local/dsh/workspace-pinned-overlay.mjs': '/opt/megmeet/workspace-pinned-overlay.mjs',
        'dev-local/dsh/office-adapter/index.mjs': '/opt/dsh/office-adapter/index.mjs',
        'dev-local/dsh/office-adapter/adapter.mjs': '/opt/dsh/office-adapter/adapter.mjs',
        'dev-local/dsh/office-adapter/transport.mjs': '/opt/dsh/office-adapter/transport.mjs',
        'office-engine/engine.py': '/opt/office/engine.py',
        'office-engine/requirements-linux.lock.txt': '/opt/office/requirements.lock.txt',
        'office-engine/ocr-config.json': '/opt/office/ocr-config.json',
        'office-engine/ocr-models.json': '/opt/office/ocr-models.json'}),
):
    output = docker('run', '--rm', '--network', 'none', '--entrypoint', 'sha256sum', image, *entries.values())
    actual = {line.split(maxsplit=1)[1].strip(): line.split(maxsplit=1)[0] for line in output.splitlines()}
    for source, target_path in entries.items():
        expected = digest(ROOT / source)
        if actual.get(target_path) != expected:
            raise SystemExit('Personal native image source differs: ' + source)
        runtime_sources[source] = expected
print('PERSONAL_NATIVE_IMAGE_SOURCE: MATCH', flush=True)
community = json.loads(docker('run', '--rm', '--network', 'none', '--entrypoint', 'node',
    WORKER_SOURCE, '--input-type=module', '-e',
    "import fs from 'node:fs';import crypto from 'node:crypto';const r=JSON.parse(fs.readFileSync('/opt/megmeet/release-manifest.json'));const m=JSON.parse(fs.readFileSync('/opt/megmeet/community-manifest.json'));const sha=p=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');for(const p of m.plugins){for(const [f,h] of Object.entries(p.files))if(sha('/opt/dsh/node_modules/'+p.name+'/'+f)!==h)throw Error('Community artifact differs');if(sha('/opt/dsh/node_modules/'+p.name+'/megmeet-guarded-host.mjs')!==r.community.plugins.find(x=>x.name===p.name).guardSha256)throw Error('Community auth guard differs')}console.log(JSON.stringify(r.community));"))
if community['commit'] != json.loads((ROOT/'dev-local/dsh/vendor-community/manifest.json').read_text())['commit']:
    raise SystemExit('Community source commit differs')
print('COMMUNITY_IMAGE_AND_AUTH_GUARDS: MATCH', flush=True)
office_models = json.loads((ROOT / 'office-engine/ocr-models.json').read_text())['models']
if not isinstance(office_models, dict) or not 1 <= len(office_models) <= 10:
    raise SystemExit('Office OCR model roster invalid')
for name, record in office_models.items():
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.onnx', name) or not re.fullmatch(r'[a-f0-9]{64}', record['sha256']):
        raise SystemExit('Office OCR model path/hash invalid')
model_paths = ['/opt/office/ocr-models/' + name for name in office_models]
model_hashes = docker('run', '--rm', '--network', 'none', '--entrypoint', 'sha256sum', WORKER_SOURCE, *model_paths)
actual_models = {line.split(maxsplit=1)[1].strip(): line.split(maxsplit=1)[0] for line in model_hashes.splitlines()}
for name, record in office_models.items():
    if actual_models.get('/opt/office/ocr-models/' + name) != record['sha256']:
        raise SystemExit('Office OCR image artifact differs: ' + name)
office = json.loads(docker('run', '--rm', '--network', 'none', '--entrypoint', '/opt/office/venv/bin/python',
    WORKER_SOURCE, '-I', '-c',
    "import contextlib,importlib,importlib.metadata,json,pathlib,runpy,sys; "
    "ns=runpy.run_path('/opt/office/engine.py'); "
    "lock=pathlib.Path('/opt/office/requirements.lock.txt').read_text().splitlines(); "
    "versions={line.split('==')[0]:importlib.metadata.version(line.split('==')[0]) for line in lock if '==' in line}; "
    "assert all(versions[line.split('==')[0]]==line.split('==')[1] for line in lock if '==' in line), 'Office dependency version differs'; "
    "[importlib.import_module(x) for x in ('docx','pptx','openpyxl','xlrd','pypdf','pypdfium2','rapidocr','PIL')]; "
    "print(json.dumps({'capabilities':ns['capabilities'](),'versions':versions}));"))
if office['capabilities']['protocol'] != 'megmeet-office-v1' or not office['capabilities']['offline_ocr'] or not office['capabilities']['libreoffice']:
    raise SystemExit('Office engine/converter/OCR image component missing')
office['ocr_models'] = office_models
print('OFFICE_ENGINE_ADAPTER_DEPS_AND_OCR: MATCH', flush=True)
images = []
for source, reference in (
    ('megmeet-dev-gateway:local', 'megmeet-ai-gateway:personal-cloud-20260918-r9'),
    ('megmeet-personal-cloud-manager:local', 'megmeet-personal-cloud-manager:20260918-r9'),
    (WORKER_SOURCE, 'megmeet-personal-dsh:20260918-r9'),
    ('postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94', 'megmeet-desktop-postgres:16-20260918'),
    ('redis@sha256:71da9275c5f3fcb97d0fa0c8c5b36cc995327265420f17a04bfd544f458059f7', 'megmeet-desktop-redis:7-20260918'),
):
    image_id = docker('image', 'inspect', '--platform', 'linux/amd64', '--format', '{{.Id}}', source)
    existing = subprocess.run(['docker', 'image', 'inspect', '--platform', 'linux/amd64', '--format', '{{.Id}}', reference],
                              env=ENV, capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() != image_id:
        raise SystemExit('Release tag already refers to different image contents: ' + reference)
    docker('tag', source, reference)
    images.append({'reference': reference, 'platform': 'linux/amd64',
                   'platform_image_id': image_id, 'source_digest': source.split('@')[-1] if '@' in source else image_id})
target = DEST / 'images.tar.gz'
if target.exists():
    raise SystemExit('Export already exists; refusing to overwrite a release')
temporary = DEST / 'images.tar.gz.partial'
with temporary.open('xb') as output:
    proc = subprocess.Popen(['docker', 'save', '--platform', 'linux/amd64', *(item['reference'] for item in images)], env=ENV, stdout=subprocess.PIPE)
    with gzip.GzipFile(filename='', mode='wb', fileobj=output, compresslevel=1, mtime=0) as compressed:
        shutil.copyfileobj(proc.stdout, compressed, length=1024*1024)
    if proc.wait() != 0:
        raise SystemExit('Docker image export failed')
temporary.rename(target)
manifest = {'scope': 'local tested candidate; company deployment not performed', 'images': images,
            'archive': {'file': target.name, 'sha256': digest(target), 'bytes': target.stat().st_size},
            'application_source_sha256': source_app,
            'community': community,
            'office': office,
            'personal_native_source_sha256': runtime_sources}
(DEST / 'images-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print('OFFLINE_SERVER_IMAGES: PASS', flush=True)
print('IMAGE_ARCHIVE_BYTES:', target.stat().st_size, flush=True)
