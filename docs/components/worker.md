# Worker

Отдельный контейнер, отдельный образ (`backend/Dockerfile.worker`), но тот же пакет `backend/app/`. Запускается через `python -c "from app.worker import main; main()"` — **не** через `python -m app.worker`, потому что RQ ре-импортирует модуль под каноническим именем `app.worker`, и если первый импорт был как `__main__`, то `prometheus_client.Counter` ловит двойную регистрацию метрик и каждый job падает с «Duplicated timeseries in CollectorRegistry». Это зафиксировано в комментарии `backend/app/worker.py:17-25` — не возвращайтесь к `python -m`.

Вся тяжёлая работа backend'а живёт тут:

- прогон ansible-playbook'ов (провижининг, revoke, migrate, node spawn);
- периодические тики (warm pool, autoscale, drain, renewal, balance charge);
- бот-нотификации (записываются сюда, читаются ботом).

## Очередь и транспорт

RQ поверх Redis, одна именованная очередь `vpn-provisioning` (имя задаётся `RQ_QUEUE`, default см. `worker.py:827`). Соединение строится из `REDIS_URL` → `Redis.from_url(url)`, пингуется при старте (`worker.py:828-829`).

```
backend process          redis              worker process
     │                     │                    │
     │ queue.enqueue(...)  │                    │
     ├────────────────────►│                    │
     │                     │◄───────────────────┤ blpop
     │                     │                    │ → run_provisioning_task(id)
     │                     │                    │   orchestrator.run_task()
     │                     │                    │   ansible-playbook ...
```

RQ-scheduler активирован через `worker.work(with_scheduler=True)` (`worker.py:928`) — это и позволяет использовать `queue.enqueue_in(delta, func)` для отложенных задач.

Бэкенд и воркер — **разные процессы в разных контейнерах**. Backend НЕ должен поднимать Worker, воркер не должен обслуживать HTTP-роуты.

### Миграции: backend владеет

Backend прогоняет `alembic upgrade head` на старте (`main.py:18`). Воркер — нет, через `SKIP_MIGRATIONS=1` в `docker-compose.yml:103`. Комментарий в compose-файле объясняет почему: два процесса, обa зовущих alembic, гоняют гонку на `alembic_version` advisory lock, проигравший висит вечно до попадания в `Worker.work()`, очередь становится, все provisioning-задачи застревают в `pending`. **Не снимайте skip.**

Внутренняя функция `main()` в воркере тоже вызывает `run_migrations()` (`worker.py:831-835`), но в нормальном прод-режиме `SKIP_MIGRATIONS=1` заставляет её сделать no-op (это поведение зависит от реализации в `backend/app/migrations.py`).

## Два типа работы

### 1. Unit-of-work задачи (единичные)

`run_provisioning_task(task_id, node_id=None)` (`worker.py:780`) — типичная RQ-job'а:

1. Backend создаёт строку в `provisioning_tasks` (`action='provision_device'`, `'revoke_device'`, `'spawn_node'`, `'migrate_subscription'`, …).
2. Backend дёргает `queue.enqueue('app.worker.run_provisioning_task', task.id, node_id)` → RQ кладёт job в Redis.
3. Воркер подхватывает, грузит строку, создаёт `ProvisioningOrchestrator` и делегирует `orchestrator.run_task(task, node=node)`.
4. Orchestrator исполняет action, пишет в `task.result` / `task.error_message`, двигает `task.status`.

Детали — `components/provisioning.md`.

### 2. Self-rescheduling cron-тики

Вся периодика — **не через rq-scheduler в явном виде и не через внешний cron**, а через паттерн «функция в конце своего тела снова ставит себя в очередь через `queue.enqueue_in(timedelta(...), 'app.worker.run_XXX_tick')`». Шесть штук:

