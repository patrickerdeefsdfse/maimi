#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
cd "$(dirname "$0")"
pg_ip=$(docker inspect --format '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}' megmeet-dev-local-postgres-1)
test -n "$pg_ip"
export IAM_TEST_DATABASE_URL="postgresql://megmeet_dev:local-synthetic-database-only@${pg_ip}:5432/megmeet_dev"
/home/megmeet/.venv/bin/python workspaces_v2_smoke.py | tee evidence/workspaces-v2-http-20260918.txt
