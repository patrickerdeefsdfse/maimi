"""Run only as Linux root with basetemp under a root-owned 0700 directory."""
import os
from pathlib import Path
import stat
import sys
import pytest
from private_store import PrivateStore, StoreError, key_bytes, key_of

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or os.geteuid()!=0,
                                reason='requires Linux root and private test directory')

@pytest.fixture
def store(tmp_path):
    root=tmp_path/'rotation'; root.mkdir(mode=0o700)
    parent=tmp_path/'credentials'; parent.mkdir(mode=0o700)
    credential=parent/'kuaidi100-mcp.json'; credential.write_bytes(key_bytes('SyntheticOld'))
    credential.chmod(0o600); os.chown(credential,10001,10001)
    return PrivateStore(root,credential,shared_locks=())

def test_roundtrip_journal_private_keys_and_same_inode(store):
    with store.locked():
        old,ident=store.read_credential()
        store.write('previous-key.json',old)
        store.write('pending-key.json',key_bytes('SyntheticNew'))
        store.write_json('journal.json',{'phase':'WRITING'})
        store.write_credential_in_place(store.read('pending-key.json'),ident)
        raw,after=store.read_credential(ident)
        assert ident==after and key_of(raw)=='SyntheticNew'
        assert stat.S_IMODE((store.root/'pending-key.json').stat().st_mode)==0o600
        assert (store.root/'pending-key.json').stat().st_uid==0
        store.write_credential_in_place(old,ident)
        assert store.read_credential(ident)[0]==old

def test_recover_empty_interrupted_write(store):
    old,ident=store.read_credential()
    with store.credential.open('wb'): pass
    store.write_credential_in_place(old,ident)
    assert store.read_credential(ident)[0]==old

def test_lock_exclusion(store):
    with store.locked():
        with pytest.raises(StoreError,match='ROTATION_BUSY'):
            with store.locked(): pass

def test_shared_release_lock_exclusion(store):
    import fcntl
    shared=store.root/'release.lock'
    fd=os.open(shared,os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        store.shared_locks=(shared,)
        with pytest.raises(StoreError,match='RELEASE_MAINTENANCE_BUSY'):
            with store.locked():pass
    finally:os.close(fd)

@pytest.mark.parametrize('mode',[0o644,0o666,0o400])
def test_credential_mode_refused(store,mode):
    store.credential.chmod(mode)
    with pytest.raises(StoreError): store.read_credential()

def test_credential_wrong_owner_refused(store):
    os.chown(store.credential,0,0)
    with pytest.raises(StoreError):store.read_credential()

def test_hardlink_refused(store):
    os.link(store.credential,store.credential.parent/'alias')
    with pytest.raises(StoreError):store.read_credential()

def test_symlink_refused(store):
    target=store.credential.with_name('actual.json'); store.credential.rename(target)
    store.credential.symlink_to(target)
    with pytest.raises((StoreError,OSError)):store.read_credential()

def test_parent_symlink_refused(store):
    actual=store.credential.parent.with_name('actual');store.credential.parent.rename(actual)
    store.credential.parent.symlink_to(actual,target_is_directory=True)
    with pytest.raises(StoreError):store.read_credential()

def test_world_writable_parent_refused(store):
    store.credential.parent.chmod(0o777)
    with pytest.raises(StoreError):store.read_credential()

def test_replaced_inode_refused(store):
    raw,ident=store.read_credential()
    replacement=store.credential.with_name('replacement');replacement.write_bytes(raw)
    replacement.chmod(0o600);os.chown(replacement,10001,10001)
    replacement.replace(store.credential)
    with pytest.raises(StoreError,match='IDENTITY_CHANGED'):store.write_credential_in_place(key_bytes('New'),ident)
    assert store.credential.read_bytes()==raw

def test_private_key_symlink_refused_without_touching_target(store):
    victim=store.root/'victim';victim.write_bytes(b'preserve');victim.chmod(0o600)
    (store.root/'pending-key.json').symlink_to(victim)
    with pytest.raises(StoreError):store.write('pending-key.json',key_bytes('New'))
    assert victim.read_bytes()==b'preserve'

def test_duplicate_key_json_refused(store):
    store.credential.write_bytes(b'{"key":"a","key":"b"}')
    with pytest.raises(StoreError):store.read_credential()

def test_no_arbitrary_private_filename(store):
    with pytest.raises(StoreError):store.write('../escape.json',b'{}')
