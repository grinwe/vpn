# Модель данных

Всё описание ниже соответствует `backend/app/models.py`. Миграции живут в `backend/app/alembic/versions/`, последняя — `0024_node_suspect_since.py`. Схема — PostgreSQL, JSONB для гибких полей, enum-типы реализованы через `sqlalchemy.Enum` → postgres enum.

```
 users ─┬──◄── invoices ─┬──◄── payments
        │                 │
        └──◄── subscriptions ─┬──◄── devices ─┬──◄── credentials
        │                     │                     │
        └──◄── balance_       │                     └──► vpn_configs ──► vpn_nodes
                transactions  │                                              │
                              │                                              │
                              └────► plans  plan_serverpool ◄──► server_pools┘
                                                                   │
                                                                   └──► cloud_providers
                                                                          ▲
                                                                 vpn_nodes┘

 standalone:
   referral_codes (owner_id → users)
   health_probes (node_id → vpn_nodes)
   provisioning_tasks (polymorphic: target_type/target_id)
   audit_logs (polymorphic: target_type/target_id)
   api_tokens
```

Всего — 17 таблиц (16 моделей + одна ассоциативная `plan_serverpool`).

## Главное, что нужно знать с первого дня

- **Цены — всегда в копейках (`Integer`).** Исключения — `Plan.price` (`Numeric(10,2)` в рублях, legacy) и `Invoice.amount` / `Payment.amount` (тоже `Numeric(10,2)` в валюте инвойса). Поток `User.balance_kopecks → Subscription.prepaid_kopecks` живёт целиком в целых копейках, чтобы ежедневные списания не плавали. См. `backend/app/models.py:275,321`.
- **Провижининг-задачи полиморфные.** `ProvisioningTask.target_type` — строка (`"device"` / `"subscription"` / `"node"`), `target_id` — id в соответствующей таблице. FK нет, целостность — на уровне кода (`services/provisioning.py`).
- **Audit log тоже полиморфный**, по тому же принципу: `target_type` + `target_id`. FK нет.
- **Две таблицы без `updated_at`** — `audit_logs` (append-only) и `balance_transactions` (append-only ledger). Все остальные rich-модели имеют `onupdate=utcnow`.
- **`Credential.config_text` и `Device.connection_uri` шифруются** через Fernet-обёртку в `backend/app/security.py`. Префикс `enc:v1:` отличает зашифрованную запись от legacy plaintext.

## Ключевые таблицы

### `users`

Пользователь системы. Фактически 1:1 с Telegram user (`telegram_id`), `email` — опционально и почти не используется в текущем UX.

```python
# backend/app/models.py:262
class User(Base):
    id, telegram_id (unique, indexed), email,
    balance_kopecks: Integer NOT NULL DEFAULT 0,
    trial_activated_at: DateTime NULL,   # NULL = триал ещё доступен
    trial_expires_at: DateTime NULL,     # момент клоубэка бонуса
    referred_by_id: FK → users.id NULL,
    banned_at: DateTime NULL,            # user-level бан, ставится из админки
    created_at
```

Инварианты:
- `balance_kopecks` должен сходиться с `SUM(balance_transactions.amount_kopecks) WHERE user_id = x`. Это не enforced — BalanceTransaction — append-only ledger, column обновляется в коде. Комментарий в модели явно это фиксирует как «trust the column for reads, reconcile nightly» (`models.py:551-562`).
- `trial_activated_at IS NULL` ⇔ пользователь ещё может дёрнуть `/api/trial/activate`.
- `banned_at IS NOT NULL` ⇒ бот молча дропает все апдейты от этого Telegram-аккаунта (без ACK, чтобы не кормить DDoS-ботов обратной связью). Ортогонально `Subscription.status=blocked`: бан юзера не трогает его подписки, а `/users/{id}/disable` не ставит `banned_at`. Ставится `POST /api/users/{id}/ban`, снимается `/unban`.

### `plans`

Статичный каталог тарифов. Чистый reference-data.

```python
# backend/app/models.py:244
class Plan(Base):
    id, name (unique),
    duration_days, max_devices, price (Numeric(10,2)),
    traffic_limit_mb NULL, is_visible,
    daily_rate_kopecks: Integer NULL  # pay-as-you-go override
```

