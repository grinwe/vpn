#!/usr/bin/env bash
# grab_xhttp_domains.sh — собрать vless_xhttp_domain с каждой RU-jump
# ноды (читает /etc/nginx/sites-available/xhttp-*.conf). На stdout
# печатает готовый SQL для UPDATE vpn_configs.sni.
#
# Запуск:
#   bash scripts/grab_xhttp_domains.sh
# Затем применить полученный SQL через psql на mgmt-хосте.

set -u

cd "$(dirname "$0")/.."

ansible -i infra/ansible/inventories/prod/hosts.yml vpn_nodes \
  --ask-vault-pass \
  -m shell \
  -a "ls /etc/nginx/sites-available/xhttp-*.conf 2>/dev/null | head -1 | xargs -r grep -h 'server_name' | head -1 | awk '{print \$2}' | tr -d ';'" \
  -o 2>&1 \
  | grep -E "rc=0" \
  | awk -F'>>' '{
      split($1, a, "|"); host=a[1]; gsub(/ /,"",host);
      gsub(/^[ \t]+|[ \t]+$/, "", $2);
      if ($2 != "") printf "-- %s\nUPDATE vpn_configs SET sni = %s%s%s WHERE node_id = (SELECT id FROM vpn_nodes WHERE name = %s%s%s) AND protocol = '"'"'vless_xhttp'"'"';\n", host, "'"'"'", $2, "'"'"'", "'"'"'", host, "'"'"'"
    }'
