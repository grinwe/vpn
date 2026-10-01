#!/usr/bin/env bash
# diag_node.sh — диагностика VPN-ноды (xray/nginx/порты/серты/geoip/конфиги/TLS).
#
# Запускать НА ноде (по ssh) под root:   bash diag_node.sh
# Read-only — ничего не меняет. Приватные ключи НЕ печатаются (только derived pbk).
# Весь вывод целиком отдать ассистенту.
set -uo pipefail
export LC_ALL=C
hr(){ printf '\n========== %s ==========\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
RC=/usr/local/etc/xray/config.json
XC=/usr/local/etc/xray/config_xhttp.json

hr "META"
date -u; uname -srm; hostname
echo "default iface: $(ip route 2>/dev/null | awk '/default/{print $5; exit}')  pub-ip: $(curl -s --max-time 5 ifconfig.me 2>/dev/null)"

hr "SERVICES (active? since? failed?)"
for u in xray xray-xhttp nginx hysteria-server; do
  systemctl list-unit-files 2>/dev/null | grep -q "^${u}\b" || continue
  printf '%-16s active=%-8s enabled=%-9s since=%s\n' "$u" \
    "$(systemctl is-active "$u" 2>/dev/null)" \
    "$(systemctl is-enabled "$u" 2>/dev/null)" \
    "$(systemctl show -p ActiveEnterTimestamp --value "$u" 2>/dev/null)"
done

hr "LISTENING PORTS (80/443/9443/10443/xray-api)"
if have ss; then ss -tlnp 2>/dev/null | grep -E ':(80|443|8443|9443|10443|10085|10087)\b' || echo "(!) ожидаемые порты НЕ слушаются";
else netstat -tlnp 2>/dev/null | grep -E ':(80|443|9443|10443)' || echo "(!) портов нет"; fi

hr "GEOIP.DAT + weekly geoip-update.timer  <<< ГЛАВНЫЙ ПОДОЗРЕВАЕМЫЙ"
# Гипотеза: weekly-таймер выкачал битый/0-байтный/HTML geoip.dat из v2fly
# releases/latest и сделал `systemctl restart xray` БЕЗ xray -test → xray
# падает на парсинге правила geoip:ru разом на всех reality-нодах.
GD=/usr/local/share/xray/geoip.dat
ls -la "$GD" 2>/dev/null || echo "(!) ОТСУТСТВУЕТ $GD"
have file && file "$GD" 2>/dev/null               # валидный = 'data'; битый = 'HTML'/'empty'/'ASCII'
have sha256sum && sha256sum "$GD" 2>/dev/null      # сверить с заведомо живой нодой
echo "--- geoip-update.timer: когда последний раз стрелял?"
systemctl list-timers geoip-update.timer --all --no-pager 2>/dev/null | head -3
echo "--- geoip-update.service последние логи:"
journalctl -u geoip-update.service -n 15 --no-pager 2>/dev/null | tail -12 || echo "(нет логов geoip-update)"

hr "XRAY CONFIG VALIDITY (-test)"
for c in "$RC" "$XC"; do
  [ -f "$c" ] || { echo "$c — нет файла"; continue; }
  printf -- '--- %s\n' "$c"
  have xray && xray -test -config "$c" 2>&1 | tail -4 || echo "(xray бинарь не найден)"
done

hr "NGINX -t"
have nginx && nginx -t 2>&1 || echo "(nginx не установлен)"

hr "REALITY: serverNames / shortIds / dest / derived PUBLIC key (pbk)"
if [ -f "$RC" ] && have jq; then
  jq -r '.inbounds[]?|select(.tag=="vless-reality")|.streamSettings.realitySettings
         |"port?  serverNames=\(.serverNames)  shortIds=\(.shortIds)  dest=\(.dest)"' "$RC" 2>/dev/null
  jq -r '.inbounds[]?|select(.tag=="vless-reality")|"port=\(.port)"' "$RC" 2>/dev/null
  PRIV=$(jq -r '.inbounds[]?|select(.tag=="vless-reality")|.streamSettings.realitySettings.privateKey // empty' "$RC" 2>/dev/null)
  if [ -n "${PRIV:-}" ] && have xray; then
    PUB=$(xray x25519 -i "$PRIV" 2>/dev/null | grep -iE 'public|password' | sed -E 's/.*:[[:space:]]*//' | head -1)
    echo "derived pbk (сверить с pbk= в сабке): ${PUB:-(не смог вывести)}"
  else
    echo "derived pbk: (нет privateKey в конфиге или нет бинаря xray)"
  fi
else
  echo "(нет $RC или jq)"
fi

hr "XHTTP: домен из nginx-vhost + xhttpSettings"
ls /etc/nginx/sites-enabled/ 2>/dev/null | grep -i xhttp || echo "(нет xhttp-vhost в sites-enabled)"
[ -f "$XC" ] && have jq && jq -c '.inbounds[]?|select(.tag=="vless-xhttp")|{port,xhttp:.streamSettings.xhttpSettings,sniffing:.sniffing.enabled}' "$XC" 2>/dev/null

hr "CERTS (letsencrypt) — subject / issuer / dates"
shopt -s nullglob
for f in /etc/letsencrypt/live/*/fullchain.pem; do
  printf -- '--- %s\n' "$f"
  openssl x509 -noout -subject -issuer -startdate -enddate -in "$f" 2>/dev/null
done
[ -d /etc/letsencrypt/live ] || echo "(нет /etc/letsencrypt/live)"

hr "LOCAL TLS — XHTTP (nginx :443)"
XDOM=$(ls /etc/nginx/sites-enabled/ 2>/dev/null | sed -n 's/^xhttp-\(.*\)\.conf$/\1/p' | head -1)
echo "xhttp домен (из vhost): ${XDOM:-?}"
echo | timeout 10 openssl s_client -connect 127.0.0.1:443 ${XDOM:+-servername "$XDOM"} 2>&1 \
  | grep -iE 'CONNECTED|subject=|issuer=|Verify return|handshake|alert|no peer' | head -8

hr "LOCAL TLS — REALITY (fallback к dest = норма)"
RSNI=$(jq -r '.inbounds[]?|select(.tag=="vless-reality")|.streamSettings.realitySettings.serverNames[0] // empty' "$RC" 2>/dev/null)
RPORT=$(jq -r '.inbounds[]?|select(.tag=="vless-reality")|.port // empty' "$RC" 2>/dev/null); RPORT=${RPORT:-9443}
echo "reality sni=$RSNI port=$RPORT"
echo | timeout 10 openssl s_client -connect 127.0.0.1:"$RPORT" ${RSNI:+-servername "$RSNI"} 2>&1 \
  | grep -iE 'CONNECTED|subject=|Verify return|handshake|alert' | head -6

hr "RECENT ERROR LOGS (xray/xhttp/nginx, 3h)"
journalctl -u xray -u xray-xhttp -u nginx --since "3 hours ago" --no-pager 2>/dev/null \
  | grep -iE 'error|fail|fatal|invalid|refus|denied|panic|cert|expire|tls|geoip|listen|bind|address already' | tail -40 \
  || echo "(journalctl недоступен)"

hr "DONE"
