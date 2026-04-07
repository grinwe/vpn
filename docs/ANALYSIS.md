# Архитектурный обзор и текущие риски

## Done / Implemented
- **Админские маршруты закрыты токеном и аудируются.** Эндпойнты управления нодами/подписками/инвойсами требуют `X-Admin-Token`, актор передаётся через `X-Admin-Actor`, все операции пишутся в `audit_logs`. 【F:backend/app/api.py†L20-L68】【F:backend/app/config.py†L1-L18】
- **Провижининг асинхронный, с отдельными задачами и логами.** `ProvisioningTask` хранит stdout/stderr Ansible, статус и ошибки; задачи можно перезапускать через API. 【F:backend/app/services/provisioning.py†L16-L128】【F:backend/app/api.py†L100-L179】
- **Инвойсы связываются с подписками.** Оплаченный инвойс либо продлевает существующую подписку, либо создаёт новую и возвращает сгенерированные креды и задачу провижининга. 【F:backend/app/api.py†L468-L640】
- **Базовые метрики и healthcheck.** `/metrics` публикует счётчики запросов и задач, `/healthz?deep=true` проверяет подключение к БД и доступность каталога Ansible. 【F:backend/app/main.py†L1-L32】【F:backend/app/api.py†L65-L95】
- **Версионированные миграции.** Добавлена таблица `schema_migrations` и миграция `0001_initial`, что исключает неконтролируемый `create_all`. 【F:backend/app/migrations.py†L1-L55】

## Текущие риски
- **Потенциальные проблемы многопоточности провижининга.** Ранее фоновые потоки делили с HTTP-запросом один SQLAlchemy Session; теперь каждый поток открывает свой `SessionLocal`, но нужна дальнейшая валидация при росте нагрузки. 【F:backend/app/services/provisioning.py†L73-L124】
- **Трафиковые лимиты пока не enforced.** Поля `traffic_limit_mb/traffic_used_mb` есть, добавлен минимальный API для обновления использования, но нет реального сборщика трафика/автоблокировок от провайдера. 【F:backend/app/models.py†L89-L146】【F:backend/app/api.py†L243-L289】
- **Ansible-линтинг только в самом начале.** CI теперь запускает `ansible-lint`, но нет unit-тестов ролей и прогонов playbook'ов, поэтому ошибки шаблонов могут ускользнуть. 【F:.github/workflows/ci.yml†L1-L28】
- **Дефолтные dev-секреты в compose.** `docker-compose.yml`, `db.py`, `bot/config.py` содержат значения по умолчанию и предназначены только для локального стенда; в проде нужно заменять их секретами/vars. 【F:docker-compose.yml†L1-L26】【F:backend/app/db.py†L1-L9】【F:bot/config.py†L1-L8】

## Backend: доменная модель и миграции
- **Миграции всё ещё минимальны.** Версионированный раннер есть, но без Alembic/автогенерации схемы. Любое изменение моделей требует ручного обновления миграций. 【F:backend/app/migrations.py†L1-L55】
- **Учёт трафика не автоматизирован.** Подписка хранит лимит/использование и теперь умеет принимать обновления по API, но нет фоновых джобов или экспортёров, которые бы обновляли `traffic_used_mb` и блокировали устройства. 【F:backend/app/models.py†L89-L146】【F:backend/app/api.py†L243-L289】
- **Отзыв/ротация кредов не автоматизированы.** Поле `revoked_at` есть, но нет сервисов/cron-задач для ротации или массового отзыва при компрометации. 【F:backend/app/models.py†L146-L183】

## Backend API и валидация
- **Нет пагинации и фильтров в списках.** `/api/provisioning/tasks` и `/api/nodes` возвращают всё сразу; для прод-нагрузки нужны `limit/offset` и фильтры по статусу/региону. 【F:backend/app/api.py†L131-L161】
- **Проверки при массовых операциях ограничены.** Блокировка пользователя/подписки вызывает revoke устройств, но не валидирует, что креды реально отозваны на нодах; нет контрольной сверки статусов. 【F:backend/app/api.py†L196-L234】

## Провижининг
- **Зависимость от локального Ansible-раннера.** Backend вызывает `ansible-playbook` через `subprocess`; если ansible не установлен в контейнере/окружении, задачи упадут. Нужен выделенный runner-образ или RPC. 【F:backend/app/services/provisioning.py†L28-L128】【F:backend/app/services/ansible_runner.py†L9-L47】
- **Идемпотентность playbook'ов частичная.** `provision_device.yml` делегирует на shell-скрипт без детальной проверки текущего состояния, что усложняет повторные запуски и обратную синхронизацию. 【F:infra/ansible/playbooks/provision_device.yml†L1-L24】
- **Нет очереди/ретраев.** Задачи запускаются в потоке при HTTP-запросе; при ошибке остаются в статусе `failed`, автоматических ретраев или бэкоффа нет. 【F:backend/app/services/provisioning.py†L61-L128】

