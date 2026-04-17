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
| `WEBAPP_JWT_TTL_SECONDS` | `1800` | backend | Время жизни WebApp сессионного JWT. |
| `WEBAPP_INIT_DATA_TTL_SECONDS` | `300` | backend | TTL для Telegram `initData` при обмене на сессионный JWT через `POST /api/webapp/auth`. Короткий handshake — бьёт replay-окно, если initData утечёт. Не путать с `WEBAPP_JWT_TTL_SECONDS` (время жизни сессионного токена). Добавлено после fix'а #53 (2026-04), до этого TTL был хардкод 86400с и ещё пропускался при `auth_date=0`. |
| `LOG_LEVEL` | `INFO` | backend, worker | DEBUG/INFO/WARNING. DEBUG очень шумный на prod. |
| `LOG_FORMAT` | `json` | backend, worker | `json` — structured JSON (для prod/log aggregators). `console` — human-readable (для local dev). Оба включают `request_id`. |
| `SLOWAPI_STORAGE_URI` | auto (`REDIS_URL` → `memory://`) | backend | Slowapi backend. Если не задан, подхватывает `REDIS_URL` (rate limits shared через Redis). Явное `memory://` — только для offline dev. |
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
| `PROVISIONING_SSH_KEY` | **обязателен** | compose, worker volume | Путь **на хосте** к ed25519 приватнику для SSH на VPN-ноды. compose откажется стартовать без неё (`?:` required). Обычно `/opt/vpn/secrets/provisioning_key`. |
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
| `PENDING_RESCUE_INTERVAL` | `60` | worker | `run_pending_rescue_tick` — re-enqueue `ProvisioningTask.status=pending` старше `PENDING_RESCUE_AGE`. Закрывает дыру, когда `enqueue_task` упал на Redis-hiccup'е и строка осталась без job'а. `0` отключает. |
| `PENDING_RESCUE_AGE` | `60` | worker | Минимальный возраст (sec) pending-задачи, чтобы её подхватил rescue-tick. Меньше этого — считается «только что создана, ещё не RQ'нулась». |
| `ANSIBLE_PLAYBOOK_TIMEOUT` | `300` | backend, worker | subprocess-таймаут (sec) на один `ansible-playbook` run. При регулярно-медленных нодах (package installs, slow SSH) можно поднять, иначе revoke/apply ловят `TimeoutExpired` и таска становится failed. |
| `TRAFFIC_STATS_INTERVAL` | `300` | worker | `run_traffic_stats_tick` — SSH-сбор xray stats + sharing violations. Phase D `detect_traffic_drops` **отключён 2026-04-15** — теперь только сбор samples. |
| ~~`TRAFFIC_DROP_ENABLED`~~ | ~~`1`~~ | worker | **Inert с 2026-04-15.** Phase D детектор отключён на уровне кода (`worker.run_traffic_stats_tick` не вызывает `detect_traffic_drops`, функция стоит no-op). Переменная оставлена для обратной совместимости env, но не читается. |
| ~~`TRAFFIC_DROP_MIN_USERS`~~ | ~~`5`~~ | worker | **Inert с 2026-04-15.** При возврате автомиграции пороги нужно пересмотреть — прежние значения ложно-триггерили миграции в idle-окнах. |
| ~~`TRAFFIC_DROP_CONFIRM_TICKS`~~ | ~~`1`~~ | worker | **Inert с 2026-04-15.** См. `TRAFFIC_DROP_MIN_USERS`. |

## Balance billing / trial

| переменная | default | кто читает | описание |
|---|---|---|---|
| `FREEZE_DAYS` | `14` | backend, worker | Сколько дней длится одна заморозка (1 раз в календарный год). `expires_at += FREEZE_DAYS` при freeze. |
| `MIN_TOPUP_KOPECKS` | `10000` | backend, worker | Минимальная сумма пополнения (₽100). Дешевле — `/checkout` 400'ит. |
| `EXTRA_DEVICE_KOPECKS_PER_MONTH` | `10000` | backend, worker | Надбавка per device/month сверх `plan.max_devices`. |
| `REFERRAL_BONUS_KOPECKS` | `5000` | backend, worker | Бонус реферреру при первом `kind=topup` реферрала + бонус реферралу при активации trial. |
| `TRIAL_DURATION_DAYS` | `30` | backend | Длина trial-периода, используется при `POST /api/trial/activate`. |
| `TRIAL_EXPIRY_WARN_DAYS` | `3` | worker | Дни до trial-expire, когда шлётся `trial_expiry_warning`. |

## Autoscale (pool-based)

Мастер-switch — наличие `ServerPool.autoscale_enabled=True` + все `autoscale_*` колонки проставлены. Env даёт defaults, per-pool override — через колонки `ServerPool`.

