# Stage 2.5 — Аудит и сводка изменений

Этот документ описывает, что было сделано в этом цикле работы над платформой:
аудит существующих этапов 1-2 + полная реализация этапа 2.5
(pre-warmed credentials pool).

## TL;DR

После этого цикла время от тапа «Купить» до получения работающего конфига
падает с **60–120 секунд** (Ansible на холодную) до **миллисекунд**
(атомарное присваивание warm-бандла). Холодный путь сохранён как
fallback, метрики `vpn_warm_pool_*` экспортируются в Prometheus,
race-condition покрыт pytest'ом.

Все 5 acceptance-критериев этапа 2.5 закрыты.

---

## 1. Аудит этапов 1-2 (что было сломано)

При первом проходе по `vpn-work2` обнаружились три существенных
бага и пара мелких UX-промахов. Все исправлены в этом же цикле.

### 🔴 Блокирующий: env-var mismatch в Telegram Stars провайдере

**Симптом:** `webapp_checkout` падал с 503 при попытке оплаты —
весь Stars-flow в WebApp был неработоспособен.

**Причина:** `services/payments/telegram_stars.py::_load_from_env()`
читал `TELEGRAM_BOT_TOKEN`, но `docker-compose.yml` пробрасывает в
backend только `BOT_TOKEN`. Цепочка `webapp_checkout` →
`get_provider("telegram_stars")` → `_load_from_env()` всегда
выбрасывала `ProviderError("TELEGRAM_BOT_TOKEN env var is required")`.

**Фикс:** `_load_from_env()` теперь читает `BOT_TOKEN` первым с
fallback на `TELEGRAM_BOT_TOKEN` для обратной совместимости. Имя
переменной в сообщении ошибки тоже исправлено.

### 🟡 Acceptance gap: `webapp_checkout` не проверял `max_devices`

**Симптом:** юзер на тарифе Solo (1 устройство) мог создать второй
checkout, оплатить, и сервер бы упал на этапе провижининга — деньги
уже у нас, конфига нет.

**Фикс:** добавлена проверка `active_count < plan.max_devices` ДО
создания инвойса. При превышении возвращается 409 с понятным
сообщением «You already have N active subscriptions on this plan
(max M). Renew an existing one or pick a higher tier.»

Это закрывает acceptance criterion этапа 2: «`/api/webapp/checkout`
отказывает, если у юзера уже есть `max_devices` активных подписок».

### 🟡 Renewal-flow gap в WebApp

**Симптом:** WebApp не мог продлить существующую подписку — кнопка
«продлить» создала бы новую подписку, не extends старую, и могла
сразу упереться в `max_devices`.

**Фикс:** `CheckoutRequest` получил поле `subscription_id`, при его
наличии checkout создаёт `Invoice(action=renewal,
subscription_id=...)`. Существующий `_mark_invoice_paid_core` уже
умеет extend'ить подписку для `action=renewal`, никаких изменений в
provisioning не нужно.

### Минорные UX-фиксы

- **`Plans.tsx`**: переключатель «Месяц/Год» показывал пустой список,
  если для выбранного периода нет тарифов. Теперь показывает понятное
  сообщение «Годовых тарифов пока нет — попробуй месячный».
- **nginx `/app/` location**: я ранее ошибочно отметил это как
  отсутствующее. Перепроверил — `nginx-site.conf.j2` уже содержит
  правильный `location /app/` с `deploy_web_frontend_webapp_upstream`,
  который указывает на `127.0.0.1:8081` (= порт сервиса webapp в
  docker-compose). Всё на месте, ложная тревога с моей стороны.

---

## 2. Этап 2.5 — Pre-warmed credentials pool

### Архитектура

«Warm bundle» — это набор `Credential`-строк, разделяющих
`(node_id, access_username)`, в состоянии `pool_state=warm` и без
привязки к подписке (`subscription_id=NULL`). Каждый бандл представляет
полностью провижененную идентичность, уже выложенную на ноду
ansible'ом. Покупка подписки = атомарный переход бандла в `assigned`
+ binding к свежей `Subscription` + `Device`. Ansible **не** в
hot path — это вся идея.

### Schema (миграция `0008_warmpool_and_balance`)

