#!/usr/bin/env bash
set -euo pipefail
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null
command -v docker >/dev/null
docker compose version >/dev/null
python3 configure.py "$@"
