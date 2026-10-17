#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
cd "$(dirname "$0")"
pg_ip=$(docker inspect --format '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}' megmeet-dev-local-postgres-1)
export IAM_TEST_DATABASE_URL="postgresql://megmeet_dev:local-synthetic-database-only@${pg_ip}:5432/megmeet_dev"
/home/megmeet/.venv/bin/python -m pytest ../services/gateway/tests/test_employee_plugins.py ../services/gateway/tests/test_personal_cloud.py -q
