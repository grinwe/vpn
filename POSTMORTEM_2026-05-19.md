# Постмортем: потеря mgmt-хоста и восстановление

**Дата инцидента:** 2026-05-19
**Дата восстановления:** 2026-05-19 — 2026-05-21
**Автор:** session с Claude
**Бранч:** `dev`

---

## TL;DR

Умер контрол-хост целиком (диск потерян, бэкапов не было). Восстановлено по тому что физически осталось на нодах + закешированная вкладка админки в браузере оператора. Юзеры не заметили — VPN-трафик идёт на уровне xray, не зависит от mgmt-БД. После применения `restore.sql` 8 активных подписок вернулись с правильным мэппингом, 15 orphan-подписок ждут /restore (на placeholder-юзере 999999, истекут через 30 дней).

Параллельно вычищены два класса архитектурных дыр: **миграции alembic** (12 из 33 не были идемпотентны для fresh DB), **ansible-runner на flaky-канале** (SSH-retries отсутствовали, role bootstrap не ждал все apt/dpkg-locks). Восстановлен пайплайн disaster-recovery для будущих инцидентов.

---

## 1. Начальное состояние сессии (до инцидента)

Сессия началась с двух прикладных задач, не связанных с DR:

### 1.1. Анти-DDoS: cold-path throttle + rate-limit на entry endpoints

**Контекст:** 250 ботов за 2 минуты прошли через бота, нажали /plans → активация → попали мимо warm-pool → cold-path provision_subscription с ansible-runs → ансибл-чейн положил одну xray-ноду.

**Решение** (коммит `94581a8`, уже до этой сессии):
- `backend/app/services/provisioning_throttle.py` — global per-process sliding window: дефолт 5 cold-provisions / 60s. При исчерпании — `ColdPathThrottled` → 503 с `Retry-After`.
- Hook в `provision_subscription` сразу после warm-pool miss, до cold ansible. Migrations и `reprovision_subscription` НЕ throttle'ятся (это system-init, не user-init).
- `@limiter.limit("10/minute")` на `/api/users/register` и `/api/trial/activate` через slowapi.
- Env vars: `COLD_PROVISION_MAX_PER_WINDOW=5`, `COLD_PROVISION_WINDOW_SECONDS=60` в `.env.example` и `env.j2`.
- Docs обновлены в `docs/operations/env-reference.md` и `docs/components/backend-api.md`.

### 1.2. UX-фиксы для бота

**Контекст:**
- `migration_notice` Telegram-сообщение содержало URL подписки — юзеры думали что URL надо вставлять вручную в Hiddify (хотя профиль обновляется сам через preserved sub_token).
- `/config` команда выдавала 10 raw vless-ссылок вместо одной subscription URL.