Поле `daily_rate_kopecks` — переопределение для balance billing: если задано, ежедневная tick списывает его вместо `price * 100 / duration_days`. См. `services/balance.py` (`charge_subscription`).

### `server_pools` + `plan_serverpool`

Группировка нод под тариф. Один тариф может быть доступен на нескольких пулах (m2m через `plan_serverpool`), один пул содержит много нод.

```python
# backend/app/models.py:150
class ServerPool(Base):
    id, name (unique),
    autoscale_enabled, autoscale_provider_id → cloud_providers,
    autoscale_region, autoscale_plan, autoscale_image,
    autoscale_high_watermark: Numeric(4,3),
    autoscale_max_nodes,
    autoscale_low_watermark: Numeric(4,3),   # stage 5 downscale
    autoscale_min_nodes,
    autoscale_fallback_provider_ids: JSONB    # stage 6 multi-cloud
```

`autoscale_*` читаются воркером в `run_autoscale_tick`. Если autoscale выключен — поля просто игнорируются.

### `vpn_nodes`

Физическая (или провайдерская) машина, где крутятся протоколы.

```python
# backend/app/models.py:183
class VPNNode(Base):
    id, name (unique), region, host, ssh_port,
    status: Enum(VPNNodeStatus),       # registering/active/disabled/error/draining
    is_active, pool_id → server_pools,
    max_users, max_bandwidth_mbps,     # capacity knobs
    health_score: Integer NULL,          # NULL = no probes yet
    last_health_check_at,
    blocked_regions: JSONB NULL,       # ['ru-mts', 'kz', ...]
    cooldown_until: DateTime NULL,     # reprovision backoff
    suspect_since: DateTime NULL,      # Phase D detector: active_users упал N→0
    relay_config: JSONB NULL,          # WG tunnel к иностранному exit'у
    provider_id → cloud_providers NULL,
    provider_external_id, provider_region, provider_plan,
    monthly_cost: Numeric(10,2) NULL
```

Жизненный цикл:
1. `registering` — провайдер создал машину, ansible ещё не прошёл.
2. `active` — провижининг завершён, ловит subs.
3. `draining` — идёт мягкий вывод (новые subs сюда не попадают, старые мигрируются воркером).
4. `disabled` — окончательно снят.
5. `error` — провижининг сломался, ручное вмешательство.

`cooldown_until` — anti-flap: если autoscale только что провижинил новую ноду и она сразу упала в health, не крутим повторный spawn в течение окна.

`suspect_since` — Phase D traffic-drop детектор (`services/traffic_stats.py::detect_traffic_drops`). Ставится моментом, когда `active_users` на ноде упал с ≥`TRAFFIC_DROP_MIN_USERS` до 0 между двумя traffic_stats тиками (пассивный ТСПУ-сигнал). На следующем тике — либо `status=error`/`cooldown_until=+3d`/`suspect_since=NULL` (подтверждение: трафик пошёл на мигрированных подписках на ноде другого региона), либо `suspect_since=NULL` (false-alarm). Не часть `VPNNodeStatus` enum'а — это промежуточное подозрение внутри детектора, не часть жизненного цикла.

### `node_user_bans`

Per-node бан юзера: ноды, на которые авто-выбор НЕ должен селить данного юзера. **Ортогонально** `users.banned_at` (тот — глобальный бан на уровне бота).

```python
# backend/app/models.py — class NodeUserBan
class NodeUserBan(Base):
    id,
    user_id → users (ON DELETE CASCADE, index),
    node_id → vpn_nodes (ON DELETE CASCADE, index),
    reason: Text NULL,
    created_by: String NULL,   # admin-actor / "auto" / telegram_id
    created_at
    # UniqueConstraint(user_id, node_id) = uq_node_user_ban
```

