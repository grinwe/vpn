# Backend API

FastAPI-приложение из `backend/app/main.py`. Три роутера, собранные в один ASGI-процесс, делят PostgreSQL-сессию, Redis-очередь и общий набор сервисов под `backend/app/services/`.

## Сборка приложения

```python
# backend/app/main.py:107-109
app.include_router(api_router)     # api.py         — admin surface
app.include_router(ext_router)     # api_extensions — mixed / public /sub
app.include_router(webapp_router)  # api_webapp     — Mini App
```

На старте (`main.py:18`) прогоняются alembic-миграции, затем `reset_stuck_tasks()` подбирает `ProvisioningTask.status=running|pending` с прошлой жизни процесса и перепихивает их в очередь. Это даёт корректный recovery после жёсткого рестарта, но двойной запуск idempotent ansible-playbook'а — плата за простоту (см. `components/worker.md`).

Middleware порядок:
1. SlowAPI rate limiter (`rate_limit.py`: `get_remote_address`, `memory://`, default `300/minute; 60/second`).
2. CORS — включается только если задан `CORS_ALLOWED_ORIGINS` (comma-separated).
3. Кастомный `add_metrics` — пишет `vpn_requests_total` / `vpn_requests_errors_total` в Prometheus, используя **matched route template** (`/api/users/{user_id}`), чтобы cardinality не взрывалась на каждом id (`main.py:92-104`).

`/metrics` доступен только под `require_admin` — `main.py:122-124`.

## Три роутера — три модели доверия

| Роутер | Префикс | Модуль | Auth | Кто зовёт |
|---|---|---|---|---|
| `api_router` | `/api` | `api.py` | `X-Admin-Token` (shared secret) или scoped `X-Api-Token` | admin SPA, bot, probe-агенты, traffic-коллекторы |
| `webapp_router` | `/api/webapp` | `api_webapp.py` | Telegram `initData` → короткоживущий JWT | браузер пользователя внутри Telegram Mini App |
| `ext_router` | `/api` | `api_extensions.py` | смешанная: `/sub/{token}` — public, остальное — `require_admin` или `optional_admin` | Hiddify/v2rayNG (public sub link), бот (нотификации), admin |

Общий префикс `/api` у `api_router` и `ext_router` означает, что они фактически делят namespace — FastAPI собирает обе таблицы маршрутов в один роутинг, различая по конкретному пути.

## `api.py` — admin surface

~2830 строк, ~55 роутов. Разбит логически на блоки (без подроутеров, только комментарии):

```
/api/healthz              health check (public, deep=true проверяет queue)
/api/stats                admin dashboard summary
/api/nodes                CRUD нод, resync/bootstrap/diagnose/active toggle
/api/nodes/{id}/configs   VPNConfig CRUD
/api/nodes/{id}/probes    агрегация health-проб (scoped: probe:write)
/api/nodes/{id}/health    последний health score (probe:read)
/api/nodes/{id}/migrate   принудительная миграция subs на другую ноду
/api/nodes/spawn          spawn через CloudProvider
/api/nodes/{id}/destroy   destroy через CloudProvider
/api/provisioning/tasks   просмотр/retry/delete/batch ProvisioningTask
/api/subscriptions        POST — создание subscription + провижининг
/api/subscriptions/{id}   status, disable, enable, devices, disable
/api/devices/{id}         GET status, POST revoke
/api/users                listing, by_telegram, balance, disable
/api/users/{id}/topup     admin топап пользователя в копейках
/api/plans                CRUD (видимость, duration, цена)
/api/invoices             listing, create, mark_paid, cancel, mark_unpaid, batch
/api/invoices/{id}/checkout создать pay_url у провайдера
/api/payments             legacy вручную записать Payment
/api/payments/webhook/{provider_name} webhook endpoints от провайдеров
/api/cloud/providers      CRUD CloudProvider
/api/pools/{id}/autoscale обновить autoscale knobs
/api/autoscale/tick       ручной дёрг autoscale-тика
/api/api-tokens           выдача/удаление scoped токенов
/api/audit-logs           чтение journal'а
/api/probes/targets       выдача списка targets probe-агенту
```

Большинство роутов сидят под `Depends(require_admin)`. Исключения:

- **Scoped endpoints** под `require_scope(...)` — всё, что касается probe/traffic: `/api/nodes/{id}/probes` (write), `/api/probes/targets` (read), `/api/nodes/{id}/traffic` (write). Эти вызываются с нод c помощью `X-Api-Token`.
- **Webhooks** `/api/payments/webhook/{provider_name}` — НЕ требуют admin-token, они авторизуются самим провайдером через HMAC-подпись. См. `components/payments.md`.
- **`/api/healthz`** — public (liveness/readiness).

