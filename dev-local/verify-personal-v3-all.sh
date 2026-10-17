#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
cd "$(dirname "$0")"
mkdir -p evidence
pg_ip=$(docker inspect --format '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}' megmeet-dev-local-postgres-1)
test -n "$pg_ip"
export IAM_TEST_DATABASE_URL="postgresql://megmeet_dev:local-synthetic-database-only@${pg_ip}:5432/megmeet_dev"
/home/megmeet/.venv/bin/python -m pytest ../services/gateway/tests . ../ops/changsha-desktop -q -rs \
  --basetemp="$(mktemp -d /tmp/megmeet-personal-v3-final-tests.XXXXXX)" \
  2>&1 | tee evidence/personal-v3-final-regression-20260918.txt
unset IAM_TEST_DATABASE_URL
