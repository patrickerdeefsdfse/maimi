#!/usr/bin/env bash
set -euo pipefail
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec python3 configure-office.py "$@"
