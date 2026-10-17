#!/usr/bin/env python3
"""Server-only interactive configuration. Never print or package secrets."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

CALLBACK = 'https://platform-ai.megmeet.com/v1/iam/callback'
IAM_ORIGIN = 'https://iam.megmeet.com'
PORT = 18091
FILES = {
    'IAM_CLIENT_SECRET_SOURCE': 'iam-client-secret',
    'IAM_TOKEN_ENCRYPTION_KEY_SOURCE': 'iam-token-encryption-key',
    'PERSONAL_KEY_SERVICE_SECRET_SOURCE': 'personal-key-service-secret',
    'PERSONAL_CLOUD_MANAGER_SECRET_SOURCE': 'personal-cloud-manager',
}
FIELDS = ('POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'IAM_BASE_URL',
          'IAM_AUTHORIZATION_BASE_URL', 'IAM_CLIENT_ID', *FILES)
# Kept verbatim when reconfiguring an older installation. This deployment uses
# SSO plus the employee's TokenKey identity; it neither needs nor rewrites the
# independently issued directory API credential.
LEGACY_FIELDS = ('IAM_DIRECTORY_ORG_URL', 'IAM_DIRECTORY_ACCOUNT_URL',
                 'IAM_DIRECTORY_API_KEY', 'IAM_DIRECTORY_SECRET_SOURCE')
LIMITS = {
    'CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES': (268435456,1,10737418240),
    'CLOUD_TASK_GLOBAL_LIMIT': (8,1,64),
    'CLOUD_TASK_WORKSPACE_LIMIT': (4,1,32),
    'CLOUD_TASK_EMPLOYEE_LIMIT': (2,1,8),
    'CLOUD_TASK_TIMEOUT_SECONDS': (120,5,180),
    'PERSONAL_CLOUD_MAX_ACTIVE_HOSTS': (2,1,32),
    'PERSONAL_CLOUD_IDLE_SECONDS': (300,30,3600),
}
ALL_FIELDS = (*FIELDS,*LEGACY_FIELDS,*LIMITS)


def regular_path(path: Path, *, exists=False):
    if not path.is_absolute():
        raise ValueError('配置路径必须为绝对路径')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('配置及 secret 路径不允许符号链接')
    if path.exists() and not path.is_file():
        raise ValueError('配置或 secret 不是普通文件')
    if exists and not path.is_file():
        raise ValueError('缺少必需配置或 secret 文件')


def read_value(path: Path):
    regular_path(path, exists=True)
    if path.stat().st_size > 16384:
        raise ValueError('配置或 secret 文件过大')
    return path.read_text(encoding='utf-8').strip()


def read_config(path: Path):
    if not path.exists():
        regular_path(path)
        return {}
    result = {}
    for line in read_value(path).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, sep, value = line.partition('=')
        if sep != '=' or name not in ALL_FIELDS or name in result:
            raise ValueError('现有 .env 含重复或未知字段，请人工核对')
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        result[name] = value
    return result


def validate(config, values):
    if any(not config.get(name) for name in FIELDS):
        raise ValueError('必填配置不完整')
    if any(any(c in config.get(name, '') for c in "'\r\n\0") for name in (*FIELDS, *LEGACY_FIELDS)):
        raise ValueError('配置值含不能安全写入.env的字符')
    for name,(default,minimum,maximum) in LIMITS.items():
        value=str(config.get(name,str(default)))
        if not re.fullmatch(r'[0-9]{1,11}',value) or not minimum<=int(value)<=maximum:
            raise ValueError('工作区配额或任务限制配置不合法')
    for field in ('POSTGRES_DB', 'POSTGRES_USER'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', config[field]):
            raise ValueError('数据库名称或用户格式不合法')
    for field in ('IAM_CLIENT_ID',):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', config[field]):
            raise ValueError('应用 ID 格式不合法')
    if config.get('IAM_DIRECTORY_API_KEY') and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', config['IAM_DIRECTORY_API_KEY']):
        raise ValueError('既有目录 API key 格式不合法')
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', config['POSTGRES_PASSWORD']):
        raise ValueError('数据库密码必须使用24至128位 URL 安全文字或十六进制')
    if config['IAM_BASE_URL'] != IAM_ORIGIN:
        raise ValueError('当前公司部署使用 IAM 正式服 https://iam.megmeet.com，请重新运行配置向导并填写正式服应用凭据')
    if config['IAM_AUTHORIZATION_BASE_URL'] != config['IAM_BASE_URL']:
        raise ValueError('IAM 授权与后端地址必须属于同一环境')
    for name in ('IAM_DIRECTORY_ORG_URL', 'IAM_DIRECTORY_ACCOUNT_URL'):
        if not config.get(name):
            continue
        parts = urlsplit(config[name])
        if any(c.isspace() or ord(c) < 32 or c == '\\' for c in config[name]) or parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('目录接口须为不含认证信息或查询参数的完整 HTTPS 地址')
        _ = parts.port
    for name in FILES:
        if any(c in config[name] for c in "'\r\n\0"):
            raise ValueError('secret 文件路径格式不合法')
        regular_path(Path(config[name]))
        value = values.get(name, '')
        if not value or any(c in value for c in '\r\n\0') or len(value) > 8192:
            raise ValueError('必需 secret 为空或格式不合法')
    secret_paths = [config[name] for name in FILES]
    if config.get('IAM_DIRECTORY_SECRET_SOURCE'):
        regular_path(Path(config['IAM_DIRECTORY_SECRET_SOURCE']))
        secret_paths.append(config['IAM_DIRECTORY_SECRET_SOURCE'])
    if len(set(secret_paths)) != len(secret_paths):
        raise ValueError('不同用途的 secret 必须使用不同文件')
    if len(values['PERSONAL_CLOUD_MANAGER_SECRET_SOURCE'])<32:
        raise ValueError('个人云调度凭据必须至少32位')
    key = values['IAM_TOKEN_ENCRYPTION_KEY_SOURCE']
    try:
        decoded = base64.b64decode(key, altchars=b'-_', validate=True)
    except Exception as exc:
        raise ValueError('既有 Fernet 密钥无效，拒绝自动替换') from exc
    if len(key) != 44 or len(decoded) != 32:
        raise ValueError('既有 Fernet 密钥无效，拒绝自动替换')


def prompt(label, default='', *, hidden=False):
    suffix = '（回车保留）' if default else ''
    value = getpass.getpass(label + suffix + ': ') if hidden else input(label + (f' [{default}]' if default else '') + ': ')
    return value.strip() or default


def docker_env():
    environment = dict(os.environ)
    # Compose interpolates exported variables before .env; discard every
    # configurable value so checks and startup use the same verified file.
    for name in (*ALL_FIELDS, 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(name, None)
    environment['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
    return environment


def compose_check(directory, *, nginx_network=False):
    args = ['docker', 'compose', '-p', 'megmeet-desktop-server', '--env-file', str(directory / '.env'), '-f', str(directory / 'compose.yaml'), '-f', str(directory / 'personal-cloud.compose.yaml')]
    if nginx_network:
        args += ['-f', str(directory / 'compose.nginx-network.yaml')]
    completed = subprocess.run(args + ['config', '--quiet'], capture_output=True, env=docker_env())
    if completed.returncode:
        raise ValueError('Compose 静默校验失败；未启动任何服务')


def owned_running_gateway():
    """Permit only this project's running gateway at the exact loopback port."""
    listed=subprocess.run(['docker','ps','--quiet','--filter','status=running',
        '--filter','label=com.docker.compose.project=megmeet-desktop-server',
        '--filter','label=com.docker.compose.service=gateway'],
        capture_output=True,text=True,env=docker_env(),timeout=15)
    ids=listed.stdout.split() if listed.returncode==0 else []
    if len(ids)!=1 or not re.fullmatch(r'[a-f0-9]{12,64}',ids[0]):return False
    shape='{"running":{{json .State.Running}},"labels":{{json .Config.Labels}},"ports":{{json .NetworkSettings.Ports}}}'
    inspected=subprocess.run(['docker','inspect','--format',shape,ids[0]],
        capture_output=True,text=True,env=docker_env(),timeout=15)
    if inspected.returncode:return False
    try:
        data=json.loads(inspected.stdout)
        labels=data.get('labels') or {}
        return (data.get('running') is True
            and labels.get('com.docker.compose.project')=='megmeet-desktop-server'
            and labels.get('com.docker.compose.service')=='gateway'
            and (data.get('ports') or {}).get('8080/tcp')==[{'HostIp':'127.0.0.1','HostPort':str(PORT)}])
    except (ValueError,TypeError,AttributeError):return False