| Функция                    | Default interval             | Что делает                                       |
|----------------------------|------------------------------|---------------------------------------------------|
| `run_autoscale_tick`       | `AUTOSCALE_INTERVAL=300`     | Проходит по server_pools, решает по utilization, спавнит новые ноды |
| `run_drain_tick`           | `DRAIN_TICK_INTERVAL=600`    | Отмечает ноды как `draining`, мигрирует их subs, уничтожает пустые |
| `run_renewal_check`        | `RENEWAL_CHECK_INTERVAL=300` | Expire-ит подписки, инициирует renewal reminders, hard-revoke после grace |
| `run_warm_pool_check`      | `WARM_POOL_CHECK_INTERVAL=120` | Топит warm pool на каждой активной ноде до `WARM_POOL_TARGET` |
| `run_balance_charge_tick`  | `BALANCE_CHARGE_INTERVAL=3600` | Renew balance-подписок, expire auto_renew=False, auto-unfreeze, clawback trial |
| `run_traffic_stats_tick`   | `TRAFFIC_STATS_INTERVAL=300` | SSH на каждую active/draining ноду, читает xray stats + sharing violations → `node_traffic_samples` + `AuditLog`. Per-protocol breakdown в `details` содержит список `access_username` (а не просто count) — админка читает последний sample через `GET /api/nodes/{id}/users`. После persist вызывается Phase D `detect_traffic_drops()` — пассивный детектор ТСПУ-блокировок (gated через `TRAFFIC_DROP_ENABLED=1`) |
| `run_pending_rescue_tick`  | `PENDING_RESCUE_INTERVAL=60` | Сканирует `ProvisioningTask.status=pending` старше `PENDING_RESCUE_AGE` секунд и re-enqueue'ит через `enqueue_task`. Дедуп по `job_id=provision-<task_id>` — если задача уже в RQ, это no-op. Закрывает дыру, когда `run_task_async` закоммитил row, но `enqueue_task` упал (транзиентный Redis hiccup, serialization issue) — до этого фикса такие задачи висели в pending до следующего рестарта бэкенда (`reset_stuck_tasks` в main.py срабатывает только на boot). Метрика: `vpn_provisioning_pending_rescue_total` инкрементится на каждый rescue |

### Bootstrap при старте воркера

В `main()` (`worker.py:813-928`) каждая тика первый раз ставится в очередь через `enqueue_in(min(interval, X), ...)` — где `X` маленькое (30-60с), чтобы после рестарта бота/воркера первый прогон был почти сразу, а дальше уже с полным интервалом. Пример:

```python
# worker.py:894-898
queue.enqueue_in(
    timedelta(seconds=min(balance_interval, 60)),
    "app.worker.run_balance_charge_tick",
    result_ttl=3600,
)
```

Затем `worker.work(with_scheduler=True)` — блокирующий вызов, запускает worker main loop + scheduler loop для отложенных задач.

### Паттерн «самопланирования» — как это работает и где ломается

Каждая тика в конце своего тела делает примерно так:

```python
# worker.py:432-443 (warm-pool pattern)
interval = int(os.getenv("WARM_POOL_CHECK_INTERVAL", "120"))
if interval > 0:
    queue = get_queue()
    if queue is not None:
        try:
            queue.enqueue_in(
                timedelta(seconds=interval),
                "app.worker.run_warm_pool_check",
                result_ttl=3600,
            )
        except Exception:
            logger.exception("warm_pool: failed to re-enqueue tick")
return summary
```

Ключевое: `enqueue_in` вызывается **в конце функции**, обычно внутри `if get_queue() is not None: try/except`. Это самопланирование работает так, что одна тика либо успешно запланировала следующую (цикл продолжается), либо нет (цикл оборвался).

Структура обработки ошибок в теле тика при этом **разная** от функции к функции:

