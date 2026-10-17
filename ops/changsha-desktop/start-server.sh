#!/usr/bin/env bash
set -euo pipefail
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
unset POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD IAM_BASE_URL IAM_AUTHORIZATION_BASE_URL
unset IAM_CLIENT_ID IAM_DIRECTORY_ORG_URL IAM_DIRECTORY_ACCOUNT_URL IAM_DIRECTORY_API_KEY
unset IAM_CLIENT_SECRET_SOURCE IAM_DIRECTORY_SECRET_SOURCE IAM_TOKEN_ENCRYPTION_KEY_SOURCE PERSONAL_KEY_SERVICE_SECRET_SOURCE
unset CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES CLOUD_TASK_GLOBAL_LIMIT CLOUD_TASK_WORKSPACE_LIMIT CLOUD_TASK_EMPLOYEE_LIMIT CLOUD_TASK_TIMEOUT_SECONDS
unset PERSONAL_CLOUD_MANAGER_SECRET_SOURCE PERSONAL_CLOUD_MAX_ACTIVE_HOSTS PERSONAL_CLOUD_IDLE_SECONDS
unset CONNECTOR_CONFIG_SOURCE DINGTALK_CLIENT_ID DINGTALK_CORP_ID DINGTALK_ROBOT_CODE DINGTALK_OAUTH_SCOPES BRAVE_SEARCH_API_KEY_FILE
unset OFFICE_SERVICE_SECRET_SOURCE OFFICE_CONCURRENCY OFFICE_QUEUE_LIMIT OFFICE_OWNER_PENDING OFFICE_MAX_JOBS
unset OFFICE_SPOOL_BYTES OFFICE_TTL_SECONDS OFFICE_TIMEOUT_SECONDS OFFICE_CHILD_MEMORY_BYTES OFFICE_MEMORY_LIMIT OFFICE_CPU_LIMIT
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
compose=(docker compose -p megmeet-desktop-server --env-file .env -f compose.yaml -f personal-cloud.compose.yaml)
check_args=(--check)
nginx_network=0
allow_running=0
connectors=0
shared_office=0
distribution_dir=''
while [ "$#" -gt 0 ]; do
  option=$1
  case "$option" in
    --nginx-network) [ "$nginx_network" = 0 ] || exit 1; nginx_network=1 ;;
    --allow-running) [ "$allow_running" = 0 ] || exit 1; allow_running=1 ;;
    --connectors) [ "$connectors" = 0 ] || exit 1; connectors=1 ;;
    --shared-office) [ "$shared_office" = 0 ] || exit 1; shared_office=1 ;;
    --distribution-dir) [ -z "$distribution_dir" ] && [ "$#" -ge 2 ] || exit 1; shift; distribution_dir=$1 ;;
    *) echo 'Usage: bash start-server.sh [--nginx-network] [--allow-running] [--connectors] [--shared-office] [--distribution-dir PATH]' >&2; exit 1 ;;
  esac
  shift
done
if [ "$nginx_network" = 1 ]; then
    compose+=(-f compose.nginx-network.yaml)
    check_args+=(--nginx-network)
    docker network inspect common-net >/dev/null
fi
if [ "$allow_running" = 1 ]; then check_args+=(--allow-running); fi
python3 verify-package.py
python3 configure.py "${check_args[@]}"
if [ -n "$distribution_dir" ]; then
    distribution_dir=$(cd -- "$distribution_dir" && pwd -P)
    [ -f "$distribution_dir/start-distribution.sh" ] && [ -f "$distribution_dir/nginx-maimi.locations.conf" ] || { echo 'DISTRIBUTION_DIRECTORY_INVALID' >&2; exit 1; }
    (cd -- "$distribution_dir" && python3 verify-distribution.py)
    entry_args=(--distribution-dir "$distribution_dir" --output runtime-entry.locations.conf)
    if [ "$nginx_network" = 1 ]; then entry_args+=(--container-nginx); fi
    python3 render-nginx-entry.py "${entry_args[@]}"
fi
if [ "$connectors" = 1 ]; then
    python3 check-connectors.py
    compose+=(--env-file .connectors.env -f connectors.compose.yaml)
    # Quiet validation avoids printing expanded database or service secrets.
    "${compose[@]}" config --quiet
fi
if [ "$shared_office" = 1 ]; then
    office_args=(--check)
    if [ "$nginx_network" = 1 ]; then office_args+=(--nginx-network); fi
    if [ "$connectors" = 1 ]; then office_args+=(--connectors); fi
    python3 configure-office.py "${office_args[@]}"
    compose+=(--env-file .office.env -f office.compose.yaml)
    "${compose[@]}" config --quiet
fi
# Image content, not mutable tags alone, must match the exported release.
python3 verify-images.py ../../../images-manifest.json
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 180
if [ -n "$distribution_dir" ]; then
    distribution_args=()
    if [ "$nginx_network" = 1 ]; then distribution_args+=(--nginx-network); fi
    bash "$distribution_dir/start-distribution.sh" "${distribution_args[@]}"
fi
echo 'CHANGSHA_SERVER_SERVICES: STARTED'
echo 'NGINX_CONFIG_CHANGED: NO'
echo 'REAL_IAM_AND_PERSONAL_MODEL_VALIDATION: PENDING'
if [ "$connectors" = 1 ]; then echo 'REAL_DINGTALK_AND_SEARCH_VALIDATION: PENDING'; fi
if [ "$shared_office" = 1 ]; then echo 'SHARED_OFFICE_REAL_WORKLOAD_AND_CAPACITY: PENDING'; fi
