#!/usr/bin/env bash
# refresh-assets.sh — обновить статику mgmt-mirror'а из upstream.
# Запускается на mgmt-host'е (он имеет нормальный outbound к github,
# в отличие от части RU-VPS) и заполняет ./assets/, который mounted'ится
# в nginx-контейнер read-only.
#
# Использование:
#   bash refresh-assets.sh                 # обновить всё
#   bash refresh-assets.sh --geoip-only    # только geoip + geosite (weekly)
#   bash refresh-assets.sh --xray-only     # только xray release zip'ы
#
# Cron на mgmt'е (раз в неделю, понедельник 04:00):
#   0 4 * * 1 bash /opt/vpn/infra/mgmt-mirror/refresh-assets.sh \
#     >> /var/log/mgmt-mirror-refresh.log 2>&1
#
# При добавлении новой версии xray-core — допиши tag в XRAY_VERSIONS
# и прокатай скрипт. Старые версии не удаляются — на случай rollback'а
# нод которые ещё не успели обновиться.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="$SCRIPT_DIR/assets"

MODE="all"
case "${1:-}" in
    --geoip-only)         MODE="geoip" ;;
    --xray-only)          MODE="xray" ;;
    --node-exporter-only) MODE="node_exporter" ;;
    "") ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
esac

# При обновлении xray-core — добавь новую версию сюда И bump'ни
# xray_core_version в infra/ansible/roles/xray_core/defaults/main.yml.
XRAY_VERSIONS=(
    # Прошлый пин — держим для откатов: скрипт скачанное не удаляет, а нода
    # запрашивает у зеркала КОНКРЕТНУЮ версию из xray_core_version.
    "v25.6.8"
    # Текущий пин роли. Зеркало обязано его раздавать: для RU-нод за
    # заблокированным github это единственный рабочий источник (ghproxy.com
    # мёртв), и без этой строки апгрейд ядра на них молча не доезжает.
    "v26.3.27"
)

# node_exporter версии — держать в синхроне с
# roles/node_exporter/defaults/main.yml (node_exporter_version, БЕЗ 'v').
NODE_EXPORTER_VERSIONS=(
    "1.8.2"
)

mkdir -p "$ASSETS_DIR/xray" "$ASSETS_DIR/node_exporter"

fetch_atomic() {
    local url="$1" dest="$2" min_bytes="${3:-100000}"
    local tmp="${dest}.new"
    echo "→ $url"
    if curl --fail --silent --show-error --location \
        --connect-timeout 30 --max-time 600 \
        --output "$tmp" "$url"; then
        local size
        size=$(stat -c %s "$tmp" 2>/dev/null || echo 0)
        if (( size < min_bytes )); then
            echo "  ✗ слишком мал ($size байт < $min_bytes); пропускаю"
            rm -f "$tmp"
            return 1
        fi
        mv "$tmp" "$dest"
        echo "  ✓ $dest ($size bytes)"
        return 0
    else
        local rc=$?
        echo "  ✗ curl rc=$rc"
        rm -f "$tmp"
        return $rc
    fi
}

if [[ "$MODE" == "all" || "$MODE" == "geoip" ]]; then
    fetch_atomic \
        "https://github.com/v2fly/geoip/releases/latest/download/geoip.dat" \
        "$ASSETS_DIR/geoip.dat" \
        5000000

    fetch_atomic \
        "https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat" \
        "$ASSETS_DIR/geosite.dat" \
        1000000
fi

if [[ "$MODE" == "all" || "$MODE" == "xray" ]]; then
    for ver in "${XRAY_VERSIONS[@]}"; do
        f="$ASSETS_DIR/xray/Xray-linux-64-${ver}.zip"
        if [ -f "$f" ]; then
            echo "→ Xray-linux-64-${ver}.zip уже есть, skip"
            continue
        fi
        fetch_atomic \
            "https://github.com/XTLS/Xray-core/releases/download/${ver}/Xray-linux-64.zip" \
            "$f" \
            1000000
    done
fi

if [[ "$MODE" == "all" || "$MODE" == "node_exporter" ]]; then
    for ver in "${NODE_EXPORTER_VERSIONS[@]}"; do
        f="$ASSETS_DIR/node_exporter/node_exporter-${ver}.linux-amd64.tar.gz"
        if [ -f "$f" ]; then
            echo "→ node_exporter-${ver} уже есть, skip"
            continue
        fi
        fetch_atomic \
            "https://github.com/prometheus/node_exporter/releases/download/v${ver}/node_exporter-${ver}.linux-amd64.tar.gz" \
            "$f" \
            1000000
    done
fi

echo
echo "=== Mirror inventory ==="
ls -la "$ASSETS_DIR" "$ASSETS_DIR/xray" "$ASSETS_DIR/node_exporter" 2>/dev/null
