#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${MEGMEET_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
docker compose --env-file .env --profile core ps
MIGRATE_ID="$(docker compose --env-file .env --profile core ps -aq db-migrate)"
if [ -z "$MIGRATE_ID" ]; then
  echo "DB_MIGRATION: FAIL (db-migrate container is missing)"
  exit 1
fi
if [ "$(docker inspect --format '{{.State.ExitCode}}' "$MIGRATE_ID")" != "0" ]; then
  echo "DB_MIGRATION: FAIL (db-migrate did not exit successfully)"
  exit 1
fi
echo "DB_MIGRATION: PASS"
curl -fsS --max-time 5 "http://127.0.0.1:${GATEWAY_PORT:-18080}/health"
echo
curl -fsS --max-time 5 "http://127.0.0.1:${GATEWAY_PORT:-18080}/ready"
echo
docker compose --env-file .env --profile core config >/dev/null
echo "COMPOSE_CONFIG: PASS"
