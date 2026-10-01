#!/usr/bin/env bash
# Скачать клиенты ТЕХ ЖЕ версий, что на нодах (источник правды — дефолты ролей).
# Кладёт в ${EGRESS_BIN_DIR:-~/.cache/vpn-egress-probe}: xray, hysteria.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="${EGRESS_BIN_DIR:-$HOME/.cache/vpn-egress-probe}"
XRAY_V="$(sed -n 's/^xray_core_version: *"\(v[^"]*\)".*/\1/p' "$ROOT/infra/ansible/roles/xray_core/defaults/main.yml")"
HY_V="$(sed -n 's/^hysteria2_version: *"\(v[^"]*\)".*/\1/p' "$ROOT/infra/ansible/roles/install_hysteria2/defaults/main.yml")"
mkdir -p "$DIR"
echo "xray $XRAY_V, hysteria $HY_V -> $DIR"
# Из WSL GitHub через корпоративный прокси качается медленно — докачка -C -.
curl -fSL -C - --retry 5 -o "$DIR/xray.zip" \
  "https://github.com/XTLS/Xray-core/releases/download/${XRAY_V}/Xray-linux-64.zip"
python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('xray', sys.argv[2])" "$DIR/xray.zip" "$DIR"
curl -fSL -C - --retry 5 -o "$DIR/hysteria" \
  "https://github.com/apernet/hysteria/releases/download/app/${HY_V}/hysteria-linux-amd64"
chmod +x "$DIR/xray" "$DIR/hysteria"
"$DIR/xray" version | head -1
"$DIR/hysteria" version 2>/dev/null | grep -i '^version' || true