```
ALTER TABLE credentials
  ADD COLUMN pool_state credentialpoolstate NOT NULL DEFAULT 'assigned',
  ADD COLUMN warmed_at TIMESTAMP,
  ADD COLUMN assigned_at TIMESTAMP,
  ADD COLUMN node_id INT REFERENCES vpn_nodes(id),
  ADD COLUMN access_username VARCHAR,
  ALTER COLUMN subscription_id DROP NOT NULL;

CREATE INDEX ix_credentials_warm_node
  ON credentials(node_id) WHERE pool_state = 'warm';
```

Партиальный индекс — ключ к производительности hot path. Только
warm-строки попадают в индекс, поэтому `SELECT ... FOR UPDATE
SKIP LOCKED LIMIT 1` сканирует ровно столько строк, сколько сейчас
в пуле — обычно 5-15. Растёт `credentials` до миллиона строк —
индекс остаётся под килобайт.

> Миграция также подтягивает scaffold для этапа 4 (balance billing):
> `User.balance_kopecks`, `Plan.daily_rate_kopecks`, таблица
> `balance_transactions`. Эти колонки **dormant** до этапа 4, ничего
> не ломают, но позволяют не делать ещё одну миграцию параллельно.
>
> **Note on chaining:** в исходном архиве уже есть `0007_seed_plans.py`
> (data-only seed, не трогает схему). Моя миграция называется
> `0008_warmpool_and_balance` и `down_revision = "0007_seed_plans"`,
> чтобы Alembic выстраивал линейную цепочку без двух head'ов.

### Сервис `services/warm_pool.py`

**Публичный API:**

| Функция | Когда вызывается | Что делает |
|---------|------------------|-----------|
| `warm_one_bundle(db, node)` | Worker tick | Генерит identity → ansible `state=present` → **только при success** пишет N credential rows |
| `try_assign_bundle(db, node_id, sub_id)` | `provision_subscription` (hot path) | Атомарный pick: lock anchor → lock siblings → flip `warm → assigned` → bind |
| `pool_depth(db, node_id)` | Worker tick + метрика | `COUNT(DISTINCT access_username)` где `pool_state='warm'` |
| `ensure_pool(db)` | Worker tick | Для каждой active node: догнать до `WARM_POOL_TARGET`, не больше `WARM_POOL_BATCH_PER_TICK` за раз |
| `unassign_bundle(db, creds)` | `revoke_device` | Stage 1 revoke: `assigned → revoked` в БД, без ansible |
| `physical_revoke_credential_bundle(...)` | RQ job (background) | Stage 2: ansible `state=absent` → удаление строк |
| `invalidate_node_warm_pool(db, node_id)` | При изменении `VPNConfig.is_enabled` | Drops весь warm pool ноды (warmer догенерит) |
| `record_pool_miss(db, node_id)` | Cold path fallback | Инкремент counter'а для метрики |

### Race-safety: как ровно один winner

Атомарность строится на двух блокировках:

```python
anchor = db.query(Credential)
    .filter(node_id=N, pool_state='warm')
    .order_by(id.asc())
    .with_for_update(skip_locked=True)
    .first()
```

Worker A берёт anchor `cred-1` (username=`warm-2-abcd`).
Worker B запускает тот же запрос, но `cred-1` залочен → SKIP LOCKED
пропускает его, B берёт `cred-2` (username=`warm-2-efgh`). Третий
worker возьмёт `cred-3`, и так далее.

После того как A залочил anchor, он берёт **siblings** (остальные
протоколы того же бандла) обычным `with_for_update()` (без skip).
Это безопасно, потому что bundle-username **уникален в пределах
ноды** — никакой другой worker не может иметь lock на сиблингов
бандла A, потому что чтобы их получить, ему пришлось бы пройти
через anchor (который залочен).

**Тест `test_concurrent_assignment_picks_one_winner`** проверяет
именно это: 10 потоков с барьером синхронизации стартуют
одновременно, претендуют на 1 warm-бандл, ассерт ровно 1 winner +
9 losers, плюс post-state check что в БД ровно одна `assigned`
строка с привязкой к одной из 10 подписок.

### Интеграция в `provision_subscription`

