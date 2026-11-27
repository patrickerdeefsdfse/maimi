#!/usr/bin/env bash
set -Eeuo pipefail

engine_dir=$(cd -- "$(dirname -- "$0")" && pwd)
server_dir=$(cd -- "$engine_dir/../.." && pwd)
models_dir=${MAIMI_OCR_MODELS_DIR:-"$engine_dir/ocr-models"}
if [[ ! -d "$models_dir" ]]; then
  echo 'Prepare the OCR models listed in ocr-models.json, or set MAIMI_OCR_MODELS_DIR.' >&2
  exit 1
fi

# Assemble only the build inputs used by the Office Dockerfile.
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/office-engine" "$stage/services/personal-cloud" "$stage/dev-local/dsh"
cp "$engine_dir"/{Dockerfile,requirements-linux.lock.txt,engine.py,ocr-config.json,ocr-models.json} "$stage/office-engine/"
cp -R "$models_dir" "$stage/office-engine/ocr-models"
cp -R "$server_dir/worker/dsh/office-adapter" "$stage/dev-local/dsh/office-adapter"
cp "$server_dir/worker/personal-cloud/worker.mjs" "$stage/services/personal-cloud/worker.mjs"
args=()
if [[ -n "${DSH_HOST_BASE_IMAGE:-}" ]]; then
  args+=(--build-arg "DSH_HOST_BASE_IMAGE=$DSH_HOST_BASE_IMAGE")
fi
docker build --pull=false "${args[@]}" -f "$stage/office-engine/Dockerfile" -t megmeet-personal-dsh:office-candidate "$stage"
docker build --pull=false --target chat -t megmeet-personal-cloud-manager:local "$server_dir/manager"
