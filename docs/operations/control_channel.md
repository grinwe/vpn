# Control channel — operator cheat-sheet

Phase A custom-клиента ([roadmap](control_channel_roadmap.md)). Это
day-to-day operator-документ; **архитектурные решения** и **roadmap**
живут в roadmap'е, тут только «как пользоваться».

## TL;DR

* Custom-клиент шлёт сигнал → CF Worker (`control-N.workers.dev`) →
  backend `/api/client/report-failure` → backend выбирает healthy ноду
  и мигрирует подписку.
* `sub_token` preserved → клиент сам подхватит новые credentials через
  refresh `/api/sub/{token}`.
* Без клиента ту же кнопку имеем в админке `/users/<id>` → `🚨 report failure`
  на subscription card.

## Endpoint'ы

| URL | Кто шлёт | Auth |
|---|---|---|
| `https://control-N.<acct>.workers.dev/report` | custom-клиент | `X-Client-ID` header (HMAC) |
| `https://mgmt.grinwer.online/api/client/report-failure` | CF Worker forward | `X-Control-Channel-Secret: <APP_SECRET_KEY>` |
| `https://mgmt.grinwer.online/api/admin/client-control/report-for-subscription` | админка | `X-Admin-Token` |

`X-Control-Channel-Secret` несёт значение `APP_SECRET_KEY` (тот же что
Fernet'ит секреты в БД) — отдельной vault-переменной нет. На Worker'е
лежит как `env.APP_SECRET_KEY` через `wrangler secret put`. На backend
читается из env (уже там был).

## Что происходит за один report

1. Backend validate auth.
2. Resolve subscription (`subscription_id` для admin / Device by HMAC client_id для клиента).
3. Если subscription не `active` → response `{action: "subscription_inactive"}`.
4. Дедуп: за последние 5 мин уже мигрировали этого юзера? → `{action: "throttled"}`.
5. `provisioning.choose_node`: healthy, active, NOT muted (legacy + diagnostics toggle), NOT в cooldown, NOT current, в том же pool. Если нет → `{action: "no_target_available"}` + audit_log `client_reported_failure_no_target`.
6. `migrate_subscription_to_new_node(sub, target)` — preserve sub_token, новый ProvisioningTask.
7. Audit: `action="client_reported_failure"` + metadata (kind, current/target node_id, task_id).
8. Response: `{ok: true, action: "migrated", target_node_id, target_node_name, task_id}`.

## Что видит оператор в дашбордах

* `/admin/tasks` — новый task с action `migrate`, target — relay-нода.
* `/admin/audit` (если есть UI) — записи `client_reported_failure` /
  `client_reported_failure_no_target`.
* В будущем — Prometheus метрика `vpn_client_reports_total{kind, action}`
  (TODO Phase A.future).

## Деплой / rotation

Worker'ы — `infra/cloudflare/control_worker/` ([README](../../../infra/cloudflare/control_worker/README.md)).
3 instance'а (control-1/2/3) для rotation. URL list передаётся клиенту
в подписочных данных (TODO: пока статичный hardcode на стороне sub-link
generator).

При block'е одного Worker'а:
1. Deploy новый Worker: `wrangler deploy --name control-4`.
2. Push secrets: `wrangler secret put` x2 (`APP_SECRET_KEY` + `BACKEND_URL`).
3. Добавить `control-4` в env `CONTROL_WORKER_URLS` backend'а.
4. Restart backend → новые подписочные данные содержат все 4 Worker'а.
5. Через ~60 дней (время чтобы клиенты подхватили) — `wrangler delete control-1`.

## Smoke-тест

```bash
# Замена реальных значений:
# - SUB_TOKEN — из БД, у любого тестового Device.sub_token
# - APP_SECRET_KEY — из vault.yml (vault_app_secret_key), он же служит
#                    как shared secret для Worker↔backend.
CLIENT_ID=$(
    python3 -c "
import os, hashlib, hmac, base64
os.environ['APP_SECRET_KEY']='$APP_SECRET_KEY'
from backend.app.security import compute_client_id_hmac
print(compute_client_id_hmac('$SUB_TOKEN'))
"
)
echo "client_id_hmac = $CLIENT_ID"

# 1. Прямой POST на backend (минуя Worker). В X-Control-Channel-Secret
#    передаём APP_SECRET_KEY (тот же что у Fernet'а):
curl -sS -X POST https://mgmt.grinwer.online/api/client/report-failure \
    -H "Content-Type: application/json" \
    -H "X-Control-Channel-Secret: $APP_SECRET_KEY" \
    -H "X-Client-ID: $CLIENT_ID" \
    -d '{"kind":"user_reported","ts":'$(date +%s)',"current_node_id":12,"fail_count":1}' \
    | jq

# 2. Через Worker (если уже задеплоен):
curl -sS -X POST https://control-1.<acct>.workers.dev/report \
    -H "Content-Type: application/json" \
    -H "X-Client-ID: $CLIENT_ID" \
    -d '{"kind":"user_reported","ts":'$(date +%s)',"current_node_id":12,"fail_count":1}' \
    | jq

# 3. Через админку (auth по X-Admin-Token):
curl -sS -X POST https://mgmt.grinwer.online/api/admin/client-control/report-for-subscription \
    -H "X-Admin-Token: $ADMIN_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"subscription_id": 21}' \
    | jq
```

Ожидаемое: `{ok: true, action: "migrated", target_node_id: <other>, task_id: <N>}`.

Полный сценарий smoke в `scripts/smoke_control_channel.sh`.

## Troubleshooting

| Симптом | Где искать |
|---|---|
| `{action: "no_target_available"}` | `SELECT id,name,status,is_active,auto_diagnose_disabled_at FROM vpn_nodes WHERE status='active' AND is_active=true AND auto_diagnose_disabled_at IS NULL;` — есть ли вообще куда мигрировать? |
| `401 invalid control-channel secret` | `APP_SECRET_KEY` в backend контейнере **!=** secret на CF Worker'е (рассинхрон при rotation). `docker compose exec worker env \| grep APP_SECRET_KEY` + `wrangler secret list --name control-1`. Push'нуть синхронно через `wrangler secret put` на все 3 Worker'а. |
| `401 unknown client_id` | Device с этим `client_id_hmac` нет. Миграция 0037 backfill'ит при naличии APP_SECRET_KEY — если env не было при миграции, backfill пропустился. Пересоздать через ручной UPDATE: `SELECT id, sub_token, client_id_hmac FROM devices WHERE client_id_hmac IS NULL AND sub_token IS NOT NULL;` → пересчитать через `compute_client_id_hmac` + UPDATE. |
| `429 Too Many Requests` | Rate-limit slowapi: 5 reports / 30 мин per client_id. Это by design — клиент дёргает повторно слишком часто. Подождать. |
| `503 APP_SECRET_KEY is not configured` | env `APP_SECRET_KEY` пустой на backend контейнере (мало вероятно — без него и Fernet/encrypt тоже не работает). Проверь `env.j2` + `vault.yml`. |

## Security threat model

* **Leak X-Client-ID** (например через CF logs или MITM) → злоумышленник может дёргать report-failure от имени юзера. Защита: rate-limit + `5-min` дедуп per subscription. Worst-case impact: один лишний migrate за 30 мин, не критично.
* **Leak `APP_SECRET_KEY`** → одновременно (1) можно отправить любой report напрямую на backend без CF и (2) вычислить любой client_id_hmac (3) расшифровать все Fernet-encrypted creds/WG keys в БД. Это catastrophic — главный rotation event. Защита: APP_SECRET_KEY только в vault, никогда не в plain. При compromise rotation:  ansible push нового vault → backend `.env` update + restart → `wrangler secret put APP_SECRET_KEY` на все 3 Worker'а → backfill `Device.client_id_hmac` с новым ключом (или принять что старые клиенты потеряют control-channel до следующего refresh sub_token).
* **Leak `sub_token`** → клиент уже имеет доступ к credentials, control-channel это не делает хуже.

## Что НЕ делаем

* Не публикуем Worker URL'ы открыто (на сайте, в README в публичных репо, в Telegram).
* Не туннелируем VPN-трафик через control-channel (это путь к demask'у, см. roadmap §1).
* Не позволяем control-channel выполнять что-то beyond migrate_subscription (никаких delete, balance change, plan change — только через основной auth flow).