## Ansible / инфраструктура
- **Статический инвентарь по умолчанию.** Хотя backend умеет собирать inventory для одиночной ноды, основной инвентарь остаётся статичным в `inventories/prod/hosts.yml`, не учитываются pool'ы/теги. 【F:backend/app/services/ansible_runner.py†L19-L46】【F:infra/ansible/inventories/prod/hosts.yml†L1-L15】
- **Hardening не охватывает db_host.** Роли безопасности применены к `vpn_nodes`, но для хоста с БД нет аналогичного набора. 【F:infra/ansible/site.yml†L1-L9】【F:infra/ansible/roles/bootstrap_node/tasks/main.yml†L1-L49】

## Telegram-бот
- **Нет ретраев к backend.** `_fetch_json` не делает backoff и возвращает общее сообщение об ошибке; пользователи могут получать необработанные таймауты. 【F:bot/handlers.py†L16-L65】
- **Авторизация админов статична.** Список `ADMIN_IDS` задаётся через env и не сверяется с backend. 【F:bot/config.py†L1-L8】

## Безопасность
- **Нет rate limiting/CSRF.** Публичные маршруты не ограничены по частоте, нет защиты от флуд-атак; стоит использовать gateway/proxy с лимитами. (общая рекомендация)

## Наблюдаемость
- **Метрики минимальны.** Есть только счётчики запросов и задач; нет гистограмм латенций и проверки очередей провижининга. 【F:backend/app/main.py†L1-L32】【F:backend/app/api.py†L65-L95】
- **Аудит ограничен backend-ом.** Ansible и бот не публикуют унифицированные логи, нет централизованного сбора. 【F:backend/app/models.py†L183-L209】

## CI/CD
- **Статический анализ ограничен.** CI запускает ruff/compileall и сборку образов, но нет unit-тестов, проверок миграций, e2e сценариев. `ansible-lint` добавлен, но нужны дальнейшие тесты ролей. 【F:.github/workflows/ci.yml†L1-L28】

## Planned fixes
- Добавить полноценный сбор трафика с нод и синхронизацию лимитов с автоматическими действиями (блок/разблок).
- Вынести ansible-runner в отдельный сервис/образ и добавить тестовый прогон playbook'ов в CI.
- Ввести очередь задач с ретраями и мониторингом (Celery/RQ/ARQ) для провижининга и отзыва устройств. Сейчас используются фоновые `threading.Thread` — это блокирует горизонтальное масштабирование API.
- Перевести миграции на Alembic и описывать изменения схемы декларативно.
- Развернуть внешний пробер-рой (RU/KZ/EU), который вызывает `POST /api/nodes/{id}/probes` — сейчас API готов, но отдельный сервис-прошер ещё не написан.
- Ротация IP (Hetzner rebuild / floating IP) при массовом бане.
- Шифрование API-токенов CloudProvider через KMS (сейчас симметричный Fernet на `APP_SECRET_KEY`).

