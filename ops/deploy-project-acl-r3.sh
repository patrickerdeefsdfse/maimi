#!/usr/bin/env bash
# Executed by the user on the existing new-system server only.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
LIVE=/data/megmeet-ai-platform-new/new-system
IMAGE=megmeet-ai-gateway:tokenkey-acl-20260915-r3
BASELINE_MAIN=39009d8d7d2e21c594394eecc1a09ddb286d6725796eca47d35475d241dd8293
test -f "$LIVE/.env"
test -f "$SRC/services/gateway/app/access_control.py"
test "$SRC" != "$LIVE"
export PYTHONIOENCODING=utf-8

# Verify the r2 baseline before overwriting a server file. Only these files
# change: Gateway entry/API, ACL implementation, and one additive index.
python3 - "$SRC" "$LIVE" "$BASELINE_MAIN" <<'PY'
from pathlib import Path
from hashlib import sha256
import shutil, sys, time
src, live = map(Path, sys.argv[1:3])
main = Path('services/gateway/app/main.py')
digest = lambda p: sha256(p.read_bytes()).hexdigest()
if digest(live / main) not in {sys.argv[3], digest(src / main)}:
    raise SystemExit('SERVER_SOURCE_DIFFERS: main.py differs from r2; stopped before changes')
for rel in (Path('services/gateway/app/access_control.py'), Path('db/migrations/007_project_acl_indexes.sql')):
    if (live / rel).exists() and digest(live / rel) != digest(src / rel):
        raise SystemExit('SERVER_SOURCE_DIFFERS: ' + str(rel) + '; stopped before changes')
backup = live.parent / ('acl-before-r3-' + time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(mode=0o700)
shutil.copy2(live / '.env', backup / '.env')
for rel in (main, Path('services/gateway/app/access_control.py'), Path('db/migrations/007_project_acl_indexes.sql')):
    dst = live / rel
    if dst.exists():
        old = backup / rel
        old.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, old)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / rel, dst)
print('ACL_SOURCE_UPDATE: PASS')
print('BACKUP:', backup)
PY

cd "$LIVE"
dc() { docker compose --env-file .env -f compose.yaml -p megmeet-ai-platform-new --profile core "$@"; }
dc config >/dev/null
GATEWAY_IMAGE="$IMAGE" dc build gateway
docker run --rm --network none --entrypoint python "$IMAGE" -c \
 'import deepseek_harness; from app import main, access_control; print("ACL_IMAGE_IMPORT: PASS")'
GATEWAY_IMAGE="$IMAGE" dc run --rm --no-deps db-migrate
GATEWAY_IMAGE="$IMAGE" dc run --rm --no-deps -T gateway python - < "$SRC/ops/probe-project-acl.py"

python3 - "$IMAGE" <<'PY'
from pathlib import Path
import sys
p = Path('.env')
lines = [line for line in p.read_text().splitlines() if not line.startswith('GATEWAY_IMAGE=')]
lines.append('GATEWAY_IMAGE=' + sys.argv[1])
p.write_text('\n'.join(lines) + '\n')
p.chmod(0o600)
PY
GATEWAY_IMAGE="$IMAGE" dc up -d --no-deps --force-recreate gateway
ready=0
for n in $(seq 1 20); do
    if curl -fsS --max-time 2 http://127.0.0.1:18090/ready >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
test "$ready" = 1 || { echo 'ACL_GATEWAY_READY: FAIL'; exit 1; }
docker exec megmeet-ai-platform-new-gateway-1 python -c \
 'import os; assert os.getenv("AUTH_REQUIRED", "").lower()=="true", "SSO must remain enabled"; print("ACL_SSO_REQUIRED: PASS")'
docker exec nginx-https nginx -t
docker exec nginx-https nginx -s reload
curl -fksS --max-time 5 --resolve platform-ai.megmeet.com:443:127.0.0.1 https://platform-ai.megmeet.com/megmeet-ai-new/health
echo
echo 'PROJECT_ACL_R3_DEPLOY: PASS'