- Заполняется авто-миграцией: «Обновить подписку» (`POST /api/subscriptions/{id}/migrate-auto` → `ProvisioningOrchestrator.migrate_subscription_to_free_node`) выбирает свободный сервер пула, исключая текущую ноду **и** ноды из бан-листа юзера (через `choose_node(exclude_node_ids=...)`), мигрирует с сохранением `sub_token`, и **банит старую ноду** (`created_by=actor`, `reason="auto: …"`), чтобы повторное «обновление» не вернуло юзера обратно.
- Ручное управление: `GET /api/users/{id}/node-bans`, `POST /api/users/{id}/node-bans` (`{node_id, reason}`, идемпотентно по паре), `DELETE /api/users/{id}/node-bans/{node_id}` (разбан). В admin SPA (Users.tsx) — панель «Бан-лист нод» с разбаном + кнопка «🔄 обновить подписку» в строке активной подписки. Аудит: `node_user_banned` / `node_user_unbanned` / `subscription_migrated`.
- Бан per-**USER**, а не per-subscription: у юзера может быть несколько подписок, бан ноды распространяется на все. Миграция 0038.
- У авто-банов есть **TTL и потолок** (без колонки в схеме — on-access prune в `api/client_control.py`): перед каждым user-driven failover'ом протухшие авто-баны (`created_by` = `client_control` / `admin_panel` / `user:*`) старше `NODE_USER_BAN_TTL_HOURS` (48) удаляются, а при `NODE_USER_BAN_MAX_PER_USER` (3) свежих авто-банах миграция идёт без бана старой ноды — юзер тапами «VPN не работает» не выжигает себе пул нод навсегда. Ручные админ-баны prune не трогает.

### `vpn_configs`

Конфигурация одного протокола на одной ноде. Одна нода обычно имеет несколько `VPNConfig` — по одной на активный протокол (ShadowTLS, Reality, WS CDN, xHTTP, Hysteria2).

```python
# backend/app/models.py:223
class VPNConfig(Base):
    id, node_id → vpn_nodes (ON DELETE CASCADE),
    name, protocol: Enum(VPNConfigProtocol),
    port, sni, public_key, fallback,
    settings: JSONB,   # payload, зависящий от протокола
    is_enabled
```

Протокольные значения — в `models.py:64-69`.

### `subscriptions`

Центральная бизнес-сущность: «один пользователь купил один план на одной ноде». Один пользователь может иметь несколько одновременно активных подписок на разные планы.

```python
# backend/app/models.py:296
class Subscription(Base):
    id, user_id → users, plan_id → plans, node_id → vpn_nodes,
    status: Enum(SubscriptionStatus),  # active/blocked/expired/frozen
    expires_at: DateTime NOT NULL,
    notes,
    traffic_limit_mb, traffic_used_mb,
    auto_renew,
    sub_token: unique indexed String NULL,   # stable dynamic link
    # Stage 4 balance billing
    prepaid_kopecks: Integer NOT NULL DEFAULT 0,
    next_charge_at: DateTime NULL,     # NULL = legacy invoice sub
    # Freeze
    frozen_at, frozen_until,
    frozen_days_used, frozen_year,
    has_frozen_this_year: Boolean
```

Инварианты:
- `sub_token` **стабильный**: миграция/freeze/unfreeze его не меняет. Все пути в `services/provisioning.py` явно это сохраняют (комментарии `:1106`, `:1239`, `services/health.py:225`).
- `prepaid_kopecks >= 0` всегда. Списывается в `charge_subscription`, возвращается в `balance_kopecks` при ручном revoke. Expired subs теряют остаток (по конструкции ≈0).
- `next_charge_at IS NULL` означает «legacy invoice-модель» — этот sub не попадает в `run_balance_charge_tick`.
- `status=frozen` ⇔ `frozen_at IS NOT NULL`.

Freeze-бухгалтерия (два правила одновременно):
- `frozen_days_used` копит дни за календарный год; `frozen_year` — какой год. Переход на новый год сбрасывает `frozen_days_used` и выставляет `frozen_year`.
- `has_frozen_this_year` — V2 упрощение: «уже морозил в этом году». Используется для правила «одна заморозка в год». Поля сосуществуют.

### `devices`

«Одна конкретная установка клиента» под данной подпиской: iPhone, ноут, роутер. Имеет уникальный `access_username` — идентификатор, под которым пользователь известен демону протокола на ноде.

```python
# backend/app/models.py:375
class Device(Base):
    id, user_id → users (CASCADE), subscription_id → subscriptions (CASCADE),
    config_id → vpn_configs,
    name, status: Enum(DeviceStatus),   # pending/active/failed/disabled/revoked
    access_username,                    # 'warm-<node_id>-<hex>' или аналогичный
    connection_uri,                     # Fernet-encrypted
    sub_token (unique, indexed),        # per-device dynamic sub-link token
    last_seen_at
```

