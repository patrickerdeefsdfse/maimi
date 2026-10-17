#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev || { printf 'Only the local MegmeetDev WSL distribution is allowed\n' >&2; exit 1; }
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
export DOCKER_HOST=unix:///var/run/docker.sock
SOURCE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$SOURCE_ROOT/dev-local"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
ENV_FILE=defaults.env
if [ -s locks/images.env ]; then ENV_FILE=locks/images.env; fi
compose=(docker compose --env-file "$ENV_FILE" -f compose.yaml -p megmeet-dev-local)
if [[ "${1:-status}" = up || "${1:-status}" = start || "${1:-status}" = retrieval-start ]]; then
  for attempt in $(seq 1 40); do
    if docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
  docker info --format '{{.ServerVersion}}' >/dev/null
fi
case "${1:-status}" in
  status)
    printf 'ENVIRONMENT: local MegmeetDev; synthetic company interfaces\n'
    uname -sr
    python3 --version
    node --version
    docker version --format 'DOCKER_SERVER: {{.Server.Version}}'
    "${compose[@]}" ps -a
    ;;
  up)
    build_args=()
    if [ -r /run/megmeet-docker-proxy.env ]; then
      # Public package downloads only. Docker's predefined build proxy args do
      # not become application ENV settings; the runtime stays on its own network.
      proxy=$(sed -n 's/^HTTPS_PROXY=//p' /run/megmeet-docker-proxy.env)
      if [ -n "$proxy" ]; then
        build_args=(--build-arg "HTTP_PROXY=$proxy" --build-arg "HTTPS_PROXY=$proxy")
      fi
    fi
    # BuildKit obtains registry authorization through the client as well as the
    # daemon, so both the CLI and build steps need the local download proxy.
    HTTP_PROXY="${proxy:-}" HTTPS_PROXY="${proxy:-}" http_proxy="${proxy:-}" https_proxy="${proxy:-}" \
      "${compose[@]}" build "${build_args[@]}"
    "${compose[@]}" up -d --wait --wait-timeout 120
    ;;
  restart) "${compose[@]}" restart gateway ;;
  restart-dsh) "${compose[@]}" restart dsh ;;
  start) "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120 ;;
  stop)
    bash retrieval/run.sh stop
    "${compose[@]}" stop
    ;;
  retrieval-start) bash retrieval/run.sh start ;;
  retrieval-stop) bash retrieval/run.sh stop ;;
  retrieval-status) bash retrieval/run.sh status ;;
  retrieval-verify) bash retrieval/run.sh verify ;;
  smoke) python3 iam_smoke.py --url http://127.0.0.1:28090 ;;
  native-smoke) python3 dsh/smoke.py ;;
  lock) python3 capture_versions.py ;;
  test)
    PYTHON=/home/megmeet/.venv/bin/python
    "$PYTHON" -m pytest "$SOURCE_ROOT/dev-local" -q
    "$PYTHON" -m pytest "$SOURCE_ROOT/services/gateway/tests" -q --basetemp="$(mktemp -d /tmp/megmeet-gateway-tests.XXXXXX)"
    "$PYTHON" -m pytest "$SOURCE_ROOT/services/native-entry/tests" -q --basetemp="$(mktemp -d /tmp/megmeet-entry-tests.XXXXXX)"
    node --test "$SOURCE_ROOT/services/native-entry/tests/renewal-controller.test.mjs" "$SOURCE_ROOT/services/native-entry/tests/renewal-browser.test.mjs"
    ;;
  *) printf 'Unsupported development action\n' >&2; exit 2 ;;
esac