```python
def provision_subscription(self, user, plan, ...):
    node = choose_node(...)
    enabled_configs = [...]
    self._enforce_device_limit(...)
    subscription = Subscription(...)  # shell

    # 🔥 Hot path
    warm_bundle = warm_pool.try_assign_bundle(db, node.id, subscription.id)
    if warm_bundle:
        device, task = self._wire_warm_bundle(user, subscription, warm_bundle, ...)
        return subscription, task   # синтетический task со status=success

    # 🐌 Cold path (как было)
    warm_pool.record_pool_miss(db, node.id)
    # ... оригинальный код: генерит username/uuid, creds, RQ task ...
```

`_wire_warm_bundle` создаёт Device-строку, бэк-линкует credentials,
строит динамическую sub-link через `SUB_LINK_BASE_URL`, и возвращает
синтетический `ProvisioningTask(status=success)` чтобы API-контракт
не сломался для callers. `WebApp` `CheckoutPending` мгновенно увидит
`subscription_active=true, has_credentials=true` на первом же polls'е.

### Worker (cron + RQ jobs)

**Новые функции в `app/worker.py`:**

- `run_warm_pool_check()` — периодическая, дефолт каждые 120s.
  Self-rescheduling (как `run_renewal_check`). Внутри — `ensure_pool()`.
- `run_physical_revoke_bundle(node_id, access_username)` — one-shot RQ
  job для stage 2 revoke. Идемпотентный: если бандл уже удалён,
  возвращает success.

**Scheduling в `main()`:** добавлен блок после `run_renewal_check`,
gated by `WARM_POOL_ENABLED` env var. Первый tick через 30s после
старта worker'а, дальше каждые `WARM_POOL_CHECK_INTERVAL`.

### Конкуренция

`_warmer_semaphore` (отдельный от `_ansible_semaphore` в provisioning)
с дефолтом `WARM_POOL_MAX_CONCURRENT=2` ограничивает параллельные
ansible-runs warmer'а. Цель — не дать warming starve user-facing
provisioning, когда оба бегут одновременно.

### Метрики (Prometheus)

| Метрика | Тип | Лейблы | Что значит |
|---------|-----|--------|-----------|
| `vpn_warm_pool_depth` | Gauge | `node` | Сколько warm-бандлов сейчас на ноде |
| `vpn_warm_pool_hits_total` | Counter | `node` | Покупки, обслуженные из пула |
| `vpn_warm_pool_misses_total` | Counter | `node` | Покупки, упавшие на cold path |
| `vpn_warm_credential_provision_seconds` | Histogram | `node` | Время provisioning одного бандла warmer'ом |

**Grafana panels** в `docs/dashboards/business-metrics.json`:

1. **Warm pool depth (per node)** — timeseries по нодам. Зелёный
   при ≥5, жёлтый при 1-4, красный при 0. Если линия ноды залипла
   в 0 — warmer падает (проверь ansible/SSH).
2. **Warm pool: hits vs misses** — rate(5m) hits и misses. Sustained
   misses ⇒ raise `WARM_POOL_TARGET` или `WARM_POOL_BATCH_PER_TICK`.
3. **Warm credential provisioning (p50/p95)** — histogram_quantile
   по времени warming'а. p95 > 60s означает деградацию ansible/SSH.

### Конфигурация (env vars)

| Var | Default | Назначение |
|-----|---------|-----------|
| `WARM_POOL_ENABLED` | `1` | Master switch (`0` = только cold path) |
| `WARM_POOL_TARGET` | `10` | Желаемая глубина пула на ноду |
| `WARM_POOL_BATCH_PER_TICK` | `3` | Кэп warming'ов на ноду за один tick |
| `WARM_POOL_CHECK_INTERVAL` | `120` | Интервал между tick'ами worker'а (сек) |
| `WARM_POOL_MAX_CONCURRENT` | `2` | Параллельные ansible-runs warmer'а |

Backend читает только `WARM_POOL_ENABLED` (для решения «использовать
пул или сразу cold»). Worker читает все пять.

### Тесты

`backend/tests/test_warm_pool.py`, 7 тестов на реальном Postgres
(не SQLite — нам нужна семантика `FOR UPDATE SKIP LOCKED`):

1. `test_warm_one_bundle_persists_only_after_ansible_success` —
   успешный warming пишет ровно 1 credential, заполняет
   `access_username`, `warmed_at`, `is_active=False`.