Ключевая вспомогательная функция — `_mark_invoice_paid_core` (`api.py:1774`). Именно её дёргают и admin, и webhook'и; она берёт `SELECT FOR UPDATE` на `Invoice`, проверяет статус, при `kind='topup'` идёт в `services.balance.topup()`, при `kind='subscription'` — в `ProvisioningOrchestrator`.

### Пагинация, фильтрация и поиск

Листинг-endpoint'ы (users, subscriptions, invoices, audit-logs, provisioning-tasks) используют Query-параметры `limit`/`offset` + опциональные фильтры. Единого API для сложных фильтров нет — каждый роут реализует свой набор Query-флагов.

### Audit logging

Внутри admin-роутов любое state-changing действие пишется через `_audit(db, actor, action, target_type, target_id, metadata=...)` (`api.py:49-69`). `actor` приходит из `_resolve_admin_actor(actor_header)` — **заголовок `X-Admin-Actor` не проверяется криптографически**, это self-declared идентификатор для различения действий бота от действий человека. См. `api.py:72-75`.

> ⚠️ Shared admin token + unchecked actor header = если компрометируется любой носитель токена, audit trail теряет доверие. См. audit/...

## `api_webapp.py` — Telegram Mini App

~1430 строк, ~20 роутов. Это отдельный security domain:

1. Пользователь открывает Mini App → клиентский JS получает от Telegram `window.Telegram.WebApp.initData` (signed query-string).
2. Frontend шлёт `POST /api/webapp/auth {init_data}` (`api_webapp.py:149`).
3. Backend верифицирует HMAC по стандартной схеме Telegram: `HMAC-SHA256(data_check_string, HMAC-SHA256("WebAppData", bot_token))` — `api_webapp.py:49-80`.
4. `auth_date` проверяется на `> 86400` секунд от now (защита от replay) — `api_webapp.py:76-78`.
5. Если User не существует — создаётся автоматически (`api_webapp.py:176-180`).
6. Выдаётся **compact hand-rolled JWT**: `base64url(payload).base64url(hmac)`, где `payload = {"uid": id, "exp": now + TTL}`, TTL из `WEBAPP_JWT_TTL_SECONDS` (дефолт 1800 = 30 минут). Не PyJWT — только `hmac` из стандартной библиотеки (`api_webapp.py:94-117`).
7. Дальнейшие запросы несут `Authorization: Bearer <token>`, проходят через `require_webapp_user` (`api_webapp.py:120-134`), которая возвращает ORM-объект `User`.

### Ключевые endpoints

```
POST /api/webapp/auth                      обмен initData → Bearer
GET  /api/webapp/me                        профиль + balance + подписки
GET  /api/webapp/plans                     видимые планы
POST /api/webapp/checkout                  создать инвойс (subscription) + pay_url
POST /api/webapp/topup                     создать инвойс (topup)
GET  /api/webapp/invoices/{id}             опрос статуса инвойса
POST /api/webapp/trial/activate            активация триала
POST /api/webapp/subscriptions/activate    списать с баланса → создать sub
POST /api/webapp/subscriptions/{id}/freeze заморозка
POST /api/webapp/subscriptions/{id}/unfreeze
POST /api/webapp/subscriptions/{id}/devices добавить устройство (extra fee)
PATCH /api/webapp/devices/{id}             переименование
DELETE /api/webapp/devices/{id}            revoke
GET  /api/webapp/transactions              постраничный BalanceTransaction
GET  /api/webapp/referral                  реферальный код + stats
```

### Ownership checks

Каждый роут, работающий с объектом, проверяет принадлежность к `user.id`:

```python
# api_webapp.py:516 (пример из /checkout)
if not target_subscription or target_subscription.user_id != user.id:
    raise HTTPException(404, "Subscription not found")
```

Паттерн единообразный: возвращается **404 (не 403)**, чтобы не утекать существование чужих объектов. Все точки проверки — `api_webapp.py:256, 516, 632, 645, 861, 924, 973, 1048, 1082, 1116, 1167`.

### Почему hand-rolled JWT, а не PyJWT

Единственная нужда — «доверять user_id в течение N минут». PyJWT тащит за собой `cryptography` и расширенный набор алгоритмов, из которых используется один. Комментарий в коде (`api_webapp.py:10-13`) явно фиксирует это как минимизацию зависимостей.

## `api_extensions.py` — смешанный роутер

Шире по назначению, уже по объёму (~460 строк, 8 роутов). Сюда попало всё, что не влезло ни в чисто admin, ни в чисто Mini App:

