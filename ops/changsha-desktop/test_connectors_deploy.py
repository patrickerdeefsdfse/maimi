"""Only synthetic files and command stubs; no daemon or provider actions."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_connectors", ROOT / "check-connectors.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def config(tmp_path, extra=""):
    directory = tmp_path / "connector-files"
    directory.mkdir()
    (tmp_path / ".connectors.env").write_text(f"CONNECTOR_CONFIG_SOURCE={directory}\n{extra}", encoding="utf-8")
    return directory


def test_optional_settings_do_not_require_either_provider(tmp_path):
    config(tmp_path)
    assert module.check(tmp_path) == {"dingtalk_configured": False, "brave_configured": False}


def test_config_rejects_main_env_overrides_and_secret_values(tmp_path):
    config(tmp_path, "POSTGRES_PASSWORD=never-display-this\n")
    with pytest.raises(ValueError, match="未知"):
        module.check(tmp_path)


def test_config_checks_file_metadata_without_reading_secrets(tmp_path, monkeypatch):
    directory = config(tmp_path, "DINGTALK_CLIENT_ID=test-app\nDINGTALK_CORP_ID=test-corp\nBRAVE_SEARCH_API_KEY_FILE=/run/connectors/brave-search-api-key\n")
    for name in ("dingtalk-client-secret", "dingtalk-accounts.json", "brave-search-api-key"):
        (directory / name).write_text("synthetic-only", encoding="utf-8")
    read_text = Path.read_text
    def guarded(path, *args, **kwargs):
        assert path.name == ".connectors.env", "secret contents must not be read"
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    assert module.check(tmp_path) == {"dingtalk_configured": True, "brave_configured": True}
    (directory / "brave-search-api-key").unlink()
    with pytest.raises(ValueError, match="缺少"):
        module.check(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Bash startup tests run in local WSL")
@pytest.mark.parametrize("enabled,failed", [(False, False), (True, False), (True, True)])
def test_startup_explicit_overlay_validation_and_no_unchecked_up(tmp_path, enabled, failed):
    shutil.copy2(ROOT / "start-server.sh", tmp_path / "start-server.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    stub = """#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
with open(os.environ['CALL_LOG'],'a') as stream:
 stream.write(json.dumps({'command':Path(sys.argv[0]).name,'args':sys.argv[1:],'injected':os.getenv('DINGTALK_CLIENT_ID')})+'\\n')
if 'check-connectors.py' in sys.argv and os.getenv('FAIL_CONNECTOR')=='1':sys.exit(1)
"""
    # A real Python shebang avoids recursion when the python3 CLI is stubbed.
    import sys
    stub = stub.replace("#!/usr/bin/env python3", "#!" + sys.executable)
    for name in ("docker", "python3"):
        path = bin_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "CALL_LOG": str(log),
           "DINGTALK_CLIENT_ID": "injected-outside-config", "FAIL_CONNECTOR": "1" if failed else "0"}
    result = subprocess.run(["bash", str(tmp_path / "start-server.sh"), *(["--connectors"] if enabled else [])],
                            text=True, capture_output=True, env=env)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(row["injected"] is None for row in calls)
    up = [row for row in calls if "up" in row["args"]]
    checks = [row for row in calls if "check-connectors.py" in row["args"]]
    assert bool(checks) == enabled
    if failed:
        assert result.returncode != 0 and not up
    else:
        assert result.returncode == 0 and len(up) == 1
        assert ("connectors.compose.yaml" in up[0]["args"]) == enabled
        if enabled:
            assert ".connectors.env" in up[0]["args"]
            quiet = next(row for row in calls if row["args"][-2:] == ["config", "--quiet"])
            assert "connectors.compose.yaml" in quiet["args"]


@pytest.mark.skipif(os.name == "nt" or not shutil.which("docker"), reason="uses local WSL Compose parser only")
def test_real_compose_merge_has_read_only_connector_mount_and_preserves_iam(tmp_path):
    directory = config(tmp_path, "DINGTALK_CLIENT_ID=synthetic-dingtalk-app\nDINGTALK_CORP_ID=synthetic-corp\n")
    values = {"POSTGRES_PASSWORD": "synthetic-password-not-real", "IAM_CLIENT_ID": "synthetic-iam-app"}
    for key in ("IAM_CLIENT_SECRET_SOURCE", "IAM_TOKEN_ENCRYPTION_KEY_SOURCE", "PERSONAL_KEY_SERVICE_SECRET_SOURCE", "PERSONAL_CLOUD_MANAGER_SECRET_SOURCE"):
        values[key] = str(tmp_path / key.lower())
    base = tmp_path / ".env"
    base.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env = {key: value for key, value in os.environ.items() if key not in module.FIELDS and key not in values}
    command = ["docker", "compose", "--env-file", str(base), "--env-file", str(tmp_path / ".connectors.env")]
    for name in ("compose.yaml", "personal-cloud.compose.yaml", "connectors.compose.yaml"):
        command += ["-f", str(ROOT / name)]
    result = subprocess.run(command + ["config", "--format", "json"], text=True, capture_output=True, env=env)
    assert result.returncode == 0, "synthetic Compose validation failed"
    gateway = json.loads(result.stdout)["services"]["gateway"]
    assert gateway["environment"]["IAM_CLIENT_ID"] == "synthetic-iam-app"
    assert gateway["environment"]["DINGTALK_CLIENT_ID"] == "synthetic-dingtalk-app"
    assert gateway["environment"]["DINGTALK_CLIENT_SECRET_FILE"] == "/run/connectors/dingtalk-client-secret"
    mount = next(row for row in gateway["volumes"] if row["target"] == "/run/connectors")
    assert mount["source"] == str(directory) and mount["read_only"] is True
    assert mount["bind"].get("create_host_path", False) is False
