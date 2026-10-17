set -euo pipefail
TASK=/home/megmeet/dsh020-r41
SRC=/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime
docker run --rm --network none --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --memory 512m --memory-swap 512m --pids-limit 32 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,uid=1000,gid=1000 --mount "type=bind,source=$SRC,target=/probe,readonly" --mount "type=bind,source=$TASK/runtime-linux-x64,target=/opt/dsh,readonly" --mount "type=bind,source=$TASK/old-runtime,target=/old-upstream,readonly" -e NARB_DISABLE_NATIVE_CACHE=1 --entrypoint node maimi-r26-worker-offline:20260928 --test /probe/tests/test-history-incremental.mjs /probe/tests/test-history-continuation.mjs > "$TASK/evidence/history020-prebuild.log" 2>&1
cat "$TASK/evidence/history020-prebuild.log"
cp "$TASK/evidence/history020-prebuild.log" "$SRC/history020-prebuild.log"
