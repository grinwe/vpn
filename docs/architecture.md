# Архитектура

Эта страница — карта «как устроено целиком». Детали компонентов — в `components/`, инфраструктура разворачивания — в `infrastructure/`, таблицы — в `data-model.md`.

## Что это вообще

Коммерческий VPN-as-a-Service для российского рынка. Пользователь приходит в Telegram-бота или Mini App, покупает подписку (или активирует триал), получает конфиги для нескольких протоколов (VLESS Reality, VLESS xHTTP, VLESS WS CDN) на одной из VPN-нод.  ShadowTLS+SS (0.2) и Hysteria2 (0.3) — legacy-поддержка существующих нод, новые конфиги через UI не создаются (см. RELAY_ROADMAP).  Ноды поднимаются и настраиваются Ansible'ом; бэкенд оркестрирует «кому что выдать», воркер выполняет тяжёлую работу и периодические задачи.

## Компоненты и их роли

```
                    ┌─────────────────┐
                    │  Telegram user  │
                    └──┬──────────────┘
                       │   TG updates / WebApp initData
                       ▼
        ┌───────────────────────────┐
        │  aiogram bot  (bot/)      │
        │  - user commands          │
        │  - admin /invoices        │
        │  - long-poll notifications│
        └──┬────────────────────────┘
           │ X-Admin-Token + X-Admin-Actor
           │ HTTP → http://backend:8000
           ▼
┌────────────────────────────────────────────────┐
│  FastAPI backend  (backend/app/)               │
│                                                │
│  /api/*          — api.py      (admin surface) │
│  /api/webapp/*   — api_webapp.py (Mini App)    │
│  /api/sub, /api/trial, /api/referral, …        │
│                  — api_extensions.py           │
└──┬───────────────────────┬─────────────────────┘
   │ SQLAlchemy            │ enqueue_task
   ▼                       ▼
┌──────────────┐     ┌──────────────┐
│ PostgreSQL   │     │ Redis + RQ   │
│ (vpn db)     │     │  (queue)     │
└──────────────┘     └──────┬───────┘
                            │
                            ▼
                   ┌──────────────────┐
                   │ worker  (RQ)     │
                   │ - provisioning   │
                   │ - cron ticks     │
                   │ - warm pool      │
                   │ - autoscale      │
                   │ - health/probes  │
                   │ - balance charge │
                   └──────┬───────────┘
                          │ ansible-playbook
                          │ (subprocess)
                          ▼
                 ┌──────────────────┐
                 │  VPN nodes       │
                 │  (Hetzner/Vultr/ │
                 │   DO/Aeza/manual)│
                 │                  │
                 │  VLESS Reality,  │
                 │  xHTTP, WS-CDN   │
                 └──────────────────┘
```

Все контейнеры (db, redis, backend, worker, admin SPA, webapp SPA, bot) крутятся в одном `docker-compose.yml` на единственном web-хосте — см. `infrastructure/deployment.md`.

## Бэкенд — три роутера в одном процессе

Единственный FastAPI-app собирается в `backend/app/main.py:107-109` и состоит из трёх роутеров:

| Роутер           | Префикс         | Назначение                               | Модель доверия                |
|------------------|-----------------|------------------------------------------|-------------------------------|
| `api_router`     | `/api`          | Полноценный admin surface                | `X-Admin-Token` (shared)      |
| `webapp_router`  | `/api/webapp`   | Telegram Mini App (пользовательский UX)  | Telegram `initData` → JWT      |
| `ext_router`     | `/api`          | Гибрид: `/sub/{token}` аноним, остальные дёргает либо admin, либо бот с admin-токеном | смешанная |

Подробности каждого — в `components/backend-api.md`.

## Границы доверия

```
┌─────────────── public internet ───────────────┐
│                                                │
│   TG clients → api.telegram.org → bot          │
│   Hiddify/v2rayNG polls → nginx → /api/sub/…   │
│   Mini App browser → nginx → /api/webapp/…     │
│                                                │
└─────────────────┬──────────────────────────────┘
                  │ 443 (CF proxy) → nginx on web-host
                  ▼
┌────────────── trusted: same host ──────────────┐
│                                                 │
│  nginx → backend(8000) → redis(6379) → db      │
│                       ↘ worker ← ansible → nodes│
│                                                 │
└─────────────────────────────────────────────────┘
                  │ ssh (ed25519, root)
                  ▼
┌──────── semi-trusted: VPN nodes ───────────────┐
│                                                 │
│  Все ноды доверяют одному provisioning-ключу.   │
│  Компрометация worker'а = доступ ко всем нодам. │
│                                                 │
└─────────────────────────────────────────────────┘
```

