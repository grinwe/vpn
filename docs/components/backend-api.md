# Backend API

FastAPI-приложение из `backend/app/main.py`. Три роутера, собранные в один ASGI-процесс, делят PostgreSQL-сессию, Redis-очередь и общий набор сервисов под `backend/app/services/`.

Видимость health-ping телеметрии: модуль `api/health_pings.py` (см. таблицу ниже) агрегирует `AuditLog` по `action IN (health_ping_request|response|opt_out)` в три admin-endpoint'а (`/health-pings/summary`, `/health-pings/recent-bad`, `/nodes/{id}/health-pings`). Webapp отдельным endpoint'ом `POST /api/webapp/health-ping-report` даёт юзеру кнопку «🆘 VPN не работает» без ожидания планового пинга. `HealthPingResponseRequest` в `api_extensions.py` носит опциональное `source: "prompted" | "self_reported"` (default — `"prompted"`, чтобы не ломать bot-клиентов) — админка отдельно подсвечивает self-reported как более сильный сигнал.

## Сборка приложения

```python
# backend/app/main.py:107-109
app.include_router(api_router)     # api/             — admin surface (package с 16 модулями)
app.include_router(ext_router)     # api_extensions   — mixed / public /sub
app.include_router(webapp_router)  # api_webapp       — Mini App
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
| `api_router` | `/api` | `api/` (package) | `X-Admin-Token` (shared secret) или scoped `X-Api-Token` | admin SPA, bot, probe-агенты, traffic-коллекторы |
| `webapp_router` | `/api/webapp` | `api_webapp.py` | Telegram `initData` → короткоживущий JWT | браузер пользователя внутри Telegram Mini App |
| `ext_router` | `/api` | `api_extensions.py` | смешанная: `/sub/{token}` — public, остальное — `require_admin` или `optional_admin` | Hiddify/v2rayNG (public sub link), бот (нотификации), admin |

Общий префикс `/api` у `api_router` и `ext_router` означает, что они фактически делят namespace — FastAPI собирает обе таблицы маршрутов в один роутинг, различая по конкретному пути.

## `api/` — admin surface

~3240 строк, 62 роута, 16 модулей. Реализован как Python-пакет: `backend/app/api/__init__.py` собирает master-router (`APIRouter(prefix="/api")`) из листовых модулей через `include_router`. Каждый подмодуль держит свой локальный `router = APIRouter()` (без префикса — пути внутри файлов читаются как `/plans`, а не `/api/plans`), что упрощает unit-тесты в изоляции.

### Карта модулей

| Модуль | Префикс(ы) | LOC | Что делает |
|---|---|---|---|
| `api/_common.py` | — | 177 | `get_db`, `_audit`, `_resolve_admin_actor`, `_get_or_create_user`, `_get_user_from_payload`, `_create_subscription_for_user`, `ADMIN_ACTOR_HEADER`, `logger`. Общие зависимости для всех листовых модулей. |
| `api/health.py` | `/healthz`, `/stats` | 102 | health check (public, `deep=true` проверяет очередь) + admin dashboard summary |
| `api/plans.py` | `/plans` | 82 | CRUD планов (видимость, duration, цена) |
| `api/audit.py` | `/audit-logs` | 44 | чтение журнала аудита |
| `api/tokens.py` | `/api-tokens` | 104 | выдача/отзыв scoped API-токенов |
| `api/nodes.py` | `/nodes` + subpaths | 780 | CRUD нод, resync/bootstrap/diagnose/active toggle, **`PATCH /nodes/{id}/status`** (ручной override статуса active/error/disabled), `/configs` CRUD (+ **`PUT /nodes/{id}/configs/{cfg_id}`** — in-place редактирование без delete+create), **`GET /nodes/{id}/users`** (кто на ноде в последнем traffic-tick'е, с джойном на Device/Subscription/User/Plan), **`GET /nodes/{id}/traffic-history?hours=24`** (временной ряд `NodeTrafficSample` для sparkline-графика в админке), spawn/destroy через CloudProvider, миграция подписок, health |
| `api/tasks.py` | `/provisioning/tasks` | 276 | просмотр/retry/delete/batch `ProvisioningTask` + `_enrich_task_telegram` (батч-резолв `telegram_id` для ADMIN_UI) |
| `api/subscriptions.py` | `/subscriptions`, `/devices` | 309 | POST — создание subscription + провижининг; status/disable/enable/devices; revoke устройства; **`POST /subscriptions/{id}/migrate`** (per-sub admin override: переселить на выбранную ноду без проверок пула/health/cooldown, только `is_active=True`) |
| `api/users.py` | `/users` | 441 | listing, by_telegram, balance, disable, **`/ban` + `/unban`** (user-level бан, ставит/снимает `User.banned_at`, ортогонально `Subscription.status=blocked` — бот-мидлварь молча дропает апдейты от забаненных, без ACK), **`GET /users/banned-telegram-ids`** (тонкий список для `BanGuard`-кэша в боте), `/topup` (admin топап в копейках). Хостит `_subscriptions_for_user`, который re-exported из `api/__init__.py` и используется `api_webapp.py` |
| `api/traffic.py` | `/subscriptions/{id}/traffic`, `/nodes/{id}/traffic` | 205 | traffic accounting: оба эндпойнта делят `_apply_traffic_delta` (revoke при превышении лимита) |
| `api/probes.py` | `/nodes/{id}/probes`, `/probes/targets` | 115 | агрегация health-проб (scoped: `probe:write`) + список targets (scoped: `probe:read`) |
| `api/health_pings.py` | `/health-pings/summary`, `/health-pings/recent-bad`, `/nodes/{id}/health-pings` | ~420 | агрегация user-side health-ping телеметрии из `AuditLog` (`action IN (health_ping_request|response|opt_out)`) для админ-дашборда `/health-pings` и виджета в карточке ноды. JSONB-join по `extra->>'node_id'` без прохода через `Subscription` (значение денормализовано на момент записи). Различает `extra.source = prompted/self_reported`. |
| `api/invoices.py` | `/invoices` | 462 | listing/create/mark_paid/cancel/mark_unpaid/batch + `_mark_invoice_paid_core` (shared с webhook'ом) |
| `api/payments.py` | `/payments`, `/invoices/{id}/checkout`, `/payments/webhook/{provider_name}` | 169 | создание провайдерского инвойса, webhook от провайдеров (HMAC, не require_admin), legacy ручная запись Payment |
| `api/cloud.py` | `/cloud/providers` | 148 | CRUD `CloudProvider` (шифрование секретов через `security._encrypt`) |
| `api/autoscale.py` | `/pools/{id}/autoscale`, `/autoscale/tick` | 98 | autoscale knobs + ручной дёрг тика |
| `api/__init__.py` | master `/api` | 66 | собирает всё через `include_router`, re-export `require_admin` (из `..auth`) и `_subscriptions_for_user` (из `.users`) |

Порядок `include_router` в `__init__.py` чисто для читаемости — FastAPI резолвит роуты по prefix/path, а не по порядку. Группировка примерно соответствует домену (health → plans → audit → tokens → nodes/tasks/subs/users → traffic/probes → invoices/payments → cloud/autoscale).

Большинство роутов сидят под `Depends(require_admin)`. Исключения:

- **Scoped endpoints** под `require_scope(...)` — всё, что касается probe/traffic: `/api/nodes/{id}/probes` (write), `/api/probes/targets` (read), `/api/nodes/{id}/traffic` (write). Эти вызываются с нод c помощью `X-Api-Token`.
- **Webhooks** `/api/payments/webhook/{provider_name}` — НЕ требуют admin-token, они авторизуются самим провайдером через HMAC-подпись. См. `components/payments.md`.
- **`/api/healthz`** — public (liveness/readiness).

Ключевая вспомогательная функция — `_mark_invoice_paid_core` (`api/invoices.py:61`). Именно её дёргают и admin, и webhook'и; она берёт `SELECT FOR UPDATE` на `Invoice`, проверяет статус, при `kind='topup'` идёт в `services.balance.topup()`, при `kind='subscription'` — в `ProvisioningOrchestrator`. Webhook из `api/payments.py` импортирует её напрямую (`from .invoices import _mark_invoice_paid_core`).

### Пагинация, фильтрация и поиск

Листинг-endpoint'ы (users, subscriptions, invoices, audit-logs, provisioning-tasks) используют Query-параметры `limit`/`offset` + опциональные фильтры. Единого API для сложных фильтров нет — каждый роут реализует свой набор Query-флагов.

### Audit logging

Внутри admin-роутов любое state-changing действие пишется через `_audit(db, actor, action, target_type, target_id, metadata=...)` (`api/_common.py:42-69`). `actor` приходит из `_resolve_admin_actor(actor_header)` — **заголовок `X-Admin-Actor` не проверяется криптографически**, это self-declared идентификатор для различения действий бота от действий человека. См. `api/_common.py:72-81`.

> ⚠️ Shared admin token + unchecked actor header = если компрометируется любой носитель токена, audit trail теряет доверие. См. audit/...

## `api_webapp.py` — Telegram Mini App

~1430 строк, ~20 роутов. Это отдельный security domain:

1. Пользователь открывает Mini App → клиентский JS получает от Telegram `window.Telegram.WebApp.initData` (signed query-string).
2. Frontend шлёт `POST /api/webapp/auth {init_data}` (`api_webapp.py:173`).
3. Backend верифицирует HMAC по стандартной схеме Telegram: `HMAC-SHA256(data_check_string, HMAC-SHA256("WebAppData", bot_token))` — `_verify_init_data()` в `api_webapp.py:52-104`.
4. `auth_date` проверяется жёстко (после fix'а #53, 2026-04): обязан быть, положительный integer, не в будущем (кроме ~60с clock-skew), не старше `WEBAPP_INIT_DATA_TTL_SECONDS` (дефолт **300**, было hardcoded 24h). Раньше `if auth_date and …` молча пропускал проверку при missing/zero — это дыра закрыта.
5. Если User не существует — создаётся автоматически (`api_webapp.py:195-204`).
6. Выдаётся **compact hand-rolled JWT**: `base64url(payload).base64url(hmac)`, где `payload = {"uid": id, "exp": now + TTL}`, TTL из `WEBAPP_JWT_TTL_SECONDS` (дефолт 1800 = 30 минут). Не PyJWT — только `hmac` из стандартной библиотеки (`issue_token`/`verify_token` в `api_webapp.py:107-140`).
7. Дальнейшие запросы несут `Authorization: Bearer <token>`, проходят через `require_webapp_user` (`api_webapp.py:142-156`), которая возвращает ORM-объект `User`.

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
    device = db.query(models.Device).filter_by(sub_token=token).first()
    if device:
        # per-device path + alias fallback
        ...
    sub = db.query(models.Subscription).filter_by(sub_token=token).first()
    # legacy per-subscription path
```

