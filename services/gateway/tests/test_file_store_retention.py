"""Whole-owner object erasure stays in its namespace and survives partial work."""
import hashlib
import os

import pytest

from app.file_store import FileStore

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='production Linux dirfd guarantees')


def namespace(owner='employee-a', scope='cloud-workspace'):
    return hashlib.sha256((owner + '\0' + scope).encode()).hexdigest()


def test_namespace_erasure_keeps_other_employee_and_other_scope(tmp_path):
    store = FileStore(tmp_path / 'objects')
    target = store.put('employee-a', 'cloud-workspace', b'cloud payload')
    others = [store.put('employee-b', 'cloud-workspace', b'other employee'),
              store.put('employee-a', 'department-workspace', b'department payload'),
              store.put('employee-a', 'local-scope', b'local payload')]
    pending = store.root / namespace() / ('.pending-' + 'a' * 32)
    pending.write_bytes(b'interrupted upload')
    assert store.delete_namespace('employee-a', 'cloud-workspace') is True
    assert not (store.root / target).exists()
    assert not pending.parent.exists()
    assert all((store.root / key).is_file() for key in others)
    assert store.delete_namespace('employee-a', 'cloud-workspace') is True


@pytest.mark.parametrize('kind', ['unknown', 'symlink', 'directory'])
def test_unrecognized_or_unsafe_entry_blocks_before_any_unlink(tmp_path, kind):
    store = FileStore(tmp_path / 'objects')
    key = store.put('employee-a', 'cloud-workspace', b'keep on failed preflight')
    directory = (store.root / key).parent
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside must survive')
    unsafe = directory / ('unexpected-name' if kind == 'unknown' else 'f' * 64)
    if kind == 'unknown':
        unsafe.write_bytes(b'unknown entry')
    elif kind == 'symlink':
        unsafe.symlink_to(outside)
    else:
        unsafe.mkdir()
    with pytest.raises(ValueError):
        store.delete_namespace('employee-a', 'cloud-workspace')
    assert (store.root / key).read_bytes() == b'keep on failed preflight'
    assert outside.read_bytes() == b'outside must survive'


def test_namespace_symlink_is_never_followed(tmp_path):
    store = FileStore(tmp_path / 'objects')
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / ('b' * 64)
    sentinel.write_bytes(b'keep')
    (store.root / namespace()).symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        store.delete_namespace('employee-a', 'cloud-workspace')
    assert sentinel.read_bytes() == b'keep'


def test_unlink_failure_is_retryable_and_does_not_touch_other_namespace(tmp_path, monkeypatch):
    store = FileStore(tmp_path / 'objects')
    store.put('employee-a', 'cloud-workspace', b'first')
    store.put('employee-a', 'cloud-workspace', b'second')
    other = store.put('employee-b', 'cloud-workspace', b'keep')
    original, calls = os.unlink, []

    def fail_second(name, **kwargs):
        calls.append(name)
        if len(calls) == 2:
            raise OSError('synthetic second unlink failure')
        return original(name, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, 'unlink', fail_second)
        with pytest.raises(OSError, match='synthetic'):
            store.delete_namespace('employee-a', 'cloud-workspace')
    assert len(list((store.root / namespace()).iterdir())) == 1
    assert (store.root / other).read_bytes() == b'keep'
    assert store.delete_namespace('employee-a', 'cloud-workspace') is True


def test_bounded_10000_object_pass_requires_followup(tmp_path):
    store = FileStore(tmp_path / 'objects')
    directory = store.root / namespace()
    directory.mkdir()
    for index in range(10001):
        (directory / ('%064x' % index)).write_bytes(b'')
    assert store.delete_namespace('employee-a', 'cloud-workspace') is False
    assert len(list(directory.iterdir())) == 1
    assert store.delete_namespace('employee-a', 'cloud-workspace') is True
    assert not directory.exists()


def test_failed_directory_sync_cannot_report_complete(tmp_path, monkeypatch):
    store = FileStore(tmp_path / 'objects')
    store.put('employee-a', 'cloud-workspace', b'owned')
    with monkeypatch.context() as scoped:
        scoped.setattr(os, 'fsync', lambda _: (_ for _ in ()).throw(OSError('synthetic fsync failure')))
        with pytest.raises(OSError, match='synthetic'):
            store.delete_namespace('employee-a', 'cloud-workspace')
    assert store.delete_namespace('employee-a', 'cloud-workspace') is True


def test_single_object_delete_requires_exact_namespace_and_preserves_other_objects(tmp_path):
    store = FileStore(tmp_path / 'objects')
    target = store.put('employee-a', 'cloud-workspace', b'target')
    sibling = store.put('employee-a', 'cloud-workspace', b'sibling')
    other = store.put('employee-b', 'cloud-workspace', b'target')
    for owner, scope, key in (('employee-a', 'cloud-workspace', other),
                              ('employee-a', 'department-workspace', target),
                              ('employee-a', 'cloud-workspace', '../outside')):
        with pytest.raises(ValueError, match='scope'):
            store.delete_object(owner, scope, key)
    store.delete_object('employee-a', 'cloud-workspace', target)
    store.delete_object('employee-a', 'cloud-workspace', target)
    assert not (store.root / target).exists()
    assert (store.root / sibling).read_bytes() == b'sibling'
    assert (store.root / other).read_bytes() == b'target'


def test_single_object_delete_rejects_symlink(tmp_path):
    store = FileStore(tmp_path / 'objects')
    key = store.put('employee-a', 'cloud-workspace', b'owned')
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside remains')
    (store.root / key).unlink()
    (store.root / key).symlink_to(outside)
    with pytest.raises(ValueError, match='unsafe'):
        store.delete_object('employee-a', 'cloud-workspace', key)
    assert outside.read_bytes() == b'outside remains'
