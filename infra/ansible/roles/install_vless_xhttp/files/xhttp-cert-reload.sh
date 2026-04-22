#!/bin/bash
# Certbot deploy-hook. certbot.timer вызывает /etc/letsencrypt/renewal-
# hooks/deploy/*.sh после успешного обновления любого сертификата.
# После Stage-4 TLS терминирует nginx, так что ему нужен reload, чтобы
# подхватить новый fullchain.pem. xray на loopback'е сертификат больше
# не читает — рестарт xray не обязателен, но оставляем на случай, если
# конфиг внезапно переведут обратно на direct-TLS (ручной rollback).
#
# RENEWED_LINEAGE даёт путь /etc/letsencrypt/live/<domain>/ того серта,
# что только что обновился — если это не наш xhttp-домен, молча
# выходим, чтобы не дёргать лишний раз nginx на общих хостах.
set -euo pipefail

systemctl reload nginx || true

# Best-effort xray restart — если юнит не установлен или не запущен
# (например этот скрипт случайно положили на web-хост), не шумим.
systemctl is-enabled --quiet xray-xhttp && systemctl restart xray-xhttp || true
