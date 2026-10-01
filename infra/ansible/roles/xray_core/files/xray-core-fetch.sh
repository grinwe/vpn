#!/usr/bin/env bash
# xray-core-fetch.sh — установить xray-core через CDN-fallback chain.
# Зеркало xray-geoip-fetch.sh: оба родились из одной боли — github.com
# у части RU-провайдеров не пускает curl (видели 5-мин SSL timeout
# на ru-cloud-web-02 при upstream install-release.sh).
#
# Использование:
#   xray-core-fetch.sh <version> [sha256]   # version = git-tag, e.g. v25.6.8
#
# sha256 — ожидаемая контрольная сумма Xray-linux-<arch>.zip, ЗАКОММИЧЕННАЯ в
# роли (roles/xray_core/defaults/main.yml). Качать .dgst по той же цепочке
# бессмысленно: кто подменит zip, подменит и хеш рядом с ним. Без неё бинарь,
# приезжающий через сторонний реверс-прокси (ghproxy.com) и ставящийся в
# /usr/local/bin/xray под root на ВЕСЬ флот, не проверяется ничем, кроме
# «размер > 1 МБ» (аудит 2026-07-25).
#
# Цепочка качания zip'а с release-asset'ом:
#   1. ghproxy.com → github       (RU reverse-proxy для release downloads)
#   2. github.com direct          (если ghproxy лёг/перегружен)
#
# jsdelivr НЕ поддерживает github release assets (только raw repo) —
# поэтому в цепочке его нет, в отличие от geoip-fetcher.
#
# Exit:
#   0 — установлено или xray уже был с нужной версией
#   1 — все источники недоступны или zip битый

set -u

VERSION="${1:?usage: $0 <xray-core version, e.g. v25.6.8> [sha256]}"
EXPECT_SHA256="${2:-}"
ARCH="64"  # amd64 — единственный таргет наших VPS
ZIP=/tmp/xray-core.zip
UNPACK=/tmp/xray-core-unpack
MIN_BYTES=1000000  # ~1MB — реальный zip 25+MB, ловит error-pages

# Mgmt-mirror как первый кандидат (если ansible прописал MIRROR_URL в
# env-файл). См. xray-geoip-fetch.sh — та же логика. Mirror раздаёт
# Xray-linux-64-<ver>.zip из /srv/assets/xray/ (см. mgmt-mirror role).
MIRROR_URL=""
[ -r /etc/default/xray-mirror ] && . /etc/default/xray-mirror

URLS=()
[ -n "$MIRROR_URL" ] && URLS+=("$MIRROR_URL/xray/Xray-linux-${ARCH}-${VERSION}.zip")
URLS+=(
    "https://ghproxy.com/https://github.com/XTLS/Xray-core/releases/download/${VERSION}/Xray-linux-${ARCH}.zip"
    "https://github.com/XTLS/Xray-core/releases/download/${VERSION}/Xray-linux-${ARCH}.zip"
)

# Already installed? Если та же версия — idempotent skip; если разная —
# переустанавливаем (bump pin'а в роли потянет переустановку).
if [ -x /usr/local/bin/xray ]; then
    have=$(/usr/local/bin/xray version 2>/dev/null | head -n1 | awk '{print $2}')
    want="${VERSION#v}"
    if [ "$have" = "$want" ]; then
        echo "xray-core $have уже установлен"
        exit 0
    fi
    echo "→ xray-core $have → переустанавливаем на $want"
fi

for url in "${URLS[@]}"; do
    echo "→ trying: $url"
    if curl --fail --silent --show-error --location \
        --connect-timeout 30 --max-time 300 \
        --output "$ZIP" "$url"; then
        size=$(stat -c %s "$ZIP" 2>/dev/null || echo 0)
        if (( size < MIN_BYTES )); then
            echo "  ✗ подозрительно мал ($size байт), пропускаю"
            rm -f "$ZIP"
            continue
        fi

        # Целостность: единственная защита от подмены бинаря по дороге
        # (ghproxy.com — сторонний реверс-прокси, mgmt-mirror — наш, но тоже
        # промежуточное звено). Несовпадение = пробуем следующий источник, а не
        # ставим «что дали».
        if [ -n "$EXPECT_SHA256" ]; then
            got_sha=$(sha256sum "$ZIP" 2>/dev/null | awk '{print $1}')
            if [ "$got_sha" != "$EXPECT_SHA256" ]; then
                echo "  ✗ sha256 не совпал (получено ${got_sha:-?}, ожидалось $EXPECT_SHA256) — источник отброшен" >&2
                rm -f "$ZIP"
                continue
            fi
            echo "  ✓ sha256 совпал"
        else
            echo "  ⚠ sha256 не задан — целостность НЕ проверена (задай xray_core_sha256 в роли)" >&2
        fi

        # Unpack + install. install-release.sh upstream'а делает ровно
        # это (плюс ставит nobody-user + systemd unit, что у нас уже
        # есть либо ниже в роли). Здесь только сам бинарь и data files.
        rm -rf "$UNPACK"
        mkdir -p "$UNPACK"
        if ! unzip -q -o "$ZIP" -d "$UNPACK"; then
            echo "  ✗ zip битый, пропускаю"
            rm -rf "$ZIP" "$UNPACK"
            continue
        fi

        # Бинарь мог не оказаться в zip'е при смене layout релиза — проверяем
        # до install, иначе install упадёт молча и на диске останется старый xray.
        if [ ! -f "$UNPACK/xray" ]; then
            echo "  ✗ в архиве нет бинаря xray (изменился layout релиза?), пропускаю" >&2
            rm -rf "$ZIP" "$UNPACK"
            continue
        fi

        mkdir -p /usr/local/share/xray
        # rc install проверяем явно: ENOSPC на /usr/local, RO-FS и пр. иначе
        # проглотятся (set -e не включён), и мы отрапортуем успех со старым бинарём.
        if ! install -m 0755 "$UNPACK/xray" /usr/local/bin/xray; then
            echo "  ✗ install xray не удался (нет места/права?), прерываю" >&2
            rm -rf "$ZIP" "$UNPACK"
            exit 1
        fi
        # geoip/geosite пакуются в release zip как первая инициализация —
        # weekly geoip-update.timer потом подтянет свежее. geosite.dat
        # auto-update'а у нас нет, поэтому обновится только на bump'е
        # xray_core_version в роли.
        [ -f "$UNPACK/geoip.dat" ] && install -m 0644 "$UNPACK/geoip.dat" /usr/local/share/xray/geoip.dat
        [ -f "$UNPACK/geosite.dat" ] && install -m 0644 "$UNPACK/geosite.dat" /usr/local/share/xray/geosite.dat

        rm -rf "$ZIP" "$UNPACK"
        ver=$(/usr/local/bin/xray version | head -n1)
        # Сверяем фактически установленную версию с запрошенной: если бинарь
        # почему-то остался старым — это тихий провал апгрейда, падаем с rc=1.
        got=$(printf '%s\n' "$ver" | awk '{print $2}')
        want="${VERSION#v}"
        if [ "$got" != "$want" ]; then
            echo "  ✗ после установки версия '$got' != требуемой '$want' — апгрейд не применился" >&2
            exit 1
        fi
        echo "✓ установлен: $ver (источник: $url)"
        exit 0
    else
        rc=$?
        echo "  ✗ curl rc=$rc"
        rm -f "$ZIP"
    fi
done

echo "✗ ВСЕ источники недоступны — xray-core НЕ установлен" >&2
exit 1
