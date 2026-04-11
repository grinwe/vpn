# Env reference

Полный справочник по environment-переменным, которые реально читает код. Источник истины — `.env.example` + `grep os.getenv` по `backend/app/` и `bot/`. Если что-то здесь устарело — `.env.example` всегда правее.

Каждая переменная имеет колонку **кто читает**: `backend` (FastAPI), `worker` (RQ), `bot` (aiogram). Это важно, потому что часть переменных **намеренно зеркалится** между backend и worker (см. `infrastructure/deployment.md` → «Зеркалирование env»), и их нужно править в обоих сервисах одновременно.

## База данных

| переменная | default | кто читает | описание |
|---|---|---|---|
| `DATABASE_URL` | — | backend, worker | SQLAlchemy URL, обычно `postgresql://vpn:<pw>@db:5432/vpn`. Обязательно. |
| `POSTGRES_PASSWORD` | — | db (compose env) | Пароль Postgres. Роль `deploy_app_stack` падает, если `length < 12`. |
| `DB_POOL_SIZE` | `10` | backend, worker | SQLAlchemy pool size. Растить при `QueuePool limit reached`. |
| `DB_MAX_OVERFLOW` | `10` | backend, worker | Допкоэффициент pool overflow. |
| `DB_POOL_RECYCLE` | `1800` | backend, worker | Секунды, после которых соединение пересоздаётся (bypass stale-conn bug'ов за NAT'ом). |
| `SKIP_MIGRATIONS` | `0` | backend, worker | Если `1` — не запускать `alembic upgrade head` на старте. **В `docker-compose.yml` хардкод `"1"` для worker'а**, чтобы backend и worker не дрались за advisory lock (см. `docker-compose.yml:98-102`). |

## Security / Auth

| переменная | default | кто читает | описание |
|---|---|---|---|
| `APP_SECRET_KEY` | — | backend, worker | Fernet key для шифрования credentials at rest (`security.encrypt/decrypt`). **Ротация без re-encrypt-миграции = все зашифрованные поля нечитаемы.** Ансибл-роль требует `length >= 16`. |
| `ADMIN_API_TOKEN` | — | backend, worker, bot | Shared secret для `X-Admin-Token` header. Используется SPA, ботом и scrape'ом Prometheus. Роль требует `length >= 20`. |
| `ADMIN_ACTOR_HEADER` | `X-Admin-Actor` | backend | Имя header'а, откуда backend читает self-declared actor id для `AuditLog.actor`. Менять незачем. |
| `WEBAPP_JWT_SECRET` | — | backend | HMAC-ключ hand-rolled JWT сессии Telegram Mini App. Ротация = все открытые WebApp-сессии форсят re-handshake через initData. Роль требует `length >= 32`. |
| `WEBAPP_JWT_TTL_SECONDS` | `1800` | backend | Время жизни WebApp JWT. |
| `LOG_LEVEL` | `INFO` | backend, worker | DEBUG/INFO/WARNING. DEBUG очень шумный на prod. |
| `SLOWAPI_STORAGE_URI` | `memory://` | backend | Slowapi backend. `memory://` — per-process, ок для одного backend-инстанса. Для multi-replica → `redis://redis:6379/1`. |
| `CORS_ALLOWED_ORIGINS` | `""` | backend | Comma-separated list. Пусто → webapp на том же origin (same nginx), CORS не нужен. |

## Telegram Bot

| переменная | default | кто читает | описание |
|---|---|---|---|
| `BOT_TOKEN` | — | bot, backend, worker | Токен бота. backend/worker используют его для API-вызовов в TG (notifications, Stars invoice). |
| `TELEGRAM_BOT_TOKEN` | — | backend (fallback) | Legacy alias для `BOT_TOKEN`, читается только `services/payments/telegram_stars.py`, если `BOT_TOKEN` не задан. Обычно не нужен. |
| `ADMIN_IDS` | `""` | bot | Comma-separated Telegram user-id с админскими командами (`/invoices`, support-forwards). **Не** валидируется backend'ом — только бот фильтрует по ним. |
| `BOT_USERNAME` | `""` | backend, bot | Username бота (без `@`) для построения t.me/<bot>?start=... в referral flow. Без этого `/api/webapp/referral` возвращает `share_url=None`. |
| `BACKEND_URL` | `http://localhost:8000` | bot | Куда бот ходит за API. Внутри docker compose — `http://backend:8000`. |
| `NOTIFICATION_POLL_INTERVAL` | `10` | bot | Интервал (секунды) между опросами `/api/notifications/pending`. `<=0` — отключить поллер. |