Два уровня lookup'а: сначала `Device.sub_token` (добавлен alembic `0022_device_sub_token`, выдаётся webapp'ом при покупке), затем legacy `Subscription.sub_token` для старых клиентов, установленных до миграции. Выдаёт base64-кодированный список URI активных credential'ов + заголовки `subscription-userinfo` (expire) и `profile-update-interval: 6`, которые Hiddify/v2rayNG читают для автообновления. Пишет `AuditLog(action='subscription_fetch')` на каждый опрос (`target_type='device'` или `'subscription'` в зависимости от пути).

**Alias-fallback для seamless migration.** Миграция (admin override, drain, auto-migrate-on-block) revoke'ает старый Device и создаёт новый с **другим** `sub_token`. Сохранённый в Hiddify URL указывал бы на revoked device и отдавал бы пустой список — юзер вынужден был бы копировать новый URL из webapp. Фикс: если найденный по токену device имеет `status != active` или у него нет активных credential'ов — ищем любой живой device на **той же** Subscription (берём самый свежий по `updated_at`, чтобы цепочка миграций A→B→C alias'илась на C) и отдаём его креды. В AuditLog extra пишется `aliased_to_device_id`, чтобы alias-путь был виден. Для multi-device подписок есть известное ограничение: `reprovision_subscription` на миграции создаёт один device, и все старые URL'ы alias'нутся на него (коллапс в единственного выжившего).

**Safety net.** Если ни прямой device, ни alias не дают ни одного работающего конфига — endpoint возвращает **503**, а не пустой 200. Hiddify/v2rayN на пустой 200 затирают локально закешированный профиль (пользователь остаётся без VPN), на 503 — оставляют last-known-good и повторяют запрос. Срабатывает в окне между revoke и provision, либо если вся подписка сломана (все устройства revoked/failed без работающего sibling).

### ⚠️ Sub-link invariant (НЕ ТРОГАТЬ)

Три инварианта живут в связке, ломать любой из них — значит каждая миграция заново пойдёт с рассылкой новых URI в боте (уже обжигались):

1. `backend/app/services/provisioning.py` `_handle_task_outcome(action="revoke")` **не удаляет** строку `Device`. Ставит `status=revoked`, деактивирует creds. `db.delete(device)` здесь — баг, который возвращает 404 всем сохранённым клиентам на подписке.
2. `reprovision_subscription` / `provision_subscription` / `create_device_for_subscription` создают **новый** `Device` с новым `sub_token`, **никогда** не мутируют `sub_token` уже существующего. Три генератора — три единственные точки записи `sub_token`.
3. `dynamic_sub_link` в `api_extensions.py` алиасит нерабочий device (статус ≠ active или все creds неактивны) на живого соседа по той же `Subscription`. Без этого — тот же 404.

Любая попытка «почистить старые revoked devices», «переиспользовать sub_token при миграции», «упростить alias-блок» — сначала читать этот раздел. Тесты, которые подтверждают инвариант, живут в `backend/tests/test_balance.py` (`original_token` фикстура).

> ⚠️ Audit-лог на каждый анонимный опрос + дефолтный nginx access_log (`infrastructure/deployment.md`) = деанон-timeline. См. audit/...

### Notification queue-через-AuditLog

Уведомления бота живут не в отдельной таблице, а в `audit_logs` со специальными `action` ('renewal_reminder', 'config_ready', 'migration_notice', 'low_balance_warning', 'trial_expiry_warning'). Бот опрашивает `/api/notifications/pending`, получает человекочитаемый текст (собранный бэкендом из `extra`-полей) и ack'ит через `/api/notifications/{id}/ack`, который просто дописывает `:delivered` в `log.action`. Детали — `api_extensions.py:359-458`.

## Модели доверия — сводно

```
                  X-Admin-Token (shared secret, env)
                      │
                      ├─► api/ (require_admin)              ← admin SPA, bot
                      ├─► api/ scoped endpoints              ← alt: X-Api-Token
                      └─► api_extensions.py (require_admin)  ← bot
                  
                  X-Api-Token (sha256 hashed, scoped)
                      │
                      └─► api/probes.py, api/traffic.py      ← probes, traffic collectors

                  Telegram initData → JWT
                      │
                      └─► api_webapp.py (require_webapp_user) ← browser in TG

                  HMAC signature in body/headers
                      │
                      └─► api/payments.py /payments/webhook/* ← payment providers

                  none
                      │
                      ├─► api/health.py /healthz
                      └─► api_extensions.py /sub/{token}
```

## Обработка ошибок

- FastAPI `HTTPException` — для бизнес-ошибок, превращается в `{"detail": "..."}`.
- Внутренние сбои логируются через `logger.exception`, наружу уходит generic 500. Исключения — провижининг: `_create_subscription_for_user` ловит любую ошибку, делает rollback, отдаёт 500 "Provisioning failed" (`api/_common.py:129-160`).
- Rate limit превышение — 429, обрабатывается SlowAPI middleware.

## Зависимости роутов от сервисов

```
api/            →  services.provisioning, services.payments,
                   services.autoscale, services.health,
                   services.node_spawner, services.balance,
                   services.cloud, services.warm_pool
api_webapp.py   →  services.balance, services.provisioning,
                   services.payments, services.trial
api_extensions.py → services.provisioning, services.trial
```

Все сервисы держат SQLAlchemy-сессию, которую им передаёт route через `Depends(get_db)`. Нет отдельного application-layer DI контейнера.

## ⚠️ Неясные места

- `_mark_invoice_paid_core` при уже `paid` инвойсе делает fallback на «latest subscription by (user_id, plan_id)» (`api/invoices.py:96-121`). Логика защиты от повторной доставки webhook'а, но может отдать пользователю данные **другой** подписки, если он успел купить второй инвойс на тот же план. Код-level ambiguity; см. audit/...
- `/api/healthz?deep=true` проверяет, что queue доступна, но не гоняет SELECT 1 на неё — `get_queue() is not None` верифицирует наличие Redis connection, но не его работоспособность (`api/health.py:21-30`).
