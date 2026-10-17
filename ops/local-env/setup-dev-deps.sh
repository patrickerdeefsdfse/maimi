#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
test "$(id -un)" = megmeet
SOURCE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
export PATH=/usr/local/bin:/usr/bin:/bin
if [ -r /run/megmeet-docker-proxy.env ]; then
    proxy=$(sed -n 's/^HTTPS_PROXY=//p' /run/megmeet-docker-proxy.env)
    export http_proxy="$proxy" https_proxy="$proxy" HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
fi
if [ ! -x /home/megmeet/.venv/bin/python ]; then
    python3 -m venv /home/megmeet/.venv
fi
PYTHON=/home/megmeet/.venv/bin/python
lock="$SOURCE_ROOT/ops/local-env/requirements-test.lock.txt"
if [ -s "$lock" ]; then
    "$PYTHON" -m pip install --no-deps -r "$lock"
else
    "$PYTHON" -m pip install -r "$SOURCE_ROOT/services/gateway/requirements.txt" -r "$SOURCE_ROOT/ops/local-env/requirements-dev.txt"
fi
"$PYTHON" -m pip check
mkdir -p /home/megmeet/reports
"$PYTHON" -m pip freeze > /home/megmeet/reports/development-requirements.txt
printf 'LINUX_DEVELOPMENT_DEPS: PASS\n'
