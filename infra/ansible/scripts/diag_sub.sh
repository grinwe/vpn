#!/usr/bin/env bash
# diag_sub.sh — внешняя диагностика доставки сабки + TLS до нод.
# Запускать со СВОЕЙ машины (НЕ на ноде):  bash diag_sub.sh <sub-token>
#
# Сам вытащит host/port/sni/pbk из сабки и прогонит TLS по каждой ноде.
# Read-only. В выводе будут UUID/pbk/токен — это норм для диагностики;
# при желании отредактируй хосты, но статусы/headers/pbk/sni/dates оставь.
#
# Переменные (по умолчанию из разбора кода):
#   SUBHOST=grn-ssync.pro   ORIGIN=grinwer.online   ORIGIN_IP=45.14.244.140
set -uo pipefail
export LC_ALL=C
SUBHOST="${SUBHOST:-grn-ssync.pro}"
ORIGIN="${ORIGIN:-grinwer.online}"
ORIGIN_IP="${ORIGIN_IP:-45.14.244.140}"
TOKEN="${1:-}"
[ -z "$TOKEN" ] && { echo "usage: $0 <sub-token>"; exit 2; }
hr(){ printf '\n========== %s ==========\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
C="curl -sS --max-time 20"

hr "META"; date -u; curl --version | head -1; openssl version

hr "1) SUB RAW (status + headers + первые строки тела)  <<< классификатор"
# 200 text/plain + base64 = OK
# 429/503/403/404 + application/json = БЭКЕНД (лимит / нет конфигов / токен)
# 52x + server:cloudflare + cf-ray + text/html = CF/origin-TLS (worker side)
$C -i "https://$SUBHOST/api/sub/$TOKEN" 2>&1 | sed -n '1,30p'

hr "2) SUB метрики (http-версия / ALPN — H2 ДОЛЖЕН быть OFF / verify)"
$C -o /dev/null -w 'http_version=%{http_version}  code=%{response_code}  content_type=%{content_type}  ssl_verify=%{ssl_verify_result}\n' \
  "https://$SUBHOST/api/sub/$TOKEN" 2>&1
echo -n "ALPN on $SUBHOST: "; echo | timeout 10 openssl s_client -connect "$SUBHOST:443" -servername "$SUBHOST" -alpn h2,http/1.1 2>/dev/null | grep -i 'ALPN' || echo "(нет ALPN-строки)"

hr "3) SUB тело: base64 декодится? что внутри?"
BODY=$($C "https://$SUBHOST/api/sub/$TOKEN" 2>/dev/null)
echo "длина=${#BODY}; первые 160: ${BODY:0:160}"
DEC=$(printf '%s' "$BODY" | base64 -d 2>/dev/null)
if [ -n "$DEC" ]; then
  echo "[OK] base64 OK. Протоколы/строки:"; printf '%s\n' "$DEC" | grep -oE '^[a-z0-9]+://' | sort | uniq -c
else
  echo "[!] base64 НЕ декодится → тело это ошибка (см. п.1: JSON 429/503/403/404 или CF 52x HTML)"
fi

hr "4) ORIGIN DIRECT (мимо CF Worker) — $ORIGIN  <<< bisect worker vs backend"
# Если origin отдаёт base64, а grn-ssync.pro нет → виноват Worker/CF-зона.
# Если origin уже не-base64 → виноват бэкенд.
$C -i "https://$ORIGIN/api/sub/$TOKEN" 2>&1 | sed -n '1,18p'

hr "5) ORIGIN TLS cert ($ORIGIN — CF Full-strict падает при битом серте origin)"
echo | timeout 10 openssl s_client -connect "$ORIGIN:443" -servername "$ORIGIN" 2>/dev/null \
  | openssl x509 -noout -subject -issuer -startdate -enddate 2>/dev/null || echo "(не удалось получить серт origin)"

hr "6) DNS sanity (после DR был угон grinwer.online на парковку 198.54.117.242)"
for h in "$SUBHOST" "$ORIGIN"; do
  printf '%-18s -> ' "$h"
  if have dig; then dig +short "$h" | tr '\n' ' '; echo; else getent hosts "$h" 2>/dev/null || nslookup "$h" 2>/dev/null | awk '/Address/{print $2}'; fi
done
echo "(!) если $ORIGIN резолвится в 198.54.117.242 — DNS угнан/отвалился"

hr "7) RATE-LIMIT проба (8 быстрых запросов; если мелькнёт 429 — общий бакет по CF-IP)"
codes=""; for i in $(seq 1 8); do codes="$codes $($C -o /dev/null -w '%{response_code}' "https://$SUBHOST/api/sub/$TOKEN" 2>/dev/null)"; done
echo "коды:$codes"
echo "$codes" | grep -q 429 && echo "[!] 429 пойман → slowapi лимит по remote-IP, а за Worker все юзеры в одном бакете" || echo "(429 не словили в 8 запросах)"

hr "8) TLS ДО НОД (извлекаю host:port:sni:pbk из сабки, бью openssl)"
if [ -z "$DEC" ]; then
  echo "(сабка не декодилась — нод не извлечь; разбирайся с п.1-4 сперва)"
else
  printf '%s\n' "$DEC" | grep -E '^(vless|hy2|hysteria2)://' | while IFS= read -r uri; do
    hp=$(printf '%s' "$uri"  | sed -E 's#^[a-z0-9]+://[^@]*@([^/?#]+).*#\1#')
    host=${hp%%:*}; port=${hp##*:}
    sni=$(printf '%s' "$uri" | grep -oE '[?&]sni=[^&#]+'      | head -1 | sed -E 's/.*sni=//')
    sec=$(printf '%s' "$uri" | grep -oE '[?&]security=[^&#]+' | head -1 | sed -E 's/.*security=//')
    typ=$(printf '%s' "$uri" | grep -oE '[?&]type=[^&#]+'     | head -1 | sed -E 's/.*type=//')
    pbk=$(printf '%s' "$uri" | grep -oE '[?&]pbk=[^&#]+'      | head -1 | sed -E 's/.*pbk=//')
    tag=$(printf '%s' "$uri" | sed -E 's/.*#//')
    echo "--- $tag | host=$host port=$port sni=${sni:-?} security=${sec:-?} type=${typ:-?} pbk=${pbk:-—}"
    if timeout 6 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then echo "  tcp:$port OPEN"; else echo "  tcp:$port ЗАКРЫТ/таймаут (xray/nginx лежит?)"; continue; fi
    echo | timeout 10 openssl s_client -connect "$host:$port" ${sni:+-servername "$sni"} 2>&1 \
      | grep -iE 'CONNECTED|subject=|issuer=|Verify return code|handshake fail|alert|no peer cert' | sed 's/^/  /' | head -6
  done
fi

hr "DONE"