Ключевые принципы, выраженные в коде:

- **Admin token один на всё.** `ADMIN_API_TOKEN` используется и backend'ом (как приёмник), и ботом (как отправитель) — `docker-compose.yml:43,169`. Это shared secret: компрометация любого из них → компрометация всего admin surface. Разграничение акторов делается через заголовок `X-Admin-Actor`, но заголовок самодекларативный (`backend/app/auth.py` — функция, разрешающая actor'а, не проверяет подпись).
- **Mini App — свой контур.** Пользовательские запросы из WebApp верифицируются через `Telegram.WebAppInitData` (HMAC-SHA256 от `BOT_TOKEN`), после чего выдаётся короткоживущий JWT со сроком из `WEBAPP_JWT_TTL_SECONDS` — `backend/app/api_webapp.py` начиная со строки 149. Mini App физически не может эскалировать до admin (разные depency chains).
- **Машинные клиенты — scoped токены.** Пробы здоровья с нод и агрегаторы трафика ходят с `X-Api-Token` и ограничены скоупами `probe:read` / `probe:write` / `traffic:write` (`backend/app/auth.py:35-43`). Токены хранятся только в виде SHA-256 хэша.

## Очередь, провижининг и фоновые задачи

Вся тяжёлая/медленная работа уезжает в RQ через `backend/app/queue.py`. Дизайн:

- **Провижининг-задачи** (`ProvisioningTask` — см. `data-model.md`) — единичные запуски ansible-playbook'а. На каждую заявку создаётся строка в БД, RQ-воркер её подхватывает, исполняет, записывает stdout/stderr/status. Орechestrator'ом выступает `ProvisioningOrchestrator` в `backend/app/services/provisioning.py`.
- **Cron-тики** — внутри воркера запущены длительные self-rescheduling задачи: warm-pool topup, autoscale, drain, renewals, balance charge, health-check, и т.п. Они не управляются внешним cron'ом — каждая тика сама ставит себя в очередь через `queue.enqueue_in(...)` на следующий запуск. Структура — в `components/worker.py`.
- **Warm pool** — отдельный механизм, который заранее провижинит безхозные credential'ы на каждой ноде, чтобы новая покупка не ждала запуска ansible. Алгоритм и гонки — в `components/warm-pool.md`.

## Платежи и биллинг

Текущая модель — «баланс в копейках на пользователе». Платежи принимаются через один из трёх провайдеров:

- **Telegram Stars** — платёж внутри TG. В webhook-режиме (#62): TG шлёт update напрямую на backend (`/tg-webhook`), backend проверяет `X-Telegram-Bot-Api-Secret-Token`. В legacy polling: бот форвардит `successful_payment` через shared-secret.
- **CryptoBot** — HMAC-SHA256 по телу от `CRYPTOBOT_TOKEN`.
- **Generic SBP** — конфигурация на слот (slug) через env, HMAC по сырому телу.

Все идут через единый интерфейс `PaymentProvider` (`backend/app/services/payments/base.py`). Ротация между ними — через `PAYMENT_PROVIDERS` (comma-separated). Детали — в `components/payments.md`.

Покупка подписки и триал — через balance: см. `docs/BILLING_V2.md` (исторический документ) и `components/backend-api.md` (что именно делает `/api/webapp/subscriptions/activate`).

## Внешние сервисы

| Сервис                 | Зачем                                              | Как интегрируется                                 |
|------------------------|----------------------------------------------------|---------------------------------------------------|
| Telegram Bot API       | получение обновлений бота, отправка сообщений      | webhook на backend `/tg-webhook` (#62) или aiogram long-poll (legacy) |
| Telegram WebApp        | Mini App UX                                        | initData HMAC в `api_webapp.py`                   |
| CryptoBot              | криптовалютные платежи                             | HTTP + HMAC webhook                               |
| Generic SBP (bank)     | СБП через банк-агрегатор                           | HTTP + HMAC webhook, конфиг на slug               |
| Hetzner / Vultr / DO / Aeza | provisioning VPN-нод через cloud API          | `backend/app/services/cloud/*.py` — raw `requests`|
| Cloudflare             | DNS для `grinwer.online`, TLS terminator перед nginx | CF в режиме Full (strict)                         |
| Let's Encrypt          | TLS cert на origin                                 | certbot DNS-01 через CF (cert-renew в Ansible)    |
| Prometheus / Grafana   | метрики backend'а + node_exporter                  | `/metrics` (admin-gated), pull model              |

## Потоки данных — два главных сценария

### 1. Новый пользователь покупает подписку через Mini App

```
 user       webapp       backend                worker        node
  │            │           │                       │           │
  │ /app/      │           │                       │           │
  │───────────►│           │                       │           │
  │            │ initData  │                       │           │
  │            │──────────►│ verify HMAC, mint JWT │           │
  │            │◄──────────│                       │           │
  │            │ /checkout │                       │           │
  │            │──────────►│ create Invoice(pending),          │
  │            │           │ create Payment(pending),          │
  │            │           │ call provider.create_invoice      │
  │            │◄──────────│ pay_url                           │
  │            │           │                       │           │
  │ [pay externally → provider webhook → backend]  │           │
  │            │           │ webhook verify → mark Invoice paid│
  │            │           │ → balance.topup (если topup)      │
  │            │           │ → или balance.activate_subscription│
  │            │           │ → создать Subscription,            │
  │            │           │   попытаться try_assign_bundle     │
  │            │           │   из warm pool                    │
  │            │           │                       │           │
  │            │           │ если warm pool не выдал —         │
  │            │           │ enqueue ProvisioningTask         │
  │            │           │                       ├──ansible─►│
  │            │           │                       │ playbook  │
  │            │           │                       │◄──────────│
  │            │           │                       │ write     │
  │            │           │                       │ Credential│
  │            │ /invoices/{id} poll                │           │
  │            │──────────►│ status=paid, device ready         │
```

### 2. Деградация ноды → миграция подписок

```
 probes        backend          worker(drain_tick)        ansible
   │              │                    │                     │
   │ POST health  │                    │                     │
   │─────────────►│ write HealthProbe  │                     │
   │              │ aggregate score    │                     │
   │              │ if degraded → mark │                     │
   │              │ node as draining   │                     │
   │              │                    │ scan draining nodes │
   │              │                    │ pick batch of subs  │
   │              │                    │ call orchestrator.  │
   │              │                    │ migrate_subscription│
   │              │                    │ (preserve sub_token)│
   │              │                    ├────────────────────►│
   │              │                    │◄────────────────────│
   │              │                    │ update Device rows  │
   │              │                    │ emit notification   │
```

## Что живёт где

Полная таблица директорий — в `README.md` внутри `docs/`. Кратко:

- `backend/app/` — FastAPI (`main.py`, три роутера, `worker.py`, `services/`)
- `bot/` — aiogram-бот
- `admin/` — React SPA админки (`admin/src/`)
- `webapp/` — React SPA Mini App (`webapp/src/`)
- `infra/ansible/` — все Ansible-роли и playbook'и
- `docker-compose.yml` — единственный prod compose-файл
- `install.sh` — bootstrap-скрипт для первичной установки на голый сервер (НЕ документирован в этой доке; при противоречии с `infrastructure/deployment.md` источник истины — deployment.md)

## ⚠️ Неясные места

- `install.sh` в корне — не документировано официально, когда его запускают vs когда ansible'ом. По коду он отличается от `deploy_app_stack` playbook'а содержимым, но не ясно, какой flow считается канонiculous для «поднять на новом хосте с нуля».
- Монтирование приватного SSH-ключа в worker-контейнер через `${PROVISIONING_SSH_KEY:-...}` в `docker-compose.yml:144` — по коду дефолт вместо пути подставляет литеральную строку публичного ключа, что гарантированно сломает `docker compose up` без env. Как оно вообще запускается — непонятно без обхода реального compose-файла на проде.
- Ноды в `inventories/prod/hosts.yml` закомментированы (только `mgmt-1`, `nl-monitoring`, `nl-web` — все на `45.14.244.140`). Неясно, где именно хранится актуальный список VPN-нод: либо они подтягиваются динамически из БД через `ansible_runner.build_inventory`, либо в отдельном inventory, не попавшем в репо.