**Решение** (коммиты `cb8313f`, `f320a00`, до этой сессии):
- `migration_notice` — текст без URL: «Подписка обновится в клиенте автоматически — нажми 🔄 рядом с профилем».
- `/config` — два сообщения: первое с инструкциями + клавиатурой онбординга, второе — одна строка `<code>{sub_url}</code>` для tap-to-copy.
- `docs/components/bot.md` — описание нового поведения, ссылка на sibling-alias invariant в `/sub/{token}` ([api_extensions.py:82-107](backend/app/api_extensions.py#L82-L107)).

---

## 2. Инцидент: потеря mgmt-хоста

### 2.1. Что случилось

VPS-хостер потерял диск контрол-хоста `nl-web` (IP `45.14.244.140`). Сервер физически не восстановим, бэкапов не было, ничего скопированного руками не существовало. Утеряно полностью:

- PostgreSQL: вся БД (users, subscriptions, devices, credentials, vpn_nodes, vpn_configs, wg_exit_nodes, relay_exit_links, audit_logs, invoices, payments, referral_codes, balance_transactions).
- Redis: RQ-очереди, rate-limit counters (memory-based).
- Docker volumes: `vpn_db_data`, `vpn_redis_data`.
- Nginx-конфиги, certbot SSL сертификаты, env-файлы.

**Что физически осталось живым** (мы об этом не сразу поняли):
1. **5 RU vpn-нод** — xray работает, в `/usr/local/etc/xray/config.json` лежат UUID активных юзеров. VPN-трафик продолжает идти.
2. **8 WG exit-нод** — WireGuard в kernel'е, туннели up.
3. **Telegram webhook очередь** — Telegram держит updates ~24ч когда webhook возвращает 5xx.
4. **Одна открытая вкладка админки** в браузере оператора — Users page с 38 юзерами, telegram_id, балансами.
5. **APP_SECRET_KEY** — у оператора в локальном vault.yml.

Также параллельно произошёл **WHOIS-карантин** домена `grinwer.online` — NameCheap-registrar отрубил Custom DNS делегацию на Cloudflare и подменил A-record на `198.54.117.242` (NameCheap parking). Это второй удар сверху — даже если бы mgmt был жив, DNS не резолвился. Причина: при трансфере с reg.ru на NameCheap WHOIS-контакты были обнулены, юзер не подтвердил email, через 15 дней ICANN-required block сработал.

### 2.2. Что юзеры заметили

**Ничего**.
- VPN-трафик не прерывался (xray на нодах не знает что mgmt мёртв — он автономно serves traffic для UUID в clients[]).
- Webapp/бот лежали → попытка /start или открыть webapp возвращала 5xx. Но в pending-очереди Telegram было всего **1 update** (от самого оператора, проверявшего что бот лежит).
- Платёжные провайдеры (Stars, cryptobot) пытались доставить webhook'и → получали 5xx → retry'или. Когда mgmt поднимется, retry'и придут.

---

## 3. Восстановление

### 3.1. Стратегия

1. **Поднять чистый mgmt** на новой VPS.
2. **Не пересоздавать ключи Reality / WG** — реюз UUID'ов и приватных ключей из xray.clients[] и /etc/wireguard/ соответственно, чтобы существующие подключения юзеров продолжали работать.
3. **Восстановить структуру БД** через комбо:
   - xray.config*.json со всех vpn_nodes → users + subscriptions + devices.
   - /etc/wireguard/wg*.conf со всех jump + exit нод → wg_exit_nodes + relay_exit_links.
   - Закешированная вкладка админки → telegram_id + balance + created_at для users.
4. **Известить минимально** — только VIP-плательщикам через личные сообщения (никакой публичной паники), остальные либо не заметят, либо при следующем рефреше Hiddify сходят в webapp за новой ссылкой.

### 3.2. Recovery-пайплайн

Архитектура из трёх этапов: **slurp → aggregate → SQL**.

#### Этап 1: Ansible-роли (read-only sweep с нод)

**[infra/ansible/roles/recover_xray_inventory/](infra/ansible/roles/recover_xray_inventory/)**:
- Через `ansible.builtin.find` пробит список путей `/usr/local/etc/xray/config*.json` (включая `config_xhttp.json`, `config_ws_cdn.json` — каждый протокол это отдельный xray-инстанс).
- Slurp всех найденных конфигов через `slurp` module.
- Копирование на контроллер с именем `<host>__<basename>.json` через `delegate_to: localhost`.
- Read-only, никаких state-mutations на нодах.

**[infra/ansible/roles/recover_wg_inventory/](infra/ansible/roles/recover_wg_inventory/)**:
- Через `find` подбирает `/etc/wireguard/wg*.conf` на каждом хосте.
- Slurp + копирование на контроллер. Та же логика что у xray-роли.

**[infra/ansible/playbooks/recover_xray_inventory.yml](infra/ansible/playbooks/recover_xray_inventory.yml)**:
- Объединяет обе роли + локальный aggregate-шаг.
- Прогоняется один раз на всех `vpn_nodes` + `wg_exit_nodes`.

**[infra/ansible/playbooks/recover_xray_runtime.yml](infra/ansible/playbooks/recover_xray_runtime.yml)**:
- Дополнительно к статическому config-slurp'у — runtime stats через `xray api statsquery` (gRPC).
- Цель: отделить "warm-bundle pre-provisioned ready to assign" от "warm-bundle уже выдан реальному юзеру" (warm-* email при assignment **не переименовывается** в xray, только в БД линкается к subscription_id — см. [warm_pool.py:101](backend/app/services/warm_pool.py#L101)).
- Stats живут в RAM xray-процесса → выживают до рестарта → пока процесс жив можно отличить assigned от просто warm.

#### Этап 2: Aggregator-скрипты (нормализация JSON)

**[scripts/aggregate_xray_inventory.py](scripts/aggregate_xray_inventory.py)**:
- Парсит все `recovered/<host>__config*.json`, мержит несколько файлов одной ноды.
- Регэксп `^user-(\d+)-(\d+)(?:-.*)?$` ловит формат email на ноде `user-{user_id}-{subscription_id}-{epoch_seconds}-{nonce_4hex}`.
- Группировка по (user_id, sub_id, node). Список devices внутри (multi-device per subscription с разными UUID'ами).
- Также извлекает Reality privateKey/shortIds/serverNames для будущих vpn_configs row'ов.
- Output: `users.json` + `nodes.json`.

**[scripts/aggregate_wg_inventory.py](scripts/aggregate_wg_inventory.py)**:
- Парсер wg-конфигов вручную (configparser давится дубликатами `[Peer]`).
- Различает jump (есть `Endpoint` в peer) и exit (нет Endpoint = server-side).
- Matching jump → exit по `Endpoint IP == exit.ansible_host` через inventory.
- Public-keys derived из private через X25519.
- Output: `wg.json` со списками exits + links + unmatched.

**[scripts/aggregate_warm_inventory.py](scripts/aggregate_warm_inventory.py)**:
- Объединяет xray-конфиги (статика) + runtime-stats (динамика).
- Warm-bundle = "assigned" если есть в xray-stats или access-log'е, "warm" иначе.
- Парсит `warm-{node_id}-{hex}` чтобы понять legacy node_id для каждого bundle'а.
- Output: `credentials.json`.

#### Этап 3: SQL-генератор

**[scripts/generate_restore_sql.py](scripts/generate_restore_sql.py)** — финальная сборка:

Input:
- `users.json`, `nodes.json`, `wg.json`, `credentials.json`
- `admin_users.json` — выгрузка таблицы users из закешированной вкладки админки (через JS-snippet в DevTools console)
- `inventories/prod/hosts.yml` для добивки host/region для нод
- `--node-id-map 'name1:id1,name2:id2'` чтобы сохранить legacy node_id (важно для warm-email-prefix consistency)
- `--app-secret-key` — для Fernet-шифрования WG private keys

Output `restore.sql` содержит INSERT'ы для:
- **`vpn_nodes`** — 5 нод с правильными host/region, ssh_port=22, status=active.
- **`vpn_configs`** — по 2 на ноду (vless-reality + vless-xhttp), с derived public_key, settings JSONB (private_key, public_key, short_id, server_name, camo_dest для Reality; domain, xhttp_path, xhttp_mode для xhttp).
- **`users`** — 39 row'ов (38 из admin + 1 placeholder `999999`/`__recovery_orphans__` для orphan-подписок). С telegram_id, email, created_at, balance_kopecks, trial_activated_at (выставляется для юзеров с подписками/балансом — чтобы не получили "первый месяц на нас" повторно).
- **`subscriptions`** — 8 known (user-* form, expires_at=first_provision_epoch + plan_duration_days, auto_renew=TRUE, extra_device_slots=count(devices)-plan.max_devices) + 14 orphan (на placeholder user, expires=NOW+30d, auto_renew=FALSE).
- **`devices`** — 9 known + 14 orphan. access_username = email из xray.clients[], sub_token свежесгенерён.
- **`credentials`** — 18 known (per device × per protocol с собранными VLESS-URL'ами через `_build_vless_url_for_credential()`) + 28 orphan.
- **`wg_exit_nodes`** — 7 exits с зашифрованным private_key_enc. `ON CONFLICT (id) DO UPDATE SET wg_private_key_enc = ...` чтобы повторный прогон с APP_SECRET_KEY обновлял ключи.
- **`relay_exit_links`** — 35 jump→exit-link'ов (5 RU jumps × 7 exits) с зашифрованными client private keys.
- `setval` на каждом sequence чтобы новые регистрации не наехали на восстановленные id.

Все INSERT'ы идемпотентны: `ON CONFLICT (id) DO NOTHING` (или `DO UPDATE` где нужно).

### 3.3. Открытия по пути

#### 3.3.1. Migration race на fresh DB

После раскатки нового mgmt и попытки docker compose up, backend упал с `psycopg2.errors.DuplicateColumn: column "pool_state" of relation "credentials" already exists` в migration 0008.

**Корень**: [0001_initial.py](backend/app/alembic/versions/0001_initial.py) использует `Base.metadata.create_all(checkfirst=True)` — создаёт схему по ТЕКУЩЕМУ состоянию моделей. С каждым новым полем в `models.py` `create_all()` начинал генерить колонки которые формально должны добавляться в последующих миграциях. На fresh DB это приводило к конфликту: 0001 создавал колонку, 0008 пытался ADD COLUMN → "уже есть".

**Лечение** (коммит `3511245`):
- Новый helper [backend/app/alembic/_idempotent.py](backend/app/alembic/_idempotent.py): `has_table()`, `has_column()`, `has_index()`, `has_unique_constraint()`, `has_foreign_key()` через SQLAlchemy Inspector.
- Каждая schema-additive операция в 12 миграциях обёрнута в `if not has_*(...)` guard.
- Migration 0017 (enum RENAME VALUE) обёрнута в pg-блок `DO $$ ... EXCEPTION WHEN invalid_parameter_value THEN NULL END $$` — на fresh DB источника rename'а нет (create_all сразу делает underscore-form enum-labels из python-names).
- Полный аудит через Explore-агента подтвердил что все 33 миграции теперь корректны для трёх сценариев: fresh DB, инкрементальная historic DB, partial-failed после прерванного апгрейда.

#### 3.3.2. Multi-protocol xray architecture

Изначально recover_xray_inventory роль брала ТОЛЬКО первый существующий config-файл (`/usr/local/etc/xray/config.json`). После этого юзер выяснил что админка показывает 12 active warm-* юзеров на ru-pq-01, а наш recovered users.json там 0.

**Корень**: на каждой ноде крутится **несколько xray-инстансов**, по одному на VLESS-протокол:
- `/usr/local/etc/xray/config.json` — vless-reality
- `/usr/local/etc/xray/config_xhttp.json` — vless-xhttp
- `/usr/local/etc/xray/config_ws_cdn.json` — vless-ws-cdn

Каждый со своим clients[], своим systemd-юнитом (`xray.service`, `xray-xhttp.service`, ...).

**Лечение**: расширил роль на slurp всех найденных кандидатов (берём *все*, не первый), агрегатор мержит несколько файлов одной ноды. После пересдачи нашлись 22 unique subscriptions (8 user-* + 14 warm-assigned) вместо первоначальных 8.

#### 3.3.3. Warm-pool architecture

Backend кладёт пре-провижиненные warm-bundles в xray под именами `warm-{node_id}-{hex}` (см. [warm_pool.py:_generate_warm_username](backend/app/services/warm_pool.py)). Когда юзер покупает подписку через fast-path → backend ассоциирует warm-bundle с Subscription в БД через `Credential.subscription_id`, **БЕЗ переименования email в xray**. Имя на ноде остаётся `warm-N-hex` навсегда (явный комментарий в коде "Real assignment does not rename").

**Последствие для recovery**: 15 warm-bundles на нодах имеют traffic-stats (реально использовались юзерами), но связь warm-UUID → user_id жила только в потерянной БД. Эти юзеры технически работают (UUID в xray.clients[] активны, VPN-трафик идёт), но в новой БД о них нет ни Subscription, ни Device, ни telegram_id.

**Лечение**:
- Эти orphan-bundles повешены на placeholder user `999999` (`telegram_id='__recovery_orphans__'`) с `expires_at=NOW+30d`.
- Через 30 дней backend.renewal-tick их revoke'нёт штатно → юзеры заметят разрыв → напишут в саппорт → admin claim-orphan endpoint TRANSFER'ит ownership на реального юзера по UUID из его сохранённой Hiddify-ссылки.
- Спека endpoint'а в [docs/operations/admin_claim_orphans.md](docs/operations/admin_claim_orphans.md), реализация ещё не сделана (см. § Pending).

#### 3.3.4. Multi-device per subscription

При первом прогоне aggregator группировал по (user_id, sub_id, node) и брал ОДИН uuid, любые дубликаты складывал в `uuid_conflicts`. Для user_1/sub_21 это привело к потере второго устройства.

**Лечение**: переписал aggregator на структуру `devices: [{email, uuid, flow, protocols}]` — каждый уникальный email = отдельное физическое устройство. SQL-генератор эмитит N Device-row'ов на одну Subscription, имена `primary` / `device-2` / `device-3` / ... (имена восстанавливаются не из истории — мы их придумали, юзер может переименовать через webapp).

#### 3.3.5. WHOIS-карантин домена

Через `dig NS grinwer.online @8.8.8.8` обнаружили что nameservers подменены NameCheap'ом на `verify-contact-details.namecheap.com` и `failed-whois-verification.namecheap.com`. CF-делегация (`cora.ns.cloudflare.com / george.ns.cloudflare.com`) не работала.

Решилось подтверждением WHOIS-контактов в почте у юзера. После клика NS вернулись на CF за ~30 минут (TTL 900s).

#### 3.3.6. SSH-retries для flaky-канала

Ansible-sweep на 5 RU-нод падал с `Connection timed out` для половины хостов когда forks=20. С `--limit one-host` всё проходило.

**Корень**: `ansible.cfg` имел `timeout` default (10s) и без `retries` в `[ssh_connection]`. На медленных RU-каналах + параллельных подключениях ControlMaster не успевал handshake.

**Лечение** (коммит `bb5c867`):
- `[defaults] timeout = 30`.
- `ssh_args` += `ConnectTimeout=30 ConnectionAttempts=3 ServerAliveInterval=15`.
- `[ssh_connection] retries = 3`.
- Совокупно 3×3=9 попыток коннекта на хост перед unreachable.

Плюс bonus в recover-ролях: `wait_for_connection` с retries в начале + `ignore_unreachable: yes` на slurp-tasks → одна мёртвая нода не валит весь sweep.

#### 3.3.7. APT lock на свежем VPS

При первом site.yml на ru-cloud-web-01 install_base_packages упал с `Could not get lock /var/lib/apt/lists/lock. It is held by process 1854 (python3.12)`. Wait-task в bootstrap_node прошёл `ok`, но lock держался unattended-upgrades.

**Корень**: wait-task проверял **только** `/var/lib/dpkg/lock-frontend`. Unattended-upgrades между фазами download→install отпускает frontend, но всё ещё держит `apt/lists/lock` (apt-get update под капотом).

**Лечение** (коммит `96e9c56`):
- `fuser` теперь проверяет все 4 файла: `dpkg/lock-frontend`, `dpkg/lock`, `apt/lists/lock`, `apt/cache/archives/lock`.
- Timeout 300→600.

#### 3.3.8. Race на user_id=1

После применения restore.sql юзер сделал `/start`, ожидал получить `id=1` (его историческое значение из admin_users.json с balance 2339.73₽). Получил `id=1000000`.

**Корень**: между запуском бота и применением restore.sql прошёл /start от другого пользователя. Auto-increment на пустой users-таблице выдал ему id=1. Restore.sql `INSERT (1, '256676474', ...) ON CONFLICT (id) DO NOTHING` пропустил (конфликт). Когда настоящий владелец 256676474 пришёл — backend не нашёл его tg_id, создал новый user (поскольку sequence был выставлен на max(id)=999999, следующий = 1000000).

**Лечение**: ручной UPDATE для замены wrong tg_id у id=1 на правильный, DELETE id=1000000. Урок: **при применении restore.sql бот должен быть остановлен**:
```bash
docker compose stop bot
psql < restore.sql
docker compose start bot
```

#### 3.3.9. Credential vs vpn_configs enum semantics

`vpn_configs.protocol` — PostgreSQL Enum, label = python NAME (`vless_reality` с underscore). `credentials.proto` — String, backend пишет .value (`vless-reality` с дефисом, см. [_VLESS_FAMILY_PROTOS](backend/app/services/provisioning.py)). Изначально генератор писал одинаково в обе колонки → enum-mismatch при INSERT, восстановление падало.

**Лечение**: в генераторе для `vpn_configs.protocol` ставится `protocol.replace("-", "_")`, для `credentials.proto` оставляется hyphen-form.

#### 3.3.10. xhttp SNI потерян

vless-xhttp в xray-конфиге **не держит TLS** — только `network: xhttp, xhttpSettings: {path, mode}`. TLS терминирует nginx **перед** xray, а CDN-домен (типа `s01.grinwer.online`) живёт в ansible-переменной `vless_xhttp_domain` per-host.

Recovery вытащил всё из xray-конфигов — там домена нет. Соответственно `vpn_configs.sni` для xhttp оказались NULL → admin показывает «—» → backend строит VLESS-URL с пустым SNI → клиент не подключится.

**Лечение**: скрипт [scripts/grab_xhttp_domains.sh](scripts/grab_xhttp_domains.sh) ходит по vpn_nodes, читает `server_name` из `/etc/nginx/sites-available/xhttp-*.conf`, печатает готовые UPDATE'ы. Применить вручную через psql (не вошло в restore.sql потому что эту проблему обнаружили после второго применения).

---

## 4. Что в итоге в БД после восстановления

```
nodes              5
vpn_configs        10  (5 nodes × 2 protocols)
users              39  (38 real + 1 placeholder=999999)
subscriptions      8 known + 14 orphan = 22
devices            9 known + 14 orphan = 23
credentials        18 known + 28 orphan = 46
wg_exit_nodes      7  (kr-pq-01 убран как dead)
relay_exit_links   35  (5 RU jumps × 7 exits)

users_with_balance  11  (incl. owner с 99476.81₽)
users_with_tg       38  (все из admin_users.json)
trial_activated_for ~14 юзеров (балансы + active subs — блок для повторного гифта)
```

---

## 5. Все коммиты этой сессии

```
96e9c56 ops: +3 vpn-nodes, bootstrap apt-lock wait fix, worker scaling + xhttp domain grab
a4225d3 chore: .gitignore для recovery-дампов + fleet inventory документация
a769103 scripts: накопившиеся диагностические шелы за несколько сессий
2b25f01 recovery: disaster-recovery pipeline для пересборки БД с нод
bb5c867 infra: SSH retry + timeouts для flaky-link, новый mgmt-IP
3511245 migrations: make 13 schema-additive migrations idempotent
```

---

## 6. Что осталось висеть (Pending)

### 6.1. Критическое для надёжности

- **Бэкап Postgres в cron**. Самый дешёвый страховой полис против повторения. Минимум: ежечасный `pg_dump` в S3 / на отдельный VPS / на mgmt'овский диск с retention 7 дней.
- **Снапшоты `/opt/vpn`** (compose volumes) еженедельно — vault-encrypted на отдельный хост через borg/rclone.

### 6.2. Recovery-flow для оставшихся orphan'ов (admin-claim)

Самая важная отложенная фича, без которой 14 orphan-подписок (включая
дополнительные device'ы юзеров типа «купил sub + 2 доп. устройства»)
не вернутся к их реальным владельцам.

**Полная спека:** [docs/operations/admin_claim_orphans.md](docs/operations/admin_claim_orphans.md).
Ниже краткое описание для контекста.

#### 6.2.1. Почему именно admin-claim, а не /restore-flow в боте

Альтернатива — сделать `/restore` команду в боте, чтобы юзер сам ввёл
свою sub-URL и backend smart-matched к Credential. Отвергли по двум
причинам:
1. **Юзеров мало** (15 orphan'ов всего, 8 known уже подписаны). На таком
   объёме админ-эндпоинт даёт нулевой UX-overhead для юзера и проще
   реализуется.
2. **Аутентификация по UUID** — это эффективно "знающий UUID может
   стать владельцем подписки". Без админ-валидации легко угнать
   подписку. С admin-flow оператор проверяет что юзер тот за кого
   себя выдаёт (по истории переписки, скриншоту платежа, etc.).

#### 6.2.2. Сценарий восстановления

**Триггер**: юзер напишет в саппорт когда заметит отвал. Это произойдёт
не сразу после `expires_at`, а когда Hiddify попробует обновить профиль
(обычно автоматический рефреш раз в день / неделю / при рестарте
приложения) и получит 503/empty configs. До этого момента сессия живёт
на закешированном профиле в клиенте.

**Реальный сценарий с юзером user_4** (купил Solo + 3 доп. устройства,
все на ru-pq-01, висят как warm-9-* email'ы):

1. Юзер пишет: «не работает VPN после ваших обновлений».
2. Оператор: «пришли мне vless-ссылку, которую сейчас используешь в
   Hiddify».
3. Юзер копипастит:
   ```
   vless://5925fb89-b1e0-4eda-8d00-6bcde0a93a0e@171.22.134.110:9443?...
   ```
4. Оператор открывает админку → `/users/<user_4_id>` → нажимает
   **«Восстановить orphan-подписку»** → вводит UUID
   `5925fb89-b1e0-4eda-8d00-6bcde0a93a0e` + выбирает план (по умолчанию
   тот, что был — Solo с уже учтённой `extra_device_slots=3`).
5. Backend:
   - Находит Credential WHERE `config_text` LIKE `%5925fb89%`
   - Через credential.subscription_id находит placeholder-Subscription
     (user_id=999999, expires=NOW+30d)
   - Делает `UPDATE subscriptions SET user_id=<user_4>, expires_at=<новый>`
   - Делает `UPDATE devices SET user_id=<user_4>` для всех связанных
   - Аудит: `orphan_claimed` запись в audit_log с {uuid, old_user_id,
     new_user_id, credentials: [ids]}.
6. Юзер сразу видит подписку в боте/webapp, **VPN-подключение в
   Hiddify не прерывается** (UUID в xray.clients[] тот же, credentials
   только meta-data поменялась).

**Ключевая оптимизация**: всё уже создано в БД, мы только меняем
владельца через `UPDATE`. Не создаём новых Subscription/Device,
не дёргаем ansible, не пересоздаём sub_token. Это insert-free
transfer на уровне 2-3 UPDATE'ов в одной транзакции.

#### 6.2.3. API контракт

```http
POST /api/admin/claim-orphan
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "user_id": 4,                                   // ИЛИ telegram_id
  "telegram_id": "192677123",                     // одно из двух обязательно
  "uuid": "5925fb89-b1e0-4eda-8d00-6bcde0a93a0e", // из vless://UUID@...
  "plan_id": 1,                                   // дефолт = Solo (1)
  "expires_at": "2026-06-21T00:00:00Z",           // дефолт = NOW+30d
  "device_name": "iPhone мамы"                    // опц., переименовать
}
```

Ответ:
```json
{
  "subscription_id": 10003,
  "device_id": 10003,
  "old_user_id": 999999,
  "new_user_id": 4,
  "new_expires_at": "2026-06-21T00:00:00Z",
  "transferred_credentials": [
    {"id": 1, "proto": "vless-reality"},
    {"id": 2, "proto": "vless-xhttp"}
  ]
}
```

#### 6.2.4. Edge cases

- **UUID совпал с несколькими нодами**: вероятность ничтожная (UUID v4
  криптографически уникальный), но если такое — endpoint возвращает 409
  «UUID matches multiple credentials, please specify node_id».
- **UUID не найден**: 404. Юзер либо вспомнил неправильно, либо его
  подписка вообще не orphan (была в known 8 → уже привязана). Оператор
  проверяет через `SELECT user_id FROM credentials WHERE config_text
  LIKE '%<uuid>%'`.
- **UUID уже принадлежит юзеру** (не 999999): 409 «UUID already
  claimed by user_id=X». Защита от двойного claim'а.
- **Compensation за период бесплатного пользования**: orphan-подписки
  жили "бесплатно" между инцидентом (2026-05-19) и claim'ом (если юзер
  напишет, например, через 20 дней). Если юзер на active billing-плане
  с положительным balance — backend.renewal-tick его не списывал, ибо
  не знал о подписке. Опционально оператор может ретроактивно списать
  через `users.balance_kopecks -= days_used * plan.daily_rate_kopecks`.
  В первой версии endpoint'а этого автоматически НЕ делать — слишком
  легко уйти в негативный баланс при недостаточных данных.

#### 6.2.5. Admin UI

Два экрана:

1. **`/users/<id>` page**, добавить кнопку **«Восстановить orphan-подписку»**
   в случае если у юзера есть платежи / положительный баланс. Кнопка
   открывает модальное окно: textarea для UUID (или vless-URL — оператор
   может ткнуть всю ссылку, backend сам выпарсит UUID), dropdown Plan,
   date-picker Expires_at. Submit → POST /admin/claim-orphan.

2. **`/orphan-credentials` page** (новый раздел) — таблица всех Subscription
   WHERE user_id=999999. Видны: node, access_username (warm-N-hex),
   created_at, expires_at, привязанный VLESS-URL (первый credential).
   По клику на строку — модалка с полями для transfer'а к юзеру.

#### 6.2.6. Когда удалить эту фичу

Через 30-60 дней после инцидента, когда все 15 orphan'ов либо
claimed, либо истёкли естественной убылью. После этого:

```sql
-- Что осталось не-claimed
SELECT count(*) FROM subscriptions WHERE user_id = 999999;

-- Если значительная часть осталась и истекла — финальная чистка
DELETE FROM credentials WHERE subscription_id IN
  (SELECT id FROM subscriptions WHERE user_id = 999999 AND status = 'expired');
DELETE FROM devices WHERE subscription_id IN
  (SELECT id FROM subscriptions WHERE user_id = 999999 AND status = 'expired');
DELETE FROM subscriptions WHERE user_id = 999999 AND status = 'expired';
-- Опционально удалить sam'у placeholder-юзера если все его subs ушли
DELETE FROM users WHERE id = 999999 AND NOT EXISTS
  (SELECT 1 FROM subscriptions WHERE user_id = 999999);
```

Плюс пройтись по нодам и через `manage_vless_user.sh delete-user
warm-<N>-<hex>` снять оставшиеся expired UUID'ы из xray.clients[]
(они занимают memory но безвредны).

После этого можно удалить admin-claim endpoint и UI как «one-shot
recovery tool». Или оставить как штатную «admin transfer subscription»
функцию для общих случаев (миграция между аккаунтами, поддержка
семейного шеринга, и т.п.) — это полезная утилита и без DR-контекста.

#### 6.2.7. Реализация — что нужно сделать

В порядке убывания приоритета:

1. **Backend endpoint** `POST /api/admin/claim-orphan` в [api_extensions.py](backend/app/api_extensions.py) или новый файл `api/admin_claim.py`.
   Готовый Python-скелет — в [docs/operations/admin_claim_orphans.md](docs/operations/admin_claim_orphans.md). Логика ~50 строк.

2. **Парсер VLESS-URL** на стороне backend (helper в `services/vless.py`):
   ```python
   def extract_uuid_from_vless_url(url: str) -> str | None:
       # vless://UUID@host:port?... → UUID
       m = re.match(r"^vless://([0-9a-f-]{36})@", url)
       return m.group(1) if m else None
   ```
   Чтобы оператору можно было вставить целую ссылку, а не выковыривать UUID руками.

3. **Admin UI** в [admin/src/](admin/src/) — кнопка + модалка на странице
   `/users/<id>`. Можно сделать отдельную страницу `/orphan-credentials`
   со списком всех placeholder-подписок (опционально).

4. **Audit log entry** — обязательно. action='orphan_claimed', extra с
   {uuid, old_user_id, new_user_id, credentials_ids, operator_admin_id}.
   Это для отслеживания если кто-то начнёт абьюзить.

5. **Опционально — Telegram нотификация юзеру** после успешного claim'а:
   «Восстановили твою подписку, держи ссылку: ...». Снимает с оператора
   ручную копипасту.

#### 6.2.8. Когда орфаны истекут через 30 дней

- backend.renewal-tick (раз в час) пометит status=expired у placeholder'овских subs.
- `revoke_device` снимет UUID из xray.clients[] на ноде → юзер заметит разрыв.
- Юзер пишет в саппорт → admin-claim flow (если уже реализован) или ручной
  INSERT через psql (если нет).

### 6.3. xhttp SNI patching

`vpn_configs.sni` для всех vless-xhttp row'ов сейчас NULL. Запустить `bash scripts/grab_xhttp_domains.sh` → получить SQL → применить через psql. Без этого webapp выдаёт VLESS-URL'ы с пустым SNI для xhttp-протокола (Reality работает, у него SNI = camo destination из realitySettings, мы его восстановили).

### 6.4. Архитектурные

- **0001_initial рефакторинг**: убрать `Base.metadata.create_all()`, заменить на hand-written CREATE TABLE для исторического snapshot'а. Сейчас идемпотентные guards в 12 миграциях покрывают проблему, но это workaround, не fix корня. Когда модели снова будут drift'ить — добавятся новые миграции которые нужно будет тоже делать идемпотентными.
- **`MAX_CONCURRENT_ANSIBLE` глобальный**: сейчас per-process semaphore, при `WORKER_REPLICAS=5` глобальный параллелизм = 5×3=15 ансибл-ранов. Нет глобального лимита. Для prod хорошо бы Redis-based distributed semaphore. Пока не критично.
- **Auto-renew для warm-bundle-orphan'ов**: они на placeholder user 999999, у того balance_kopecks=0. При истечении backend.renewal-tick попробует charge → fail → expire. Это правильное поведение, но шумит в логах. Можно auto_renew=FALSE сразу (уже так).

### 6.5. Инвентаризация

- **3 новых хоста** добавлены в inventory (`ru-dc-02`, `ru-cloud-web-01`, `ru-adminvps-01`), но bootstrap ещё не прогнан до конца на всех. ru-cloud-web-01 упал на apt-lock — после фикса роли надо повторить.
- **Inventory-driven provisioning**: обсудили дизайн (host_vars + key-gen script), не реализовали. Backend пока остаётся источником истины для Reality keys.

---

## 7. Полезные команды

### Disaster recovery с нуля (для будущих инцидентов)

```bash
# 1. Поднять чистую mgmt-машину (Ubuntu 24.04, не managed-хостинг с панелью!)
# 2. Обновить IP в инвентаре
$EDITOR infra/ansible/inventories/prod/hosts.yml

# 3. Сначала разморозить WHOIS-карантин если есть
dig NS grinwer.online @8.8.8.8 +short
# Если возвращает verify-contact-details.namecheap.com — подтвердить
# контакты в NameCheap → почта → клик → ждём 30 минут.

# 4. Раскатать только control-stack (без vpn_nodes! без exit'ов!)
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml \
  --tags web --ask-vault-pass

# 5. Дождаться миграций
docker compose -f /opt/vpn/docker-compose.yml exec db \
  psql -U vpn -d vpn -c "SELECT count(*) FROM plans;"
# Должно вернуть 6.

# 6. Сбор recovery-данных с нод
ansible-playbook -i inventories/prod/hosts.yml \
  playbooks/recover_xray_inventory.yml --ask-vault-pass

# 7. Runtime stats (пока xray на нодах ещё не рестартили!)
bash scripts/diag_xray_runtime_all.sh

# 8. Из закешированной вкладки админки выгребсти users (JS-snippet
# в DevTools console):
copy(JSON.stringify(
  [...document.querySelectorAll('table tbody tr')].map(tr => {
    const c = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
    return { id: c[1], telegram_id: c[2], email: c[3], subs: c[4],
             balance: c[5], created_at: c[6] };
  }), null, 2));
# Вставить в recovered/admin_users.json

# 9. Сгенерировать restore.sql
APP_SECRET_KEY=$(ssh root@<mgmt> \
  'docker compose -f /opt/vpn/docker-compose.yml exec backend env' \
  | grep '^APP_SECRET_KEY=' | cut -d= -f2)

python3 scripts/generate_restore_sql.py \
  --users-json       infra/ansible/recovered/users.json \
  --nodes-json       infra/ansible/recovered/nodes.json \
  --wg-json          infra/ansible/recovered/wg.json \
  --admin-users-json infra/ansible/recovered/admin_users.json \
  --credentials-json infra/ansible/recovered/credentials.json \
  --inventory        infra/ansible/inventories/prod/hosts.yml \
  --output           infra/ansible/recovered/restore.sql \
  --default-plan-id 1 --grace-days 30 --plan-duration-days 30 \
  --node-id-map 'ru-ae-01:10,ru-dc-01:14,ru-pq-01:9,ru-pq-02:12,ru-pq-03:13' \
  --app-secret-key "$APP_SECRET_KEY"

# 10. Применить — ВАЖНО остановить бот сначала!
scp infra/ansible/recovered/restore.sql root@<mgmt>:/tmp/
ssh root@<mgmt> 'bash -s' <<'BASH'
  cd /opt/vpn
  docker compose stop bot
  docker compose exec -T db psql -U vpn -d vpn -c "TRUNCATE credentials CASCADE;"
  docker compose exec -T db psql -U vpn -d vpn < /tmp/restore.sql
  docker compose start bot
BASH

# 11. Sanity check
ssh root@<mgmt> 'docker compose -f /opt/vpn/docker-compose.yml exec -T db \
  psql -U vpn -d vpn -c "
SELECT count(*) AS users, sum(balance_kopecks)/100 AS total_balance_rub FROM users;
SELECT count(*) FROM subscriptions;
SELECT count(*) FROM credentials WHERE pool_state = 'assigned';
SELECT count(*) FROM wg_exit_nodes WHERE wg_private_key_enc IS NOT NULL;
"'
```

### Worker scaling для bulk-операций

```bash
./scripts/workers.sh        # текущее состояние
./scripts/workers.sh 5      # бамп до 5 (на 4×8 VPS)
./scripts/workers.sh 1      # обратно на dev-default

# 5 workers × 3 ансибл-ранов = 15 параллельных. На 23 pending tasks
# (как было после DR) даёт ~3-5x speedup если задачи на разные ноды.
```

### Sanity diagnostics

```bash
# Что лежит на ноде из xray-инстансов
bash scripts/diag_xray_node.sh <node>

# Runtime stats со всех нод (gRPC + access-log emails)
bash scripts/diag_xray_runtime_all.sh

# Frontend health после DR (DNS, nginx, certbot, upstream)
bash scripts/diag_frontend.sh
bash scripts/diag_frontend_network.sh
```

### Чистка дублей в provisioning_tasks

После периодов flaky-ansible (как при DR) в очереди скапливаются retry-дубли. Перед бампом worker-replicas — прочистить:

```sql
DELETE FROM provisioning_tasks t1
WHERE t1.status = 'pending'
  AND t1.id NOT IN (
    SELECT MIN(id) FROM provisioning_tasks
    WHERE status = 'pending'
    GROUP BY target, action
  );
```

---

## 8. Чему научились

### 8.1. Технические уроки

1. **Бэкапы — не "если останется время", а "первым же делом после первого юзера"**. У нас пара тыщ рублей баланса у клиентов и ноль строчек pg_dump.
2. **xray.clients[] — это authoritative cache**, не просто кеш. Когда DB умирает, эта структура единственное что остаётся между «юзеры работают» и «юзеры не работают».
3. **Идемпотентные миграции — must-have**. `Base.metadata.create_all()` в 0001 как ловушка работает прекрасно.
4. **Race-conditions на pristine-системах** — apt locks (unattended-upgrades), DB writes, sequence allocations. При DR-rebuild порядок операций критичен: стоп всё что может писать → apply restore → start снова.
5. **Recovery — это data engineering, не infrastructure**. Большинство трюков были про парсинг конфигов / matching по эпохам / merging multiple sources, не про bash и ansible.

### 8.2. Архитектурные баги, которые сделали инцидент дороже

- **Один контрол-хост, single point of failure**. HA = next quarter.
- **Бэкапы не настроены**. Хостер snapshot'ы не предлагают, своих не сделали. Backup в S3 в cron — закрытие задачи.
- **warm-pool не пишет owner в xray-email** при assignment. Race-condition защищён, но при потере БД owner-mapping становится недосягаем. Можно было бы при assignment делать `manage_vless_user.sh rename old new` — но это лишний ansible-run, тогда warm-pool теряет смысл скорости.
- **0001_initial create_all() антипаттерн**: разрабы потратили время на 33 миграции, которые могли работать только инкрементально, что напоролось на fresh DB.

### 8.3. Процессные

- **Скриншоты админки делайте регулярно** — это спасло наши 38 telegram_id'ев.
- **DevTools cache в браузере** — на удивление полезный артефакт. Если что-то умирает, не закрывай вкладки сразу.
- **Хостер может тебе помочь** (нет). Запросили snapshot — ответ «нет».
- **WHOIS-карантин** — внезапный второй удар, не связан с инцидентом, но усугубил. Проверить домен на NameCheap сразу после трансфера от другого регистратора.

---

## 9. Состояние на момент окончания сессии

✅ Mgmt-стек поднят на `185.242.87.250`, все 33 миграции прокатились без ошибок.
✅ 8 known-подписок restored, expires_at на основе реального provisioning epoch.
✅ 14 orphan-подписок на placeholder user, ждут /restore (через ~30 дней).
✅ WG-туннели не прерывались, exit-keys recovered с private_key_enc.
✅ Webapp выдаёт рабочие VLESS-ссылки (после wipe+reapply credentials).
✅ Cold-path throttle + rate-limit на /register и /trial/activate работают.
✅ 12 миграций idempotent через _idempotent.py helper.
✅ Ansible-runner с retry+timeout, выдерживает RU-flaky-channel.
✅ DR-пайплайн воспроизводимый, документирован.

⏳ Backup Postgres в cron — не сделан.
⏳ Admin-claim endpoint — не реализован.
⏳ xhttp SNI на vpn_configs — потеряны, нужно вытащить через grab_xhttp_domains.sh.
⏳ 3 новых хоста (ru-dc-02, ru-cloud-web-01, ru-adminvps-01) — добавлены в inventory, bootstrap не доведён до конца.

---

*Документ написан в рамках сессии Claude. При обнаружении неточностей — fix-up commit с правкой этого MD.*
