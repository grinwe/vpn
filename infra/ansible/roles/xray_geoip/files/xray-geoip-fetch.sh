#!/usr/bin/env bash
# xray-geoip-fetch.sh — выкачать свежий geoip.dat от v2fly с fallback'ом
# по цепочке зеркал. Используется И при первом bootstrap'е ноды (ansible
# зовёт скрипт напрямую), И в weekly timer'е (geoip-update.service).
#
# Зачем цепочка: github.com periodically блокируется/троттлится у части
# RU-провайдеров (TLS-handshake timeout, SNI-фильтрация). Если первый
# URL фейлится — пробуем следующий. На момент написания jsdelivr раздаёт
# v2fly через CF edge и работает стабильнее всего из РФ.
#
# Цепочка:
#   1. jsdelivr (Cloudflare CDN) — основной
#   2. ghproxy.com — российский reverse-прокси github releases
#   3. github.com — last resort (если jsdelivr+ghproxy упали, а инет жив)
#
# Sanity: настоящий geoip.dat ≈ 22-25MB. Если выкачали <5MB — почти
# точно error-страница, не подменяем рабочий файл.
#
# Exit:
#   0 — обновлено или уже было up-to-date (TODO: ETag/If-Modified-Since)
#   1 — все источники упали (timer покажет failed; alert в админке)

set -u

DEST=/usr/local/share/xray/geoip.dat
TMP="${DEST}.new"
MIN_BYTES=5000000  # 5MB — настоящий ~22MB, эффективно ловит error-pages
CONNECT_TIMEOUT=30
MAX_TIME=180

URLS=(
    "https://cdn.jsdelivr.net/gh/v2fly/geoip@release/geoip.dat"
    "https://ghproxy.com/https://github.com/v2fly/geoip/releases/latest/download/geoip.dat"
    "https://github.com/v2fly/geoip/releases/latest/download/geoip.dat"
)

mkdir -p "$(dirname "$DEST")"

for url in "${URLS[@]}"; do
    echo "→ trying: $url"
    if curl --fail --silent --show-error --location \
        --connect-timeout "$CONNECT_TIMEOUT" \
        --max-time "$MAX_TIME" \
        --output "$TMP" \
        "$url"; then
        size=$(stat -c %s "$TMP" 2>/dev/null || echo 0)
        if (( size < MIN_BYTES )); then
            echo "  ✗ подозрительно мал ($size байт), пропускаю"
            rm -f "$TMP"
            continue
        fi
        # Atomic rename — xray всё время видит либо старый, либо новый
        # geoip.dat целиком, никаких half-written состояний.
        mv "$TMP" "$DEST"
        chmod 0644 "$DEST"
        echo "✓ done: $size bytes from $url"
        exit 0
    else
        rc=$?
        echo "  ✗ curl rc=$rc"
        rm -f "$TMP"
    fi
done

echo "✗ ВСЕ источники недоступны — geoip.dat НЕ обновлён" >&2
exit 1
