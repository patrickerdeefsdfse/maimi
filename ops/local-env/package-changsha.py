"""Package an explicit server-source allowlist and the verified offline images."""
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'releases/changsha-personal-cloud-20260918-r9'
TARGET = ROOT / 'releases/MEGMEET-Changsha-Personal-Cloud-20260918-r9.zip'

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

if TARGET.exists():
    raise SystemExit('Release archive already exists; refusing to replace it')
images = json.loads((RELEASE / 'images-manifest.json').read_text())
if digest(RELEASE / 'images.tar.gz') != images['archive']['sha256']:
    raise SystemExit('Offline image archive checksum differs')
# Never pair a newly edited source bundle with an older already-exported image.
for name, expected in images['application_source_sha256'].items():
    if digest(ROOT / 'services/gateway/app' / name) != expected:
        raise SystemExit('Gateway source changed after the reviewed image export')
for relative, expected in images['personal_native_source_sha256'].items():
    if digest(ROOT / relative) != expected:
        raise SystemExit('Native/office source changed after the reviewed image export: ' + relative)
office = images.get('office', {})
if office.get('capabilities', {}).get('protocol') != 'megmeet-office-v1':
    raise SystemExit('Office image verification evidence is missing')
for required in ('office-engine/engine.py', 'dev-local/dsh/office-adapter/index.mjs',
                 'dev-local/dsh/office-adapter/adapter.mjs', 'dev-local/dsh/office-adapter/transport.mjs'):
    if required not in images['personal_native_source_sha256']:
        raise SystemExit('Office image source verification is missing: ' + required)
files = list((ROOT / 'services/gateway/app').glob('*.py'))
files += [p for p in (ROOT / 'services/personal-cloud').iterdir() if p.is_file() and (p.suffix in {'.py','.mjs','.md'} or p.name.startswith('Dockerfile'))]
files += list((ROOT / 'db/migrations').glob('*.sql'))
files += [ROOT / 'services/gateway/requirements.txt', ROOT / 'dev-local/Dockerfile.gateway',
          ROOT / 'dev-local/install_dependencies.py', ROOT / 'dev-local/locks/requirements.installed.txt',
          ROOT / 'dev-local/locks/runtime-manifest.json', ROOT / 'dev-local/locks/images.env',
          ROOT / 'docs/IAM-DIRECT.md', ROOT / 'docs/DESKTOP-AUTH-PERSONAL-MODEL.md',
          ROOT / 'docs/PERSONAL-CLOUD-AND-PLUGINS-V3.md',
          ROOT / 'docs/DSH-UPGRADE-AND-EXTENSION-BOUNDARY.md']
files += [ROOT / 'docs/STORAGE-LIFECYCLE.md']
files += list((ROOT / 'dev-local/dsh').glob('*.mjs'))
files += [ROOT/'dev-local/dsh/package.json',ROOT/'dev-local/dsh/package-lock.json',ROOT/'dev-local/dsh/Dockerfile']
files += [p for p in (ROOT/'dev-local/dsh/vendor-community').rglob('*') if p.is_file()]
# Office binaries, Python and OCR weights are already in the fixed Linux image.
# Explicit audit/rebuild sources only: never recurse into Windows runtime/LO.
files += [ROOT / 'office-engine' / name for name in (
    'engine.py', 'requirements.in', 'requirements.lock.txt', 'requirements-linux.lock.txt',
    'ocr-config.json', 'ocr-models.json', 'prepare_ocr.py', 'THIRD-PARTY.md', 'README.md',
    'Dockerfile', 'build_linux.sh')]
files += [ROOT / 'dev-local/dsh/office-adapter' / name for name in (
    'index.mjs', 'adapter.mjs', 'transport.mjs', 'README.md')]
files += [p for p in (ROOT / 'ops/changsha-desktop').iterdir()
          if p.is_file() and not p.name.startswith('test_') and
          (p.name == '.env.example' or p.suffix in {'.py', '.sh', '.md', '.yaml', '.conf'})]
records = {}
for path in sorted(set(files)):
    if path.is_symlink(): raise SystemExit('Source symlink not accepted: ' + path.name)
    records['new-system/' + path.relative_to(ROOT).as_posix()] = {'sha256': digest(path), 'bytes': path.stat().st_size}
