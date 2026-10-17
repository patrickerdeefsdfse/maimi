#!/usr/bin/env bash
set -euo pipefail
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
python3 verify-distribution.py
compose=(docker compose -p megmeet-desktop-distribution -f compose.yaml)
if [ "$#" -gt 1 ]; then echo 'Usage: bash start-distribution.sh [--nginx-network]' >&2; exit 1; fi
if [ "$#" = 1 ]; then
  [ "$1" = --nginx-network ] || { echo 'Unsupported option' >&2; exit 1; }
  docker network inspect common-net >/dev/null
  compose+=(-f compose.nginx-network.yaml)
fi
expected=$(python3 -c 'import json; print(json.load(open("image-lock.json"))["platform_image_id"])')
reference=$(python3 -c 'import json; print(json.load(open("image-lock.json"))["reference"])')
actual=$(docker image inspect --platform linux/amd64 --format '{{.Id}}' "$reference")
[ "$actual" = "$expected" ] || { echo 'DISTRIBUTION_IMAGE_MISMATCH: load the matching verified server images first' >&2; exit 1; }
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
echo 'DISTRIBUTION_SERVICE: READY on 127.0.0.1:18092'
echo 'NGINX_AND_IAM: NOT_CHANGED'
echo 'PUBLIC_DOWNLOAD_URL: https://platform-ai.megmeet.com/maimi/ (requires Nginx route activation)'
