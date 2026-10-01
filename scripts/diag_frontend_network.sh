#!/usr/bin/env bash
# diag_frontend_network.sh — сетевая половина диагностики фронта.
# Парная к diag_frontend.sh (та про nginx/upstream/loopback внутри ноды).
# Здесь проверяем то, что снаружи: публичный IP, DNS, CF, ufw/iptables,
# fail2ban и certbot. Цель — отделить «лежит сервис» от «домен не туда
# смотрит / CF не пускает / файрвол режет / RKN перекрыл».
#
# Usage:
#   ./scripts/diag_frontend_network.sh
#   ./scripts/diag_frontend_network.sh 2>&1 | tee diag_frontend_network.out
#
# Env overrides: DOMAIN (grinwer.online), ADMIN_MOUNT (mgmt-316dee00).

set -u

DOMAIN="${DOMAIN:-grinwer.online}"
ADMIN_MOUNT="${ADMIN_MOUNT:-mgmt-316dee00}"

echo "--- external IP сервера ---"
# -4 принудительно: иначе curl на dual-stack хосте может вернуть
# IPv6, который потом не сжуёт curl -I https://<ipv6>/ (нужны
# квадратные скобки) — увидим путаную ошибку про port number.
PUB="$(curl -4 -s --max-time 5 ifconfig.me)"
echo "${PUB:-<not resolved>}"

echo "--- публичный DNS для ${DOMAIN} ---"
dig +short "${DOMAIN}" @1.1.1.1
dig +short "${DOMAIN}" @8.8.8.8

echo "--- CF или прямой? (104./162./172./188. = Cloudflare) ---"
echo "external_ip=${PUB:-?}"
echo "сравни с DNS-ответами выше — совпадает = direct, отличается = CF/прокси"

echo "--- сами достучимся до своего публичного IP по 443? ---"
if [[ -n "${PUB:-}" ]]; then
  echo "Trying https://${PUB}/ from itself"
  curl -kI --connect-timeout 5 "https://${PUB}/" 2>&1 | head -10
else
  echo "пропускаем — публичный IP не определился"
fi

echo "--- по DNS-имени с сервера наружу через интернет ---"
curl -I --connect-timeout 10 "https://${DOMAIN}/${ADMIN_MOUNT}/" 2>&1 | head -15

echo "--- ufw / iptables (правила на 443/80) ---"
ufw status verbose 2>/dev/null | head -30
iptables -L INPUT -n -v 2>&1 | head -30

echo "--- fail2ban (вдруг свой IP отбанили) ---"
fail2ban-client status 2>/dev/null | head -20

echo "--- certbot certificates ---"
certbot certificates 2>&1 | grep -E "Domain|Expiry|Path"