## Redis / RQ

| переменная | default | кто читает | описание |
|---|---|---|---|
| `REDIS_PASSWORD` | — | redis (compose), backend, worker | Обязательно — без него Redis открыт на внутренней сети docker'а. RQ использует pickle, unauth redis = RCE. |
| `REDIS_URL` | — | backend, worker | Обычно `redis://:${REDIS_PASSWORD}@redis:6379/0`. |
| `QUEUE_BACKEND` | `""` | backend, worker | `"rq"` — использовать RQ очередь. Любое другое значение переключает провижининг в in-process thread'ы (только dev/test). |
| `RQ_QUEUE` | `vpn-provisioning` | backend, worker | Имя RQ queue. |
| `RQ_JOB_TIMEOUT` | `900` | worker | Hard-kill per job. Должен быть длиннее самого медленного playbook'а (bootstrap ~5 мин). Subprocess-таймаут ansible'а отдельный — см. `MAX_CONCURRENT_ANSIBLE`. |
| `RQ_FAILED_TTL` | `604800` | worker | Сколько держать job'ы в failed registry (7 дней). |
| `RQ_RESULT_TTL` | `86400` | worker | Сколько держать результаты успешных job'ов (1 день). |

## Ansible / provisioning

| переменная | default | кто читает | описание |
|---|---|---|---|
| `PROVISIONING_SSH_KEY` | (compose default — **literal public key string**) | compose, worker volume | Путь **на хосте** к приватнику для SSH на VPN-ноды. Дефолт в compose — literal строка-ключ, что ломает монтирование. См. `infrastructure/deployment.md` → PROVISIONING_SSH_KEY foot-gun. Должно быть `/opt/vpn/secrets/provisioning_key`. |
| `ANSIBLE_PRIVATE_KEY_FILE` | — | worker | Путь **внутри контейнера** к тому же ключу (обычно `/run/secrets/provisioning_key`, туда маппится volume). |
| `ANSIBLE_ROOT` | — | worker | Директория с `playbooks/` и `roles/`. По умолчанию `/app/infra/ansible`. |
| `MAX_CONCURRENT_ANSIBLE` | `3` | worker | Размер `_ansible_semaphore` в `ProvisioningOrchestrator`. Каждый процесс ansible ест ~200MB. Отдельный от warm-pool семафора. |
| `ALLOW_INPROCESS_PROVISIONING` | `""` | backend | Dev escape-hatch: `"1"` → backend выполняет ansible сам, без RQ. **Не** включать в prod — блокирует HTTP request'ы. |
| `MIN_HEALTHY_SCORE` | `50` | backend, worker | Минимальный `health_score` ноды для попадания в `choose_node` / `_eligible_nodes`. |

## Worker ticks

Все интервалы — в секундах. Каждая тика — self-rescheduling через `queue.enqueue_in`.

| переменная | default | кто читает | тик |
|---|---|---|---|
| `RENEWAL_CHECK_INTERVAL` | `300` | worker | `run_renewal_check` — находит expiring-подписки, шлёт reminder'ы, флипает expired → revoked после grace. |
| `RENEWAL_GRACE_HOURS` | `24` | worker | Сколько часов после `expires_at` подписка висит в `expired` до hard revoke'а. |
| `BALANCE_CHARGE_INTERVAL` | `3600` | worker | `charge_subscriptions` — hourly tick, burns daily_rate × devices из `prepaid_kopecks`. Плюс trial-expiry фаза. |
| `LOW_BALANCE_WARN_DAYS` | `3` | worker | Триггерит `low_balance_warning` notification, когда runway (balance / daily_rate) < этого. |
| `WARM_POOL_CHECK_INTERVAL` | `120` | worker | `run_warm_pool_check` — тик warmer'а (ensure_pool + revoke GC). |
| `AUTOSCALE_INTERVAL` | `0` / `300` | worker | `run_autoscale_check`. `0` — отключить. `.env.example` ставит `300`. |
| `DRAIN_TICK_INTERVAL` | `600` | worker | `run_drain_tick` (stage 5 downscale). Продолжает тикать даже если `AUTOSCALE_DOWNSCALE_ENABLED=0`, чтобы ручные draining-ноды могли дочиститься. |
| `DRAIN_MIGRATE_BATCH` | `10` | worker | Сколько подписок за один tick мигрируется с одной draining-ноды. |

