#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
cd /home/megmeet/project/new-system
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
args=()
if [ -r /run/megmeet-docker-proxy.env ]; then
  proxy=$(sed -n 's/^HTTPS_PROXY=//p' /run/megmeet-docker-proxy.env)
  if [ -n "$proxy" ]; then args=(--build-arg "HTTP_PROXY=$proxy" --build-arg "HTTPS_PROXY=$proxy"); fi
fi
# A small owned build context excludes employee data, secrets and release archives.
stage=$(mktemp -d /tmp/megmeet-office-build.XXXXXX)
python3 - "$stage" <<'PY'
from pathlib import Path
import shutil,sys
root=Path.cwd(); out=Path(sys.argv[1])
for relative in ['office-engine/Dockerfile','office-engine/requirements-linux.lock.txt','office-engine/engine.py','office-engine/ocr-config.json','office-engine/ocr-models.json','services/personal-cloud/worker.mjs','services/personal-cloud/manager.py','services/personal-cloud/Dockerfile.manager']:
    target=out/relative; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(root/relative,target)
for relative in ['office-engine/ocr-models','dev-local/dsh/office-adapter']:
    shutil.copytree(root/relative,out/relative)
PY
docker build --pull=false "${args[@]}" -f "$stage/office-engine/Dockerfile" -t megmeet-personal-dsh:office-candidate "$stage"
printf 'OFFICE_LINUX_IMAGE: PASS\n'
docker build --pull=false -f "$stage/services/personal-cloud/Dockerfile.manager" -t megmeet-personal-cloud-manager:local "$stage"
printf 'OFFICE_MANAGER_IMAGE: PASS\n'
