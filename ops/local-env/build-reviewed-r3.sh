#!/usr/bin/env bash
set -Eeuo pipefail
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
cd /home/megmeet/project/new-system
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
docker build --pull=false -f dev-local/dsh/Dockerfile -t megmeet-dev-dsh:0.1.6-alpha.2-upstream-ui .
docker build --pull=false -f services/personal-cloud/Dockerfile.worker -t megmeet-personal-dsh:local .
docker compose --env-file dev-local/locks/images.env -f dev-local/compose.yaml -p megmeet-dev-local build gateway personal-cloud-manager
echo REVIEWED_R3_IMAGES_BUILD_PASS
