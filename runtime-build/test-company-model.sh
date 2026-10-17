set -euo pipefail
R=/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime
docker run --rm --network none --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --mount "type=bind,source=$R,target=/candidate,readonly" --entrypoint node maimi-dsh020-r41:local --test /candidate/tests/test-company-model-policy.mjs > "$R/company-model-policy-unit.log"
