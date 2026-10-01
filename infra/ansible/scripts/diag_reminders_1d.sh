#!/usr/bin/env bash
# diag_reminders_1d.sh — диагностика спама 1-day reminder'ов.
# Гоняет 5 SQL-запросов против vpn-db-1 и собирает картинку:
#   Q1 — распределение action за сутки;
#   Q2 — последние 30 записей за час;
#   Q3 — подписки в окне expiring_1d прямо сейчас;
#   Q4 — JOIN expiring-подписок и их 1-day audit_logs;
#   Q5 — orphan-логи (target_id без подписки).
#
# Usage:
#   ./scripts/diag_reminders_1d.sh                 # на хосте с docker
#   ./scripts/diag_reminders_1d.sh 2>&1 | tee diag_reminders.out

set -u

CONTAINER="${DB_CONTAINER:-vpn-db-1}"
DB_USER="${DB_USER:-vpn}"
DB_NAME="${DB_NAME:-vpn}"

docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" <<'SQL'
\timing on

\echo
\echo ========== Q1: action distribution за сутки ==========
SELECT action, COUNT(*) AS cnt,
       MIN(created_at) AS oldest, MAX(created_at) AS newest
FROM audit_logs
WHERE created_at > NOW() - INTERVAL '24 hours'
  AND (action LIKE 'renewal_reminder_1d%' OR action LIKE 'expiry_reminder_1d%')
GROUP BY action
ORDER BY cnt DESC;

\echo
\echo ========== Q2: последние 30 записей за час ==========
SELECT id, action, target_type, target_id, created_at,
       metadata->>'subscription_id' AS md_sub_id,
       metadata->>'telegram_id'     AS tg_id
FROM audit_logs
WHERE created_at > NOW() - INTERVAL '1 hour'
  AND (action LIKE 'renewal_reminder_1d%' OR action LIKE 'expiry_reminder_1d%')
ORDER BY created_at DESC
LIMIT 30;

\echo
\echo ========== Q3: подписки сейчас в окне expiring_1d ==========
SELECT s.id AS sub_id, s.user_id, u.telegram_id, s.status, s.auto_renew,
       s.expires_at,
       ROUND(EXTRACT(EPOCH FROM (s.expires_at - NOW()))/3600, 2) AS hours_left,
       u.balance_kopecks, u.notify_renewals
FROM subscriptions s
JOIN users u ON u.id = s.user_id
WHERE s.status = 'active'
  AND s.expires_at > NOW()
  AND s.expires_at <= NOW() + INTERVAL '1 day'
ORDER BY s.expires_at;

\echo
\echo ========== Q4: JOIN expiring_1d и 1-day audit_logs ==========
WITH expiring AS (
  SELECT s.id, s.user_id, s.auto_renew, s.expires_at
  FROM subscriptions s
  WHERE s.status = 'active'
    AND s.expires_at > NOW()
    AND s.expires_at <= NOW() + INTERVAL '1 day'
)
SELECT e.id AS sub_id, e.auto_renew,
       al.id AS log_id, al.action, al.target_id AS log_target_id,
       al.created_at
FROM expiring e
LEFT JOIN audit_logs al
       ON al.target_type = 'subscription'
      AND al.target_id  = e.id
      AND (al.action LIKE 'renewal_reminder_1d%' OR al.action LIKE 'expiry_reminder_1d%')
ORDER BY e.id, al.created_at DESC NULLS LAST
LIMIT 200;

\echo
\echo ========== Q5: orphan-логи (нет подписки) ==========
SELECT al.id, al.action, al.target_id, al.created_at
FROM audit_logs al
LEFT JOIN subscriptions s ON s.id = al.target_id
WHERE al.created_at > NOW() - INTERVAL '24 hours'
  AND (al.action LIKE 'renewal_reminder_1d%' OR al.action LIKE 'expiry_reminder_1d%')
  AND al.target_type = 'subscription'
  AND s.id IS NULL
ORDER BY al.created_at DESC
LIMIT 20;
SQL
