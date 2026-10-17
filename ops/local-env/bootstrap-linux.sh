#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
test "$(id -u)" = 0
test "${WSL_DISTRO_NAME:-}" = MegmeetDev || { echo "Only the local MegmeetDev WSL distribution is allowed" >&2; exit 1; }
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_CODENAME" = noble

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg git rsync unzip xz-utils python3 python3-venv python3-pip build-essential ripgrep
install -m 0755 -d /etc/apt/keyrings
curl --fail --show-error --silent --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y --no-install-recommends \
  'docker-ce=5:29.8.1-1~ubuntu.24.04~noble' \
  'docker-ce-cli=5:29.8.1-1~ubuntu.24.04~noble' \
  'containerd.io=2.3.5-1~ubuntu.24.04~noble' \
  'docker-buildx-plugin=0.37.1-1~ubuntu.24.04~noble' \
  'docker-compose-plugin=5.5.1-1~ubuntu.24.04~noble'
apt-mark hold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
install -d /etc/docker
if [ ! -e /etc/docker/daemon.json ]; then
    printf '%s\n' '{"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}' > /etc/docker/daemon.json
fi
systemctl enable --now docker
if ! id megmeet >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash megmeet
fi
usermod -aG docker megmeet
cat > /etc/wsl.conf <<'EOF'
[boot]
systemd=true
[user]
default=megmeet
[interop]
appendWindowsPath=false
EOF

NODE_VERSION=22.20.0
NODE_ARCHIVE="node-v${NODE_VERSION}-linux-x64.tar.xz"
install -d /opt/megmeet-downloads
cd /opt/megmeet-downloads
curl --fail --show-error --silent --retry 3 "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_ARCHIVE}" -o "$NODE_ARCHIVE"
curl --fail --show-error --silent --retry 3 "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" -o SHASUMS256.txt
grep " ${NODE_ARCHIVE}$" SHASUMS256.txt | sha256sum -c -
tar -xJf "$NODE_ARCHIVE" -C /opt
ln -sfn "/opt/node-v${NODE_VERSION}-linux-x64/bin/node" /usr/local/bin/node
ln -sfn "/opt/node-v${NODE_VERSION}-linux-x64/bin/npm" /usr/local/bin/npm
ln -sfn "/opt/node-v${NODE_VERSION}-linux-x64/bin/npx" /usr/local/bin/npx
npm install --global --prefix /usr/local pnpm@11.7.0

install -d -o megmeet -g megmeet /home/megmeet/work /home/megmeet/reports
python3 -m venv /home/megmeet/.venv
chown -R megmeet:megmeet /home/megmeet/.venv
docker version --format 'DOCKER_SERVER: {{.Server.Version}}'
docker compose version
python3 --version
node --version
pnpm --version
dpkg-query -W docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin python3 > /home/megmeet/reports/system-versions.txt
printf '%s\n' 'LINUX_BOOTSTRAP: PASS'
