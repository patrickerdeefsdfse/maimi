#!/usr/bin/env bash
# This machine's existing Windows proxy is reachable through the WSL NAT gateway.
# Only run inside the dedicated local development distribution.
set -Eeuo pipefail
test "$(id -u)" = 0
test "${WSL_DISTRO_NAME:-}" = MegmeetDev
install -d /usr/local/sbin /etc/systemd/system/docker.service.d
cat > /usr/local/sbin/megmeet-docker-proxy <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
gateway=$(ip -4 route show default | awk '{print $3; exit}')
[[ "$gateway" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
umask 022
printf 'HTTP_PROXY=http://%s:7897\nHTTPS_PROXY=http://%s:7897\nNO_PROXY=localhost,127.0.0.1,::1\n' "$gateway" "$gateway" > /run/megmeet-docker-proxy.env
EOF
chmod 755 /usr/local/sbin/megmeet-docker-proxy
cat > /etc/systemd/system/megmeet-docker-proxy.service <<'EOF'
[Unit]
Description=Resolve the Windows download proxy for the local Megmeet Docker engine
After=network-online.target
Wants=network-online.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/megmeet-docker-proxy
RemainAfterExit=yes
EOF
cat > /etc/systemd/system/docker.service.d/megmeet-proxy.conf <<'EOF'
[Unit]
Requires=megmeet-docker-proxy.service
After=megmeet-docker-proxy.service

[Service]
EnvironmentFile=/run/megmeet-docker-proxy.env
EOF
systemctl daemon-reload
systemctl restart megmeet-docker-proxy.service
systemctl restart docker
systemctl is-active docker
printf 'LOCAL_DOCKER_PROXY: CONFIGURED\n'
