#!/usr/bin/env python3
"""Offline optional connector config check; never read provider secret content."""
from pathlib import Path
import re
import sys

FIELDS = frozenset(("CONNECTOR_CONFIG_SOURCE", "DINGTALK_CLIENT_ID", "DINGTALK_CORP_ID",
                    "DINGTALK_ROBOT_CODE", "DINGTALK_OAUTH_SCOPES", "BRAVE_SEARCH_API_KEY_FILE"))
DEFAULT_SCOPES = "openid corpid Contact.User.Read Calendar.Event.Read Calendar.Event.Write Todo.Todo.Write"


def metadata(path, *, directory=False):
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("连接器配置路径必须为无符号链接的绝对路径")
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError("缺少连接器配置目录或必需文件")
    if not directory and not 0 < path.stat().st_size <= 2 * 1024 * 1024:
        raise ValueError("连接器配置文件为空或过大")


def check(directory):
    path = directory.resolve() / ".connectors.env"
    metadata(path)
    if path.stat().st_size > 16384:
        raise ValueError("连接器环境配置过大")
    config = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=" or key not in FIELDS or key in config:
            raise ValueError("连接器环境配置含未知或重复字段")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if any(c in value for c in "\"'$`\r\n\x00") or value != value.strip():
            raise ValueError("连接器环境配置含不支持的字符")
        config[key] = value
    source = Path(config.get("CONNECTOR_CONFIG_SOURCE", ""))
    metadata(source, directory=True)
    for key in ("DINGTALK_CLIENT_ID", "DINGTALK_CORP_ID", "DINGTALK_ROBOT_CODE"):
        if config.get(key) and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", config[key]):
            raise ValueError("钉钉应用标识格式错误")
    dingtalk = bool(config.get("DINGTALK_CLIENT_ID") or config.get("DINGTALK_CORP_ID") or config.get("DINGTALK_ROBOT_CODE"))
    if dingtalk:
        if not config.get("DINGTALK_CLIENT_ID") or not config.get("DINGTALK_CORP_ID"):
            raise ValueError("钉钉需要独立应用 Client ID 和 Corp ID")
        scope = config.get("DINGTALK_OAUTH_SCOPES") or DEFAULT_SCOPES
        if not re.fullmatch(r"[A-Za-z0-9._ ]{1,1000}", scope) or not {"openid", "corpid"}.issubset(scope.split()):
            raise ValueError("钉钉权限必须包含 openid 和 corpid")
        metadata(source / "dingtalk-client-secret")
        metadata(source / "dingtalk-accounts.json")
    brave = config.get("BRAVE_SEARCH_API_KEY_FILE", "")
    if brave:
        if brave != "/run/connectors/brave-search-api-key":
            raise ValueError("搜索密钥必须使用固定容器文件路径")
        metadata(source / "brave-search-api-key")
    return {"dingtalk_configured": dingtalk, "brave_configured": bool(brave)}


if __name__ == "__main__":
    try:
        check(Path(__file__).resolve().parent)
    except (ValueError, OSError, UnicodeError):
        # Never echo contents or full values from a malformed config.
        print("CONNECTOR_CONFIG_METADATA: FAIL (check .connectors.env and required files)", file=sys.stderr)
        raise SystemExit(1)
    print("CONNECTOR_CONFIG_METADATA: PASS")
    print("CONNECTOR_CREDENTIAL_CONTENT_AND_PROVIDER_ACCESS: NOT_CHECKED")
