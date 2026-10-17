set -euo pipefail
TASK=/home/megmeet/dsh020-r41
SRC=/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime
docker run --rm --network none --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --memory 512m --memory-swap 512m --pids-limit 32 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=96m,uid=1000,gid=1000 --mount "type=bind,source=$SRC,target=/probe,readonly" --entrypoint node maimi-r26-worker-offline:20260928 --test /probe/tests/test-history-independent-review.mjs > "$TASK/evidence/history-independent-review.log" 2>&1
cat "$TASK/evidence/history-independent-review.log"
cp "$TASK/evidence/history-independent-review.log" "$SRC/history-independent-review.log"
