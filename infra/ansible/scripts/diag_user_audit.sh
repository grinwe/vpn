#!/usr/bin/env bash
# diag_user_audit.sh — расширенный аудит конкретного юзера (7 дней + tz-окна).
# Гоняет 4 SQL-запроса против vpn-db-1:
#   A — юзер и все его подписки (часы до expiry, баланс, флаги);
#   B — ВСЕ audit_logs жертвы за 7 дней (по metadata.telegram_id или target);
#   C — всё в audit_logs в окне 18:20-18:45 за 36 часов (UTC и MSK ловит оба);
#   D — per-action распределение для жертвы за 7 дней.
#
# Usage:
#   ./scripts/diag_user_audit.sh <telegram_id>
#   ./scripts/diag_user_audit.sh 166230395 2>&1 | tee diag_user_audit.out
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
\echo ========== A: юзер и все его подписки ==========
SELECT u.id AS user_id, u.telegram_id, u.balance_kopecks, u.notify_renewals,
       s.id AS sub_id, s.status, s.auto_renew, s.expires_at,
       ROUND(EXTRACT(EPOCH FROM (s.expires_at - NOW()))/3600, 2) AS hours_left,
       s.plan_id
FROM users u
LEFT JOIN subscriptions s ON s.user_id = u.id
WHERE u.telegram_id = :tg_id;

\echo
\echo ========== B: все audit_logs жертвы за 7 дней ==========
WITH v  AS (SELECT id FROM users WHERE telegram_id = :tg_id),
     vs AS (SELECT id FROM subscriptions WHERE user_id = (SELECT id FROM v))
SELECT id, actor, actor_type, action, target_type, target_id, created_at,
       metadata->>'telegram_id'     AS md_tg,
       metadata->>'subscription_id' AS md_sub,
       metadata
FROM audit_logs
WHERE created_at > NOW() - INTERVAL '7 days'
  AND (metadata->>'telegram_id' = :tg_id
       OR (target_type = 'user'         AND target_id = (SELECT id FROM v))
       OR (target_type = 'subscription' AND target_id IN (SELECT id FROM vs)))
ORDER BY created_at DESC
LIMIT 200;

\echo
\echo ========== C: окно 18:20-18:45 за 36 часов (UTC и MSK) ==========
SELECT id, actor_type, action, target_type, target_id, created_at,
       metadata->>'telegram_id' AS tg_id
FROM audit_logs
WHERE (created_at >= NOW() - INTERVAL '36 hours')
  AND (
    EXTRACT(HOUR FROM created_at) IN (15, 18)
    AND EXTRACT(MINUTE FROM created_at) BETWEEN 20 AND 45
  )
ORDER BY created_at DESC
LIMIT 100;

\echo
\echo ========== D: per-action распределение жертвы за 7 дней ==========
SELECT action, COUNT(*) AS cnt,
       MIN(created_at) AS first, MAX(created_at) AS last
FROM audit_logs
WHERE created_at > NOW() - INTERVAL '7 days'
  AND metadata->>'telegram_id' = :tg_id
GROUP BY action
ORDER BY MAX(created_at) DESC;
SQL