`sub_token` — уникальный токен на уровне устройства (не подписки). `/sub/{token}` сначала ищет `Device.sub_token` и возвращает только credentials этого устройства. Если не найден — fallback на `Subscription.sub_token` (backward compat для старых клиентов). Это предотвращает sharing: поделившись ссылкой, пользователь раскрывает только один device, а не всю подписку.

**Device.sub_token сохраняется после revoke** — строка Device с `status=revoked` остаётся в БД, и её токен продолжает резолвиться в endpoint через alias на живого соседа по Subscription. Это load-bearing для seamless-миграций; подробности — `components/backend-api.md` «Sub-link invariant». Запрещается чистить revoked-devices без апдейта alias-логики.

`connection_uri` — URL динамической подписки вида `<SUB_LINK_BASE_URL>/<device.sub_token>`. Шифруется Fernet.

### `credentials`

Один протокол × один device (или warm, без device). Денормализованная таблица, через которую ходят hot-path запросы warm pool.

```python
# backend/app/models.py:373
class Credential(Base):
    id,
    subscription_id NULL,   # NULL = warm
    device_id NULL,
    config_id → vpn_configs,
    node_id → vpn_nodes (денормализация!),
    proto,                  # 'shadowtls+shadowsocks', 'vless-reality', ...
    config_text,            # Fernet-encrypted URI или конфиг
    access_username (indexed),
    is_active, revoked_at,
    pool_state: Enum(CredentialPoolState),  # warm/assigned/revoked
    warmed_at, assigned_at,
    leg_published,          # идёт ли этот протокол в саб-линк (схема 4×1)
    leg_role,               # primary/fast/backup/reserve/dup; NULL = не опубликован
```

**`leg_published` ≠ `is_active`.** `is_active` означает «учётка жива на ноде» и
массово переставляется провижинингом; `leg_published` — «этот протокол отдан
человеку в подписку». Тёплый бандл назначается ЦЕЛИКОМ (на ноде под одним
именем лежат все её протоколы), а при схеме 4×1 публикуется ровно один — отсюда
главное свойство: сменить протокол на той же ноде стоит переставленного флага,
без ansible. Инвариант: `leg_role IS NOT NULL ⟺ leg_published`. Схема включается
`SUB_LEG_SCHEME=4x1`; при `legacy` фильтр не применяется вовсе.
См. `docs/operations/subset_epic_2026_07_29.md`.

Три критичных индекса для warm pool (`0008_warmpool_and_balance.py`):
- `ix_credentials_access_username` — группировка warm-пучков.
- `ix_credentials_node_id` — базовый.
- `ix_credentials_warm_node` (**partial**): `ON credentials(node_id) WHERE pool_state = 'warm'`. Это и есть ключ к тому, что `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` на warm pool работает суб-миллисекундно независимо от общего размера таблицы.

Инварианты:
- `node_id` здесь **денормализация** от `config_id.node_id` — `models.py:383-384`. Пересчёт при миграции обязан синхронизировать оба поля.
- Один warm-пучок = все строки с одним `access_username` на одном `node_id`, все в состоянии `warm`, не привязаны к subscription/device. Группируются как атомарная единица выдачи.

### `invoices` + `payments`

Invoice — то, что выставили пользователю. Payment — попытка оплаты. N:1 от payments к invoice.

```python
# backend/app/models.py:430
class Invoice(Base):
    id, user_id → users,
    plan_id NULL,              # NULL для kind='topup'
    subscription_id NULL,
    amount (Numeric(10,2)), currency,
    status: Enum(InvoiceStatus),        # pending/paid/failed
    action: Enum(InvoiceAction),        # new_subscription/renewal
    kind: String NOT NULL DEFAULT 'subscription'  # 'subscription' | 'topup'
```

`kind` — дискриминатор stage 4:
- `kind='subscription'` — классический flow, на paid создаётся/продлевается sub.
- `kind='topup'` — «пополнить баланс», на paid просто кредитуется `user.balance_kopecks`, никакого provisioning.

