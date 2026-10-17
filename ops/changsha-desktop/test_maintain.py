import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('maintenance',Path(__file__).with_name('maintain.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_snapshot_verifier_rejects_traversal_tampering_and_links(tmp_path):
    data=tmp_path/'owned-volume.tar.gz';data.write_bytes(b'synthetic archive')
    manifest={'schema':1,'volumes':{'owned-volume':{'file':data.name,'bytes':data.stat().st_size,'sha256':m.digest(data)}}}
    file=tmp_path/'manifest.json';file.write_text(json.dumps(manifest))
    assert m.verify_snapshot(tmp_path)['volumes']==1
    data.write_bytes(b'tampered')
    with pytest.raises(ValueError):m.verify_snapshot(tmp_path)
    manifest['volumes']['owned-volume']['file']='../other.tar.gz';file.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):m.verify_snapshot(tmp_path)


def test_backup_refuses_running_application_without_any_mutation(monkeypatch,tmp_path):
    monkeypatch.setattr(m,'inventory',lambda *args:({'id':{'State':'running'}},{'owned-volume':{}}))
    monkeypatch.setattr(m,'docker',lambda *args:pytest.fail('No Docker operation after live-state refusal'))
    with pytest.raises(RuntimeError,match='maintenance window'):m.backup('owned','owned-manager',tmp_path,'image')
    assert list(tmp_path.iterdir())==[]


def test_backup_refuses_foreign_active_mount(monkeypatch,tmp_path):
    monkeypatch.setattr(m,'inventory',lambda *args:({}, {'owned-volume':{}}))
    monkeypatch.setattr(m,'docker',lambda *args:'foreign-container')
    with pytest.raises(RuntimeError,match='running container'):m.backup('owned','owned-manager',tmp_path,'image')
    assert list(tmp_path.iterdir())==[]


def test_inventory_uses_owned_labels_and_filters_inspect_fields(monkeypatch):
    calls=[]
    def docker(*args):
        calls.append(args)
        if args[:2]==('ps','-a'):return ''
        if args[:2]==('volume','ls'):return 'owned-volume'
        return json.dumps({m.COMPOSE_LABEL:'owned','not-a-secret':'omitted'})
    monkeypatch.setattr(m,'docker',docker)
    containers,volumes=m.inventory('owned','owned-manager')
    assert not containers and volumes=={'owned-volume':{m.COMPOSE_LABEL:'owned'}}
    assert all('--format' in call for call in calls if call[:2]==('volume','inspect'))
    with pytest.raises(ValueError):m.inventory('../../unsafe','owned-manager')
