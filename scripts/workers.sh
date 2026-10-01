#!/usr/bin/env bash
# workers.sh — отскейлить worker-контейнеры на mgmt-хосте.
#
# Use cases:
#   ./scripts/workers.sh 5     # бамп до 5 на массовый bootstrap/resync
#   ./scripts/workers.sh 1     # обратно к prod-дефолту после
#   ./scripts/workers.sh       # без аргумента — показать сколько сейчас
#
# 4×8 VPS держит до ~5 спокойно (5×3 = 15 параллельных ansible-ранов
# I/O bound через SSH, peak ~4GB RAM). Выше 5 — без необходимости,
# CPU всё равно простаивает (ansible ждёт remote-ноды).
#
# Env overrides:
#   MGMT_HOST — IP/hostname mgmt-хоста. Default: парсится из ansible
#               inventory (db_host → mgmt-1.ansible_host).
#   MGMT_USER — SSH user. Default: root.
#   STACK_DIR — путь к /opt/vpn на mgmt'е. Default: /opt/vpn.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MGMT_USER="${MGMT_USER:-root}"
STACK_DIR="${STACK_DIR:-/opt/vpn}"

# Если MGMT_HOST не задан явно, вытаскиваем из inventory: db_host →
# mgmt-1 → ansible_host. Падать с понятной ошибкой если структура
# поменяется или python3/PyYAML отсутствуют.
if [[ -z "${MGMT_HOST:-}" ]]; then
    MGMT_HOST=$(python3 -c "
import yaml, sys
try:
    inv = yaml.safe_load(open('$REPO_ROOT/infra/ansible/inventories/prod/hosts.yml'))
    print(inv['all']['children']['db_host']['hosts']['mgmt-1']['ansible_host'])
except Exception as e:
    sys.stderr.write(f'Cannot resolve mgmt host: {e}\n')
    sys.exit(2)
") || {
        echo "Set MGMT_HOST=<ip> or fix inventory parsing" >&2
        exit 2
    }
fi

N="${1:-}"

# No args = показать текущее состояние и выйти.
if [[ -z "$N" ]]; then
    echo "MGMT: ${MGMT_USER}@${MGMT_HOST}:${STACK_DIR}"
    echo "Current worker replicas:"
    ssh "${MGMT_USER}@${MGMT_HOST}" \
        "docker compose -f ${STACK_DIR}/docker-compose.yml ps worker --format 'table {{.Name}}\t{{.State}}\t{{.Status}}'"
    exit 0
fi

# Sanity: число в пределах разумного.
if ! [[ "$N" =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 <N>   # N = integer (replicas)" >&2
    exit 1
fi
if (( N < 1 || N > 20 )); then
    echo "N=$N выглядит подозрительно (нормально 1-10 на 4×8 VPS)" >&2
    exit 1
fi

echo "→ ${MGMT_USER}@${MGMT_HOST}: setting worker replicas = ${N}"

ssh "${MGMT_USER}@${MGMT_HOST}" \
    "set -e
     cd ${STACK_DIR}
     # Сохраняем в .env чтоб переживало docker compose restart/up
     if grep -q '^WORKER_REPLICAS=' .env 2>/dev/null; then
         sed -i 's/^WORKER_REPLICAS=.*/WORKER_REPLICAS=${N}/' .env
     else
         echo 'WORKER_REPLICAS=${N}' >> .env
     fi
     docker compose up -d --scale worker=${N} worker
     echo
     echo '=== worker status ==='
     docker compose ps worker --format 'table {{.Name}}\t{{.State}}'
     echo
     echo '=== resource usage (snapshot) ==='
     docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
         | head -15"