```
GET  /api/sub/{token}                          ← public, anonymous polling
POST /api/subscriptions/{id}/auto_renew         ← require_admin
POST /api/referral/code                         ← optional_admin
POST /api/users/register                        ← optional_admin
POST /api/trial/activate                        ← optional_admin
POST /api/users/by_telegram/{tg}/regenerate     ← require_admin
GET  /api/notifications/pending                 ← require_admin (polls бот)
POST /api/notifications/{id}/ack                ← require_admin (ack от бота)
```

`optional_admin` значит: токен не обязателен, но если прислан — проверяется как admin. Это оставлено для совместимости со старыми бот-сборками, которые ходили без заголовка; сейчас бот всегда подкладывает admin-token, поэтому `optional` фактически совпадает с `required`.

### `/api/sub/{token}` — единственный анонимный роут бэкенда

```python
# backend/app/api_extensions.py:59
@ext_router.get("/sub/{token}")
def dynamic_sub_link(token: str, db: Session = Depends(get_db)):
    sub = db.query(models.Subscription).filter_by(sub_token=token).first()
    ...
```

Выдаёт base64-кодированный список URI всех активных credential'ов подписки + заголовки `subscription-userinfo` (expire) и `profile-update-interval: 6`, которые Hiddify/v2rayNG читают для автообновления. Пишет `AuditLog(action='subscription_fetch')` на каждый опрос.

> ⚠️ Audit-лог на каждый анонимный опрос + дефолтный nginx access_log (`infrastructure/deployment.md`) = деанон-timeline. См. audit/...

### Notification queue-через-AuditLog

Уведомления бота живут не в отдельной таблице, а в `audit_logs` со специальными `action` ('renewal_reminder', 'config_ready', 'migration_notice', 'low_balance_warning', 'trial_expiry_warning'). Бот опрашивает `/api/notifications/pending`, получает человекочитаемый текст (собранный бэкендом из `extra`-полей) и ack'ит через `/api/notifications/{id}/ack`, который просто дописывает `:delivered` в `log.action`. Детали — `api_extensions.py:359-458`.

## Модели доверия — сводно

```
                  X-Admin-Token (shared secret, env)
                      │
                      ├─► api.py (require_admin)            ← admin SPA, bot
                      ├─► api.py scoped endpoints            ← alt: X-Api-Token
                      └─► api_extensions.py (require_admin)  ← bot
                  
                  X-Api-Token (sha256 hashed, scoped)
                      │
                      └─► api.py scoped                      ← probes, traffic collectors

                  Telegram initData → JWT
                      │
                      └─► api_webapp.py (require_webapp_user) ← browser in TG

                  HMAC signature in body/headers
                      │
                      └─► api.py /payments/webhook/*          ← payment providers

                  none
                      │
                      ├─► api.py /healthz
                      └─► api_extensions.py /sub/{token}
```

## Обработка ошибок

- FastAPI `HTTPException` — для бизнес-ошибок, превращается в `{"detail": "..."}`.
- Внутренние сбои логируются через `logger.exception`, наружу уходит generic 500. Исключения — провижининг: `_create_subscription_for_user` ловит любую ошибку, делает rollback, отдаёт 500 "Provisioning failed" (`api.py:119-122`).
- Rate limit превышение — 429, обрабатывается SlowAPI middleware.

## Зависимости роутов от сервисов

```
api.py          →  services.provisioning, services.payments,
                   services.autoscale, services.health,
                   services.node_spawner, services.balance,
                   services.cloud, services.warm_pool
api_webapp.py   →  services.balance, services.provisioning,
                   services.payments, services.trial
api_extensions.py → services.provisioning, services.trial
```

Все сервисы держат SQLAlchemy-сессию, которую им передаёт route через `Depends(get_db)`. Нет отдельного application-layer DI контейнера.

## ⚠️ Неясные места

- `api_webapp.py` импортирует `_subscriptions_for_user` прямо из `api.py` (`api_webapp.py:30`). Это единственная точка крест-модульного импорта приватных helper'ов — неясно, является ли это осознанным «этот helper shared» или остатком рефакторинга. Если рефакторинг — есть риск неожиданного поведения при изменениях в api.py.
- `_mark_invoice_paid_core` при уже `paid` инвойсе делает fallback на «latest subscription by (user_id, plan_id)» (`api.py:1810-1820`). Логика защиты от повторной доставки webhook'а, но может отдать пользователю данные **другой** подписки, если он успел купить второй инвойс на тот же план. Код-level ambiguity; см. audit/...
- `/api/healthz?deep=true` проверяет, что queue доступна, но не гоняет SELECT 1 на неё — `get_queue() is not None` верифицирует наличие Redis connection, но не его работоспособность.
