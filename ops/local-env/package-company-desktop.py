"""Freeze the already packaged company desktop into a portable candidate zip."""
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from collections import deque
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / 'desktop-local'
ARTIFACT = json.loads((DESKTOP / 'artifact-company.json').read_text(encoding='utf-8'))
VERSION = json.loads((DESKTOP / 'package.json').read_text())['version']
SOURCE = DESKTOP / f'dist/{VERSION}-company/MEGMEET AI-win32-x64'
TARGET = ROOT / f'releases/MEGMEET-AI-Desktop-{VERSION}-company.zip'
if ARTIFACT['deployment'] != {'mode': 'company', 'gatewayOrigin': 'https://platform-ai.megmeet.com'}:
    raise SystemExit('Company profile required')
if TARGET.exists(): raise SystemExit('Candidate archive already exists')
def digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()
for row in ARTIFACT['binaries']:
    relative = 'resources/app.asar' if row['path'].endswith('app.asar') else 'MEGMEET-AI.exe'
    if digest(SOURCE / relative) != row['sha256']:
        raise SystemExit('Candidate app differs from its artifact record')
readme = '''MEGMEET AI 桌面公司联调候选版 VERSION

完整解压此目录后打开 MEGMEET-AI.exe，不能仅复制单个exe。
内置Windows原生DSH 0.1.6-alpha.1运行核心，无需安装Node、Python、WSL或Docker。
固定连接 https://platform-ai.megmeet.com。公司服务部署及IAM注册完成后方可登录。
现阶段不应拿此包代替桌面上的“MEGMEET AI 本地开发版”进行模拟验证。

公司浏览器登录回调由长沙服务器接收：
https://platform-ai.megmeet.com/v1/iam/callback

登录后在DSH原生界面选择个人本地文件夹；聊天历史按可信工号保存在本机。
模型权限和个人TokenKey由公司服务提供，桌面不包含服务秘密或个人Key。
“云端工作区”为每名员工独立的服务器工作区，置顶保留、只在手动点击后连接。
企业插件按本人授权显示，选择后以标签加入当前对话，不需要再选择工作台。

这是未签名的联调候选包，真实公司联调、正式签名安装及受控自动更新尚未完成。
不要关闭系统防护或绕过公司终端管理要求来安装。
'''.replace('VERSION', VERSION)
files = [p for p in SOURCE.rglob('*') if p.is_file()]
if any(p.is_symlink() or '.before-review-' in p.name for p in files):
    raise SystemExit('Unexpected build link/backup')
with ZipFile(TARGET, 'x', compression=ZIP_DEFLATED, compresslevel=3) as archive:
    # Bounded parallel reads avoid serial antivirus/filesystem latency for the
    # 23k native runtime files. Archive writes remain ordered and single-threaded.
    def read_file(path):
        info = ZipInfo.from_file(path, 'MEGMEET-AI/' + path.relative_to(SOURCE).as_posix())
        return info, path.read_bytes()
    with ThreadPoolExecutor(max_workers=8) as reader:
        remaining = iter(sorted(files))
        pending = deque()
        for _ in range(16):
            item = next(remaining, None)
            if item is not None: pending.append(reader.submit(read_file, item))
        completed = 0
        while pending:
            info, contents = pending.popleft().result()
            archive.writestr(info, contents, compress_type=ZIP_DEFLATED, compresslevel=3)
            item = next(remaining, None)
            if item is not None: pending.append(reader.submit(read_file, item))
            completed += 1
            if completed % 5000 == 0: print('DESKTOP_FILES_PACKAGED:', completed, flush=True)
    archive.writestr('MEGMEET-AI/使用说明.txt', readme)
    portable_manifest = {key: ARTIFACT[key] for key in ('version', 'deployment', 'electron', 'signed', 'companyDeploymentVerified')}
    portable_manifest['binaries'] = [{'file': 'resources/app.asar' if item['path'].endswith('app.asar') else 'MEGMEET-AI.exe',
        'sha256': item['sha256'], 'bytes': item['bytes']} for item in ARTIFACT['binaries']]
    archive.writestr('MEGMEET-AI/release.json', json.dumps(portable_manifest, indent=2) + '\n')
with ZipFile(TARGET) as archive:
    if archive.testzip() is not None: raise SystemExit('Desktop zip CRC failed')
sha = digest(TARGET)
TARGET.with_suffix('.zip.sha256').write_text(sha + '  ' + TARGET.name + '\n')
print('COMPANY_DESKTOP_CANDIDATE_ZIP: PASS')
print('PACKAGE_BYTES:', TARGET.stat().st_size)
print('PACKAGE_SHA256:', sha)