```python
# backend/app/models.py:412
class Payment(Base):
    id, subscription_id NULL, invoice_id → invoices (indexed) NULL,
    amount (Numeric(10,2)), currency,
    status: Enum(PaymentStatus),        # pending/paid/failed/refunded
    provider, external_id (NULL)
    UNIQUE(provider, external_id)         # #52 — NULLs excluded by Postgres
```

Идемпотентность webhook'ов держится на `UNIQUE(provider, external_id)` (#52) + status-flip идемпотентность в `_mark_invoice_paid_core`. Constraint предотвращает дупликаты из webhook-ретраев и double-click checkout'ов. `IntegrityError` обрабатывается в `api/payments.py` — checkout возвращает существующий pay_url, admin create → 409. См. `components/payments.md`.

### `balance_transactions`

Append-only ledger с подписанной суммой в копейках. Каждый top-up, каждая spend-tick, каждый refund/bonus/adjust — отдельная строка.

```python
# backend/app/models.py:551
class BalanceTransaction(Base):
    id, user_id → users (CASCADE, indexed),
    amount_kopecks: Integer NOT NULL,   # signed
    kind: Enum(BalanceTxKind),          # topup/spend/refund/bonus/adjust
    reference: String NULL,             # 'invoice:123', 'daily-billing-2025-12-01', ...
    note: Text,
    created_at (indexed)
```

Инвариант: `User.balance_kopecks == SUM(balance_transactions.amount_kopecks)`. Поддерживается в коде; column'у доверяют для чтения.

### `provisioning_tasks`

Rows — входящие queue-job'ы для воркера (через RQ). Полиморфные.

```python
# backend/app/models.py:456
class ProvisioningTask(Base):
    id, target_type, target_id,
    action, status: Enum(ProvisioningTaskStatus),
    payload: JSONB, result: JSONB, error_message,
    created_at, started_at, finished_at
```

`action` — свободная строка (`'provision_device'`, `'revoke_device'`, `'spawn_node'`, `'migrate_subscription'`, …). `services/provisioning.py::run_task` диспатчит по ней.

Нет FK на `subscriptions`/`devices`/`vpn_nodes` — целостность держится в коде. При восстановлении после рестарта `backend/app/main.py:27-64` перезапускает все `status=running` и `status=pending` задачи.

### `cloud_providers`

Конфиги доступа к hosting-провайдерам.

```python
# backend/app/models.py:472
class CloudProvider(Base):
    id, name (unique), kind: Enum(CloudProviderKind),   # hetzner/vultr/digitalocean/aeza/manual
    api_token_enc: Text (Fernet-encrypted),
    default_image, ssh_key_ids: JSONB, default_region, default_plan,
    is_active
```

`api_token_enc` расшифровывается в `services/cloud/base.py::get_driver` и никогда не логируется.

### `health_probes`

Точки телеметрии от probe-агентов с нод. Пишутся через `/api/health/probe` (scoped token `probe:write`).

```python
# backend/app/models.py:489
class HealthProbe(Base):
    id, node_id → vpn_nodes (CASCADE, indexed),
    source_region (indexed), source_kind,
    result: Enum(ProbeResult),  # ok/timeout/refused/tls_fail/unknown
    latency_ms,
    observed_at (indexed), details: JSONB
```

Агрегат используется health-скорером для bump/drop `VPNNode.health_score` и `blocked_regions`. Сырые строки хранятся ограниченное время (см. cleanup-тик в воркере, если есть — иначе ⚠️).

### `node_traffic_samples`

Периодические снимки xray stats-счётчиков ноды (Phase B → D). Пишутся `services.traffic_stats.collect_node_stats` раз в `TRAFFIC_STATS_INTERVAL` (default 300с).

```python
# backend/app/models.py:552
class NodeTrafficSample(Base):
    id, node_id → vpn_nodes (CASCADE, indexed),
    observed_at (default=utcnow),
    interval_seconds,                          # длина окна, фактический tick
    uplink_bytes, downlink_bytes: BigInteger,  # delta-счётчики (xray statsquery --reset)
    active_users: Integer,                     # count уникальных access_username
    details: JSONB                             # per-protocol breakdown (см. ниже)
```

Формат `details`:

