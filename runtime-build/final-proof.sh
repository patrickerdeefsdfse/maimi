set -euo pipefail
R=/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime
docker run --rm --network none --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --entrypoint node maimi-dsh020-r41:local /opt/personal-cloud/verify-runtime020.mjs > "$R/native-runtime-proof.json"
cp /home/megmeet/dsh020-r41/runtime-linux-x64/package-lock.json "$R/package-lock-linux-x64.json"
