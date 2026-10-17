#!/usr/bin/env bash
set -Eeuo pipefail

# Safe first deployment for an isolated Linux test directory.
# It never removes containers, volumes, images, or old projects.
ROOT="${MEGMEET_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

test -f .env || {
  echo "ERROR: $ROOT/.env is missing; copy .env.example to .env and fill test values"
  exit 1
}

docker compose --env-file .env --profile core config >/dev/null
docker compose --env-file .env --profile core up -d --build postgres redis db-migrate gateway

echo "Waiting for Gateway health..."
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${GATEWAY_PORT:-18080}/ready" >/dev/null; then
    echo "GATEWAY_HEALTH: PASS"
    exit 0
  fi
  sleep 2
done
echo "GATEWAY_HEALTH: FAIL"
exit 1
