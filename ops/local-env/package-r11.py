"""Package the complete r11 allowlist with the verified offline images."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'releases/changsha-personal-cloud-20260921-r11'
TARGET = ROOT / 'releases/MEGMEET-Changsha-Personal-Cloud-20260921-r11.zip'

def checked_path(path):
    path = Path(path).absolute()
    base = ROOT.absolute()
    try:
        relative = path.relative_to(base)
    except ValueError:
        raise SystemExit('Source path not accepted: '+path.name) from None
    cursor = base
    for part in ('', *relative.parts):
        cursor = cursor/part
        if cursor.is_symlink(): raise SystemExit('Source symlink not accepted: '+path.name)
    if base not in path.resolve().parents or not path.is_file():
        raise SystemExit('Source path not accepted: '+path.name)
    return path

def digest(path):
    value = hashlib.sha256()
    with checked_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): value.update(chunk)
    return value.hexdigest()

def community_files():
    # The reviewed vendor manifest is the allowlist. Do not recursively copy
    # accidental dotfiles, credentials, scratch files or nested symlinks.
    base = ROOT/'dev-local/dsh/vendor-community'
    manifest = checked_path(base/'manifest.json')
    value = json.loads(manifest.read_text())
    files = [manifest]
    for plugin in value['plugins']:
        directory = plugin['directory']
        if not isinstance(directory,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',directory):
            raise SystemExit('Community directory not accepted')
        for name,expected in plugin['files'].items():
            relative = PurePosixPath(name)
            if (not name or relative.is_absolute() or '\\' in name
                    or any(part in {'.','..'} or part.startswith('.') for part in relative.parts)
                    or (relative.suffix not in {'.js','.mjs','.cjs','.json','.yml','.yaml','.md','.txt','.css','.svg','.png','.ico','.html'}
                        and relative.name != 'LICENSE')):
                raise SystemExit('Community file not accepted')
            path = checked_path(base/directory/name)
            if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected) or digest(path)!=expected:
                raise SystemExit('Community source differs from reviewed manifest')
            files.append(path)
    return files

def source_files():
    files = list((ROOT/'services/gateway/app').glob('*.py'))
    files += [p for p in (ROOT/'services/personal-cloud').iterdir() if p.is_file()
              and not p.name.startswith(('test-', 'test_'))
              and (p.suffix in {'.py','.mjs','.md'} or p.name.startswith('Dockerfile'))]
    files += list((ROOT/'db/migrations').glob('*.sql'))
    files += [ROOT/name for name in (
        'services/gateway/requirements.txt','dev-local/Dockerfile.gateway',
        'dev-local/install_dependencies.py','dev-local/locks/requirements.installed.txt',
        'dev-local/locks/runtime-manifest.json','dev-local/locks/images.env',
        'docs/IAM-DIRECT.md','docs/DESKTOP-AUTH-PERSONAL-MODEL.md',
        'docs/PERSONAL-CLOUD-AND-PLUGINS-V3.md','docs/DSH-UPGRADE-AND-EXTENSION-BOUNDARY.md',
        'docs/STORAGE-LIFECYCLE.md','docs/ENTERPRISE-CONNECTORS.md','docs/WEB-CLOUD-MULTICLIENT.md')]
    files += list((ROOT/'dev-local/dsh').glob('*.mjs'))
    files += [ROOT/'dev-local/dsh'/name for name in ('package.json','package-lock.json','Dockerfile')]
    files += community_files()
    # Large runtimes and OCR weights are already in the verified Linux image.
    files += [ROOT/'office-engine'/name for name in (
        'engine.py','requirements.in','requirements.lock.txt','requirements-linux.lock.txt',
        'ocr-config.json','ocr-models.json','prepare_ocr.py','THIRD-PARTY.md','README.md','Dockerfile','build_linux.sh')]
    files += [ROOT/'dev-local/dsh/office-adapter'/name for name in ('index.mjs','adapter.mjs','transport.mjs','README.md')]
    files += [ROOT/'toolkit'/name for name in ('session-tools.mjs','adapter/index.mjs','README.md','package.json','package-lock.json')]
    files += [p for p in (ROOT/'ops/changsha-desktop').iterdir() if p.is_file()
              and not p.name.startswith('test_') and (p.name in {'.env.example','connectors.env.example'}
              or p.suffix in {'.py','.sh','.md','.yaml','.conf'})]
    files += [Path(__file__).resolve(),ROOT/'ops/local-env/export-r11-images.py']
    return sorted(set(files))

def main():
    temporary = TARGET.with_suffix('.zip.partial')
    if TARGET.exists() or temporary.exists():
        raise SystemExit('Release archive already exists; refusing to replace it')
    images = json.loads(checked_path(RELEASE/'images-manifest.json').read_text())
    if images.get('release') != '20260921-r11' or images.get('iam_target') != 'https://iam.megmeet.com':
        raise SystemExit('Wrong image release or IAM target')
    if digest(RELEASE/'images.tar.gz') != images['archive']['sha256']:
        raise SystemExit('Offline image archive checksum differs')
    actual_app = {p.name:digest(p) for p in (ROOT/'services/gateway/app').glob('*.py')}
    if actual_app != images['application_source_sha256']:
        raise SystemExit('Gateway source changed after the reviewed image export')
    required_native = {
        'services/personal-cloud/'+name for name in ('manager.py','worker.mjs','catalog.mjs',
        'history-native.mjs','history-server.mjs','guard-runtime.mjs','task-authority.mjs','task-control-plugin.mjs')}
    required_native |= {'toolkit/session-tools.mjs','toolkit/adapter/index.mjs',
        'dev-local/dsh/enterprise-native.mjs','office-engine/engine.py',
        'dev-local/dsh/office-adapter/index.mjs','dev-local/dsh/office-adapter/adapter.mjs','dev-local/dsh/office-adapter/transport.mjs'}
    if not required_native <= set(images['personal_native_source_sha256']):
        raise SystemExit('Required native/toolkit source verification missing')
    for relative,expected in images['personal_native_source_sha256'].items():
        if digest(ROOT/relative) != expected:
            raise SystemExit('Native source changed after the reviewed image export: '+relative)
    if images.get('office',{}).get('capabilities',{}).get('protocol') != 'megmeet-office-v1':
        raise SystemExit('Office image verification evidence missing')
    if images.get('native',{}).get('dsh') != '0.1.6-alpha.2' or images['native'].get('packages') != 250:
        raise SystemExit('Coherent alpha2 image evidence missing')
    files = source_files()
    records = {}
    for path in files:
        checked_path(path)
        records['new-system/'+path.relative_to(ROOT).as_posix()] = {'sha256':digest(path),'bytes':path.stat().st_size}
    required = {'new-system/toolkit/session-tools.mjs','new-system/toolkit/adapter/index.mjs',
        'new-system/services/gateway/app/connector_web_reader.py',
        'new-system/services/personal-cloud/task-authority.mjs',
        'new-system/ops/changsha-desktop/connectors.env.example',
        'new-system/ops/changsha-desktop/render-nginx-entry.py'}
    if not required <= set(records): raise SystemExit('Required enterprise release sources missing')
    readme = '''# MEGMEET 公司服务器完整部署包 r11 / 2026-09-21

配套桌面及下载分发0.8.0，DSH前端和运行时统一0.1.6-alpha.2（250包）。
包含五个固定linux/amd64镜像、独立办公与会话工具组件、网页/桌面共享个人云、SSO及个人TokenKey代理、部署脚本与迁移。
直接使用IAM正式服 https://iam.megmeet.com。应用首页 https://platform-ai.megmeet.com/maimi/；回调 https://platform-ai.megmeet.com/v1/iam/callback。

此包已完成本地候选验证，真实公司IAM/TokenKey、HTTPS入口、钉钉与搜索服务仍须公司部署后联调。正式OAuth凭据由用户在服务器交互填写；不复用测试应用凭据或测试IAM固定IP。不要求IAM目录接口密钥。

1. 在堡垒机会话上传、校验外层ZIP，解压到新目录；不覆盖r9/r10。
2. 运行包内verify-package.py及server-preflight.py --network，回传不含秘密的检查报告。
3. 校验并导入包内五镜像，私密运行configure.sh。镜像只追加新标签，不删除旧镜像或数据。
4. 按FIRST-PILOT.md运行start-server.sh，同时指定0.8.0下载分发目录。已有Docker Nginx/common-net时使用--nginx-network。
5. 备份现有平台站点后合并生成的完整入口，先nginx -t再reload；检查正式IAM登录、个人模型和网页聊天。服务启动不等于入口已接入。

详细分步见 new-system/ops/changsha-desktop/FIRST-PILOT.md。install.sh是合并入口，仅在已检查步骤与网络后使用。
发行包不包含真实.env、服务密钥、员工凭据、聊天、个人工作区或测试账号。不要把配置文件或秘密内容回传。

钉钉和联网搜索对员工可见，实际调用仍需配置公司应用/员工授权与Brave服务。默认不依赖这些密钥启动聊天；按connectors.env.example另行启用。浏览器及电脑操作仅桌面本机可显式开启。
办公引擎保留PDF/Word/Excel/PPT/图像、离线OCR和LibreOffice，输出另存原件保留；复杂格式结果需要复核。
默认最多2个同时活跃的个人云Host，其他有界排队。网页增加服务器负载，现有容量并非全员并发承诺。
程序与员工数据分开，原生历史格式3与旧桌面0.7.1基本云功能已兼容验证；完整新功能使用0.8.0。
数据库密码和加密密钥只在首次生成，后续保留。停机备份与恢复工具见MAINTENANCE.md；机外备份和企业Windows代码签名尚待环境配置。

镜像归档SHA256：IMAGE_SHA
'''.replace('IMAGE_SHA',images['archive']['sha256'])
    manifest = {'date':'2026-09-21','release':'20260921-r11','desktop':'0.8.0','dsh':'0.1.6-alpha.2',
        'iam_target':'https://iam.megmeet.com','callback':'https://platform-ai.megmeet.com/v1/iam/callback',
        'company_deployed':False,'real_iam_verified':False,'files':records}
    with ZipFile(temporary,'x') as archive:
        for path in files:
            archive.write(path,'new-system/'+path.relative_to(ROOT).as_posix(),compress_type=ZIP_DEFLATED)
        archive.write(RELEASE/'images.tar.gz','images.tar.gz',compress_type=ZIP_STORED)
        archive.write(RELEASE/'images-manifest.json','images-manifest.json',compress_type=ZIP_DEFLATED)
        archive.writestr('INSTALL.md',readme,compress_type=ZIP_DEFLATED)
        archive.writestr('release-manifest.json',json.dumps(manifest,indent=2)+'\n',compress_type=ZIP_DEFLATED)
    with ZipFile(temporary) as archive:
        if archive.testzip() is not None: raise SystemExit('Zip CRC verification failed')
        for name,record in records.items():
            value=archive.read(name)
            if len(value)!=record['bytes'] or hashlib.sha256(value).hexdigest()!=record['sha256']:
                raise SystemExit('Final ZIP source verification failed: '+name)
        if set(archive.namelist()) != set(records)|{'images.tar.gz','images-manifest.json','INSTALL.md','release-manifest.json'}:
            raise SystemExit('Unexpected ZIP entries')
    temporary.rename(TARGET)
    checksum=digest(TARGET)
    TARGET.with_suffix('.zip.sha256').write_text(checksum+'  '+TARGET.name+'\n')
    print('SERVER_RELEASE_ZIP: PASS',flush=True)
    print('SOURCE_FILES:',len(records),flush=True)
    print('PACKAGE_BYTES:',TARGET.stat().st_size,flush=True)
    print('PACKAGE_SHA256:',checksum,flush=True)

if __name__ == '__main__': main()
