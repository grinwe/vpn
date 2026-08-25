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
1. SlowAPI rate limiter (`rate_limit.py`: кастомный `rate_limit_key`, default `300/minute; 60/second`, storage `SLOWAPI_STORAGE_URI` → `REDIS_URL` → `memory://`). Ключ — реальный клиентский IP: за доверенным прокси (private-сети докера либо `RATE_LIMIT_TRUSTED_PROXIES`) берётся `X-Real-IP` (fallback — последний элемент `X-Forwarded-For`, дописанный nginx), с недоверенных адресов заголовки игнорируются. Запросы с валидным `X-Admin-Token` (бот, админка) освобождены от per-IP лимитов (уникальный ключ на запрос), кроме `/api/agent/*` — там «потолок на источник» намеренный.
2. CORS — включается только если задан `CORS_ALLOWED_ORIGINS` (comma-separated).
3. Кастомный `add_metrics` — пишет `vpn_requests_total` / `vpn_requests_errors_total` в Prometheus, используя **matched route template** (`/api/users/{user_id}`), чтобы cardinality не взрывалась на каждом id. Необработанные исключения (реальные 500-краши) тоже считаются в оба счётчика с `status="500"` — краш перехватывается в `except` и пере-бросывается, финальный ответ строит `@app.exception_handler(Exception)` (`main.py`).

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
| `api/nodes.py` | `/nodes` + subpaths | 780 | CRUD нод, resync/bootstrap/diagnose/active toggle, **`PATCH /nodes/{id}/status`** (ручной override статуса active/error/disabled), **`PATCH /nodes/{id}`** (правка `name`/`region`/`pool_id`/`notes`; имя валидируется как inventory-хост + уникальность, переименование БЕЗ re-bootstrap — ansible коннектится по `ansible_host=host`), **`GET /pools`** (id+name пулов для дропдауна), **`DELETE /nodes/{id}`** (удаление записи; 409 `active_subs`/`live_vm`, `?force=true`) vs **`POST /nodes/{id}/destroy`** (снос VPS у хостера), `/configs` CRUD (+ **`PUT /nodes/{id}/configs/{cfg_id}`** — in-place редактирование без delete+create), **`GET /nodes/{id}/users`** (кто на ноде в последнем traffic-tick'е, с джойном на Device/Subscription/User/Plan), **`GET /nodes/{id}/traffic-history?hours=24`** (временной ряд `NodeTrafficSample` для sparkline-графика в админке), spawn/destroy через CloudProvider, миграция подписок, health |
| `api/tasks.py` | `/provisioning/tasks` | 276 | просмотр/retry/delete/batch `ProvisioningTask` + `_enrich_task_telegram` (батч-резолв `telegram_id` для ADMIN_UI) |
| `api/subscriptions.py` | `/subscriptions`, `/devices` | 309 | POST — создание subscription + провижининг; status/disable/enable/devices; revoke устройства; **`POST /subscriptions/{id}/migrate`** (per-sub admin override: переселить на выбранную ноду без проверок пула/health/cooldown, только `is_active=True`); **массовые операции по `{user_ids}` (cap 25/запрос — ansible-heavy, `_BULK_USERS_MAX`): `POST /subscriptions/bulk-regenerate-sublink`** (перегенерация sub-link каждому живому устройству, старое в `disabled` без revoke → UUID живёт на ноде, новый токен в ЛК, Telegram `sublink_rotated`; стоимость не меняется) и **`POST /subscriptions/bulk-migrate-auto`** (bulk `migrate-auto`: авто-выбор свободной ноды + бан старой, `sub_token` сохраняется) |
| `api/users.py` | `/users` | 540 | listing, by_telegram, balance, disable, **`/ban` + `/unban`** (user-level бан, ставит/снимает `User.banned_at`, ортогонально `Subscription.status=blocked` — бот-мидлварь молча дропает апдейты от забаненных, без ACK), **`POST /users/batch_ban`** (массовый ban/unban, audit — отдельная запись на юзера с `extra.batch=true`), **`GET /users/banned-telegram-ids`** (тонкий список для `BanGuard`-кэша в боте), `/topup` (admin топап в копейках). Хостит `_subscriptions_for_user`, который re-exported из `api/__init__.py` и используется `api_webapp.py` |
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

Внутри admin-роутов любое state-changing действие пишется через `_audit(db, actor, action, target_type, target_id, metadata=..., commit=True)`. По умолчанию `_audit` коммитит сам (обратная совместимость со ~100 call-site'ами); передайте `commit=False`, чтобы только застейджить строку и зафиксировать действие вместе с аудитом одной транзакцией (без окна «мутация есть, следа нет»). Так, например, работает `_get_or_create_user` — User и его `user_created`-строка пишутся атомарно, а гонка на уникальном `telegram_id` ловится через `IntegrityError` + перечитывание (409 только если строка так и не нашлась). `actor` приходит из `_resolve_admin_actor(actor_header)` — **заголовок `X-Admin-Actor` не проверяется криптографически**, это self-declared идентификатор для различения действий бота от действий человека.

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

Два уровня lookup'а: сначала `Device.sub_token` (добавлен alembic `0022_device_sub_token`, выдаётся webapp'ом при покупке), затем legacy `Subscription.sub_token` для старых клиентов, установленных до миграции. Выдаёт base64-кодированный список URI активных credential'ов + заголовки `subscription-userinfo` (expire) и `profile-update-interval` (часы, как часто клиент сам перечитывает сабу и через sibling-alias подхватывает новую ноду после failover/миграции). Захардкоженные 6ч означали окно устаревания конфига до полусуток после аварии/бана; дефолт снижен до **2ч** и вынесен в env `SUB_PROFILE_UPDATE_INTERVAL_H` (прод может ужать до 1ч — компромисс свежесть failover ↔ нагрузка read-пути). Ответ также несёт `cache-control: no-store, private` (+ `pragma: no-cache`): тело — персональный динамический конфиг, меняющийся при миграции/ротации токена, и кэшировать его CF-Worker'у/прокси нельзя (иначе seamless-alias обнулится закэшированным старым конфигом). Пишет `AuditLog(action='subscription_fetch')` на каждый опрос (`target_type='device'` или `'subscription'` в зависимости от пути); в горячем read-пути запись сэмплируется env-рубильником `SUB_FETCH_AUDIT_SAMPLE` (int, по умолчанию 1 = писать каждый фетч; N>1 — примерно 1 из N, чтобы `audit_logs` не рос неограниченно без retention-джобы).

**Фильтр нездоровых нод.** При сборке конфигов исключаются креды нод, которые не `is_active`, в активном `cooldown_until`, заглушены диагностикой (`diagnostics_disabled_at`/`auto_diagnose_disabled_at`) или имеют `health_score < MIN_HEALTHY_SCORE` (зеркалит фильтр `choose_node`) — иначе клиент держит заведомо мёртвый эндпоинт в ротации client-side failover. Fallback: если фильтр выкинул **все** креды (напр. единственная нода в cooldown), отдаётся нефильтрованный набор (живой-но-неоптимальный конфиг лучше 503). Kill-switch — env `SUB_FILTER_UNHEALTHY_NODES=0`.

**Фильтр эндпоинтов без split-tunnel (аварийный, по умолчанию ВЫКЛЮЧЕН).** На relay-ноде (есть хотя бы один `RelayExitLink`) «наружу» означает «через WG в зарубежный exit», и туда попадает только то, что процесс явно забиндил на интерфейс. Штатно это делает каждый из четырёх протоколов: три vless-флавора через `sockopt.interface`, hysteria2 — через `outbounds[].direct.bindDevice` + `acl.inline` (с 2026-07-28; до того он не биндил ничего и выпускал весь трафик с российского IP). Рубильник `SUB_FILTER_TUNNEL_BLIND=1` выкидывает из саб-конфига креды протоколов вне `_SPLIT_TUNNEL_PROTOS` (`_tunnel_blind_node_ids`) — нужен, когда ansible-раскатка где-то не прошла и слепые эндпоинты надо убрать из выдачи раньше, чем починится нода. Двухступенчатый fallback: health-фильтр снимается раньше этого (эндпоинт без VPN — не «неоптимально», а не тот продукт), но если туннелирующих легов не осталось вовсе, набор всё же отдаётся с `logger.error` — 503 навсегда оставил бы юзера без связи. `shadowtls+shadowsocks` в белый список не входит намеренно: роль отключена, split-tunnel там никто не делал.

**Статусы подписки.** `frozen` (временная user-initiated пауза, `sub_token` сохраняется) отдаёт **503 + `Retry-After`**, а не 403: многие клиенты на 403 чистят сохранённый профиль и перестают опрашивать ссылку, и после разморозки бесшовного авто-восстановления не происходит. `blocked`/`expired` — терминальные, остаются **403**.

**Alias-fallback для seamless migration.** Миграция (admin override, drain, auto-migrate-on-block) revoke'ает старый Device и создаёт новый с **другим** `sub_token`. Сохранённый в Hiddify URL указывал бы на revoked device и отдавал бы пустой список — юзер вынужден был бы копировать новый URL из webapp. Фикс: если найденный по токену device имеет `status != active` или у него нет активных credential'ов — ищем любой живой device на **той же** Subscription (берём самый свежий по `updated_at`, чтобы цепочка миграций A→B→C alias'илась на C) и отдаём его креды. В AuditLog extra пишется `aliased_to_device_id`, чтобы alias-путь был виден. Для multi-device подписок есть известное ограничение: `reprovision_subscription` на миграции создаёт один device, и все старые URL'ы alias'нутся на него (коллапс в единственного выжившего).

**Safety net.** Если ни прямой device, ни alias не дают ни одного работающего конфига — endpoint возвращает **503** с заголовком `Retry-After` (сек, env `SUB_RETRY_AFTER_SEC`, дефолт 60), а не пустой 200. Hiddify/v2rayN на пустой 200 затирают локально закешированный профиль (пользователь остаётся без VPN), на 503 — оставляют last-known-good и повторяют запрос; `Retry-After` даёт машиночитаемый хинт перезапросить сразу после провижининга, а не ждать плановый `profile-update-interval`. Срабатывает в окне между revoke и provision, либо если вся подписка сломана (все устройства revoked/failed без работающего sibling). Недешифруемый credential (пустой `_decrypt` — рассинхрон `APP_SECRET_KEY`, битый `config_text`) в **обеих** ветках логируется `logger.warning` с контекстом (cred/proto/node/device/sub), чтобы частичная деградация подписки была видна в логах.

### Формат тела — плоский список или Xray-JSON

По умолчанию тело одно на всех: строки-директивы Happ + список URI, склеенные `\n` и завёрнутые в base64 (`_sub_body`). Клиентам, которые умеют читать полноценный конфиг ядра (**Happ** и **v2rayTun**), можно вместо списка отдавать **Xray-JSON** — массив профилей с балансировщиком: `backend/app/services/xray_client_config.py`, врезка — `_sub_payload` рядом с `_sub_body`.

Зачем: плоский список не даёт фейловера **внутри** сессии — пока человек сам не переподключится, он сидит на упавшей ноде. Заголовок `subscription-autoconnect-type: lowestdelay` выбирает лучший лег только в момент подключения. В JSON-варианте ядро само пингует каждый лег (`observatory`, проба раз в 120 с) и уводит трафик на самый быстрый живой (`balancers` + `leastPing` + `fallbackTag`).

Состав выдачи: первым идёт профиль **«🎯 Автовыбор»** (весь набор легов в одном балансировщике), затем по профилю на каждый лег — чтобы ручной выбор конкретного сервера остался как был. Селектор балансировщика матчит по **префиксу**, поэтому один `main` накрывает `main`, `main-2`, `main-3`, …

| Гейт | Значение |
|---|---|
| `SUB_XRAY_JSON` | `off` (дефолт) / `allowlist` / `on` |
| `SUB_XRAY_JSON_TOKENS` | CSV саб-токенов для режима `allowlist` |
| `SUB_XRAY_JSON_CLIENTS` | кому из умеющих отдаём (`happ`, `v2raytun`); пусто = всем умеющим |
| `SUB_XRAY_PROBE_INTERVAL_S` | период пинг-пробы, сек (дефолт 300, пол 30) |

`SUB_XRAY_JSON_CLIENTS` отделён от режима намеренно: «формат клиент понимает» и «мы проверили на живом устройстве» — разные утверждения. На раскатке 2026-08-25 оба клиента подтверждены на живых устройствах, поэтому в проде рычаг пуст; он ждёт следующего клиента, которого добавят в список умеющих раньше, чем кто-нибудь его пощупает.

Что важно при чтении кода:

- **Hysteria2 участвует наравне с vless.** Xray 26.x умеет hy2 нативно (`proxy/hysteria`), а `obfs=salamander` выражается через `streamSettings.finalmask.udp` — маска UDP-пакетов поверх транспорта, отдельная от настроек протокола. Балансировщик пингует hy2-леги как остальные: проверено вживую — при мёртвых vless трафик уходит на hysteria. Если hy2 когда-нибудь снова начнёт выпадать из выдачи, ищите не в протоколе, а в `finalmask`.
- **Меньше двух легов → падаем на плоский список** (`MIN_BALANCER_LEGS`): автовыбор из одного элемента даёт тот же сервер, но без запасного.
- **Клиентских routing-правил не передаём**, кроме приватных подсетей → `direct`. RU-split живёт на ноде (xray `routing` + hysteria `acl`), и дублировать его на клиенте значило бы держать два расходящихся источника правды. Секции `dns` нет намеренно: при `domainStrategy: "AsIs"` домен резолвит нода — ровно как с плоскими ссылками.
- Заголовки ответа **не меняются**: `_sub_response_headers` работает одинаково для обеих веток, меняется только `media_type`. Диагностический `?fmt=plain` формат не переключает.

Проверено на Xray 26.3.27 (версия флота): конфиг валиден, reality/xhttp/ws-cdn подключаются, при мёртвом `main` трафик сам уходит на `main-2`.

### Заголовки подписки — по клиенту, а не одним набором

Клиенты называют себя в `User-Agent` (`Happ/2.4.1`, `v2rayTun/5.24.76`,
`HiddifyNext/4.1.1 … like ClashMeta v2ray sing-box`), и набор украшений у них
разный. Классификатор — `_client_kind()` в `api_extensions.py`, единственное
место с UA-регулярками.

| Слот | Happ | v2rayTun | Hiddify | unknown |
|---|---|---|---|---|
| `profile-title`, `profile-update-interval`, `subscription-userinfo`, `fallback-url` | ✅ | ✅ | ✅ | ✅ |
| `providerid`, `sub-info-*`, `sub-expire*`, `notification-subs-expire` | ✅ | — | — | ✅ |
| `announce` без цвета | ✅ (auto: молчит при providerid) | — | — | ✅ |
| `announce` с `#RRGGBB` + `announce-url` | — | ✅ | — | — |
| `profile-web-page-url` на страницу починки | — | — | ✅ | — |

Почему ветвление обязательно: имя `announce` у Happ и v2rayTun **общее, а
синтаксис разный** — цветовые коды `#RRGGBB`, которые v2rayTun красит, Happ
показал бы буквально («#e05252Не подключается?»). Второй конфликт — `routing`
(у v2rayTun это base64 их собственного экспорта, у Happ `happ://routing/…`);
мы его не шлём, но помнить надо.

`unknown` (пустой UA, curl, экзотика) получает набор Happ — то есть
поведение для неопознанного клиента ровно то, что было до ветвления.

Hiddify беден по дизайну: текстовых блоков и объявлений у него нет вовсе,
поэтому вход на страницу отдаётся единственным доступным способом — ссылкой
в меню профиля. Подробности про v2rayTun — `operations/env-reference.md` и
ресёрч в памяти проекта.

### Страница починки `/api/sub/{token}?fix=1`

Тот же путь, что выдача конфигов, но для БРАУЗЕРА: `Accept: text/html` →
HTML-страница «починить и продлить», иначе (VPN-клиенты) — обычный base64.
Существует потому, что домен саб-ссылки доступен без VPN, а Telegram — нет.

* GET `?fix=1` — экран, ничего не пишет в БД (ветвление стоит ДО аудита,
  отметки первой выдачи и проверки «подписка обслуживаема»).
* POST `?fix=1&n=<nonce>` — шаг лестницы ротации через
  `services/self_repair.py`. POST, а не GET, чтобы префетчеры браузера не
  жгли конечный шаг лестницы; nonce = CSRF (HMAC от `APP_SECRET_KEY`,
  окно 15 мин); лимит `2/minute;6/hour` по ТОКЕНУ (не по IP — CGNAT).
* POST `…&pay=1` — счёт на продление (провайдер пинится `SUB_FIX_PROVIDER`)
  и 303 на оплату; возврат — на `?fix=1&paid=<invoice_id>`.
* Неизвестный токен → тот же camo-лендинг, что на корне домена, с тем же
  кодом 200: иначе код ответа сам становится оракулом для пробера.

Право на починку проверяет ЯДРО (`handle_broken_device`), а не экран:
POST приходит по URL, и защита в слое отображения его не покрывает.

Флаги: `SUB_FIX_PAGE`, `SUB_FIX_PAY`, `SUB_FIX_ENTRYPOINTS`,
`SUB_FIX_THROTTLE_SEC`, `SUB_FIX_DAILY_MAX`, `SUB_FIX_PROVIDER` — см.
`operations/env-reference.md` и эпик `operations/sub_fix_epic_2026_07_29.md`.

### ⚠️ Sub-link invariant (НЕ ТРОГАТЬ)

Три инварианта живут в связке, ломать любой из них — значит каждая миграция заново пойдёт с рассылкой новых URI в боте (уже обжигались):

1. `backend/app/services/provisioning.py` `_handle_task_outcome(action="revoke")` **не удаляет** строку `Device`. Ставит `status=revoked`, деактивирует creds. `db.delete(device)` здесь — баг, который возвращает 404 всем сохранённым клиентам на подписке.
2. `reprovision_subscription` / `provision_subscription` / `create_device_for_subscription` создают **новый** `Device` с новым `sub_token`, **никогда** не мутируют `sub_token` уже существующего. Три генератора — три единственные точки записи `sub_token`.
3. `dynamic_sub_link` в `api_extensions.py` алиасит нерабочий device (статус ≠ active или все creds неактивны) на живого соседа по той же `Subscription`. Без этого — тот же 404.

Любая попытка «почистить старые revoked devices», «переиспользовать sub_token при миграции», «упростить alias-блок» — сначала читать этот раздел. Тесты, которые подтверждают инвариант, живут в `backend/tests/test_balance.py` (`original_token` фикстура) и `backend/tests/test_auditfix_api_extensions_py.py` (per-device ветка целиком: happy-path по токену девайса, alias отозванного на живого соседа + `aliased_to_device_id` в AuditLog, выбор самого свежего соседа в цепочке A→B→C, 503 вместо пустого 200).

> ⚠️ Audit-лог на каждый анонимный опрос + дефолтный nginx access_log (`infrastructure/deployment.md`) = деанон-timeline. См. audit/...

### Notification queue-через-AuditLog

Уведомления бота живут не в отдельной таблице, а в `audit_logs` со специальными `action` ('renewal_reminder', 'config_ready', 'migration_notice', 'low_balance_warning', 'trial_expiry_warning'). Бот опрашивает `/api/notifications/pending`, получает человекочитаемый текст (собранный бэкендом из `extra`-полей) и ack'ит через `/api/notifications/{id}/ack`, который просто дописывает `:delivered` в `log.action`. Детали — `api_extensions.py:359-458`.

**Приоритет + FIFO на выборке (сетевой аудит).** `get_pending_notifications` делит `action` на два класса и выбирает **priority-строки первыми** (всё, кроме `admin_broadcast`), добивая свободные слоты `limit` массовой рассылкой. Внутри выборка — `order_by(created_at.asc())` (FIFO), а не прежний `.desc()` (LIFO). Раньше диспетчер рассылки наполнял очередь батчами по 50/тик, поллер сливал 20/тик, и из-за DESC-сортировки более свежие `admin_broadcast` вытесняли срочные транзакционные пуши (`config_ready`, `expiry_reminder_1d`, `migration_notice`, `health_ping_request`) в хвост на десятки минут. Теперь рассылка не может вытеснить срочный пуш из окна доставки. Это серверная страховка в дополнение к within-tick сортировке на стороне бота (см. `docs/components/bot.md`).

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
- Внутренние сбои логируются через `logger.exception` в `@app.exception_handler(Exception)`; наружу уходит generic 500 с `request_id` в теле и заголовке `X-Request-ID`, по которому краш находится в логах. Исключения — провижининг: `_create_subscription_for_user` ловит любую ошибку, делает rollback, отдаёт 500 "Provisioning failed" (`api/_common.py:129-160`).
- Rate limit превышение — 429, обрабатывается SlowAPI middleware.
- Cold-path throttle — 503 с `Retry-After` header. Вызывается, когда окно `COLD_PROVISION_MAX_PER_WINDOW / COLD_PROVISION_WINDOW_SECONDS` исчерпано на cold branch `provision_subscription` (warm-pool промах). Migrations и `reprovision_subscription` лимит **не** трогают — только user-initiated активации. Перехватывается централизованно в `main.py` (`@app.exception_handler(ColdPathThrottled)`). Реализация — `services/provisioning_throttle.py`. Защита, придуманная после инцидента 2026-04-15 с bot-флудом.

## Защита от бот-флуда

Два независимых слоя:

1. **Per-IP rate-limit на hot endpoint'ы входа** — `POST /api/users/register` и `POST /api/trial/activate` декорированы `@limiter.limit("10/minute")`. Общий дефолт SlowAPI (`300/minute; 60/second`) остаётся для остального. Бот ходит в бэк через один хост, так что per-IP фактически = per-bot-backend; не идеально, но достаточно на практике.
2. **Global cold-path throttle на провижининг** — см. выше. Защита именно от цепочки ansible-ранов, а не от запросов. Миграции и reprovision бесплатны.

Incident 2026-04-15: за 2 минуты в бот зашло ~250 ботов, все промахнулись мимо warm-pool, цепочка cold-path активаций положила одну xray-ноду. Оба слоя вместе гасят эту комбинацию: register/trial зарежутся на rate-limit, а те, что просочились — наткнутся на `ColdPathThrottled`.

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
