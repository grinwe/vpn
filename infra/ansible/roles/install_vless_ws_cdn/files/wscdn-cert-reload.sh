#!/bin/bash
# Certbot deploy-hook. certbot.timer вызывает /etc/letsencrypt/renewal-
# hooks/deploy/*.sh после успешного обновления любого сертификата.
# nginx терминирует TLS для ws-cdn, так что ему нужен reload, чтобы
# подхватить новый fullchain.pem. xray на loopback'е сертификат не
# читает — рестарт xray-ws-cdn необязателен, но оставляем на случай
# ручного отката на direct-TLS.
#
# Хук общий для всех LE-доменов на ноде (xhttp кладёт свой xhttp-reload.sh):
# reload nginx идемпотентен и безопасен независимо от того, какой именно
# домен обновился, поэтому фильтр по RENEWED_LINEAGE не нужен.
set -euo pipefail

systemctl reload nginx || true

# Best-effort xray restart — если юнит не установлен или не запущен
# (например этот скрипт случайно положили на web-хост), не шумим.
systemctl is-enabled --quiet xray-ws-cdn && systemctl restart xray-ws-cdn || true
