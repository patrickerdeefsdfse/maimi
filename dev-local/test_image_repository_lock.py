import pytest
from capture_versions import matching_repository_digest


def test_local_release_alias_does_not_replace_original_pullable_repository():
    sha = 'sha256:' + 'a' * 64
    assert matching_repository_digest('postgres:16', ['megmeet-desktop-postgres@' + sha,
        'postgres@' + sha]) == 'postgres@' + sha
    assert matching_repository_digest('redis:7', ['docker.io/library/redis@' + sha]) == 'docker.io/library/redis@' + sha
    with pytest.raises(RuntimeError):
        matching_repository_digest('postgres:16', ['megmeet-desktop-postgres@' + sha])