def check(directory, *, check_port=True, check_compose=True, nginx_network=False, allow_running=False):
    if len(directory.parents) >= 3 and (directory.parents[2] / '.preparation-incomplete').exists():
        raise ValueError('发行目录准备未完成，不允许配置或启动')
    if (directory / '.configuration-in-progress').exists():
        raise ValueError('上次配置提交未完成；先核对文件，不允许启动')
    config = read_config(directory / '.env')
    values = {name: read_value(Path(config.get(name, '/nonexistent'))) for name in FILES}
    validate(config, values)
    env_stat = (directory / '.env').stat()
    if stat.S_IMODE(env_stat.st_mode) & 0o077:
        raise ValueError('.env 必须仅管理员可读写（0600）')
    for name in FILES:
        current = Path(config[name]).stat()
        mode = stat.S_IMODE(current.st_mode)
        readable = (current.st_uid == 10001 and mode & 0o400) or (current.st_gid == 10001 and mode & 0o040)
        if not readable or mode & 0o027:
            raise ValueError('secret 权限须容器10001可读、其他用户不可读写，如root:10001/0440')
    if check_port:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', PORT))
            except OSError as exc:
                if not allow_running or not owned_running_gateway():
                    raise ValueError('专用端口18091已占用且未确认属于本项目运行中的Gateway；不会停止其他服务') from exc
    if check_compose:
        compose_check(directory, nginx_network=nginx_network)
    return config


