set -euo pipefail
TASK=/home/megmeet/dsh020-r41
CID=$(docker create --network none --entrypoint true maimi-r26-worker-offline:20260928)
trap 'docker rm "$CID" >/dev/null' EXIT
docker cp "$CID:/opt/dsh/community.patch.json" "$TASK/base-export/"
