#!/usr/bin/env bash
# hysteria-fetch.sh — установить бинарь hysteria с проверкой версии и sha256.
# Зеркало xray-core-fetch.sh: та же цепочка источников, та же боль (github у
# части RU-провайдеров не пускает curl).
#
# Зачем вообще: раньше роль ставила hysteria через `bash <(curl -fsSL
# https://get.hy2.sh/)` И ТОЛЬКО ЕСЛИ бинаря нет. Следствия были такие:
#   * версия не пинилась — нода получала latest на день своего рождения, и
#     разъезд версий по флоту никто не замечал (у xray такой пин есть с самого
#     начала, у hysteria не было);
#   * обновить бинарь роль не могла в принципе (гейт `command -v hysteria`);
#   * upstream-installer исполнялся из pipe под root, без проверки целостности,
#     и требовал доступа к get.hy2.sh — на RU-нодах это лишняя точка отказа.
#
# Использование:
#   hysteria-fetch.sh <version> [sha256]     # version = v2.10.0 (без префикса app/)
#
# sha256 — ожидаемая сумма бинаря hysteria-linux-amd64, ЗАКОММИЧЕННАЯ в роли
# (defaults/main.yml). Upstream публикует hashes.txt рядом с релизом, но качать
# его по той же цепочке бессмысленно: кто подменит бинарь, подменит и хеш рядом.
#
# ⚠️ Тег релиза у apernet/hysteria — `app/vX.Y.Z` (монорепа: app + core), в URL
# слэш экранируется как %2F. Поэтому VERSION здесь без префикса, а префикс
# добавляется при сборке URL.
#
# Exit:
#   0 — установлено или уже стояла нужная версия
#   1 — все источники недоступны / битый бинарь / версия не применилась

set -u

VERSION="${1:?usage: $0 <hysteria version, e.g. v2.10.0> [sha256]}"
EXPECT_SHA256="${2:-}"
ASSET="hysteria-linux-amd64"   # amd64 — единственный таргет наших VPS
BIN=/usr/local/bin/hysteria
TMP=/tmp/hysteria-download
MIN_BYTES=5000000  # реальный бинарь ~20MB; гейт ловит error-page вместо файла

# Mgmt-mirror первым кандидатом. Файл env исторически называется
# /etc/default/xray-mirror (его пишет bootstrap_node), но раздаёт зеркало любые
# ассеты — переиспользуем, чтобы не плодить второй конфиг на ноде.
MIRROR_URL=""
[ -r /etc/default/xray-mirror ] && . /etc/default/xray-mirror

URLS=()
[ -n "$MIRROR_URL" ] && URLS+=("$MIRROR_URL/hysteria/${ASSET}-${VERSION}")
URLS+=("https://github.com/apernet/hysteria/releases/download/app%2F${VERSION}/${ASSET}")

# Already installed? Та же версия — idempotent skip; другая — переустановка
# (bump пина в роли потянет апгрейд, чего прежний гейт `command -v` не умел).
if [ -x "$BIN" ]; then
    have=$("$BIN" version 2>/dev/null | awk -F'\t' '/^Version/{print $2}')
    if [ "$have" = "$VERSION" ]; then
        echo "hysteria $have уже установлен"
        exit 0
    fi
    echo "→ hysteria ${have:-?} → переустанавливаем на $VERSION"
fi

for url in "${URLS[@]}"; do
    echo "→ trying: $url"
    if curl --fail --silent --show-error --location \
        --connect-timeout 30 --max-time 300 \
        --output "$TMP" "$url"; then
        size=$(stat -c %s "$TMP" 2>/dev/null || echo 0)
        if (( size < MIN_BYTES )); then
            echo "  ✗ подозрительно мал ($size байт), пропускаю"
            rm -f "$TMP"
            continue
        fi

        if [ -n "$EXPECT_SHA256" ]; then
            got_sha=$(sha256sum "$TMP" 2>/dev/null | awk '{print $1}')
            if [ "$got_sha" != "$EXPECT_SHA256" ]; then
                echo "  ✗ sha256 не совпал (получено ${got_sha:-?}, ожидалось $EXPECT_SHA256) — источник отброшен" >&2
                rm -f "$TMP"
                continue
            fi
            echo "  ✓ sha256 совпал"
        else
            echo "  ⚠ sha256 не задан — целостность НЕ проверена (задай hysteria2_sha256 в роли)" >&2
        fi

        # Бинарь голый (не архив, в отличие от xray) — сразу install. rc
        # проверяем явно: ENOSPC/RO-FS иначе проглотятся, и мы отрапортуем
        # успех со старым бинарём. install заменяет файл атомарно (rename),
        # поэтому работающий процесс продолжает крутиться на старом inode до
        # рестарта — это и нужно: рестарт делает роль, отдельным шагом.
        if ! install -m 0755 "$TMP" "$BIN"; then
            echo "  ✗ install hysteria не удался (нет места/права?), прерываю" >&2
            rm -f "$TMP"
            exit 1
        fi
        rm -f "$TMP"

        got=$("$BIN" version 2>/dev/null | awk -F'\t' '/^Version/{print $2}')
        if [ "$got" != "$VERSION" ]; then
            echo "  ✗ после установки версия '${got:-?}' != требуемой '$VERSION' — апгрейд не применился" >&2
            exit 1
        fi
        echo "✓ установлен: hysteria $got (источник: $url)"
        exit 0
    else
        rc=$?
        echo "  ✗ curl rc=$rc"
        rm -f "$TMP"
    fi
done

echo "✗ не удалось скачать hysteria $VERSION ни из одного источника" >&2
exit 1
