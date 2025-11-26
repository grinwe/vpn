#!/usr/bin/env bash
set -euo pipefail

if [ -f /usr/local/etc/xray/config.json ]; then
  echo "VLESS Reality already configured"
  exit 0
fi

mkdir -p /usr/local/etc/xray
cat > /usr/local/etc/xray/config.json <<'CFG'
{
  "inbounds": [
    {
      "port": 9443,
      "protocol": "vless",
      "settings": {"clients": [{"id": "REPLACE-UUID", "flow": "xtls-rprx-vision"}]},
      "streamSettings": {"security": "reality", "realitySettings": {"dest": "www.cloudflare.com:443", "serverNames": ["www.cloudflare.com"], "privateKey": "REPLACE-KEY"}}
    }
  ],
  "outbounds": [{"protocol": "freedom", "tag": "direct"}]
}
CFG

systemctl restart xray 2>/dev/null || true
