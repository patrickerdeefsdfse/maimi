#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev || { echo 'Only local MegmeetDev is allowed' >&2; exit 1; }
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
unset LOCAL_MILVUS_IMAGE LOCAL_ELASTIC_IMAGE
export DOCKER_HOST=unix:///var/run/docker.sock
cd -P "$(dirname "${BASH_SOURCE[0]}")"
compose=(docker compose --env-file images.env -f compose.yaml -p megmeet-dev-retrieval)
case "${1:-status}" in
  pull) "${compose[@]}" pull milvus elasticsearch ;;
  start|recreate)
    internal=$(docker network inspect megmeet-dev-local_local-only --format '{{.Internal}}')
    test "$internal" = true || { echo 'Local internal network required' >&2; exit 1; }
    extra=()
    if [ "$1" = recreate ]; then extra=(--force-recreate); fi
    # Start sequentially to avoid simultaneous initialization memory peaks.
    "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 180 "${extra[@]}" milvus
    "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 180 "${extra[@]}" elasticsearch
    ;;
  stop) "${compose[@]}" stop ;;
  seed|verify|cleanup) "${compose[@]}" --profile test run --rm --no-deps probe "$1" ;;
  lock) python3 capture.py ;;
  status)
    "${compose[@]}" ps -a
    for service in milvus elasticsearch; do
      container=$("${compose[@]}" ps -a -q "$service")
      if [ -n "$container" ]; then
        docker inspect --format '{{.Name}} OOM={{.State.OOMKilled}} ports={{json .HostConfig.PortBindings}}' "$container"
        docker stats --no-stream --format '{{.Name}} MEM={{.MemUsage}} CPU={{.CPUPerc}}' "$container"
      fi
    done
    ;;
  *) echo 'Use pull/start/recreate/stop/seed/verify/cleanup/lock/status' >&2; exit 2 ;;
esac
