set -euo pipefail
OUT=/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime
IMAGE=${1:?pass tested immutable candidate image ID}
case "$IMAGE" in sha256:*) ;; *) exit 2;; esac
python3 "$OUT/export-delivery.py" "$IMAGE"
python3 "$OUT/seal-delivery.py" "$IMAGE"
