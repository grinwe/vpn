#!/usr/bin/env bash
# fix_xhttp_sni.sh — пост-DR fix для vless-xhttp на старых нодах:
# восстанавливает port=443 + sni=sXX.grinwer.online + settings.domain.
#
# Проблема: после disaster-recovery 2026-05-19 значение домена осело
# в Fallback вместо SNI (UI-форма раскладывала поля не туда), а порт
# восстановился как 10443 вместо 443. Из-за пустого SNI nginx на
# фронте не матчит vhost — xhttp-трафик не доходит до xray. Cм. скрин
# Cloudflare DNS + админка `/nodes` → редактирование vless-xhttp.
#
# Маппинг host → cf-домен (с DNS Cloudflare):
#   171.22.134.110 → s.grinwer.online   (id=9,  ru-pq-01)
#   178.20.208.67  → s01.grinwer.online (id=10, ru-ae-01)
#   171.22.134.123 → s02.grinwer.online (id=12, ru-pq-02)
#   171.22.134.124 → s03.grinwer.online (id=13, ru-pq-03)
#   45.91.53.69    → s04.grinwer.online (id=14, ru-dc-01)
#
# Skip'ы:
#   15 ru-dc-02       (45.91.53.67) — уже корректно настроен
#   16 ru-cloud-web-01 (45.95.234.78) — то же
#
# Use cases:
#   ./scripts/fix_xhttp_sni.sh             # DRY-RUN: видно BEFORE/UPDATE/AFTER + ROLLBACK
#   ./scripts/fix_xhttp_sni.sh --apply     # реальный COMMIT
#
# Env overrides (как в workers.sh):
#   MGMT_HOST — IP/hostname mgmt-хоста. Default: парсится из ansible
#               inventory (db_host → mgmt-1.ansible_host).
#   MGMT_USER — SSH user. Default: root.
#   STACK_DIR — путь к /opt/vpn на mgmt'е. Default: /opt/vpn.
#
# ВАЖНО: SQL-апдейт только меняет данные в БД. Xray на нодах
# подхватит новый конфиг только при пере-рендере, который делает
# bootstrap-task. После --apply кликни «bootstrap» на каждой из 5
# нод в /nodes (или прогони site.yml массово).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MGMT_USER="${MGMT_USER:-root}"
STACK_DIR="${STACK_DIR:-/opt/vpn}"

# MGMT_HOST: env override либо парсинг из inventory (как в workers.sh).
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

MODE="${1:-dry-run}"
if [[ "$MODE" != "dry-run" && "$MODE" != "--apply" ]]; then
    echo "Usage: $0 [--apply]" >&2
    echo "       без аргумента — dry-run (показывает SQL + ROLLBACK)" >&2
    exit 1
fi

# host (ansible_host) → cf-домен
HOSTS=(
    "171.22.134.110:s.grinwer.online"
    "178.20.208.67:s01.grinwer.online"
    "171.22.134.123:s02.grinwer.online"
    "171.22.134.124:s03.grinwer.online"
    "45.91.53.69:s04.grinwer.online"
)

build_sql() {
    cat <<'SQL'
\set ON_ERROR_STOP true
BEGIN;

\echo '--- BEFORE ---'
SELECT n.id, n.name, n.host, c.port, c.sni,
       c.settings->>'domain' AS settings_domain
FROM vpn_nodes n
JOIN vpn_configs c ON c.node_id = n.id
WHERE c.protocol = 'vless_xhttp'
  AND n.host IN (
      '171.22.134.110','178.20.208.67','171.22.134.123',
      '171.22.134.124','45.91.53.69'
  )
ORDER BY n.id;
SQL

    for entry in "${HOSTS[@]}"; do
        host="${entry%%:*}"
        domain="${entry##*:}"
        cat <<SQL

-- $host → $domain
UPDATE vpn_configs
SET port = 443,
    sni  = '$domain',
    settings = jsonb_set(
        coalesce(settings, '{}'::jsonb),
        '{domain}',
        '"$domain"'
    )
WHERE protocol = 'vless_xhttp'
  AND node_id = (SELECT id FROM vpn_nodes WHERE host = '$host')
RETURNING node_id, port, sni, settings->>'domain' AS new_domain;
SQL
    done

    cat <<'SQL'

\echo '--- AFTER ---'
SELECT n.id, n.name, n.host, c.port, c.sni,
       c.settings->>'domain' AS settings_domain
FROM vpn_nodes n
JOIN vpn_configs c ON c.node_id = n.id
WHERE c.protocol = 'vless_xhttp'
  AND n.host IN (
      '171.22.134.110','178.20.208.67','171.22.134.123',
      '171.22.134.124','45.91.53.69'
  )
ORDER BY n.id;
SQL

    if [[ "$MODE" == "--apply" ]]; then
        echo "COMMIT;"
    else
        echo "\\echo '--- DRY-RUN: rolling back, ничего не сохранено ---'"
        echo "ROLLBACK;"
    fi
}

echo "→ ${MGMT_USER}@${MGMT_HOST}: $MODE"
echo ""

build_sql | ssh "${MGMT_USER}@${MGMT_HOST}" \
    "cd ${STACK_DIR} && docker compose exec -T db psql -U vpn -d vpn"

if [[ "$MODE" == "--apply" ]]; then
    echo ""
    echo "=== Готово. Теперь не забудь пересобрать xray-config на нодах:"
    echo "    кликни «bootstrap» на каждой из 5 нод (9, 10, 12, 13, 14) в /nodes"
    echo "    или прогони site.yml массово через ansible."
fi
