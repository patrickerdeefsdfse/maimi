#!/usr/bin/env bash
# Run from the extracted, verified release. Credentials stay on the server.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
python3 verify-package.py
python3 server-preflight.py
if ! python3 verify-images.py ../../../images-manifest.json >/dev/null 2>&1; then
  expected=$(python3 -c 'import json; print(json.load(open("../../../images-manifest.json"))["archive"]["sha256"])')
  bash load-images.sh "$(realpath ../../../images.tar.gz)" "$expected"
fi
if [ ! -f .env ]; then bash configure.sh; fi
bash start-server.sh "$@"
echo '安装完成。按 FIRST-PILOT.md 接入已登记的 HTTPS 路径及下载页，核对真实 IAM 与个人模型调用。没有两账号白名单或强制双账号人工验收前提；默认云端并发仍为两个 Host。'