## Balance billing / trial

| переменная | default | кто читает | описание |
|---|---|---|---|
| `MAX_FREEZE_DAYS_PER_PERIOD` | `14` | backend, worker | Hard cap на одно freeze-событие. |
| `FREEZE_YEAR_BUDGET_DAYS` | `30` | backend, worker | Годовой лимит freeze-дней на подписку. Сбрасывается при смене `frozen_year`. |
| `FREEZE_DAYS` | `7` | backend | Продление `expires_at` при активации freeze — deprecated, остался от stage 3. |
| `MIN_TOPUP_KOPECKS` | `10000` | backend, worker | Минимальная сумма пополнения (₽100). Дешевле — `/checkout` 400'ит. |
| `EXTRA_DEVICE_KOPECKS_PER_MONTH` | `10000` | backend, worker | Надбавка per device/month сверх `plan.max_devices`. |
| `REFERRAL_BONUS_KOPECKS` | `5000` | backend, worker | Бонус реферреру при первом `kind=topup` реферрала + бонус реферралу при активации trial. |
| `TRIAL_DURATION_DAYS` | `30` | backend | Длина trial-периода, используется при `POST /api/trial/activate`. |
| `TRIAL_EXPIRY_WARN_DAYS` | `3` | worker | Дни до trial-expire, когда шлётся `trial_expiry_warning`. |

## Autoscale (pool-based)

Мастер-switch — наличие `ServerPool.autoscale_enabled=True` + все `autoscale_*` колонки проставлены. Env даёт defaults, per-pool override — через колонки `ServerPool`.

| переменная | default | кто читает | описание |
|---|---|---|---|
| `AUTOSCALE_HIGH_WATERMARK` | `0.8` | worker | `utilization >= high` → spawn. Hysteresis с low. |
| `AUTOSCALE_LOW_WATERMARK` | `0.3` | worker | `utilization < low` → mark draining. |
| `AUTOSCALE_MAX_NODES` | `10` | worker | Ceiling для spawn'а. `error`-ноды не считаются в `counted_nodes` (иначе сломанная нода навсегда держала бы cap). |
| `AUTOSCALE_MIN_NODES` | `1` | worker | Floor — не шринкать ниже этого. |
| `AUTOSCALE_FALLBACK_CAPACITY` | `50` | worker | Предполагаемая capacity ноды, когда `max_users=NULL`. Используется autoscale math'ом. |
| `AUTOSCALE_SPAWN_BACKOFF` | `600` | worker | Сколько секунд парковать пул после fail'а spawn'а (in-memory, reset на рестарт). |
| `AUTOSCALE_DOWNSCALE_ENABLED` | `0` | backend, worker | Мастер-switch для downscale. По дефолту выключен; флипается после cycle наблюдения в проде. |
| `AUTOSCALE_DRAIN_GRACE_HOURS` | `24` | worker | Grace window между «0 live_subs на draining ноде» и `destroy_node`. Проверяется против `updated_at`, не mark-таймстампа. |

## VLESS Reality defaults

Используются `node_spawner.spawn_node` при создании Reality-конфига на свежеспавненной ноде. Per-node override — через admin SPA.

| переменная | default | кто читает | описание |
|---|---|---|---|
| `REALITY_SNI` | `www.microsoft.com` | backend, worker | «Borrowed» SNI в Reality handshake. Должен быть real TLS 1.3 host, **не** зацензуренный в target markete. |
| `REALITY_DEST` | `www.microsoft.com:443` | backend, worker | Куда Reality проксирует трафик не-VPN клиента (должен совпадать с SNI в 99% случаев). |
| `REALITY_PORT` | `443` | backend, worker | Порт inbound'а. Менять только если конфликт с другим сервисом на :443. |

## Платёжные провайдеры

