#!/usr/bin/env bash
# Run with bash; all strict shell options stay in this child, not the SSH shell.
set -Eeuo pipefail
probe="${1:-$(dirname "$0")/probe-dsh-native-web.py}"
gateway=megmeet-ai-new-r10-gateway-1
name="megmeet-native-web-probe-$(date +%s)-$$"
test -f "$probe" || { echo "MISSING_PROBE: $probe"; exit 1; }
command -v timeout >/dev/null || { echo 'MISSING_TOOL: timeout'; exit 1; }
command -v docker >/dev/null || { echo 'MISSING_TOOL: docker'; exit 1; }
# Reuse the exact image currently running. Do not pull, build or restart it.
image=$(docker inspect --format '{{.Image}}' "$gateway")
[[ "$image" == sha256:* ]] || { echo 'IMAGE_LOOKUP: FAIL'; exit 1; }
# Do not take ownership of a pre-existing container, even on a name collision.
if docker inspect "$name" >/dev/null 2>&1; then
  echo 'PROBE_NAME_IN_USE: retry with a fresh script process'
  exit 1
fi
cleanup() {
  if docker rm -f "$name" >/dev/null 2>&1; then
    echo 'PROBE_CLEANUP: PASS'
  else
    echo "PROBE_CLEANUP: CHECK_REQUIRED ($name)"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "IMAGE_ID: $image"
echo 'SCOPE: temporary offline container; no ports, no data mounts, no keys'
set +e
timeout --signal=TERM --kill-after=8s 100s docker run --init -i \
  --name "$name" --pull never --network none --read-only \
  --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --cpus 1 --memory 1536m --memory-swap 1536m --pids-limit 256 \
  --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=512m \
  --entrypoint python "$image" - < "$probe"
rc=$?
set -e
if [[ "$rc" -ne 0 ]]; then
  docker inspect --format 'PROBE_OOM: {{.State.OOMKilled}}' "$name" 2>/dev/null || true
fi
echo "PROBE_EXIT_CODE: $rc"
exit "$rc"
