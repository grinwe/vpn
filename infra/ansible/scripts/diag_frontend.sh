#!/usr/bin/env bash
# diag_frontend.sh — диагностика веб-фронта grinwer.online с самой ноды.
# Шестиэтапный пробег:
#   1) upstream-ы напрямую (admin:8080, webapp:8082, backend:8000)
#   2) хвост nginx error.log
#   3) HTTPS на 127.0.0.1 c Host: grinwer.online (минуя DNS/CF)
#   4) эффективная nginx-конфа по серверу grinwer
#   5) TLS-сертификат (даты)
#   6) счётчик строк bot-логов (sanity на «бот вообще что-то делал»)
#
# Usage:
#   ./scripts/diag_frontend.sh
#   ./scripts/diag_frontend.sh 2>&1 | tee diag_frontend.out
#
# Env overrides:
#   ADMIN_PORT (8080), WEBAPP_PORT (8082), BACKEND_PORT (8000)
#   DOMAIN (grinwer.online), ADMIN_MOUNT (mgmt-316dee00)
#   BOT_CONTAINER (vpn-bot-1)

set -u

ADMIN_PORT="${ADMIN_PORT:-8080}"
WEBAPP_PORT="${WEBAPP_PORT:-8082}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
DOMAIN="${DOMAIN:-grinwer.online}"
ADMIN_MOUNT="${ADMIN_MOUNT:-mgmt-316dee00}"
BOT_CONTAINER="${BOT_CONTAINER:-vpn-bot-1}"

echo "--- admin (${ADMIN_PORT}) ---"
curl -sI "http://127.0.0.1:${ADMIN_PORT}/" 2>&1 | head -5
curl -sI "http://127.0.0.1:${ADMIN_PORT}/${ADMIN_MOUNT}/" 2>&1 | head -5

echo "--- webapp (${WEBAPP_PORT}) ---"
curl -sI "http://127.0.0.1:${WEBAPP_PORT}/" 2>&1 | head -5
curl -sI "http://127.0.0.1:${WEBAPP_PORT}/app/" 2>&1 | head -5

echo "--- backend (${BACKEND_PORT}) ---"
curl -s "http://127.0.0.1:${BACKEND_PORT}/healthz"
echo

echo "--- nginx error.log ---"
tail -30 /var/log/nginx/error.log

echo "--- via host nginx (TLS to 127.0.0.1, Host: ${DOMAIN}) ---"
curl -kv "https://127.0.0.1/${ADMIN_MOUNT}/" \
  --resolve "${DOMAIN}:443:127.0.0.1" \
  -H "Host: ${DOMAIN}" 2>&1 | tail -30

echo "--- nginx -T (server_name ${DOMAIN} block) ---"
nginx -T 2>/dev/null | sed -n "/server_name .*${DOMAIN}/,/^}/p" | head -60

echo "--- cert ---"
echo | openssl s_client -servername "${DOMAIN}" -connect 127.0.0.1:443 2>/dev/null \
  | openssl x509 -noout -dates 2>/dev/null

echo "--- bot logs since boot ---"
docker logs "${BOT_CONTAINER}" 2>&1 | wc -l
