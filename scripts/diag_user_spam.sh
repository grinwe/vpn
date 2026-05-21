#!/usr/bin/env bash
# diag_user_spam.sh — расследование, чем спамит бот конкретного юзера.
# Гоняет 4 SQL-запроса против vpn-db-1:
#   Q1 — юзер + его подписка (часы до expiry, баланс, флаги);
#   Q2 — все audit_logs по юзеру и его подпискам за 24 часа;
#   Q3 — всё, что писалось в audit_logs в окрестности 18:30 сегодня;
#   Q4 — распределение action для жертвы за неделю.
#
# Usage:
#   ./scripts/diag_user_spam.sh <telegram_id>
#   ./scripts/diag_user_spam.sh 166230395 2>&1 | tee diag_user_spam.out
#
# Env overrides: DB_CONTAINER, DB_USER, DB_NAME.

set -u

TG_ID="${1:-}"
if [[ -z "$TG_ID" ]]; then
  echo "Usage: $0 <telegram_id>" >&2
  exit 2
fi

CONTAINER="${DB_CONTAINER:-vpn-db-1}"
DB_USER="${DB_USER:-vpn}"
DB_NAME="${DB_NAME:-vpn}"

docker exec -i "$CONTAINER" \
  psql -U "$DB_USER" -d "$DB_NAME" -v tg_id="'$TG_ID'" <<'SQL'
\timing on

\echo
\echo ========== Q1: юзер и его подписка ==========
SELECT u.id AS user_id, u.telegram_id, u.balance_kopecks, u.notify_renewals,
       s.id AS sub_id, s.status, s.auto_renew,
       s.expires_at,
       ROUND(EXTRACT(EPOCH FROM (s.expires_at - NOW()))/3600, 2) AS hours_left,
       s.plan_id
FROM users u
LEFT JOIN subscriptions s ON s.user_id = u.id
WHERE u.telegram_id = :tg_id;

\echo
\echo ========== Q2: audit_logs по юзеру/подпискам за 24 часа ==========
WITH victim AS (
  SELECT id FROM users WHERE telegram_id = :tg_id
),
victim_subs AS (
  SELECT id FROM subscriptions WHERE user_id = (SELECT id FROM victim)
)
SELECT id, actor, actor_type, action, target_type, target_id, created_at,
       metadata
FROM audit_logs
WHERE created_at > NOW() - INTERVAL '24 hours'
  AND (
    metadata->>'telegram_id' = :tg_id
    OR (target_type = 'user'         AND target_id = (SELECT id FROM victim))
    OR (target_type = 'subscription' AND target_id IN (SELECT id FROM victim_subs))
  )
ORDER BY created_at DESC
LIMIT 100;

\echo
\echo ========== Q3: всё в audit_logs в окрестности 18:30 сегодня ==========
SELECT id, actor_type, action, target_type, target_id, created_at,
       metadata->>'telegram_id' AS tg_id
FROM audit_logs
WHERE created_at BETWEEN
    (CURRENT_DATE + INTERVAL '18 hours 20 minutes') AND
    (CURRENT_DATE + INTERVAL '18 hours 45 minutes')
ORDER BY created_at;

\echo
\echo ========== Q4: распределение action для жертвы за неделю ==========
SELECT action, COUNT(*) AS cnt,
       MIN(created_at) AS oldest, MAX(created_at) AS newest
FROM audit_logs
WHERE created_at > NOW() - INTERVAL '7 days'
  AND metadata->>'telegram_id' = :tg_id
GROUP BY action
ORDER BY MAX(created_at) DESC;
SQL
