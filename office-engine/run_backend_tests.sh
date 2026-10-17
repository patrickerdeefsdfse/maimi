#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
export DOCKER_HOST=unix:///var/run/docker.sock
db=$(docker inspect --format '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}' megmeet-dev-local-postgres-1)
redis=$(docker inspect --format '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}' megmeet-dev-local-redis-1)
test -n "$db" && test -n "$redis"
export IAM_TEST_DATABASE_URL="postgresql://megmeet_dev:local-synthetic-database-only@$db:5432/megmeet_dev"
export REDIS_TEST_URL="redis://$redis:6379/15"
/home/megmeet/.venv/bin/python -m pytest /home/megmeet/project/new-system/services/gateway/tests -q --basetemp="$(mktemp -d /tmp/office-backend-test.XXXXXX)"