```json
{
  "vless-reality": {
    "uplink": 12345,
    "downlink": 67890,
    "users": ["user-1-2", "user-4-7"],   // sorted access_username list (2026-04+)
    "user_count": 2                       // == len(users)
  },
  "shadowtls+shadowsocks": { "uplink": 0, "downlink": 0, "users": [], "user_count": 0 },
  "_errors": ["vless-xhttp"]              // протоколы, на которых collect упал
}
```

Legacy-rows (pre-2026-04) хранят `users` как bare `int` (count без списка) — читатели обязаны проверять `isinstance(v, list)` (`api/nodes.py::list_node_users`). Backfill не нужен: через один tick после деплоя все ноды пишут новый формат.

Фазы:
- **Phase B**: сбор time-series, `active_users` column, без автодействий.
- **Phase D**: `traffic_stats.detect_traffic_drops` читает `active_users` из двух последних sample'ов для ноды, срабатывает на drop ≥`TRAFFIC_DROP_MIN_USERS` → 0. Не читает `details`.

Админские потребители: `GET /api/nodes/{id}/users` (последний sample + джойн по `access_username`), `GET /api/nodes/{id}/traffic-history?hours=24` (sparkline).

### `api_tokens`

Scoped машинные токены.

```python
# backend/app/models.py:504
class ApiToken(Base):
    id, name (unique),
    token_hash (unique, indexed),    # SHA-256 hex
    scopes: ARRAY(String) NOT NULL,
    is_active, created_at, last_used_at
```

Plaintext токен показывается один раз при создании и больше нигде не хранится. Скоупы: `probe:read`, `probe:write`, `traffic:write` (`backend/app/auth.py:35-43`).

### `audit_logs`

Append-only журнал действий.

```python
# backend/app/models.py:516
class AuditLog(Base):
    id, actor, actor_type: Enum(AuditActor),   # user/admin/system
    action, target_type, target_id NULL,
    created_at, extra: JSONB (column name 'metadata')
```

**Важно**: column на диске называется `metadata` (чтобы не конфликтовать с SQLAlchemy reserved `Base.metadata`), в Python-модели — `extra`. См. `models.py:526`.

Особенность: тот же audit log используется как «очередь уведомлений боту» — воркер пишет строки с `action in ('renewal_reminder', 'config_ready', 'migration_notice', …)`, бот опрашивает их через `/api/notifications/pending` (`backend/app/api_extensions.py:359`) и помечает delivered добавлением `:delivered` в `action`.

Из-за этой hot-path роли на таблице объявлены индексы `ix_audit_logs_action_created_at (action, created_at)` (под поллер уведомлений и worker-тики, фильтрующие по `action`) и `ix_audit_logs_created_at (created_at)` (под дашборды/выборки по времени). DESC-вариант не нужен — btree читается в обе стороны.

**Health-ping actions** (источник данных для админ-дашборда `/health-pings`):

| `action` | Актор | Что в `extra` |
|---|---|---|
| `health_ping_request` | `system` | `telegram_id, subscription_id, node_id, node_name` — worker пишет на каждый плановый пинг |
| `health_ping_response` | `user` | `telegram_id, answer ("ok"\|"bad"), node_id, source ("prompted"\|"self_reported")` — ответ юзера |
| `health_ping_opt_out` | `user` | `telegram_id` — юзер нажал «не показывать» |

Поле `extra.source` различает, пришёл ли `bad`-ответ по плановому пингу (`prompted`) или юзер сам инициировал жалобу через self-report кнопку в боте/вебапе (`self_reported`). Legacy rows без этого поля трактуются как `prompted`.

### `referral_codes`

```python
# backend/app/models.py:529
class ReferralCode(Base):
    id, owner_id → users (CASCADE),
    code: String(32) unique indexed,
    bonus_days DEFAULT 3, reward_days DEFAULT 3,
    uses, max_uses NULL, is_active, created_at
```

Связка с `User.referred_by_id` → реферер. Правила начисления — в `api_extensions.py::register_user` и `_mark_invoice_paid_core` (см. `components/payments.md`).

## Индексы и критичные миграции

Основные не-PK индексы, имеющие значение для производительности:

