import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

root = Path('D:/MegmeetDev')
root.mkdir(parents=True, exist_ok=True)
registered = subprocess.check_output(['wsl.exe', '--list', '--quiet'])
registered = registered.decode('utf-16-le' if b'\x00' in registered else 'utf-8').splitlines()
if 'MegmeetDev' in [name.strip() for name in registered]:
    raise SystemExit('MegmeetDev already exists; keeping the installed distribution.')
url = 'https://releases.ubuntu.com/24.04.5/ubuntu-24.04.5-wsl-amd64.wsl'
expected = 'bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e'
image = root / 'ubuntu-24.04.5-wsl-amd64.wsl'
part = root / 'ubuntu-24.04.5-wsl-amd64.download'
last = 0
count = 0
print('DOWNLOAD_START', url, flush=True)
with urllib.request.urlopen(url, timeout=60) as response, part.open('wb') as output:
    while chunk := response.read(1024 * 1024):
        output.write(chunk)
        count += len(chunk)
        if time.monotonic() - last > 15:
            print('DOWNLOADED_MIB', round(count / 1024 / 1024, 1), flush=True)
            last = time.monotonic()
with part.open('rb') as content:
    actual = hashlib.file_digest(content, 'sha256').hexdigest()
if actual != expected:
    raise SystemExit('CHECKSUM_MISMATCH')
part.replace(image)
(root / 'ubuntu-image-provenance.json').write_text(json.dumps({
    'url': url, 'sha256': actual, 'bytes': count,
    'manifest': 'https://raw.githubusercontent.com/microsoft/WSL/master/distributions/DistributionInfo.json',
}, indent=2))
print('UBUNTU_IMAGE_SHA256: PASS', flush=True)
result = subprocess.run([
    'wsl.exe', '--install', '--from-file', str(image), '--name', 'MegmeetDev',
    '--location', 'D:\\MegmeetDev\\Ubuntu-24.04', '--no-launch',
], capture_output=True, timeout=600)
raw = result.stdout + result.stderr
print(raw.decode('utf-16-le' if b'\x00' in raw else 'utf-8', errors='replace'), flush=True)
print('UBUNTU_INSTALL_EXIT:', result.returncode, flush=True)
raise SystemExit(result.returncode)