| переменная | default | кто читает | описание |
|---|---|---|---|
| `AUTOSCALE_HIGH_WATERMARK` | `0.8` | worker | `utilization >= high` → spawn. |
| `AUTOSCALE_MAX_NODES` | `10` | worker | Ceiling для spawn'а. `error`-ноды не считаются в `counted_nodes` (иначе сломанная нода навсегда держала бы cap). |
| `AUTOSCALE_FALLBACK_CAPACITY` | `50` | worker | Предполагаемая capacity ноды, когда `max_users=NULL`. Используется autoscale math'ом. |
| `AUTOSCALE_SPAWN_BACKOFF` | `600` | worker | Сколько секунд парковать пул после fail'а spawn'а (in-memory, reset на рестарт). |

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
| `TELEGRAM_STARS_WEBHOOK_SECRET` | `""` | backend, bot | **Deprecated** (#62). Shared secret для legacy polling-режима. Заменён на `TELEGRAM_WEBHOOK_SECRET_TOKEN`. |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | `""` | backend | Secret для native Telegram webhook (`setWebhook`). Backend проверяет `X-Telegram-Bot-Api-Secret-Token` header на каждом update. |
| `TELEGRAM_WEBHOOK_URL` | `""` | backend | Публичный URL для `/tg-webhook` (напр. `https://grinwer.online/tg-webhook`). Если пусто — webhook не регистрируется. |
| `BOT_INTERNAL_WEBHOOK_URL` | `""` | backend | Внутренний URL бота для forward не-payment update'ов (напр. `http://bot:8081/webhook`). |
| `BOT_WEBHOOK_PORT` | `0` | bot | Порт для aiohttp webhook-сервера. `> 0` — webhook-режим, `0` — legacy polling. |
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

## Cold-path provisioning throttle

Sliding-window лимит на `ProvisioningOrchestrator.provision_subscription` в cold branch (промах warm-pool). Migrations и `reprovision_subscription` бесплатны. Добавлено после инцидента 2026-04-15: ~250 ботов зашли в бота за 2 минуты и уронили xray-ноду цепочкой ansible-апплаев. Превышение лимита → HTTP 503 + `Retry-After` → бот/webapp показывают «подожди N секунд». Окно на каждую реплику бэкенда — per-process, не кластерное.

| переменная | default | кто читает | описание |
|---|---|---|---|
| `COLD_PROVISION_MAX_PER_WINDOW` | `5` | backend | Максимум cold-path активаций в окне. Повышай вместе с `WARM_POOL_TARGET`, чтобы органический трафик шёл через быстрый путь и не упирался. |
| `COLD_PROVISION_WINDOW_SECONDS` | `60` | backend | Размер окна в секундах. |

## Кросс-сервисное зеркалирование — список

Переменные, которые **обязательно** должны быть одинаковыми во всех читающих сервисах. Если где-то поменяли — перезапускайте **все** соответствующие контейнеры.

| переменная | сервисы |
|---|---|
| `DATABASE_URL`, `APP_SECRET_KEY`, `ADMIN_API_TOKEN`, `REDIS_URL` | backend, worker |
| `BOT_TOKEN` | backend, worker, bot |
| `WARM_POOL_ENABLED` | backend, worker |
| `SUB_LINK_BASE_URL` | backend, worker, bot |
| `FREEZE_DAYS` | backend, worker |
| `MIN_TOPUP_KOPECKS`, `EXTRA_DEVICE_KOPECKS_PER_MONTH`, `REFERRAL_BONUS_KOPECKS` | backend, worker |
| `PAYMENT_PROVIDER`, `PAYMENT_PROVIDERS` | backend, bot |
| `TELEGRAM_STARS_WEBHOOK_SECRET` | backend, bot (только в legacy polling-режиме; в webhook-режиме не нужна) |

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
- ✅ **`PROVISIONING_SSH_KEY` теперь обязателен.** Compose использует `${PROVISIONING_SSH_KEY:?...}` — без переменной `docker-compose up` выдаст ошибку, а не сломанный mount.
- ✅ **`FREEZE_DAYS` — единственный freeze-tunable.** `MAX_FREEZE_DAYS_PER_PERIOD` и `FREEZE_YEAR_BUDGET_DAYS` убраны (были dead code). `FREEZE_DAYS=14` по умолчанию, 1 раз в год.
- **Нет env для включения/выключения individual-провайдера.** Включение CryptoBot — только `PAYMENT_PROVIDER=cryptobot` или наличие в `PAYMENT_PROVIDERS` списке. Нет способа «оставить rotation, но временно выключить конкретно SBP» без редактирования списка.
- **`SBP_<SLUG>_*` не валидируются на старте backend'а.** Если `PAYMENT_PROVIDERS=sbp:foo,cryptobot`, но нет `SBP_FOO_HMAC_SECRET` — ошибка всплывёт только в момент первого `/checkout` c этим провайдером. Pre-flight check для SBP не зафиксирован.
- **`BOT_USERNAME` может быть пустым** — рефералки покажут бесшовный код вместо share-link. Warning'а backend не эмитит.