| Индекс | Таблица | Поля | Зачем |
|---|---|---|---|
| `ix_credentials_warm_node` | credentials | partial `(node_id) WHERE pool_state = 'warm'` | hot path warm pool — `0008_warmpool_and_balance.py:60` |
| `ix_credentials_access_username` | credentials | access_username | группировка warm-пучков |
| `ix_credentials_node_id` | credentials | node_id | общая навигация |
| `ix_subscriptions_sub_token` | subscriptions | unique(sub_token) | lookup по динамической ссылке, `0006_protocols_referrals_subtoken.py:29` |
| `ix_balance_transactions_user_id` | balance_transactions | user_id | сверка баланса |
| `ix_balance_transactions_created_at` | balance_transactions | created_at | периодическая reconciliation |
| `ix_health_probes_*` | health_probes | `(node_id)`, `(source_region)`, `(observed_at)` | агрегация health |
| `ix_api_tokens_token_hash` | api_tokens | unique(token_hash) | быстрый lookup при auth |
| `uq_payments_provider_external_id` | payments | unique(provider, external_id) | #52 — dedup webhook retries / double-click checkout |

## Миграции

Начальный bootstrap — `0001_initial.py`. Дальше по номерам:

- `0002` — health + cloud provider tables.
- `0003` — autoscale поля на ServerPool.
- `0004` — nullable subscription_id на Payment.
- `0005` — `api_tokens`.
- `0006` — новые протоколы, `referral_codes`, `sub_token`, `is_visible`, `referred_by_id`.
- `0007` — data-only, seed базовых Plan'ов.
- `0008` — warm pool + balance column + `balance_transactions` + partial warm index. Критическая.
- `0009` — balance billing: `daily_rate_kopecks`, `next_charge_at`.
- `0010` — downscale поля (`low_watermark`, `min_nodes`).
- `0011` — `autoscale_fallback_provider_ids`.
- `0012` — trial bonus (`trial_activated_at`, `trial_expires_at`).
- `0013` — `Subscription.prepaid_kopecks`.
- `0014` — data-only, backfill `prepaid_kopecks` для существующих subs.
- `0015` — billing v2 (тонкая настройка balance flow; см. `docs/BILLING_V2.md`).
- `0016` — `vless-xhttp` в `VPNConfigProtocol` enum.
- `0017` — `0017_fix_enum_values.py` — правки enum-значений после багов в предыдущих.
- `0018` — `extra_device_slots`.
- `0019` — `node_traffic_samples` (Phase B).
- `0020` — `user_health_ping` (Phase C).
- `0021` — `UNIQUE(provider, external_id)` на payments (#52). Дедупликация перед наложением constraint.
- `0022` — `Device.sub_token` (per-device credential isolation). Backfill existing devices с уникальными токенами + unique index.
- `0023` — `user_notification_prefs`.
- `0024` — `VPNNode.suspect_since: DateTime NULL` для Phase D traffic-drop детектора.

Миграции запускаются только backend'ом на старте (`main.py:18` → `migrations.run_migrations()`). Воркер явно skip'ает миграции через `SKIP_MIGRATIONS=1` в `docker-compose.yml:103`, чтобы не гонять гонку на `alembic_version` advisory lock.

## ⚠️ Неясные места

- `HealthProbe`: есть ли периодический cleanup старых строк (retention)? В коде воркера не сразу видно такой тики, но таблица по смыслу должна расти быстро. Если cleanup'а нет — это отдельная тема.
- Связь `Payment.subscription_id` и `Payment.invoice_id`: оба nullable. Какой из них авторитетен для stage-4 балансного flow — из модели не видно, нужен переход в `services/balance.py` и `_mark_invoice_paid_core`.
- Поле `Subscription.traffic_used_mb` и `traffic_limit_mb` — кто его обновляет? Ноды пишут через `/api/nodes/{id}/traffic` (есть схема `NodeTrafficReport`), но не ясно, как агрегируются device-уровень в subscription-уровень — код агрегации смотрим в `api.py`, при необходимости отдельный проход.
- `has_frozen_this_year` vs `frozen_days_used` / `frozen_year`: три поля одновременно описывают freeze-историю, одно из них — V2 упрощение. Какое правило сейчас в силе — «один раз в год» или «до N дней в год» — из модели нельзя однозначно сказать.