- `run_warm_pool_check` (`worker.py:423-430`): `try: ensure_pool() except Exception: log finally: close` — внутренний catch-all, падать ничего не должно, дойдёт до `enqueue_in`.
- `run_balance_charge_tick` (`worker.py:674-763`): такой же внешний `try/except Exception` на всё тело + `finally: close`. Дойдёт до `enqueue_in`.
- `run_autoscale_tick` (`worker.py:50-69`): только `try/finally`, **без `except`**. Если `evaluate_all_pools` кинул — exception пролетает наверх, `enqueue_in` не выполняется, цикл обрывается. RQ залогирует job failed, но следующего enqueue не произойдёт.
- `run_drain_tick` (`worker.py:107-204`): внутри каждой фазы свой try/except, но явного внешнего catch-all на всё тело нет. Большинство exception'ов ловятся точечно, но не гарантированно все.
- `run_renewal_check` — аналогично, точечные try/except по проходам (см. `worker.py:207+`).

> ⚠️ `enqueue_in` не в `finally` → частично-защищённый цикл. См. audit/...

Единственный способ восстановить оборвавшуюся тику сейчас — рестарт воркер-контейнера (который bootstrap'ит все тики заново в `main()`).

## Конкретные тики — суть, а не сигнатуры

### `run_autoscale_tick`

Одна функция, один проход по всем пулам: `services.autoscale.evaluate_all_pools(session)` возвращает `PoolDecisionOut` на каждый пул. Решения о том, спавнить ли ноду, принимает `evaluate_all_pools`; тика просто исполняет и возвращает summary для RQ result backend.

### `run_drain_tick` — downscale

Две фазы в одной тике (`worker.py:72-204`):

1. **Mark.** `evaluate_all_downscale` ходит по пулам и может перевести **одну** ноду в `draining` при соблюдении hysteresis + `min_nodes` + master-switch (`AUTOSCALE_DOWNSCALE_ENABLED=1`).
2. **Migrate + destroy.** Для каждой уже `draining` ноды берётся batch `DRAIN_MIGRATE_BATCH=10` активных subs, каждой вызывается `orchestrator.migrate_subscription_to_new_node(sub)`. Если после batch'а подписок не осталось И прошло больше `AUTOSCALE_DRAIN_GRACE_HOURS=24` часов с `updated_at` — вызывается `destroy_node(session, node)`.

Заметка: grace-окно считается от `node.updated_at`, не от момента первой постановки в drain. Любой UPDATE на ноде (health merge, admin edit notes) обновляет `updated_at` и сдвигает окно.

> ⚠️ Drain grace измеряется от `updated_at`. См. audit/...

Mетрика: `autoscale.DRAIN_SUBS_REMAINING.labels(pool, node)` — сколько subs ещё осталось мигрировать. Тика переставляет значение в каждой итерации.

Drain тика продолжает работать даже при `AUTOSCALE_DOWNSCALE_ENABLED=0` — чтобы оператор мог руками перевести ноду в `draining` через admin SPA и ждать, что её мигрируют и уничтожат. `AUTOSCALE_DOWNSCALE_ENABLED` гейтит только фазу Mark.

### `run_renewal_check`

Два прохода (`worker.py:207+`):

1. **Expire.** Все `active` subs с `expires_at < now` → flip в `expired`. `/api/sub/{token}` после этого отдаёт 403, клиент клиента перестаёт обновляться (`api_extensions.py:70-74`).
2. **Hard revoke.** Для `expired` subs с `expires_at < now - RENEWAL_GRACE_HOURS` — запускается orchestrator.revoke → ansible чистит пользователя с ноды. Grace по дефолту 24h.
3. **Renewal reminders.** Для `active` subs с `expires_at < now + 3 days` пишется `AuditLog(action='renewal_reminder', extra={telegram_id, expires_at})`. Бот это считает (`components/bot.md`).

### `run_traffic_stats_tick` — сбор samples (Phase D детектор отключён)

Каждые 5 минут SSH'ится на все active/draining ноды, вычитывает xray statsquery + sharing_violations.jsonl → пишет `node_traffic_samples` и `AuditLog(action="sharing_violation")`.

**Phase D детектор отключён (2026-04-15).** Раньше после `collect_all_active_nodes()` вызывался `traffic_stats.detect_traffic_drops(session, rows)`, который на любом idle-окне (например, все клиенты поставили телефон на зарядку, active_users=0 при прошлой пачке ≥`TRAFFIC_DROP_MIN_USERS`) помечал ноду `suspect_since` и через `CONFIRM_TICKS*TRAFFIC_STATS_INTERVAL` вызывал `migrate_subscriptions_off(exclude_same_region=True)` + флипал ноду в `error`. В проде это проявлялось как отвал подключения у активных юзеров каждые 5-10 минут (совпадает с `TRAFFIC_STATS_INTERVAL=300s`).

Сейчас:
- Вызов `detect_traffic_drops` в `run_traffic_stats_tick` закомментирован.
- Сама функция `detect_traffic_drops` стоит no-op (`return []`), тело сохранено для быстрого возврата.
- Переселение нод теперь только ручное — через админ-UI (`POST /api/nodes/{id}/migrate`) или при апгрейде/даунскейле пула.

Чтобы вернуть автомиграцию — нужно пересмотреть пороги (`TRAFFIC_DROP_MIN_USERS`, `CONFIRM_TICKS`, `MIN_IDLE_BYTES`) и добавить anti-flap гейты, затем расхешировать блок в `worker.run_traffic_stats_tick` + снять early-return в `traffic_stats.detect_traffic_drops`.

Подробнее алгоритм (на случай возврата) — `infrastructure/nodes.md` → Phase D.

### `run_warm_pool_check`

Тонкая обёртка над `services.warm_pool.ensure_pool(session)`. Summary — `{node_id: warmed_count}`. Вся логика — в `components/warm-pool.md`.

### `run_balance_charge_tick`

Четыре прохода (`worker.py:644-777`):

1. **Renew.** Active subs с `expires_at <= now` и `auto_renew=True`. Берётся `SELECT FOR UPDATE SKIP LOCKED LIMIT 500`. На каждый вызывается `balance.renew_subscription(session, sub)`. Ok → продлили, emit `_maybe_emit_low_balance_warning`. Not ok (денег нет) → status=expired.
2. **Expire non-renewing.** Active + `auto_renew=False` + expires < now → expired. Без продления.
3. **Auto-unfreeze.** Frozen + `frozen_until <= now` → `balance.unfreeze_subscription(session, sub, auto=True)`.
4. **Trial expiry.** T-3 warning + clawback при истечении триала.

`_maybe_emit_low_balance_warning` (`worker.py:450-520`) — идемпотентен на календарный день: проверяет, нет ли уже `low_balance_warning` audit-лога на сегодня у этого пользователя, только тогда пишет новый. Это и есть единственный механизм дедупликации нотификаций — бот не отслеживает ack'и сверх `:delivered` маркера.

## Retry и DLQ

Единичные provisioning-задачи (`run_provisioning_task`) ставятся в очередь через `queue.enqueue_task()` с:

- `Retry(max=3, interval=[10, 30, 120])` — три попытки с нарастающим backoff
- `job_timeout=RQ_JOB_TIMEOUT` (default 900s) — hard-kill per job
- `failure_ttl=RQ_FAILED_TTL` (default 604800 = 1 неделя) — сколько RQ хранит упавший job в FailedJobRegistry

Если все 3 retry исчерпаны, RQ вызывает `dlq_exception_handler` (зарегистрирован на Worker через `exception_handlers`). Хендлер:

1. Инкрементирует `vpn_provisioning_dlq_total` Prometheus counter
2. Пишет `AuditLog(action="provisioning_dlq")` с `job_id`, `exc_type`, `error` (первые 500 символов)
3. Логирует через `logger.error`

Отдельной DLQ-очереди нет — RQ's FailedJobRegistry и есть DLQ. Audit-лог обеспечивает видимость в admin UI без ковыряния Redis.

## Метрики

Работают prometheus-клиенты напрямую в процессе воркера. Глобальная `CollectorRegistry` — это то, почему воркер обязан импортироваться под каноническим именем (см. выше). Экспорт метрик воркера в backend'овый `/metrics` **не происходит автоматически** — воркер не слушает HTTP, его метрики доступны только если поверх натянут push-gateway или отдельный exporter.

Основные:
- `vpn_provisioning_dlq_total` — provisioning jobs, исчерпавшие retry (dead-letter)
- `vpn_renewal_check_runs_total{outcome}` — количество renewal-тиков (`worker.py:26-28`)
- `vpn_renewal_check_last_run_timestamp` — unix ts последнего успешного тика
- `vpn_renewal_revoked_total` — subs auto-revoked после grace
- `autoscale.DRAIN_SUBS_REMAINING{pool, node}` — см. `services/autoscale.py`

## Взаимодействие с provisioning

Воркер — единственный процесс, который вызывает `subprocess.run(["ansible-playbook", ...])` (`backend/app/services/ansible_runner.py`). Backend'а может не быть в контейнере ansible вовсе — см. `api.py:181-195`: deep health-check на backend'е умеет проверять только доступность queue, не ansible'а, потому что «API image intentionally ships without ansible».

То есть backend'у для провижининга **обязательно** нужен работающий воркер. Без воркера у backend'а есть legacy in-process thread fallback (упоминается в комментарии `api.py:189-190`), но это безопасная крайность, не штатный путь.

## Восстановление после падения

При каждом старте процесса backend'а (`main.py:27-64`):

- `ProvisioningTask.status=running` → переставляется в `pending` + re-enqueue в RQ. Если queue недоступна — помечается `failed` с сообщением "Interrupted by server restart".
- `ProvisioningTask.status=pending` → re-enqueue в RQ (на случай, если прошлый воркер упал до `blpop`).

Это не требует heartbeat'а от воркера — берётся оптимистически, полагаясь на idempotency ansible-playbook'ов (повторный запуск `manage_*_user.sh` — no-op на уже созданном юзере).

> ⚠️ `reset_stuck_tasks` перезапускает running без heartbeat. См. audit/...

## Что воркер НЕ делает

- Не обслуживает HTTP-роуты (нет FastAPI, нет uvicorn — только RQ Worker).
- Не прогоняет миграции в проде (`SKIP_MIGRATIONS=1`).
- Не читает env на горячую — все тики читают `os.getenv(...)` в своём теле при каждом запуске, поэтому смена env требует рестарта процесса (чтобы перевыбрать запланированный интервал в bootstrap-блоке).
- Не имеет отдельной auth-поверхности. Всё внутри воркера — trusted, потому что контейнер с тем же admin-токеном и тем же провижининг-ключом, что и бэкенд.

## ⚠️ Неясные места

- Структура обработки ошибок между тиками неконсистентна (см. выше). Не ясно, намеренно ли `run_autoscale_tick` падает молча при ошибке `evaluate_all_pools`, или это пропущено.
- `WARM_POOL_MAX_CONCURRENT` задаётся в compose (`docker-compose.yml:127`, default 2), но в коде `run_warm_pool_check` сам его не читает — лимит применяется глубже, в `services/warm_pool.py` через `threading.Semaphore`. Поведение этого семафора при горизонтальном масштабировании воркеров — см. `components/warm-pool.md`.
- ✅ Retry-политика единичных `run_provisioning_task`: `Retry(max=3, interval=[10, 30, 120])` + `dlq_exception_handler` при окончательном провале → пишет в audit_log. См. § Retry и DLQ.
- `worker.work(with_scheduler=True)` поднимает scheduler-поток внутри worker-процесса. При двух параллельно запущенных воркерах обa поднимут scheduler — как RQ это разруливает (один scheduler побеждает по advisory lock?) из кода не видно.