readme = '''# MEGMEET 长沙桌面服务端候选包 2026-09-18 r9

r9新增独立办公文件组件1.0.0，个人云Host包含PDF/Word/Excel/图像解析与另存处理、离线OCR及LibreOffice转换，DSH核心仍为0.1.6-alpha.1。原有授权续期、云Host排队、四个固定社区扩展和90+30天历史保留继续沿用，数据库结构不变。宿主机校验继续兼容Python3.10。

回调注册地址：https://platform-ai.megmeet.com/v1/iam/callback
用户已取得 https://iam-test.megmeet.com 测试应用凭据并确认回调登记一致，真实值由用户私下配置；生产切换另行验收。

包内包括五个固定Linux/amd64镜像、个人原生云Host、服务源码、数据库迁移、配置向导和Nginx片段。
不包含真实配置、服务秘密、员工凭据、聊天记录、个人工作区或本地测试账号。
它是已经本地验证的部署候选包，不代表已部署长沙或已通过真实公司IAM联调。

1. 用SHA256校验外层zip，在服务器解压到新的专用目录，不覆盖历史服务目录。
2. 进入 new-system/ops/changsha-desktop，运行 bash install.sh（现有Docker Nginx使用common-net时加 --nginx-network）。
3. 向导验证源文件与镜像、导入离线镜像、私下询问首次配置，执行迁移并等待服务就绪；不重复生成已有密钥。
4. 最后按 FIRST-PILOT.md 将经核对的Nginx路径和下载页接入现有HTTPS域名，核对真实公司登录/个人模型调用。没有两账号白名单或强制双账号人工验收前提。

桌面配套0.7.0下载分发附加包，含可选择目录的安装器及签名更新清单。云端历史90天未活动后入回收区，再保留30天。项目文件独立持久卷，不随聊天删除。
PostgreSQL是原生历史的闲置结构化投影；DSH原始日志保留兼容格式。Milvus/ES按项目需求接入，主包不启动。
办公输出必须写入当前工作区的新文件，保留原件；只读模式拒绝生成和修改。OCR/复杂排版/宏和外部数据兼容性有限，重要结果需要复核。办公引擎与DSH通过megmeet-office-v1协议连接，不更改DSH核心。实际模型驱动效果仍需按员工授权模型验证。
Linux Python/转换器/OCR权重已打进离线镜像，公司部署不需下载办公依赖或重新编译。包内build_linux.sh只用于本地MegmeetDev复现，不是公司部署步骤；不包含Windows Python/LibreOffice大目录。

镜像归档SHA256：IMAGE_SHA

专用本机服务端口18091。脚本不清理旧服务，不修改Nginx，不自动申请证书。
真实IAM client_id/client_secret、目录授权以及测试IAM与TokenKey兼容性仍需公司提供/确认。
数据库密码和会话加密密钥由向导首次随机生成，后续保留；不应反复重置。

本机工作区运行在Windows桌面；手动进入的个人云端原生DSH、云历史和文件、IAM/个人模型代理运行在长沙。
另有独立Windows公司版候选包，固定连接 https://platform-ai.megmeet.com，不依赖WSL。
Windows Authenticode企业代码签名尚未接入。默认2个同时活动的云Host，其他用户有界排队；应用900秒授权可自动续期，但不能延长公司IAM自身有效期。提供停机备份工具，机外备份介质、告警渠道与硬磁盘额度仍须公司环境配置。不能将本地通过等同全员生产容量验收。
'''.replace('IMAGE_SHA', images['archive']['sha256'])
manifest = {'date': '2026-09-18', 'callback': 'https://platform-ai.megmeet.com/v1/iam/callback',
            'company_deployed': False, 'real_iam_verified': False, 'files': records}
with ZipFile(TARGET, 'x') as archive:
    for path in sorted(set(files)):
        archive.write(path, 'new-system/' + path.relative_to(ROOT).as_posix(), compress_type=ZIP_DEFLATED)
    archive.write(RELEASE / 'images.tar.gz', 'images.tar.gz', compress_type=ZIP_STORED)
    archive.write(RELEASE / 'images-manifest.json', 'images-manifest.json', compress_type=ZIP_DEFLATED)
    archive.writestr('INSTALL.md', readme, compress_type=ZIP_DEFLATED)
    archive.writestr('release-manifest.json', json.dumps(manifest, indent=2) + '\n', compress_type=ZIP_DEFLATED)
with ZipFile(TARGET) as archive:
    if archive.testzip() is not None: raise SystemExit('Zip CRC verification failed')
checksum = digest(TARGET)
TARGET.with_suffix('.zip.sha256').write_text(checksum + '  ' + TARGET.name + '\n')
print('SERVER_RELEASE_ZIP: PASS')
print('SOURCE_FILES:', len(records))
print('PACKAGE_BYTES:', TARGET.stat().st_size)
print('PACKAGE_SHA256:', checksum)
