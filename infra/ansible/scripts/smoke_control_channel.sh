#!/usr/bin/env bash
# Smoke test для control-channel Phase A.
#
# Что проверяем:
#   1. Endpoint доступен (через CF Worker или прямо на backend).
#   2. HMAC client_id корректно резолвится в Device.
#   3. select_target_node возвращает healthy ноду.
#   4. migrate_subscription_to_new_node запускается.
#   5. audit_log запись появилась.
#   6. Rate-limit срабатывает после 5 запросов в 30 мин.
#
# Запуск:
#   APP_SECRET_KEY=... ADMIN_API_TOKEN=... \
#     scripts/smoke_control_channel.sh <sub_token>
#
# APP_SECRET_KEY используется для (1) вычисления client_id_hmac локально
# и (2) как shared secret в X-Control-Channel-Secret header — отдельной
# CONTROL_CHANNEL_SECRET переменной нет (см. control_channel.md).
#
# Опциональные env vars:
#   BACKEND_URL — default https://mgmt.grinwer.online
#   WORKER_URL  — если хотим тестировать через CF Worker
#                 (например https://control-1.<acct>.workers.dev)
#                 если пусто — шлём прямо на BACKEND_URL.

set -u

SUB_TOKEN="${1:-}"
if [[ -z "$SUB_TOKEN" ]]; then
    echo "Usage: $0 <sub_token>" >&2
    exit 1
fi

: "${APP_SECRET_KEY:?need APP_SECRET_KEY}"

BACKEND_URL="${BACKEND_URL:-https://mgmt.grinwer.online}"
WORKER_URL="${WORKER_URL:-}"

# ── 1. Compute client_id_hmac локально через тот же helper ─────────────
CLIENT_ID=$(python3 -c "
import os, sys
sys.path.insert(0, '$(dirname "$0")/..')
os.environ['APP_SECRET_KEY']='$APP_SECRET_KEY'
from backend.app.security import compute_client_id_hmac
print(compute_client_id_hmac('$SUB_TOKEN'))
")

if [[ -z "$CLIENT_ID" ]]; then
    echo "Failed to compute client_id_hmac" >&2
    exit 1
fi

echo "── computed client_id_hmac = $CLIENT_ID ──"
echo

# ── 2. Один report — ожидаем 'migrated' или 'throttled' ────────────────
echo "── Test 1: single report ──"

if [[ -n "$WORKER_URL" ]]; then
    URL="$WORKER_URL/report"
    HEADERS=(
        "-H" "Content-Type: application/json"
        "-H" "X-Client-ID: $CLIENT_ID"
    )
    echo "(via Worker: $URL)"
else
    URL="$BACKEND_URL/api/client/report-failure"
    HEADERS=(
        "-H" "Content-Type: application/json"
        "-H" "X-Control-Channel-Secret: $APP_SECRET_KEY"
        "-H" "X-Client-ID: $CLIENT_ID"
    )
    echo "(direct to backend: $URL)"
fi

PAYLOAD=$(printf '{"kind":"user_reported","ts":%d,"current_node_id":12,"fail_count":1}' "$(date +%s)")

RESP=$(curl -sS -X POST "$URL" "${HEADERS[@]}" -d "$PAYLOAD" -w "\n__STATUS__:%{http_code}")
STATUS=$(echo "$RESP" | grep -oE '__STATUS__:[0-9]+' | cut -d: -f2)
BODY=$(echo "$RESP" | sed 's/__STATUS__:[0-9]*$//')

echo "status: $STATUS"
echo "body:   $BODY"
echo

# ── 3. Rate-limit check ────────────────────────────────────────────────
echo "── Test 2: rate-limit (ожидаем 429 после 5 быстрых запросов) ──"

for i in 1 2 3 4 5 6 7; do
    sleep 0.5
    RESP=$(curl -sS -X POST "$URL" "${HEADERS[@]}" -d "$PAYLOAD" -w "\n__STATUS__:%{http_code}")
    STATUS=$(echo "$RESP" | grep -oE '__STATUS__:[0-9]+' | cut -d: -f2)
    BODY=$(echo "$RESP" | sed 's/__STATUS__:[0-9]*$//')
    ACTION=$(echo "$BODY" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('action', d.get('detail', '?')))" 2>/dev/null || echo "?")
    echo "  #$i: status=$STATUS action=$ACTION"
    if [[ "$STATUS" == "429" ]]; then
        echo "  ✓ rate-limit сработал на $i-м запросе"
        break
    fi
done

echo
echo "── Test 3: admin trigger (требует ADMIN_API_TOKEN) ──"

if [[ -z "${ADMIN_API_TOKEN:-}" ]]; then
    echo "  skip — ADMIN_API_TOKEN не задан"
else
    # Берём subscription_id первый active в БД.
    # На smoke-host'е выполнить нельзя, поэтому пропускаем если не задан.
    if [[ -z "${TEST_SUBSCRIPTION_ID:-}" ]]; then
        echo "  skip — TEST_SUBSCRIPTION_ID не задан (передай как env)"
    else
        ADMIN_RESP=$(curl -sS -X POST "$BACKEND_URL/api/admin/client-control/report-for-subscription" \
            -H "X-Admin-Token: $ADMIN_API_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"subscription_id\":$TEST_SUBSCRIPTION_ID}" \
            -w "\n__STATUS__:%{http_code}")
        ADMIN_STATUS=$(echo "$ADMIN_RESP" | grep -oE '__STATUS__:[0-9]+' | cut -d: -f2)
        ADMIN_BODY=$(echo "$ADMIN_RESP" | sed 's/__STATUS__:[0-9]*$//')
        echo "  admin status: $ADMIN_STATUS"
        echo "  admin body:   $ADMIN_BODY"
    fi
fi

echo
echo "── Done. Проверь /admin/tasks + audit_log на client_reported_failure ──"