## Recent changes (slim images, Alembic, autoscale, payments)
- **Два образа вместо одного.** [backend/Dockerfile](../backend/Dockerfile) — slim API без ansible (только uvicorn + Python deps). [backend/Dockerfile.worker](../backend/Dockerfile.worker) — отдельный образ с `ansible-core`, `openssh-client`, `sshpass`, `rsync` и скопированным `infra/ansible`. В `docker-compose.yml` сервис `worker` теперь билдится из корня репо (`context: .`) и монтирует SSH-ключ read-only через `${PROVISIONING_SSH_KEY}:/run/secrets/provisioning_key:ro`, `ANSIBLE_PRIVATE_KEY_FILE` передаётся воркеру env-var'ом. API-контейнер больше не держит SSH-ключи и ansible binary — blast radius компрометации API драматически сужен.
- **ansible_runner уважает `ANSIBLE_ROOT` env.** Раньше путь к плейбукам был вычислен относительно файла; теперь сначала проверяется env, что нужно для split-образа. Команда `ansible-playbook` получает `--private-key` если `ANSIBLE_PRIVATE_KEY_FILE` выставлен. 【F:backend/app/services/ansible_runner.py†L13-L90】
- **`/healthz?deep=true` в API-образе** больше не дёргает `_ensure_ansible_root` (ansible'а в API-образе больше нет); вместо этого пробует получить RQ-очередь. 【F:backend/app/api.py†L176-L195】

- **Alembic.** Хэнд-роллд раннер заменён на Alembic: [backend/alembic.ini](../backend/alembic.ini), [backend/app/alembic/env.py](../backend/app/alembic/env.py), ревизии `0001_initial` / `0002_health_and_cloud` / `0003_autoscale` в [backend/app/alembic/versions/](../backend/app/alembic/versions/). `0001` — делегирует `Base.metadata.create_all(checkfirst=True)` (модели — single source of truth для первого наката); последующие ревизии — explicit DDL с `IF NOT EXISTS`, чтобы ехать поверх уже существующих деплоев. `app/migrations.py` оставлен единой public entrypoint: на старте делает backfill из `schema_migrations` в `alembic_version` (stamp без перезаката) и зовёт `alembic upgrade head`. Если alembic не установлен — fallback на legacy create_all. `SKIP_MIGRATIONS=1` выключает автомиграции на бут.

- **Автоскейл.** [services/autoscale.py](../backend/app/services/autoscale.py) — per-pool оценка: `utilization = active_subs / Σ max_users(healthy_nodes)`, если ≥ `autoscale_high_watermark` и `node_count < autoscale_max_nodes` — зовёт `spawn_node` с дефолтами пула. Нездоровые ноды и ноды в cooldown исключаются из знаменателя, иначе автоскейл "не увидит" реальное насыщение. Downscale не делается — шринк живого пула мигрирует пользователей и пока не стоит сложности.
  - Новые колонки в `server_pools`: `autoscale_enabled`, `autoscale_provider_id`, `autoscale_region`, `autoscale_plan`, `autoscale_image`, `autoscale_high_watermark`, `autoscale_max_nodes` (ревизия `0003_autoscale`).
  - Периодический запуск через RQ: воркер на старте делает одноразовый `enqueue_in(AUTOSCALE_INTERVAL)`, а сам job `run_autoscale_tick` в конце re-enqueue'ит себя — recurring без rq-scheduler.
  - API: `PUT /api/pools/{id}/autoscale` для конфига, `POST /api/autoscale/tick` для ручного запуска с возвратом `PoolDecision[]`.

- **Платёжный шлюз.** [services/payments/](../backend/app/services/payments/) — `PaymentProvider` Protocol, реализация `CryptoBotProvider` (Crypto Pay API @CryptoBot): `create_invoice` + `verify_webhook` с HMAC-SHA256 по спеке, поддерживает USD→USDT маппинг.
  - `POST /api/invoices/{id}/checkout` — создаёт invoice у провайдера, возвращает `pay_url` для вставки в сообщение бота.
  - `POST /api/payments/webhook/{provider_name}` — принимает колбэк, верифицирует подпись (auth не через admin token!), при `status=paid` дергает рефакторенную `_mark_invoice_paid_core` — тот же пайплайн что и у админского `mark_paid`.
  - Админский `mark_invoice_paid` теперь тонкая обёртка над `_mark_invoice_paid_core`, вся логика провижининга шарится.
  - Env: `PAYMENT_PROVIDER=cryptobot`, `CRYPTOBOT_TOKEN=...`.

## Recent changes (task queue)
- **RQ + Redis.** Добавлен сервис `redis` в [docker-compose.yml](../docker-compose.yml) и отдельный сервис `worker`, запускающий `python -m app.worker` из того же backend-образа. Провижининг теперь ходит через RQ с 3 ретраями и бэкофом `10s/30s/120s`. 【F:backend/app/queue.py†L1-L90】【F:backend/app/worker.py†L1-L95】
- **Stateless API.** `ProvisioningOrchestrator.run_task_async` сначала пробует `enqueue_task()`; если Redis недоступен — откатывается на старый `threading.Thread` (для локального dev без Redis и для тестов). После переката на RQ воркер можно сделать единственным контейнером, где установлен `ansible` и лежит SSH-ключ — API-реплик это больше не касается. 【F:backend/app/services/provisioning.py†L136-L170】
- **Recovery after restart.** `reset_stuck_tasks` теперь не просто помечает `running → failed`, а requeue'ит их в RQ и заодно переотправляет все `pending`-задачи — так перезапуск API/воркера не теряет работу. 【F:backend/app/main.py†L18-L60】
- **Конфигурация через env.** `QUEUE_BACKEND=rq`, `REDIS_URL`, `RQ_QUEUE`, `RQ_JOB_TIMEOUT`, `RQ_FAILED_TTL`, `RQ_RESULT_TTL`. Если `QUEUE_BACKEND` не выставлен — провижининг работает как раньше (in-process threads), что удобно в юнит-тестах.

## Recent changes (auto-scale + block-aware)
- **Шифрование секретов в БД.** Добавлен `app.security` (Fernet, ключ из `APP_SECRET_KEY`). `Credential.config_text`, `Device.connection_uri`, `CloudProvider.api_token_enc` шифруются на запись и прозрачно расшифровываются в DTO (`CredentialOut`, `DeviceOut`). Если ключ не задан — работает в plaintext и логирует предупреждение. 【F:backend/app/security.py†L1-L84】【F:backend/app/schemas.py†L6-L68】
- **Capacity/health-aware scheduling.** В `VPNNode` добавлены `max_users`, `max_bandwidth_mbps`, `health_score`, `blocked_regions`, `cooldown_until`. `choose_node` учитывает cooldown, health_score ≥ `MIN_HEALTHY_SCORE` (env, default 50), капасити по `max_users` и умеет исключать ноды через `exclude_node_ids` — это один и тот же код для покупки и для миграции при блокировке. 【F:backend/app/models.py†L100-L145】【F:backend/app/services/provisioning.py†L28-L100】
- **Health probing + auto-migration.** Новая таблица `health_probes`, сервис `app.services.health`: агрегация по regions/result за скользящее окно (15 мин по умолчанию), расчёт `health_score`, список `blocked_regions` по порогу (default 30% success rate). Если нода считается dead (overall < 20%) — ставится cooldown и все активные подписки миграются на здоровую ноду через `migrate_subscriptions_off`. API: `POST /api/nodes/{id}/probes`, `GET /api/nodes/{id}/health`, `POST /api/nodes/{id}/migrate`. 【F:backend/app/services/health.py†L1-L180】【F:backend/app/api.py†L850-L950】
- **Cloud provider abstraction + Hetzner.** Новый пакет `app.services.cloud` с интерфейсом `CloudDriver` и реализацией `HetznerDriver` (raw `requests`, poll до status=running). Модель `CloudProvider` хранит зашифрованный API-токен и дефолты. `app.services.node_spawner.spawn_node`: создаёт VM через драйвер → сохраняет `VPNNode` с `provider_*` полями → ставит задачу `bootstrap` в существующий оркестратор провижининга. API: `POST /api/cloud/providers`, `POST /api/nodes/spawn`, `POST /api/nodes/{id}/destroy`. 【F:backend/app/services/cloud/base.py†L1-L70】【F:backend/app/services/cloud/hetzner.py†L1-L130】【F:backend/app/services/node_spawner.py†L1-L110】
- **Пагинация и фильтры** в `GET /api/nodes` (status, region, pool_id, is_active, limit/offset) и `GET /api/provisioning/tasks` (status, target_type, limit/offset). 【F:backend/app/api.py†L220-L320】
- **Бот-HTTP-клиент.** Единый aiohttp session с keep-alive, 3 попытки с линейным бэкофом на 5xx и сетевых ошибках, закрытие сессии на shutdown. 【F:bot/handlers.py†L1-L75】【F:bot/bot.py†L1-L22】
- **Миграция 0002.** Идемпотентный SQL-скрипт добавляет таблицы `cloud_providers`, `health_probes`, все новые колонки `vpn_nodes` через `IF NOT EXISTS`. 【F:backend/app/migrations.py†L24-L100】
- **Фикс AuditLog.metadata.** `metadata` — зарезервированное имя в SQLAlchemy Declarative; переименовано в Python-атрибут `extra`, колонка в БД осталась `metadata` для back-compat. 【F:backend/app/models.py†L310-L325】【F:backend/app/api.py†L45-L66】

## Что ещё не сделано, но важно
- **Real probing rig.** API для приёма проб готов, но самих проберов (RU-MTS/МГТС, KZ, EU) пока нет. Нужны небольшие Go/Python-демоны, раскиданные по целевым провайдерам/симкам.
- **Traffic collector.** На стороне ноды (xray stats API / ss-tproxy counters) → push на `/api/subscriptions/{id}/traffic`.
- **Второй платёжный провайдер.** `PaymentProvider` абстракция готова, есть `CryptoBotProvider`. YooKassa/Telegram Stars — аккуратное добавление новых классов.
- **Даунскейл пулов.** Автоскейл умеет только расти. Шринк требует миграции пользователей — отложено до наличия реальных данных об использовании.
- **Integration тесты и прогон миграций в CI.** Сейчас только ruff/ansible-lint/compile; нужно поднимать postgres в CI и проверять `alembic upgrade head` + роллбэк.
