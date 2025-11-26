#!/usr/bin/env bash
set -euo pipefail

# Placeholder installer. Replace with real shadowtls + shadowsocks deployment.
# Expected to be idempotent when rerun.
if [ -f /etc/shadowtls-ss/shadowsocks.json ]; then
  echo "ShadowTLS+SS already configured"
  exit 0
fi

mkdir -p /etc/shadowtls-ss
cat > /etc/shadowtls-ss/shadowsocks.json <<'CFG'
{
  "server": "0.0.0.0",
  "server_port": 8443,
  "password": "CHANGE_ME",
  "method": "aes-256-gcm",
  "mode": "tcp_and_udp"
}
CFG

echo "shadowtls placeholder" > /etc/shadowtls-ss/shadowtls.conf
systemctl restart shadowsocks-libev 2>/dev/null || true
