import hashlib
import os
from pathlib import Path
import pytest
from app.file_store import FileStore,attachment_bytes

pytestmark=pytest.mark.skipif(os.name!='posix',reason='production Linux dirfd guarantees')

def test_roundtrip_namespaces_and_legacy(tmp_path,monkeypatch):
    monkeypatch.setenv('PLATFORM_FILE_ROOT',str(tmp_path))
    store=FileStore(); data=b'owned project file'
    a=store.put('employee-a','project',data);b=store.put('employee-b','project',data)
    assert a!=b
    row={'object_key':a,'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data)}
    assert attachment_bytes(row)==data
    assert attachment_bytes({'content':data})==data
    assert store.put('employee-a','project',data)==a
    assert not list(tmp_path.rglob('.pending-*'))

def test_traversal_symlink_and_integrity(tmp_path):
    store=FileStore(tmp_path/'private'); data=b'hello';digest=hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError):store.read('../outside',expected_size=5,expected_sha256=digest)
    key=store.put('a','p',data)
    (store.root/key).write_bytes(b'wrong')
    with pytest.raises(ValueError):store.read(key,expected_size=5,expected_sha256=digest)
    (store.root/key).unlink();(store.root/key).symlink_to(tmp_path/'outside')
    with pytest.raises(OSError):store.read(key,expected_size=5,expected_sha256=digest)
    namespace=hashlib.sha256(b'b\0p').hexdigest()
    (store.root/namespace).symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(OSError):store.put('b','p',data)

def test_namespace_is_durable_before_object_is_returned(tmp_path,monkeypatch):
    store=FileStore(tmp_path/'private'); synced=[]; original=os.fsync
    def sync(fd):
        synced.append(os.fstat(fd).st_ino)
        original(fd)
    monkeypatch.setattr(os,'fsync',sync)
    key=store.put('employee','project',b'durable')
    assert synced[0]==store.root.stat().st_ino
    assert synced[-1]==(store.root/key).parent.stat().st_ino

def test_failed_namespace_sync_never_returns_an_object_key(tmp_path,monkeypatch):
    store=FileStore(tmp_path/'private')
    def fail(fd):raise OSError('synthetic directory sync failure')
    monkeypatch.setattr(os,'fsync',fail)
    with pytest.raises(OSError,match='synthetic'):store.put('employee','project',b'durable')
    assert not list(store.root.glob('*/*'))