def commit(directory, config, values):
    """Stage every file before replacing any; recover on ordinary write errors.

    A marker denies startup after a crash during this multi-file commit. Fernet
    is read/preserved before this call and never regenerated for existing files.
    """
    changes = [(Path(config[name]), (values[name] + '\n').encode(), 0o440, 10001) for name in FILES]
    body = '# Server-only configuration; do not copy to desktop or Git.\n'
    body += ''.join(f"{name}='{config[name]}'\n" for name in FIELDS)
    body += ''.join(f"{name}='{config[name]}'\n" for name in LEGACY_FIELDS if name in config)
    body += ''.join(f"{name}='{config.get(name,str(bounds[0]))}'\n" for name,bounds in LIMITS.items())
    changes.append((directory / '.env', body.encode(), 0o600, 0))
    staged, previous, written = [], {}, []
    marker = directory / '.configuration-in-progress'
    if marker.exists():
        raise ValueError('存在未完成配置标记，请先人工核对')
    try:
        for target, content, mode, gid in changes:
            regular_path(target)
            if target in previous:
                raise ValueError('配置目标路径重复')
            parent_existed = target.parent.exists()
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            if target != directory / '.env' and not parent_existed:
                os.chown(target.parent, 0, 10001)
                os.chmod(target.parent, 0o750)
            previous[target] = (target.read_bytes(), target.stat()) if target.exists() else None
            fd, temporary = tempfile.mkstemp(prefix='.configure-', dir=target.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), mode); os.fchown(stream.fileno(), 0, gid)
            staged.append((Path(temporary), target))
        with marker.open('x', encoding='utf-8') as stream:
            stream.write('Configuration commit in progress; do not start services.\n')
            stream.flush(); os.fsync(stream.fileno())
        for temporary, target in staged:
            os.replace(temporary, target); written.append(target)
        marker.unlink()
    except Exception:
        restored = True
        for target in reversed(written):
            try:
                old = previous[target]
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    fd, tmp = tempfile.mkstemp(prefix='.restore-', dir=target.parent)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(old[0]); stream.flush(); os.fsync(stream.fileno())
                        os.fchmod(stream.fileno(), stat.S_IMODE(old[1].st_mode))
                        os.fchown(stream.fileno(), old[1].st_uid, old[1].st_gid)
                    os.replace(tmp, target)
            except Exception:
                restored = False
        if restored:
            marker.unlink(missing_ok=True)
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def configure(directory, secrets_directory):
    if len(directory.parents) >= 3 and (directory.parents[2] / '.preparation-incomplete').exists():
        raise ValueError('发行目录准备未完成，不允许配置或启动')
    if not sys.stdin.isatty():
        raise ValueError('请在堡垒机交互终端运行配置，不接受脚本或聊天中的明文 secret')
    current = read_config(directory / '.env')
    if not current.get('POSTGRES_PASSWORD'):
        if subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, env=docker_env()).returncode:
            raise ValueError('Docker不可用，无法核对既有数据库卷；未生成密码')
        existing = subprocess.run(['docker', 'volume', 'inspect', 'megmeet-desktop-server_postgres-data'], capture_output=True, env=docker_env())
        if existing.returncode == 0:
            raise ValueError('已存在本系统数据库卷但缺数据库密码，拒绝自动生成；请恢复原.env')
    config = {**current, 'POSTGRES_DB': current.get('POSTGRES_DB', 'megmeet_ai'),
              'POSTGRES_USER': current.get('POSTGRES_USER', 'megmeet'),
              'POSTGRES_PASSWORD': current.get('POSTGRES_PASSWORD') or secrets.token_hex(32)}
    config['IAM_BASE_URL'] = prompt('IAM地址', IAM_ORIGIN)
    config['IAM_AUTHORIZATION_BASE_URL'] = config['IAM_BASE_URL']
    switching_iam = bool(current.get('IAM_BASE_URL') and current['IAM_BASE_URL'] != config['IAM_BASE_URL'])
    config['IAM_CLIENT_ID'] = prompt('IAM client_id', '' if switching_iam else current.get('IAM_CLIENT_ID', ''), hidden=True)
    values = {}
    for field, filename in FILES.items():
        target = Path(current.get(field) or (secrets_directory / filename))
        regular_path(target)
        config[field] = str(target)
        existing = read_value(target) if target.exists() else ''
        if switching_iam and field == 'IAM_CLIENT_SECRET_SOURCE':
            existing = ''  # Never silently reuse an application's test-environment secret.
        if field == 'IAM_TOKEN_ENCRYPTION_KEY_SOURCE':
            values[field] = existing if target.exists() else base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        elif field == 'PERSONAL_CLOUD_MANAGER_SECRET_SOURCE':
            values[field] = existing if target.exists() else secrets.token_urlsafe(48)
        else:
            values[field] = prompt({'IAM_CLIENT_SECRET_SOURCE': 'IAM client_secret',
                                   'PERSONAL_KEY_SERVICE_SECRET_SOURCE': 'TokenKey服务 X-API-Key'}[field], existing, hidden=True)
    validate(config, values)
    with socket.socket() as probe:
        try: probe.bind(('127.0.0.1', PORT))
        except OSError as exc: raise ValueError('专用端口18091已占用；未修改配置或启动服务') from exc
    commit(directory, config, values)
    check(directory)
    print('SERVER_CONFIGURATION: PASS')
    print('FERNET_KEY: PRESERVED_OR_CREATED (value not displayed)')
    print('IAM_CALLBACK:', CALLBACK)
    print('SERVICES_STARTED: NO')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--allow-running', action='store_true', help='allow an occupied port only for this exact running Compose gateway')
    parser.add_argument('--nginx-network', action='store_true')
    parser.add_argument('--secrets-directory', type=Path, default=Path('/data/megmeet-desktop-secrets'))
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        raise ValueError('请在长沙Linux服务器以管理员运行')
    os.umask(0o077)
    directory = Path(__file__).absolute().parent
    if args.check:
        check(directory, allow_running=args.allow_running, nginx_network=args.nginx_network)
        print('SERVER_PRECHECK: PASS (no company request or service start)')
    else:
        configure(directory, args.secrets_directory)


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, EOFError, KeyboardInterrupt) as exc:
        # Do not include exception details from HTTP/process libraries or files.
        print('CONFIGURATION_STOP:', str(exc) if isinstance(exc, ValueError) else type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
