"""Synthetic r11 packaging and immutable candidate selection; never real Docker/export."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
from zipfile import ZipFile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]/'local-env'
NATIVE = [
    'services/personal-cloud/'+name for name in ('manager.py','worker.mjs','catalog.mjs','history-native.mjs',
    'history-server.mjs','guard-runtime.mjs','task-authority.mjs','task-control-plugin.mjs')]
NATIVE += ['toolkit/session-tools.mjs','toolkit/adapter/index.mjs','dev-local/dsh/enterprise-native.mjs',
           'office-engine/engine.py', *['dev-local/dsh/office-adapter/'+name for name in ('index.mjs','adapter.mjs','transport.mjs')]]


@pytest.fixture
def package(tmp_path):
    root = tmp_path/'new-system'
    folder = root/'ops/local-env'
    folder.mkdir(parents=True)
    for name in ('package-r11.py','export-r11-images.py'):
        shutil.copyfile(SCRIPTS/name, folder/name)
    spec = importlib.util.spec_from_file_location('synthetic_package_r11', folder/'package-r11.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for relative in NATIVE + ['services/gateway/app/connector_web_reader.py', 'services/gateway/app/main.py',
                             'db/migrations/001.sql','ops/changsha-desktop/connectors.env.example',
                             'ops/changsha-desktop/render-nginx-entry.py','ops/changsha-desktop/.env.example']:
        path = root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic public source\n')
    vendor = root/'dev-local/dsh/vendor-community'
    (vendor/'file-change-viewer').mkdir(parents=True)
    readme = vendor/'file-change-viewer/README.md'
    readme.write_text('synthetic public plugin documentation\n')
    manifest = {'schema':1,'plugins':[{'directory':'file-change-viewer',
                  'files':{'README.md':hashlib.sha256(readme.read_bytes()).hexdigest()}}]}
    (vendor/'manifest.json').write_text(json.dumps(manifest))
    for path in module.source_files():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic public source\n')
    module.RELEASE.mkdir(parents=True)
    archive = module.RELEASE/'images.tar.gz'
    archive.write_bytes(b'synthetic image archive, no real binaries')
    module.images = {'release':'20260921-r11','iam_target':'https://iam.megmeet.com',
        'archive':{'sha256':module.digest(archive)},
        'application_source_sha256':{path.name:module.digest(path) for path in (root/'services/gateway/app').glob('*.py')},
        'personal_native_source_sha256':{relative:module.digest(root/relative) for relative in NATIVE},
        'office':{'capabilities':{'protocol':'megmeet-office-v1'}},
        'native':{'dsh':'0.1.6-alpha.2','packages':250}}
    def build():
        (module.RELEASE/'images-manifest.json').write_text(json.dumps(module.images))
        with contextlib.redirect_stdout(io.StringIO()): module.main()
        return module.TARGET
    module.build = build
    return module


def test_complete_synthetic_package_has_required_tools_and_no_runtime_secrets(package):
    private = ['ops/changsha-desktop/.env','ops/changsha-desktop/.connectors.env',
               'office-engine/runtime-win/python/python.exe','office-engine/ocr-models/private.onnx',
               'toolkit/node_modules/unused.js','services/personal-cloud/test_private.py',
               'dev-local/dsh/vendor-community/file-change-viewer/.env',
               'dev-local/dsh/vendor-community/file-change-viewer/private.pem']
    for relative in private:
        path=package.ROOT/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'SYNTHETIC-PRIVATE-DO-NOT-SHIP')
    with ZipFile(package.build()) as archive:
        names=set(archive.namelist())
        required = ['toolkit/session-tools.mjs','toolkit/adapter/index.mjs','docs/ENTERPRISE-CONNECTORS.md',
                    'docs/WEB-CLOUD-MULTICLIENT.md','ops/changsha-desktop/connectors.env.example',
                    'services/gateway/app/connector_web_reader.py','services/personal-cloud/task-authority.mjs']
        assert all('new-system/'+name in names for name in required)
        assert all('new-system/'+name not in names for name in private)
        assert not any(b'SYNTHETIC-PRIVATE-DO-NOT-SHIP' in archive.read(name) for name in names)
        manifest=json.loads(archive.read('release-manifest.json'))
        assert set(manifest['files']) == names-{'release-manifest.json','INSTALL.md','images-manifest.json','images.tar.gz'}


@pytest.mark.parametrize('relative', ['services/gateway/app/main.py','toolkit/session-tools.mjs'])
def test_source_changed_after_export_blocks_archive(package, relative):
    (package.ROOT/relative).write_text('changed after image verification')
    with pytest.raises(SystemExit, match='source changed'): package.build()
    assert not package.TARGET.exists() and not package.TARGET.with_suffix('.zip.partial').exists()


def test_missing_toolkit_image_evidence_is_refused(package):
    del package.images['personal_native_source_sha256']['toolkit/session-tools.mjs']
    with pytest.raises(SystemExit, match='source verification missing'): package.build()
    assert not package.TARGET.exists()


def test_existing_archive_or_partial_is_not_overwritten(package):
    target = package.build()
    original = target.read_bytes()
    with pytest.raises(SystemExit, match='refusing to replace'): package.build()
    assert target.read_bytes() == original
    target.unlink()
    partial = target.with_suffix('.zip.partial')
    partial.write_bytes(b'old-incomplete-candidate')
    with pytest.raises(SystemExit, match='refusing to replace'): package.build()
    assert partial.read_bytes() == b'old-incomplete-candidate'


@pytest.mark.skipif(os.name=='nt', reason='link boundary executes in local WSL')
@pytest.mark.parametrize('parent', [False, True])
def test_file_and_internal_parent_symlinks_are_rejected(package, parent):
    if parent:
        origin = package.ROOT/'toolkit/adapter'
        other = package.ROOT/'same-root-adapter'
        origin.rename(other)
        origin.symlink_to(other, target_is_directory=True)
    else:
        origin = package.ROOT/'toolkit/adapter/index.mjs'
        other = package.ROOT/'same-root-adapter.mjs'
        origin.rename(other)
        origin.symlink_to(other)
    with pytest.raises(SystemExit, match='symlink'): package.build()
    assert not package.TARGET.exists()


def test_listed_vendor_bytes_must_match_reviewed_manifest(package):
    (package.ROOT/'dev-local/dsh/vendor-community/file-change-viewer/README.md').write_text('changed vendor bytes')
    with pytest.raises(SystemExit, match='Community source differs'): package.build()


@pytest.mark.parametrize('name', ['.env','../outside.md','private.pem'])
def test_vendor_manifest_cannot_request_private_or_escaping_file(package, name):
    path=package.ROOT/'dev-local/dsh/vendor-community/manifest.json'
    value=json.loads(path.read_text())
    value['plugins'][0]['files']={name:'0'*64}
    path.write_text(json.dumps(value))
    with pytest.raises(SystemExit, match='Community file not accepted'): package.build()


@pytest.mark.parametrize('drift', [False, True])
def test_exporter_freezes_accepted_ids_before_any_probe_or_export(tmp_path, monkeypatch, drift):
    root=tmp_path/'new-system'
    script=root/'ops/local-env/export-r11-images.py'
    script.parent.mkdir(parents=True)
    script.write_text((SCRIPTS/'export-r11-images.py').read_text())
    output=tmp_path/'output/upgrade-20260920'
    output.mkdir(parents=True)
    ids={name+':alpha2-candidate-20260920':'sha256:'+str(n)*64 for n,name in enumerate(
        ('megmeet-personal-dsh','megmeet-ai-gateway','megmeet-personal-cloud-manager'),1)}
    (output/'acceptance-summary.json').write_text(json.dumps({'candidate_image_ids':ids}))
    monkeypatch.setenv('WSL_DISTRO_NAME','MegmeetDev')
    monkeypatch.setenv('DOCKER_TLS_VERIFY','synthetic-invalid')
    monkeypatch.setenv('DOCKER_CERT_PATH','synthetic-invalid')
    import subprocess
    calls=[]
    def inspect(command, **kwargs):
        assert 'DOCKER_TLS_VERIFY' not in kwargs['env'] and 'DOCKER_CERT_PATH' not in kwargs['env']
        calls.append(command[-1])
        if '--platform' in command:
            assert command[:7] == ['docker','image','inspect','--platform','linux/amd64','--format','{{.Id}} {{.Os}}/{{.Architecture}}']
            assert command[-1] in ids.values()
            return command[-1].replace('sha256:','sha256:a',1)[:-1]+' linux/amd64\n'
        assert command[:5] == ['docker','image','inspect','--format','{{.Id}}']
        return ('sha256:'+'f'*64 if drift else ids[command[-1]])+'\n'
    monkeypatch.setattr(subprocess,'check_output',inspect)
    tree=ast.parse(script.read_text())
    first_probe=next(i for i,node in enumerate(tree.body) if isinstance(node,ast.Assign)
                     and any(isinstance(target,ast.Name) and target.id=='image_app' for target in node.targets))
    # Only run the initial selection stage; the real exporter body never runs.
    tree.body=tree.body[:first_probe]
    namespace={'__file__':str(script),'__name__':'synthetic_export_selection'}
    if drift:
        with pytest.raises(SystemExit,match='differs from the completed local acceptance'):
            exec(compile(tree,str(script),'exec'),namespace)
        assert len(calls)==1
    else:
        exec(compile(tree,str(script),'exec'),namespace)
        assert namespace['accepted_sources']==ids
        assert {namespace[name] for name in ('WORKER_SOURCE','GATEWAY_SOURCE','MANAGER_SOURCE')}==set(ids.values())
        assert namespace['accepted_platforms']=={value:value.replace('sha256:','sha256:a',1)[:-1] for value in ids.values()}
        assert len(calls)==6
    assert not (root/'releases/changsha-personal-cloud-20260921-r11/images.tar.gz').exists()
