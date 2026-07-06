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
| `DB_POOL_TIMEOUT` | `5` | backend, worker | Секунды ожидания свободного соединения из пула перед `TimeoutError`. Короткий таймаут даёт быстрый отказ (500) вместо 30-секундной очереди под всплеском (bot flood). |
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
| `RATE_LIMIT_TRUSTED_PROXIES` | loopback + private-сети (docker) | backend | CSV из CIDR: с каких peer-адресов rate-limiter верит `X-Real-IP`/`X-Forwarded-For` (см. `rate_limit.py`). Дефолт покрывает nginx в docker-сети. |
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
| `NOTIFICATION_BROADCAST_PER_TICK` | `50` | bot | Максимум `admin_broadcast`, отправляемых поллером за один тик. Срочные типы (`config_ready`/`health_ping_request`/`admin_alert_*`) сортируются в начало тика и не режутся; хвост рассылки сверх лимита переносится на следующие тики (защита от head-of-line). |

## Redis / RQ

| переменная | default | кто читает | описание |
|---|---|---|---|
| `REDIS_PASSWORD` | — | redis (compose), backend, worker | Обязательно — без него Redis открыт на внутренней сети docker'а. RQ использует pickle, unauth redis = RCE. |
| `REDIS_URL` | — | backend, worker | Обычно `redis://:${REDIS_PASSWORD}@redis:6379/0`. |
| `QUEUE_BACKEND` | `""` | backend, worker | `"rq"` — использовать RQ очередь. Любое другое значение переключает провижининг в in-process thread'ы (только dev/test). |
| `RQ_QUEUE` | `vpn-provisioning` | backend, worker | Имя RQ queue. |
| `RQ_JOB_TIMEOUT` | `1800` | worker | Hard-kill per job. **Инвариант (audit #48): строго больше самого медленного playbook-таймаута + запас на очередь/семафор/пост-обработку.** Самый долгий — `site.yml` (timeout=900с). Если job_timeout ≤ 900, RQ убивает джобу РАНЬШЕ конца плейбука → ansible-сирота продолжает конфигурить ноду, а Retry запускает второй параллельный прогон. Меняешь `site.yml` timeout — подними и это. Subprocess-таймаут ansible'а отдельный — см. `MAX_CONCURRENT_ANSIBLE`. |
| `RQ_FAILED_TTL` | `604800` | worker | Сколько держать job'ы в failed registry (7 дней). |
| `RQ_RESULT_TTL` | `86400` | worker | Сколько держать результаты успешных job'ов (1 день). |

## Ansible / provisioning

| переменная | default | кто читает | описание |
|---|---|---|---|
| `PROVISIONING_SSH_KEY` | **обязателен** | compose, worker volume | Путь **на хосте** к ed25519 приватнику для SSH на VPN-ноды. compose откажется стартовать без неё (`?:` required). Обычно `/opt/vpn/secrets/provisioning_key`. |
| `ANSIBLE_PRIVATE_KEY_FILE` | — | worker | Путь **внутри контейнера** к тому же ключу (обычно `/run/secrets/provisioning_key`, туда маппится volume). |
| `ANSIBLE_ROOT` | — | worker | Директория с `playbooks/` и `roles/`. По умолчанию `/app/infra/ansible`. |
| `MAX_CONCURRENT_ANSIBLE` | `3` | worker | Размер `_ansible_semaphore` в `ProvisioningOrchestrator`. Каждый процесс ansible ест ~200MB. Отдельный от warm-pool семафора. **PER-PROCESS, не глобальный кап** (audit #200): при нескольких RQ-воркерах реальный параллелизм ansible = `WORKER_REPLICAS`, а не это число. |
| `ALLOW_INPROCESS_PROVISIONING` | `""` | backend | Dev escape-hatch: `"1"` → backend выполняет ansible сам, без RQ. **Не** включать в prod — блокирует HTTP request'ы. |
| `MIN_HEALTHY_SCORE` | `50` | backend, worker | Минимальный `health_score` ноды для попадания в `choose_node` / `_eligible_nodes`. |
| `CHOOSE_NODE_INCLUDE_REGISTERING` | `0` | backend, worker | audit #72. По умолчанию `choose_node` НЕ выдаёт юзеров на ноду в статусе `registering` (bootstrap ещё идёт → холодный provision падает). `"1"` возвращает старое поведение (registering участвует в выборке). |
| `RESTORE_HY2_AFTER_REINSTALL` | `1` | worker | audit #78. После reinstall (диск стёрт) бэкенд авто-восстанавливает пер-юзерные hysteria2-учётки через `resync_node_hysteria2_clients` (device/apply-таски). `"0"` отключает (оператор восстанавливает вручную по warning-логу). |

## Worker ticks

Все интервалы — в секундах. Каждая тика — self-rescheduling через `queue.enqueue_in`.

| переменная | default | кто читает | тик |
|---|---|---|---|
| `RENEWAL_CHECK_INTERVAL` | `300` | worker | `run_renewal_check` — находит expiring-подписки, шлёт reminder'ы, флипает expired → revoked после grace. |
| `RENEWAL_GRACE_HOURS` | `24` | worker | Сколько часов после `expires_at` подписка висит в `expired` до hard revoke'а. |
| `RENEWAL_WINDOW_LIMIT` | `2000` | worker | Верхняя граница подписок, обрабатываемых `run_renewal_check` за один тик в каждом окне напоминаний (ORDER BY `expires_at` ASC, хвост — следующим тиком). Не даёт тику упереться в `job_timeout` на тысячах истекающих. |
| `BALANCE_CHARGE_INTERVAL` | `3600` | worker | `charge_subscriptions` — hourly tick, burns daily_rate × devices из `prepaid_kopecks`. Плюс trial-expiry фаза. |
| `LOW_BALANCE_WARN_DAYS` | `3` | worker | Триггерит `low_balance_warning` notification, когда runway (balance / daily_rate) < этого. |
| `WARM_POOL_CHECK_INTERVAL` | `120` | worker | `run_warm_pool_check` — тик warmer'а (`ensure_pool`, топит пул). Стадия 2 отзыва вынесена в отдельный тик `run_warm_pool_revoke_tick`. |
| `WARM_POOL_REVOKE_INTERVAL` | `300` | worker | `run_warm_pool_revoke_tick` — стадия 2 отзыва warm-пула: `run_warm_pool_revoke_sweep` физически снимает `revoked`-бандлы с нод и удаляет строки (аудит-фикс #71). Gated на `WARM_POOL_ENABLED`. `0` — отключить. |
| `RETENTION_INTERVAL` | `86400` | worker | `run_retention_tick` — раз в сутки чистит `audit_logs` (`subscription_fetch`/`*:delivered`) и `node_traffic_samples` старше N дней (аудит-фикс #247). `0` — отключить. |
| `AUDIT_LOG_RETENTION_DAYS` | `90` | worker | Порог (дни) для удаления `subscription_fetch` + `*:delivered` из `audit_logs`. `0` — не чистить audit_logs. Прочие action'ы не трогаются. |
| `TRAFFIC_SAMPLE_RETENTION_DAYS` | `30` | worker | Порог (дни) для удаления `node_traffic_samples`. `0` — не чистить. Детекторам/агрегатам нужны лишь последние тики. |
| `RETENTION_DELETE_BATCH` | `10000` | worker | Размер батча удаления retention-тика (id IN (SELECT … LIMIT), коммит после каждого — короткие локи). |
| `RETENTION_MAX_BATCHES` | `200` | worker | Потолок батчей на таблицу за один retention-тик. Остаток донесётся следующим прогоном. |
| `AUTOSCALE_INTERVAL` | `0` / `300` | worker | `run_autoscale_check`. `0` — отключить. `.env.example` ставит `300`. |
| `PENDING_RESCUE_INTERVAL` | `60` | worker | `run_pending_rescue_tick` — re-enqueue `ProvisioningTask.status=pending` старше `PENDING_RESCUE_AGE`. Закрывает дыру, когда `enqueue_task` упал на Redis-hiccup'е и строка осталась без job'а. `0` отключает. |
| `PENDING_RESCUE_AGE` | `60` | worker | Минимальный возраст (sec) pending-задачи, чтобы её подхватил rescue-tick. Меньше этого — считается «только что создана, ещё не RQ'нулась». |
| `ANSIBLE_PLAYBOOK_TIMEOUT` | `300` | backend, worker | subprocess-таймаут (sec) на один `ansible-playbook` run. При регулярно-медленных нодах (package installs, slow SSH) можно поднять, иначе revoke/apply ловят `TimeoutExpired` и таска становится failed. |
| `TRAFFIC_STATS_INTERVAL` | `300` | worker | `run_traffic_stats_tick` — SSH-сбор xray stats + sharing violations. Phase D `detect_traffic_drops` **отключён 2026-04-15** — теперь только сбор samples. |
| `TRAFFIC_STATS_BUDGET_SEC` | `100` | worker | Wall-clock-бюджет одного traffic-stats тика. Сэмплы коммитятся по-нодно; при исчерпании бюджета недособранный хвост нод откладывается до следующего тика (job_timeout тика = 120с, бюджет держит запас). |
| `TRAFFIC_STATS_SSH_WORKERS` | `8` | worker | Параллелизм SSH-сбора в `collect_all_active_nodes` (ThreadPoolExecutor). Записи в БД — только из главного потока. |
| `TRAFFIC_STATS_MAX_SKIPS` | `3` | worker | Сколько тиков подряд нода может быть отсеяна по бюджету, прежде чем поднимется отдельный error-алерт «систематически не опрашивается». Ноды сабмитятся в порядке давности последнего сэмпла (never-sampled первыми), так что отсев не бьёт всегда по одним и тем же. |
| `TRAFFIC_STATS_SSH_CONNECT_TIMEOUT` | `15` | worker | SSH connect-таймаут сборщика (`collect_node_stats`). Паритет с `ssh_bootstrap`. |
| `TRAFFIC_STATS_SSH_BANNER_TIMEOUT` | `20` | worker | SSH banner-таймаут сборщика. Прежние 10с давали ложные «collect failed» на нагруженных нодах. |
| `TRAFFIC_STATS_SSH_AUTH_TIMEOUT` | `20` | worker | SSH auth-таймаут сборщика. |
| `TRAFFIC_STATS_SSH_COMMAND_TIMEOUT` | `15` | worker | Таймаут удалённой команды `xray api statsquery` / чтения sharing-violations. |
| ~~`TRAFFIC_DROP_ENABLED`~~ | ~~`1`~~ | worker | **Inert с 2026-04-15.** Phase D детектор отключён на уровне кода (`worker.run_traffic_stats_tick` не вызывает `detect_traffic_drops`, функция стоит no-op). Переменная оставлена для обратной совместимости env, но не читается. |
| ~~`TRAFFIC_DROP_MIN_USERS`~~ | ~~`5`~~ | worker | **Inert с 2026-04-15.** При возврате автомиграции пороги нужно пересмотреть — прежние значения ложно-триггерили миграции в idle-окнах. |
| ~~`TRAFFIC_DROP_CONFIRM_TICKS`~~ | ~~`1`~~ | worker | **Inert с 2026-04-15.** См. `TRAFFIC_DROP_MIN_USERS`. |
| `NODE_REACHABILITY_STALE_MIN` | `30` | worker | Окно «голодания» reachability-тика (мин): цели без проба дольше этого попадают в гейдж `vpn_reachability_stale_targets` / `summary.stale_targets`. Стабильно >0 — бюджета `NODE_REACHABILITY_BUDGET_SEC` не хватает на весь флот (аудит-фикс #95). |
| `NODE_VPN_PROBE_PORTS` | *(пусто → из VPNConfig)* | worker | Сетевой аудит #1: reachability-тик при живом SSH дополнительно пробит TCP VPN-порт(ы) ноды — SSH-liveness ≠ VPN-liveness. Пусто = порты берутся из enabled `VPNConfig` ноды (hysteria2/UDP исключается, чтобы не ловить ложный `degraded`). CSV (`443,8443`) — ручной override для всех нод. ЗАКРЫТЫ ВСЕ порты при живом SSH → статус `degraded` + пуш + on-host diagnose. |
| `NODE_CONTROLLER_ANCHORS` | `1.1.1.1:443,8.8.8.8:443` | worker | Сетевой аудит #3: self-check связности контроллера ПЕРЕД пер-нодовыми пробами. CSV `host:port` внешних якорей. Хоть один ответил → сеть есть. ВСЕ молчат → воркер потерял сеть: тик пропускает пробы (не метит весь флот ложным DOWN) и шлёт один агрегированный алерт. Пусто = проверка отключена. |
| `NODE_CONTROLLER_ANCHOR_TIMEOUT` | `5` | worker | TCP-таймаут (sec) на якорь в self-check связности контроллера. |
| `NODE_PROBE_GAP_FACTOR` | `2` | worker | Сетевой аудит #7: если между прошлым и текущим пробом цели дыра > `NODE_REACHABILITY_INTERVAL * factor` (цель выпала в обрезанный бюджетом хвост / тик подвисал), серия DOWN перезапускается, а не эскалируется по дырявому wall-clock. |
| `NODE_MASS_DOWN_FRACTION` | `0` | worker | Сетевой аудит #3 (доп.): доля DOWN/degraded среди пробитых целей ≥ этого (при `checked >= NODE_MASS_DOWN_MIN`) → индивидуальные алерты подавляются, шлётся один «массовая недоступность». `0` = выключено (основной механизм — `NODE_CONTROLLER_ANCHORS`). |
| `NODE_MASS_DOWN_MIN` | `5` | worker | Минимум пробитых целей за тик, прежде чем срабатывает mass-down подавление (`NODE_MASS_DOWN_FRACTION`). |
| `OPS_PLAN_REAPER_INTERVAL` | `300` | worker | `run_ops_plan_reaper_tick` — бэкстоп ops-агента: добивает планы, залипшие в `executing` (воркер умер / джоба убита по `job_timeout`), в `failed` с `execution.phase='crash'`. `0` — отключить (аудит-фикс #120). |
| `OPS_PLAN_REAPER_GRACE` | `120` | worker | Запас (sec) сверх `OPS_EXECUTE_JOB_TIMEOUT` до реапа executing-плана (ожидание в очереди / clock skew). |
| `OPS_EXECUTE_JOB_TIMEOUT` | `1800` | worker | Считается таймаутом RQ-джобы `run_ops_plan_execute` для реапера (должен совпадать с `job_timeout` enqueue'а в `api/agent.py`). |

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
| `REALITY_SNI` | _empty_ → pool rotation | backend, worker | «Borrowed» SNI в Reality handshake. Пустое значение включает выбор из `REALITY_DEST_POOL` (`www.yandex.ru`, `vk.ru`, `mail.ru`, `rutube.ru`, `lenta.ru`) — наименее используемый домен per-node. Задайте явное значение только чтобы форснуть один SNI для всех новых нод (dev/test). |
| `REALITY_DEST` | `<sni>:443` | backend, worker | Куда Reality проксирует трафик не-VPN клиента. **Применяется ТОЛЬКО когда задан `REALITY_SNI` (dev/test override)** — в проде (ротация SNI по пулу) dest всегда выводится из выбранного per-node SNI как `<sni>:443` (audit #81: раньше ручка читалась, но нигде не применялась). |
| `REALITY_PORT` | `443` | backend, worker | Порт inbound'а. Менять только если конфликт с другим сервисом на :443. |

## Платёжные провайдеры

| переменная | default | кто читает | описание |
|---|---|---|---|
| `PAYMENT_PROVIDER` | `cryptobot` | backend, bot | Single-provider mode (legacy). Если `PAYMENT_PROVIDERS` задан — игнорируется. |
| `PAYMENT_PROVIDERS` | `""` | backend, bot | Comma-separated список провайдеров. Backend при checkout'е выбирает `random.choice(list)`. |
| `CRYPTOBOT_TOKEN` | `""` | backend | Bearer token к CryptoBot API. HMAC webhook-сигнатура считается от `sha256(token)` как ключа. |
| `CRYPTOBOT_RUB_PER_USDT` | `0` | backend | Курс ₽ за 1 USDT для конвертации RUB-счетов в `/checkout` (аудит #108). `0`/не задан = RUB-счёт через cryptobot отклоняется с 503 (защита от выставления рублей как USDT 1:1). Сумма округляется вверх до цента. |
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
| `SUB_LINK_BASE_URL` | `""` | backend, worker, bot | Base URL sub-links вида `<base>/<sub_token>`. Пусто → sub links disabled, клиенты получают raw URIs. **Зеркалится** в backend и worker, потому что обе стороны пишут `Device.connection_uri`. Указывать на «boring» CDN-домен, не на основной grinwer.online — RKN-блокировка основного не убьёт installed-клиентов. **CDN-фронт обязан отдавать HTTP/1.1**: RKN DPI на мобильных операторах режет H2 stream после TLS-handshake (headers доходят, тело — нет), H1.1 проскакивает. На Cloudflare отключение HTTP/2 требует Pro-плана (Free-план оставляет H2 включённым). Текущий фронт — `grn-ssync.pro` (CF Worker `v8-sub`, проксирует `/<token>` → `https://grinwer.online/api/sub/<token>`). |
| `SUB_PROFILE_UPDATE_INTERVAL_H` | `2` | backend | Заголовок `profile-update-interval` (часы) в саб-ответе — как часто Hiddify/v2rayNG/HAPP сами перечитывают сабу и через sibling-alias подхватывают новую ноду после failover/миграции. Раньше было захардкожено 6ч (окно устаревания конфига до полусуток). Прод для анти-РКН профиля может ужать до `1` (компромисс свежесть failover ↔ нагрузка read-пути; write-amplification срезается `SUB_FETCH_AUDIT_SAMPLE`). |
| `SUB_RETRY_AFTER_SEC` | `60` | backend | Значение заголовка `Retry-After` (сек) на транзиентных 503 саб-линка (пустой набор конфигов / `frozen`-подписка). Даёт корректному клиенту машиночитаемый хинт перезапросить сразу после провижининга/разморозки вместо ожидания планового `profile-update-interval`. |
| `SUB_FILTER_UNHEALTHY_NODES` | `1` | backend | Kill-switch фильтра нездоровых нод при сборке саб-конфига: `0`/`off`/`false` — отдавать креды всех нод как раньше. При включённом (дефолт) — исключать креды нод в `cooldown`/декоммишене/с низким `health_score` (см. `MIN_HEALTHY_SCORE`), чтобы клиент не держал мёртвый эндпоинт в ротации; fallback к полному набору, если фильтр выкинул все креды. |

## Warm credential pool

| переменная | default | кто читает | описание |
|---|---|---|---|
| `WARM_POOL_ENABLED` | `1` | backend, worker | Мастер-switch. `0`/`false`/`no` — `ensure_pool` возвращает `{}`, `try_assign_bundle` всегда `None`. Cold path работает. |
| `WARM_POOL_TARGET` | `10` | worker | Глубина пула на одну ноду. |
| `WARM_POOL_BATCH_PER_TICK` | `3` | worker | Максимум новых warm'ов за одну тику на одну ноду. Чтобы свежая нода не получила 10 последовательных ansible-runs. |
| `WARM_POOL_MAX_CONCURRENT` | `2` | worker | Размер `_warmer_semaphore`. Отдельный от `_ansible_semaphore` ProvisioningOrchestrator'а. Process-local. |
| `WARM_POOL_CHECK_INTERVAL` | `120` | worker | Тик warmer'а (см. секцию «Worker ticks»). |
| `WARM_POOL_REVOKE_INTERVAL` | `300` | worker | Интервал стадии 2 отзыва (`run_warm_pool_revoke_tick`, см. секцию «Worker ticks»). |
| `WARM_POOL_REVOKE_BATCH_PER_TICK` | `5` | worker | Сколько `revoked`-бандлов снимает с нод один revoke-sweep (по одному ansible-run на бандл). |
| `WARM_POOL_REVOKE_MAX_ATTEMPTS` | `5` | worker | После стольких подряд провалов физического отзыва бандл откладывается (лог + ручной разбор), чтобы не молотить мёртвую ноду вечно. Process-local счётчик, сбрасывается на рестарте воркера. |

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
