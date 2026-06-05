#!/usr/bin/env bash
# check_exit_tcp_conns.sh — разобраться почему на exit-ноде много TCP
# connections. Запускается СНАРУЖИ (с машины оператора), SSH'нется в
# ноду по имени из ansible inventory.
#
# Usage:
#   ./scripts/check_exit_tcp_conns.sh <node-name>
#
# Пример:
#   ./scripts/check_exit_tcp_conns.sh ur-pq-01
#
# Читает ansible inventory и резолвит ansible_host/ansible_user по имени.
# Нужен локально: ssh, ansible-inventory (опционально — fallback на host).

set -euo pipefail

NODE="${1:-}"
if [[ -z "${NODE}" ]]; then
    echo "usage: $0 <node-name>" >&2
    echo "examples: ur-pq-01, cz-cent, nl-cent-01, fr-pq-01" >&2
    exit 1
fi

INVENTORY="${INVENTORY:-infra/ansible/inventories/prod/hosts.yml}"

# Резолвим hostname → IP через ansible-inventory (если есть), иначе
# ssh по имени напрямую (работает если у юзера настроен ~/.ssh/config).
if command -v ansible-inventory >/dev/null 2>&1 && [[ -f "${INVENTORY}" ]]; then
    HOST=$(ansible-inventory -i "${INVENTORY}" --host "${NODE}" 2>/dev/null \
        | sed -n 's/.*"ansible_host": *"\([^"]*\)".*/\1/p' | head -1)
    if [[ -z "${HOST}" ]]; then
        echo "error: node '${NODE}' not found in ${INVENTORY}" >&2
        exit 2
    fi
else
    HOST="${NODE}"
fi

SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
SSH_USER="${SSH_USER:-root}"

echo "=== ${NODE} (${HOST}) ==="
echo

# Пачкой команд запускаем heredoc на ноде — меньше SSH-round-trip'ов.
# ss(8) встроенный в iproute2, должен быть везде в Ubuntu/Debian.
ssh ${SSH_OPTS} "${SSH_USER}@${HOST}" bash -s <<'REMOTE'
set -e

echo "-- uptime --"
uptime

echo
echo "-- все TCP состояния (total breakdown) --"
ss -tan | awk 'NR>1 {print $1}' | sort | uniq -c | sort -rn

echo
echo "-- ESTABLISHED: сколько и куда --"
established_count=$(ss -tn state established 2>/dev/null | wc -l)
# вычитаем заголовок ss если он есть
if [ "${established_count}" -gt 0 ]; then
    established_count=$(( established_count - 1 ))
fi
echo "total established: ${established_count}"

echo
echo "-- top-10 destination IP'шек (исходящие conns) --"
ss -tn state established 2>/dev/null | awk 'NR>1 {print $5}' \
    | awk -F: '{
        # IPv6 с [::1]:port vs IPv4 host:port
        n = split($0, parts, ":");
        port = parts[n];
        ip = $0; sub(":[0-9]+$", "", ip);
        print ip
    }' | sort | uniq -c | sort -rn | head -10

echo
echo "-- распределение по LOCAL port (что слушает) --"
ss -tn state established 2>/dev/null | awk 'NR>1 {print $4}' \
    | awk -F: '{
        n = split($0, parts, ":");
        print parts[n]
    }' | sort | uniq -c | sort -rn | head -10

echo
echo "-- распределение по REMOTE port (куда ходим) --"
ss -tn state established 2>/dev/null | awk 'NR>1 {print $5}' \
    | awk -F: '{
        n = split($0, parts, ":");
        print parts[n]
    }' | sort | uniq -c | sort -rn | head -10

echo
echo "-- WG interfaces (если есть) --"
if command -v wg >/dev/null 2>&1; then
    wg show all dump | awk 'BEGIN {FS="\t"} NF>=5 {
        iface=$1; peer=$2; endpoint=$4;
        print iface, endpoint
    }' | head -20 || true
    echo
    echo "(peer count per iface:)"
    wg show all dump | awk 'BEGIN {FS="\t"} NF>=5 {print $1}' | sort | uniq -c
else
    echo "wg(8) not installed"
fi

echo
echo "-- zombie/long-living: CLOSE_WAIT / LAST_ACK / FIN_WAIT --"
ss -tan state close-wait 2>/dev/null | wc -l
echo "CLOSE_WAIT count (включает заголовок):"
ss -tan state close-wait 2>/dev/null | head -5

echo
echo "-- conntrack, если доступен (NAT/masquerade картина) --"
if command -v conntrack >/dev/null 2>&1; then
    conntrack -L 2>/dev/null | wc -l | xargs -I{} echo "conntrack entries: {}"
    echo "top-5 proto+state:"
    conntrack -L 2>/dev/null | awk '{print $1, $3}' | sort | uniq -c | sort -rn | head -5
else
    echo "conntrack not installed (apt install conntrack)"
fi

echo
echo "-- topowner процессов с open sockets (если cap позволяет) --"
ss -tnp state established 2>/dev/null \
    | grep -oE 'users:\(\("[^"]+"' | sed 's/users:(("//' | sort | uniq -c | sort -rn | head
REMOTE