| переменная | default | кто читает | описание |
|---|---|---|---|
| `PAYMENT_PROVIDER` | `cryptobot` | backend, bot | Single-provider mode (legacy). Если `PAYMENT_PROVIDERS` задан — игнорируется. |
| `PAYMENT_PROVIDERS` | `""` | backend, bot | Comma-separated список провайдеров. Backend при checkout'е выбирает `random.choice(list)`. |
| `CRYPTOBOT_TOKEN` | `""` | backend | Bearer token к CryptoBot API. HMAC webhook-сигнатура считается от `sha256(token)` как ключа. |
| `TELEGRAM_STARS_WEBHOOK_SECRET` | `""` | backend, bot | Shared secret, который бот добавляет в `X-Telegram-Stars-Secret` header при форварде `successful_payment` в backend. **Должен быть одинаковым** в двух сервисах. |
| `SBP_<SLUG>_HMAC_SECRET` | — | backend | HMAC secret для provider'а `sbp:<slug>`. Обязательно. |
| `SBP_<SLUG>_PAY_URL_TEMPLATE` | — | backend | `str.format` template URL с placeholder'ами `{invoice_id}`/`{amount}`. Альтернатива `_CREATE_URL`. |
| `SBP_<SLUG>_CREATE_URL` | — | backend | URL для POST `{invoice_id, amount, currency}` → возвращает pay URL. Альтернатива `_PAY_URL_TEMPLATE`. |
| `SBP_<SLUG>_DISPLAY_NAME` | `sbp:<slug>` | backend | То, что пишется в `Payment.provider`. |
| `SBP_<SLUG>_AUTH_HEADER` | — | backend | Дополнительный header для create-url запроса, формат `Name:Value`. |
| `SBP_<SLUG>_SIGNATURE_HEADER` | `x-sbp-signature` | backend | Имя header'а, в котором приходит HMAC signature webhook'а. |
| `SBP_<SLUG>_PAID_STATUSES` | — | backend | Comma-separated список значений `status` в payload'е, которые считаются успехом. |

**Слаг нормализуется в upper-case**: `sbp:sber` → переменные `SBP_SBER_*`.

## Telegram WebApp

| переменная | default | кто читает | описание |
|---|---|---|---|
| `WEBAPP_BASE_URL` | `""` | backend, bot | Public HTTPS URL для Mini App, напр. `https://grinwer.online/app`. Должен быть `https://` — иначе бот скрывает inline-кнопку. |
| `WEBAPP_STARS_PER_RUB` | `0.67` | backend | Конвертация ₽ → Stars для UX карточки тарифа. **Не** real exchange rate — просто калькулятор. При изменении price-карточки для Stars — проверять против real @PremiumBot pricing. |

## Dynamic subscription links

| переменная | default | кто читает | описание |
|---|---|---|---|
| `SUB_LINK_BASE_URL` | `""` | backend, worker, bot | Base URL sub-links вида `<base>/<sub_token>`. Пусто → sub links disabled, клиенты получают raw URIs. **Зеркалится** в backend и worker, потому что обе стороны пишут `Device.connection_uri`. Указывать на «boring» CDN-домен, не на основной grinwer.online — RKN-блокировка основного не убьёт installed-клиентов. |

## Warm credential pool

| переменная | default | кто читает | описание |
|---|---|---|---|
| `WARM_POOL_ENABLED` | `1` | backend, worker | Мастер-switch. `0`/`false`/`no` — `ensure_pool` возвращает `{}`, `try_assign_bundle` всегда `None`. Cold path работает. |
| `WARM_POOL_TARGET` | `10` | worker | Глубина пула на одну ноду. |
| `WARM_POOL_BATCH_PER_TICK` | `3` | worker | Максимум новых warm'ов за одну тику на одну ноду. Чтобы свежая нода не получила 10 последовательных ansible-runs. |
| `WARM_POOL_MAX_CONCURRENT` | `2` | worker | Размер `_warmer_semaphore`. Отдельный от `_ansible_semaphore` ProvisioningOrchestrator'а. Process-local. |
| `WARM_POOL_CHECK_INTERVAL` | `120` | worker | Тик warmer'а (см. секцию «Worker ticks»). |

## Кросс-сервисное зеркалирование — список

