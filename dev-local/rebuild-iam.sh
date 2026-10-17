#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
cd "$(dirname "$0")"
proxy=$(sed -n 's/^HTTPS_PROXY=//p' /run/megmeet-docker-proxy.env)
compose=(docker compose --env-file locks/images.env -f compose.yaml -p megmeet-dev-local)
HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy" \
  "${compose[@]}" build --build-arg "HTTP_PROXY=$proxy" --build-arg "HTTPS_PROXY=$proxy" gateway mock-company
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120 gateway iam-directory mock-company edge
python3 iam_smoke.py
