#!/usr/bin/env bash
set -euo pipefail
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
if [ "$#" -ne 2 ]; then
  echo 'Usage: bash load-images.sh /absolute/path/images.tar.gz EXPECTED_SHA256' >&2
  exit 1
fi
archive="$1"
expected="$2"
[[ "$archive" = /* ]] && [ -f "$archive" ]
[[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]]
printf '%s  %s\n' "$expected" "$archive" | sha256sum -c -
gzip -dc -- "$archive" | docker image load
echo 'OFFLINE_IMAGES_LOADED: PASS'
echo 'SERVICES_STARTED: NO'
