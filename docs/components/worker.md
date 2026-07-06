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
| `run_renewal_check`        | `RENEWAL_CHECK_INTERVAL=300` | Expire-ит подписки, инициирует renewal reminders, hard-revoke после grace. Для `auto_renew=ON` сначала пробует `balance.renew_subscription`; expire — ТОЛЬКО на явное «недостаточно средств» (renew вернул False). Исключение в renew (обрыв БД, deadlock) — skip до следующего тика, подписка без плана — admin-алерт `renewal_broken_sub` с суточным дедупом (аудит-фикс #59) |
| `run_warm_pool_check`      | `WARM_POOL_CHECK_INTERVAL=120` | Топит warm pool на каждой активной ноде до `WARM_POOL_TARGET` |
| `run_balance_charge_tick`  | `BALANCE_CHARGE_INTERVAL=3600` | Renew balance-подписок, expire auto_renew=False, auto-unfreeze, clawback trial |
| `run_traffic_stats_tick`   | `TRAFFIC_STATS_INTERVAL=300` | SSH на каждую active/draining ноду (параллельно, `TRAFFIC_STATS_SSH_WORKERS=8`; per-node commit; wall-clock-бюджет `TRAFFIC_STATS_BUDGET_SEC=100` — недособранный хвост уходит в следующий тик), читает xray stats + sharing violations → `node_traffic_samples` + `AuditLog`. Per-protocol breakdown в `details` содержит список `access_username` (а не просто count) — админка читает последний sample через `GET /api/nodes/{id}/users`. После persist вызывается Phase D `detect_traffic_drops()` — пассивный детектор ТСПУ-блокировок (gated через `TRAFFIC_DROP_ENABLED=1`) |
| `run_pending_rescue_tick`  | `PENDING_RESCUE_INTERVAL=60` | Сканирует `ProvisioningTask.status=pending` старше `PENDING_RESCUE_AGE` секунд и re-enqueue'ит через `enqueue_task`. Дедуп по `job_id=provision-<task_id>` — если задача уже в RQ, это no-op. Закрывает дыру, когда `run_task_async` закоммитил row, но `enqueue_task` упал (транзиентный Redis hiccup, serialization issue) — до этого фикса такие задачи висели в pending до следующего рестарта бэкенда (`reset_stuck_tasks` в main.py срабатывает только на boot). Метрика: `vpn_provisioning_pending_rescue_total` инкрементится на каждый rescue |
| `run_reconcile_tick`       | `RECONCILE_INTERVAL=3`       | Phase 3 reconcile: сходит dirty-ноды (`desired_generation > reconciled_generation` и `reconcile_due_at <= now`) одним coalesced bootstrap'ом, до `RECONCILE_MAX_PER_TICK=15` нод/тик (FIFO по due_at). No-op пока `RECONCILER_ENABLED` выкл. Self-reschedules. Watchdog-гейджи `vpn_reconcile_pending_nodes` / `vpn_reconcile_oldest_overdue_seconds` (+ WARNING при `oldest_overdue > RECONCILE_OVERDUE_WARN_S=120`) — ловят cap-starvation, повторно падающий bootstrap и (через staleness гейджа) зависший scheduler. См. `docs/operations/provisioning_reconciler_epic.md` |

### Bootstrap при старте воркера

В `main()` (`worker.py:1220-1401`) каждая тика первый раз ставится в очередь через `schedule_tick(func_name, min(interval, X), tick_id)` — где `X` маленькое (30-60с), чтобы после рестарта воркера первый прогон был почти сразу, а дальше уже с полным интервалом. Пример:

```python
# worker.py (bootstrap-ветка для balance-charge)
schedule_tick(
    "app.worker.run_balance_charge_tick",
    min(balance_interval, 60),
    tick_id="tick-balance-charge",
)
```

`schedule_tick` — обёртка над `queue.enqueue_in(..., job_id=tick_id)` с zombie-reclaim и дедупом поверх. Без детерминированного `job_id` каждый рестарт воркера плодил бы новую цепочку тика: после 3-4 рестартов `rq info` показывал 50+ `scheduled` jobs вместо 8 (репро 2026-04-17). Детали — § Дедупликация тиков ниже.

Затем `worker.work(with_scheduler=True)` — блокирующий вызов, запускает worker main loop + scheduler loop для отложенных задач.

#### Stale scheduler-lock после деплоя (фикс 2026-06-09)

`with_scheduler=True` форкает `RQScheduler`, который захватывает лок
`SET rq:scheduler-lock:<queue> <pid> NX EX (interval+60)` и стартует
**только если лок захвачен**. Граница: `acquire_locks` зовётся на старте
**один раз**; reacquire-петля живёт *внутри* форкнутого scheduler-процесса,
так что при незахвате ретрая нет.

Деплой (`docker compose up -d` recreate) убивает старый worker-scheduler
SIGKILL'ом после `stop_grace` → graceful `stop()→release_locks()` не
вызывается → лок мёртвого инстанса висит ~`interval+60`с. Свежий контейнер
на старте упирается в `NX` → scheduler не форкается → **все тики стоят**,
пока следующий рестарт случайно не попадёт в окно после истечения TTL
(наблюдали ~9 мин полного простоя периодики; симптом — `rq` показывает
N jobs в `scheduled`, очередь пустая, `Job OK` в логах нет).

Фикс (`worker.py`, перед `work()`): т.к. `worker-scheduler` это
`replicas: 1` и recreate последовательный (стоп старого → старт нового),
легитимного держателя лока в момент старта нет — поэтому чистим
`RQScheduler.get_locking_key(q.name)` в Redis до `work()`, гарантируя
чистый `acquire`. Это убирает костыль «передёрнуть worker-scheduler руками
после деплоя».

### Паттерн «самопланирования» — как это работает и где ломается

Каждая тика в конце своего тела делает примерно так:

```python
# warm-pool pattern
interval = int(os.getenv("WARM_POOL_CHECK_INTERVAL", "120"))
if interval > 0:
    try:
        schedule_tick(
            "app.worker.run_warm_pool_check",
            interval,
            tick_id="tick-warm-pool",
        )
    except Exception:
        logger.exception("warm_pool: failed to re-enqueue tick")
return summary
```

Ключевое: `schedule_tick` вызывается **в конце функции**, обычно внутри `try/except`. Это самопланирование работает так, что одна тика либо успешно запланировала следующую (цикл продолжается), либо нет (цикл оборвался).

Структура обработки ошибок в теле тика при этом **разная** от функции к функции:

- `run_warm_pool_check` (`worker.py:423-430`): `try: ensure_pool() except Exception: log finally: close` — внутренний catch-all, падать ничего не должно, дойдёт до `enqueue_in`.
- `run_balance_charge_tick` (`worker.py:674-763`): такой же внешний `try/except Exception` на всё тело + `finally: close`. Дойдёт до `enqueue_in`.
- `run_autoscale_tick` (`worker.py:50-69`): только `try/finally`, **без `except`**. Если `evaluate_all_pools` кинул — exception пролетает наверх, `enqueue_in` не выполняется, цикл обрывается. RQ залогирует job failed, но следующего enqueue не произойдёт.
- `run_renewal_check` — аналогично, точечные try/except по проходам (см. `worker.py:207+`).

> ⚠️ `enqueue_in` не в `finally` → частично-защищённый цикл. См. audit/...

Единственный способ восстановить оборвавшуюся тику сейчас — рестарт воркер-контейнера (который bootstrap'ит все тики заново в `main()`).

### Дедупликация тиков (`schedule_tick`)

`queue.enqueue_in` без `job_id` — это RQ-вызов, который генерит случайный UUID на каждый job. Для unit-of-work `provision-{task_id}` такого риска нет, но для self-rescheduling тиков — был: каждый рестарт воркера в `main()` делал bootstrap-enqueue **поверх** ещё не стрельнувшего scheduled-job'а от прошлой инкарнации. После N рестартов на одну логическую тику висело N параллельных цепочек → очередь флудится, БД получает умноженную нагрузку, `_maybe_emit_low_balance_warning` перестаёт быть идемпотентным по календарному дню (две цепочки balance_charge в один и тот же час).

Репро 2026-04-17: после пары рестартов worker-контейнера `rq info -u $REDIS_URL` показал 57 scheduled jobs вместо 8, все с уникальными uuid; CPU на бэке 80% от дублей `run_renewal_check`.

Решение — `queue.schedule_tick(func_name, interval_seconds, tick_id)` в `backend/app/queue.py`. Под капотом:

1. `StartedJobRegistry(queue=queue).cleanup()` — reclaim zombie (см. § Retry и DLQ).
2. `Job.fetch(tick_id)`: если статус в `{queued, started, deferred, scheduled}` → возвращаем существующий id, повторный enqueue — no-op.
3. Иначе (failed / finished / canceled / stopped / `NoSuchJob`) → `existing.delete()` (если есть) + `queue.enqueue_in(delta, func, job_id=tick_id, result_ttl=3600)`.

`tick_id` — стабильный slug, по одному на тик:

| Функция                      | `tick_id`              |
|------------------------------|------------------------|
| `run_pending_rescue_tick`    | `tick-pending-rescue`  |
| `run_autoscale_tick`         | `tick-autoscale`       |
| `run_renewal_check`          | `tick-renewal`         |
| `run_warm_pool_check`        | `tick-warm-pool`       |
| `run_balance_charge_tick`    | `tick-balance-charge`  |
| `run_traffic_stats_tick`     | `tick-traffic-stats`   |
| `run_user_health_ping_tick`  | `tick-health-ping`     |
| `run_relay_link_health_tick` | `tick-relay-link-health` |
| `run_node_reachability_tick` | `tick-node-reachability` |
| `run_ops_plan_reaper_tick`   | `tick-ops-plan-reaper`   |

`run_node_reachability_tick` (diagnostics overhaul, env `NODE_REACHABILITY_INTERVAL=300`) — единственный владелец node/exit down-детекта: пробит ВСЕ active ноды + exit'ы staged-пробой (ping/ssh), на падении открывает инцидент (`services/diagnostics_state.should_diagnose` = одна диагностика на инцидент), шлёт говорящий пуш и enqueue'ит on-host диагноз; на recovery закрывает инцидент. Анти-голодание (аудит-фикс #95): цели обходятся в порядке `last_probe_at` ASC NULLS FIRST (самые давно не пробованные первыми), поэтому хвост, обрезанный wall-clock бюджетом `NODE_REACHABILITY_BUDGET_SEC`, идёт первым в следующем тике; гейдж `vpn_reachability_stale_targets` (+`summary.stale_targets`) показывает число целей без проба дольше `NODE_REACHABILITY_STALE_MIN` (30 мин) — стабильно >0 значит бюджета не хватает на флот. Подробнее — `docs/operations/diagnostics.md` § Overhaul.

`run_ops_plan_reaper_tick` (аудит-фикс #120, env `OPS_PLAN_REAPER_INTERVAL=300`, 0=off) — бэкстоп ops-агента: переводит в `failed` (`execution.phase='crash'`) планы, залипшие в `executing` дольше `OPS_EXECUTE_JOB_TIMEOUT`(1800) + `OPS_PLAN_REAPER_GRACE`(120) секунд с момента арма (момент — из AuditLog `agent_ops_execute_armed`, fallback `expires_at`/`created_at`). Ловит смерть воркера (OOM/рестарт) и kill джобы по `job_timeout` — in-job страховка в `run_ops_plan_execute` (generic-except → `failed`) в этих случаях не срабатывает, а эндпоинт `/agent/ops/execute` принимает только `proposed`. Аудит — `agent_ops_execute_reaped`.

Все `tick-*` места (self-reschedule + bootstrap в `main()`) используют `schedule_tick`. После фикса параллельных цепочек быть не может: даже 10 рестартов подряд оставят ровно по одному scheduled job на тик.

Смотреть текущее состояние тиков — `GET /api/provisioning/queue-status` (см. § Admin monitoring очереди).

## Admin monitoring очереди

Для просмотра состояния очереди и ручного ремонта — два эндпоинта в `api/tasks.py`, доступны на `/admin/tasks`:

- **`GET /api/provisioning/queue-status`** — снимок из RQ registries + быстрые counts по `ProvisioningTask`. Возвращает: `{queued, started, failed, deferred, scheduled, workers, db_pending, db_running, ticks_scheduled: [{tick_id, enqueue_at, status}]}`. Админская Tasks.tsx поллит это раз в 10с и рисует баннер если `failed > 0` или `queued > threshold`.

- **`POST /api/provisioning/queue/reset-stuck`** — ручной reset zombie-состояния:
  1. `StartedJobRegistry.cleanup()` — reclaim jobs, чей worker умер вместе с контейнером.
  2. Дедуп тиков: для каждого `tick-*` job_id — если статус терминальный (failed / finished / canceled / stopped) или `NoSuchJobError` → пере-enqueue через `schedule_tick` с коротким delay (60с).
  3. `reset_stuck_tasks()` — тот же флоу, что на старте backend'а: переставляет `ProvisioningTask.status=running` в `pending` и пере-enqueue'ит их.

  Возвращает `{ticks_rescheduled: [...], tasks_requeued: N, zombies_cleaned: N}`. Пишет `AuditLog(action="queue_reset_stuck")`.

Когда использовать reset:
- Очередь заметно раздута и `rq info` показывает одни и те же тики в десятках копий (пре-фикс мира без `schedule_tick`; остаточные хвосты после миграции).
- Таски висят в `pending` при живом воркере и `run_pending_rescue_tick` почему-то не успевает их вытащить.
- Ручной SIGKILL worker-контейнера без graceful shutdown — какие-то jobs могут остаться в `started` без heartbeat.

## Конкретные тики — суть, а не сигнатуры

### `run_autoscale_tick`

Одна функция, один проход по всем пулам: `services.autoscale.evaluate_all_pools(session)` возвращает `PoolDecisionOut` на каждый пул. Решения о том, спавнить ли ноду, принимает `evaluate_all_pools`; тика просто исполняет и возвращает summary для RQ result backend.

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

### Дедупликация и zombie-reclaim (`job_id = "provision-{task_id}"`)

`enqueue_task` использует детерминированный `job_id`, чтобы повторный enqueue одной и той же ProvisioningTask был идемпотентным (важно для `reset_stuck_tasks` и `run_pending_rescue_tick` — оба спокойно кидают в очередь всё, что `pending`, без риска размножить работу).

Порядок проверок перед `queue.enqueue(...)`:

1. **`StartedJobRegistry.cleanup()`** — RQ по heartbeat-ам воркеров находит jobs, чей хэндлер умер вместе с контейнером (SIGKILL / OOM / `docker compose down` в середине ansible-прогона), и переводит их из `started` в `failed`. Без этого шага следующий `Job.fetch` возвращает zombie, и мы уходим в короткий путь «уже в работе» — таска висит в `pending` бесконечно, пока оператор руками не дропнет Redis-ключ.
2. **`Job.fetch(job_id)`** — если job существует и статус в `{queued, started, deferred, scheduled}`, возвращаем его id без повторного enqueue.
3. Иначе (статус `failed`/`finished`/`canceled`/`stopped` после cleanup'а) — `existing.delete()` и нормальный `queue.enqueue(...)` с тем же `job_id`.

То есть «таска застряла в pending» теперь воспроизводится только если:
- воркер вообще не поднят (`depends_on` / redis недоступен), или
- `Retry(max=3)` исчерпан и job ушёл в DLQ до того, как автосамохил успел его пере-enqueue'нуть (по задумке — дальше `dlq_exception_handler` + audit-лог).

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

## Скейл воркеров из админки

Виджет **Workers** (`admin/src/workerHealth.tsx`, открывается в Nodes) умеет
менять число реплик: степпер `реплик [−][N][+] OK` → `POST /ops/worker/scale {replicas}`.

Механика (зеркало `scripts/workers.sh`, но из UI):
- API-образ (`backend/Dockerfile`) **без ssh/ключа** → сам скейлить не может.
  Поэтому эндпоинт **энкьюит RQ-job** `app.worker.run_scale_workers(N)`, и его
  подхватывает **worker** (`Dockerfile.worker` несёт `openssh-client` +
  `/run/secrets/provisioning_key`).
- Worker SSH-ит на mgmt-хост и гонит `docker compose up -d --scale worker=N worker`
  + пишет `WORKER_REPLICAS=N` в `.env` (чтобы пережило plain `docker compose up`).
  Docker-команда исполняется **на хосте**, так что даже scale-DOWN, убивающий
  этот же worker, доходит до конца.
- API коротко (≤30с) ждёт результат job'а → отдаёт `applied/failed/enqueued`.

Параметры (env воркера; дефолты под текущий prod): `MGMT_HOST` (иначе резолв
из inventory `db_host→mgmt-1`), `MGMT_USER` (root), `MGMT_STACK_DIR`
(`/opt/vpn`), ключ — `ANSIBLE_PRIVATE_KEY_FILE`.

**Предусловия:** (1) нужен ≥1 живой worker, который подхватит job (для бампа
вверх — всегда; при 0 воркеров сперва «↻ Рестарт»); (2) provisioning-ключ
должен пускать `MGMT_USER` на mgmt с правом `docker compose`. Если нет — job
вернёт SSH-ошибку, она прилетит в UI как `failed` + stderr. Аудит — `worker_scale`.

## ⚠️ Неясные места

- Структура обработки ошибок между тиками неконсистентна (см. выше). Не ясно, намеренно ли `run_autoscale_tick` падает молча при ошибке `evaluate_all_pools`, или это пропущено.
- `WARM_POOL_MAX_CONCURRENT` задаётся в compose (`docker-compose.yml:127`, default 2), но в коде `run_warm_pool_check` сам его не читает — лимит применяется глубже, в `services/warm_pool.py` через `threading.Semaphore`. Поведение этого семафора при горизонтальном масштабировании воркеров — см. `components/warm-pool.md`.
- ✅ Retry-политика единичных `run_provisioning_task`: `Retry(max=3, interval=[10, 30, 120])` + `dlq_exception_handler` при окончательном провале → пишет в audit_log. См. § Retry и DLQ.
- `worker.work(with_scheduler=True)` поднимает scheduler-поток внутри worker-процесса. При двух параллельно запущенных воркерах обa поднимут scheduler — как RQ это разруливает (один scheduler побеждает по advisory lock?) из кода не видно.
