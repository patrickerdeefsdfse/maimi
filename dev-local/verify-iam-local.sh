#!/usr/bin/env bash
# Synthetic local resources only; SQL history tests use connection-local pg_temp.
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
cd "$(dirname "$0")"
mkdir -p evidence
pg_ip=$(docker inspect --format '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}' megmeet-dev-local-postgres-1)
test -n "$pg_ip"
export IAM_TEST_DATABASE_URL="postgresql://megmeet_dev:local-synthetic-database-only@${pg_ip}:5432/megmeet_dev"
/home/megmeet/.venv/bin/python -m pytest ../services/gateway/tests . -q \
  --basetemp="$(mktemp -d /tmp/megmeet-iam-final-tests.XXXXXX)" \
  2>&1 | tee evidence/iam-regression-20260917.txt
unset IAM_TEST_DATABASE_URL
python3 iam_smoke.py | tee evidence/iam-smoke-20260917.txt
python3 dsh/smoke.py | tee evidence/native-brand-smoke-20260917.txt
python3 capture_versions.py