Переменные, которые **обязательно** должны быть одинаковыми во всех читающих сервисах. Если где-то поменяли — перезапускайте **все** соответствующие контейнеры.

| переменная | сервисы |
|---|---|
| `DATABASE_URL`, `APP_SECRET_KEY`, `ADMIN_API_TOKEN`, `REDIS_URL` | backend, worker |
| `BOT_TOKEN` | backend, worker, bot |
| `WARM_POOL_ENABLED` | backend, worker |
| `SUB_LINK_BASE_URL` | backend, worker, bot |
| `MAX_FREEZE_DAYS_PER_PERIOD`, `FREEZE_YEAR_BUDGET_DAYS` | backend, worker |
| `MIN_TOPUP_KOPECKS`, `EXTRA_DEVICE_KOPECKS_PER_MONTH`, `REFERRAL_BONUS_KOPECKS` | backend, worker |
| `PAYMENT_PROVIDER`, `PAYMENT_PROVIDERS`, `TELEGRAM_STARS_WEBHOOK_SECRET` | backend, bot |

## Обязательные vs опциональные

**Без этих деплой не поднимется** (pre-flight assert в `deploy_app_stack/tasks/main.yml:6-17`):

- `POSTGRES_PASSWORD` (≥12 символов)
- `ADMIN_API_TOKEN` (≥20)
- `APP_SECRET_KEY` (≥16)
- `BOT_TOKEN` (любая непустая строка)
- `ADMIN_IDS` (любая непустая строка)
- `WEBAPP_JWT_SECRET` (≥32)

Остальные — либо имеют sane default в коде, либо роль требует их только в специфических условиях (Cloudflare API token — только если `deploy_web_frontend_use_dns_challenge=true`).

## Изменение переменных в проде

Изменение `.env` **не применяется автоматически** к работающим контейнерам (см. `runbook.md` сценарий 11). Workflow:

```bash
# 1. Отредактировать
vim /opt/vpn/.env

# 2. Форсить recreate — compose не перечитывает .env сам по себе
docker compose up -d --force-recreate backend worker bot

# 3. Проверить, что значения доехали
docker compose exec backend env | grep WARM_POOL
docker compose exec worker env | grep WARM_POOL
```

Handler `recreate app stack` в `deploy_app_stack` делает это автоматически, если запуск роли изменил `.env` или SSH-ключ. Для ad-hoc — ручной `--force-recreate`.

## ⚠️ Неясные места

- **`RENEWAL_CHECK_INTERVAL` в `.env.example` = 300, в коде default = 3600.** `worker.py` имеет `os.getenv("RENEWAL_CHECK_INTERVAL", "3600")`, а `.env.example` ставит `300`. Разница в 12×. Непонятно, какое считается правильным.
- **`AUTOSCALE_INTERVAL` default = `0` в коде, `300` в `.env.example`.** `0` означает «отключить полностью». Чистый env без `.env.example` выключит autoscale — это может быть сюрпризом при dev-разворачивании.
- **`PROVISIONING_SSH_KEY` compose default — literal public key.** Уже упомянуто в `deployment.md`/`runbook.md`. Повторяем: `.env.example` ставит `/opt/vpn/secrets/provisioning_key`, но compose fallback на `ssh-ed25519 AAA...` — чистое compose-only окружение без `.env.example` получит broken mount.
- **`FREEZE_DAYS=7` в коде, но без использования в stage 4 логике.** Похоже, deprecated stage 3 наследие. Неясно, можно ли удалять.
- **Нет env для включения/выключения individual-провайдера.** Включение CryptoBot — только `PAYMENT_PROVIDER=cryptobot` или наличие в `PAYMENT_PROVIDERS` списке. Нет способа «оставить rotation, но временно выключить конкретно SBP» без редактирования списка.
- **`SBP_<SLUG>_*` не валидируются на старте backend'а.** Если `PAYMENT_PROVIDERS=sbp:foo,cryptobot`, но нет `SBP_FOO_HMAC_SECRET` — ошибка всплывёт только в момент первого `/checkout` c этим провайдером. Pre-flight check для SBP не зафиксирован.
- **`BOT_USERNAME` может быть пустым** — рефералки покажут бесшовный код вместо share-link. Warning'а backend не эмитит.
