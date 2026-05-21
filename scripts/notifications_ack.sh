#!/usr/bin/env bash
# notifications_ack.sh — посмотреть pending-нотификации и/или пометить
# их :delivered для юзера, который заблокировал бота.
#
# Зачем: poller гоняет AuditLog с action != '%:delivered' и пытается
# доставить. Если юзер заблокировал бота, доставки нет — записи висят
# навсегда. Скрипт обрывает этот висяк руками: проставляет ':delivered'
# суффикс на все system-логи юзера. Идемпотентно (повторный запуск
# обработает только новонабежавшие).
#
# Usage:
#   ./scripts/notifications_ack.sh                  # только просмотр
#   ./scripts/notifications_ack.sh <telegram_id>    # просмотр + UPDATE
#
# Env overrides: DB_CONTAINER, DB_USER, DB_NAME.

set -u

TG_ID="${1:-}"
CONTAINER="${DB_CONTAINER:-vpn-db-1}"
DB_USER="${DB_USER:-vpn}"
DB_NAME="${DB_NAME:-vpn}"

if [[ -n "$TG_ID" ]]; then
  docker exec -i "$CONTAINER" \
    psql -U "$DB_USER" -d "$DB_NAME" -v tg_id="'$TG_ID'" <<'SQL'
\timing on

\echo
\echo ========== A: pending по telegram_id + action (top 20) ==========
SELECT metadata->>'telegram_id' AS tg, action, COUNT(*) AS cnt
FROM audit_logs
WHERE actor_type = 'system' AND action NOT LIKE '%:delivered'
GROUP BY metadata->>'telegram_id', action
ORDER BY cnt DESC
LIMIT 20;

\echo
\echo ========== B: помечаем :delivered для tg_id = :tg_id ==========
BEGIN;
UPDATE audit_logs
SET action = action || ':delivered'
WHERE actor_type = 'system'
  AND action NOT LIKE '%:delivered'
  AND metadata->>'telegram_id' = :tg_id;
COMMIT;
SQL
else
  docker exec -i "$CONTAINER" \
    psql -U "$DB_USER" -d "$DB_NAME" <<'SQL'
\timing on

\echo
\echo ========== pending по telegram_id + action (top 20) ==========
SELECT metadata->>'telegram_id' AS tg, action, COUNT(*) AS cnt
FROM audit_logs
WHERE actor_type = 'system' AND action NOT LIKE '%:delivered'
GROUP BY metadata->>'telegram_id', action
ORDER BY cnt DESC
LIMIT 20;
SQL
fi