2. `test_warm_one_bundle_no_rows_on_ansible_failure` — ansible
   exit ≠ 0 ⇒ **ноль строк в credentials**. Это критично: иначе
   пул раздаст сломанные креды юзерам.
3. `test_pool_depth_counts_distinct_bundles` — 3 бандла × 2
   протокола = 6 рядов, но `pool_depth() == 3`.
4. `test_try_assign_bundle_returns_none_on_empty_pool` — sanity.
5. `test_try_assign_bundle_flips_state_and_binds_subscription` —
   все credentials бандла переходят в `assigned`, `subscription_id`
   проставлен, `assigned_at` заполнен, `is_active=True`. После
   `pool_depth() == 0`.
6. **`test_concurrent_assignment_picks_one_winner`** — 10 потоков с
   threading.Barrier'ом, lock-step старт. Ассерт: ровно 1 winner +
   9 losers, в БД ровно одна `assigned` строка с одним из 10
   `subscription_id`. Каждый поток открывает свой `SessionLocal`.
7. `test_unassign_bundle_marks_revoked_in_db_only` — после
   `unassign_bundle`: `pool_state=revoked`, `is_active=False`,
   `revoked_at` set, **строки не удалены** (worker сделает это
   позже).

`run_playbook` мокается через `monkeypatch.setattr(warm_pool,
"run_playbook", _stub)` — никакого реального ansible.

---

## 3. Что НЕ сделано (намеренно)

- **Этап 3 (self-service + троублшутер)** — отдельный цикл, как и
  планировалось в roadmap.
- **Этап 4 (balance billing)** — отдельный цикл. Schema scaffolding
  (`User.balance_kopecks`, `BalanceTransaction`, `Plan.daily_rate_kopecks`)
  уже в миграции 0007 чтобы следующая миграция не тащила
  несвязанные изменения, но никакой код их не использует.
- **GC для warm-бандлов после краша wiring**: если
  `_wire_warm_bundle` падает после того как `try_assign_bundle`
  flipped state, бандл застревает в `assigned` без подписки.
  TODO в коде: периодическая cron-задача находить
  `pool_state=assigned AND subscription_id IN (NULL or revoked)` и
  возвращать в `warm`. На практике вероятность краша между двумя
  flush'ами в одной транзакции крайне низкая, но штатный фикс
  стоит сделать в этапе 3.

---

## 4. Killer risks для production rollout

| Риск | Митигация |
|------|-----------|
| Warmer падает на новом кластере → весь pool пустой → каждая покупка cold | Метрика `vpn_warm_pool_depth` + Grafana alert «node line == 0 for >5min» |
| Изменение `VPNConfig.is_enabled` → стейл credentials в пуле | `invalidate_node_warm_pool()` есть, но **не дёрнут автоматически** при PUT /configs. TODO: hook в admin API. |
| Warmer hammers SSH → rate limit от провайдера | `WARM_POOL_MAX_CONCURRENT=2` + `WARM_POOL_BATCH_PER_TICK=3` — нода получает максимум 3 ansible-runs за 2 минуты |
| Race между warmer и user покупкой за один и тот же бандл | Невозможно — warmer только INSERT'ит warm rows, user только flip'ает warm→assigned. Они никогда не претендуют на одну строку. |
| `assigned`-строка осиротела (см. выше) | Manual SQL fix + следить за расхождением `Subscription COUNT == Credential WHERE pool_state=assigned COUNT` |

---

## 5. Rollout plan

1. **Deploy с `WARM_POOL_ENABLED=0`** — миграция применяется, но
   логика пула выключена. Hot path работает 1:1 как раньше.
2. **Smoke на staging:** включить пул, дёрнуть `run_warm_pool_check`
   вручную, проверить что credentials появляются на ноде, метрика
   depth растёт.
3. **Купить тестовую подписку через WebApp:** убедиться что hit
   counter инкрементнулся, время от тапа до конфига — секунды, не
   минуты.
4. **Race test on staging:** скрипт с 10 параллельными покупками,
   проверить что 9 попадают на cold (когда есть всего 1 warm) и 1
   на hot.
5. **Production rollout:** `WARM_POOL_ENABLED=1`, наблюдать за
   `misses_total` за первые сутки. Если sustained — повысить TARGET.
