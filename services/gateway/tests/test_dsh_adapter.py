from pathlib import Path
import sys
import shutil
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.dsh_adapter import DshConversationRequest, DshSdkBackend


@pytest.fixture
def local_tmp():
    root = Path(tempfile.mkdtemp(prefix=".dsh-adapter-test-", dir=Path.cwd()))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sdk_backend_scopes_workspace_and_home_separately(local_tmp, monkeypatch):
    """A user's workspace is shared by conversations, while DSH_HOME is per conversation."""
    workspace_root = local_tmp / "workspaces"
    home_root = local_tmp / "dsh"
    monkeypatch.setenv("DSH_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("DSH_HOME_ROOT", str(home_root))
    monkeypatch.delenv("MODEL_GATEWAY_API_KEY_FILE", raising=False)
    monkeypatch.delenv("TOKENKEY_API_KEY_FILE", raising=False)
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("DSH_PROVIDER_MAP_JSON", '{"tokenkey":"deepseek-official"}')

    captured = []

    class FakeConfig:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeHarness:
        def __init__(self, config):
            self.config = config

        def run(self, _input, session_id):
            return type("Result", (), {"final_response": f"ok:{session_id}"})()

    backend = DshSdkBackend.__new__(DshSdkBackend)
    backend._config_type = FakeConfig
    backend._harness_type = FakeHarness

    first = backend._run_sync(
        DshConversationRequest(
            conversation_id="conversation-1",
            input_text="hello",
            workspace_key="user-opaque-a",
        )
    )
    second = backend._run_sync(
        DshConversationRequest(
            conversation_id="conversation-2",
            input_text="hello",
            workspace_key="user-opaque-a",
        )
    )

    assert first.text == "ok:conversation-1"
    assert second.text == "ok:conversation-2"
    assert captured[0]["cwd"] == str(workspace_root / "user-opaque-a")
    assert captured[1]["cwd"] == captured[0]["cwd"]
    assert captured[0]["dsh_home"] == str(home_root / "user-opaque-a" / "conversation-1")
    assert captured[1]["dsh_home"] == str(home_root / "user-opaque-a" / "conversation-2")
    assert (workspace_root / "user-opaque-a").is_dir()
    assert (home_root / "user-opaque-a" / "conversation-1" / ".cache").is_dir()


def test_sdk_backend_falls_back_to_conversation_workspace(local_tmp, monkeypatch):
    monkeypatch.setenv("DSH_WORKSPACE_ROOT", str(local_tmp / "workspaces"))
    monkeypatch.setenv("DSH_HOME_ROOT", str(local_tmp / "dsh"))
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "test-key")

    captured = []

    class FakeConfig:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeHarness:
        def __init__(self, _config):
            pass

        def run(self, _input, session_id):
            return type("Result", (), {"text": session_id})()

    backend = DshSdkBackend.__new__(DshSdkBackend)
    backend._config_type = FakeConfig
    backend._harness_type = FakeHarness
    result = backend._run_sync(
        DshConversationRequest(conversation_id="conversation-fallback", input_text="hello")
    )

    assert result.text == "conversation-fallback"
    assert Path(captured[0]["cwd"]).name == "conversation-fallback"
