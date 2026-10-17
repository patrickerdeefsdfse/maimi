set -euo pipefail
TASK=/home/megmeet/dsh020-r41
mkdir -p "$TASK/base-export" "$TASK/build"
cd "$TASK"
CID=$(docker create --network none --entrypoint true maimi-r26-worker-offline:20260928)
trap 'docker rm "$CID" >/dev/null' EXIT
docker cp "$CID:/opt/personal-cloud" "$TASK/base-export/"
for ITEM in toolkit office-adapter enterprise-native.mjs workspace-pinned-overlay.mjs office-response-client.mjs; do
 docker cp "$CID:/opt/dsh/$ITEM" "$TASK/base-export/"
done
docker cp "$CID:/opt/dsh/node_modules/@dshp" "$TASK/base-export/"
docker image inspect maimi-r26-worker-offline:20260928 --format '{{.Id}}' > "$TASK/evidence/base-image.txt"
docker run --rm --network none --entrypoint node maimi-r26-worker-offline:20260928 --version
