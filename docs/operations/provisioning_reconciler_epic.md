# EPIC: Provisioning reconciler — от «задача-на-действие» к desired-state convergence

**Статус:** Phase 0 ✅ · Phase 1 ✅ · Phase 2+3 ✅ — **`RECONCILER_ENABLED=1`
в проде с 2026-06-15** (role-default остаётся OFF). Предусловие — перевод
ручных bootstrap-эндпоинтов на `defer_to_reconciler=False` — выполнено (см.
«Состояние реализации» ниже) · Phase 4 ⏳ частично (cap+ordering, backoff —
есть; diff-skip, стриминг ansible, reconciler'ы exit/relay — deferred) ·
**Создан:** 2026-06-08 · **Обновлён:** 2026-06-15

## Зачем
Сейчас каждое действие (правка/добавление/удаление конфига, ручной bootstrap)
создаёт `ProvisioningTask` и сразу гонит ansible. Последствия (по коду):
- **Нет дедупа на создании** (`provisioning.py:830` `create_task` — просто `db.add`).
  N правок → N тасок → нода в петле re-bootstrap'ов (наблюдали 2026-06-08).
- **Нельзя отменить**: в `ProvisioningTaskStatus` нет `cancelled`, нет cancel-пути,
  ansible зовётся блокирующим `subprocess.run` (`ansible_runner.py:365`).
- **`defer_bootstrap` — ручной костыль**: UI сам помнит флаг и в конце дёргает
  bootstrap. Коалесинг руками, гонки если забыл флаг.
- **Семафор `MAX_CONCURRENT_ANSIBLE=3` — per-process, НЕ глобальный** (audit #200):
  провижининг идёт в RQ-воркерах, каждый в своём процессе → реальный глобальный
  параллелизм ansible = число реплик воркера (`WORKER_REPLICAS`, до 20 через
  `/ops/worker/scale`), а не 3. И семафор НЕ per-node → возможны два bootstrap'а
  на одну ноду параллельно.

Команда императивная: «сделай site.yml СЕЙЧАС» на каждый чих. Приказы копятся,
отменить нельзя.

## Целевая модель: reconciler (термостат / k8s-controller)
Действие не создаёт таску — оно меняет **желаемое состояние** и бампает счётчик
`desired_generation`. Один reconcile-цикл сходит ноды, у которых
`desired_generation > reconciled_generation`, ОДНИМ прогоном site.yml, и ставит
`reconciled = desired_на_момент_старта`. Бесплатно (ansible уже идемпотентен):
- **коалесинг** — burst правок → 1-2 прогона к последнему состоянию;
- **supersession вместо отмены** — устаревший прогон не отменяем, делаем
  неактуальным (desired бампнулся → следующий цикл прогонит снова);
- **per-node сериализация** — один reconcile-слот на ноду;
- **батчи не нужны** — коалесинг автоматический.

`ProvisioningTask` остаётся как **лог прогонов** (одна строка на прогон + какой
generation сошёлся) — UI истории/прогресса живёт.

Две самые трудные части УЖЕ есть: идемпотентный `site.yml` + очередь тиков
(`queue.py`/`worker.py`). Это не переписывание, а смена точки входа оркестрации.

---

## Фазы

### Phase 0 — Coalescing dedup (самое дешёвое, низкий риск) ✅
Инвариант: **≤1 активный (pending|running) bootstrap на ноду**. Edits продолжают
триггерить, но коалесятся.
- Миграция: partial unique index `(target_type, target_id, action)
  WHERE status IN ('pending','running')` + колонка `rerun_requested bool`.
- `create_or_coalesce_node_bootstrap(node, payload)`: есть активный → ставим
  `rerun_requested=true` на нём (не плодим); иначе create + enqueue. Гонку ловим
  через IntegrityError на индексе.
- Worker на финише bootstrap'а: если `rerun_requested` — создаём один свежий
  bootstrap + enqueue, флаг сбрасываем. → burst правок во время прогона = ровно
  ОДИН повтор после.
- Рероут всех node-bootstrap точек (`nodes.py:72,542,881,1021,1106`) на coalesce.
- **Acceptance:** 10 быстрых правок подряд → ≤2 прогона; нода не зацикливается.

### Phase 1 — Cancellation
- Статус `cancelled` + `cancel_requested_at`. Воркер проверяет перед стартом
  (скип→cancelled). `ansible_runner`: `subprocess.run` → `Popen` + SIGTERM на
  отмену. Эндпоинт `POST /provisioning/tasks/{id}/cancel` + кнопка в админке.
- **Acceptance:** можно отменить pending (мгновенно) и running (SIGTERM, ≤5с).

### Phase 2 — Debounce window ✅ (flag-gated)
- `node.reconcile_due_at`; edit ставит `due_at = now + Ns` вместо мгновенного
  запуска; тик подбирает «созревшие». Burst коллапсит сам. `defer_bootstrap`
  становится не нужен.
- **Acceptance:** 10 правок за 3с → один прогон через ~Ns после последней.

### Phase 3 — Desired-state generations (сам reconciler) ✅ (flag-gated)
- `VPNNode.desired_generation` / `reconciled_generation` / `reconcile_due_at`.
  Edit бампает desired через `mark_node_dirty` (НЕ создаёт таску).
  `reconcile_due_nodes()` reconcile-тик (двигатель + safety net). На успехе
  `reconciled = gen@start`; если desired бампнулся во время прогона — прогон
  снова (supersession через re-arm `due_at`). ProvisioningTask = лог.
- **Реализация vs. план:** отдельная колонка `reconciling_task_id` НЕ заведена —
  per-node сериализацию уже держит Phase-0 инвариант (partial unique index
  `uq_active_node_bootstrap`, ≤1 активный bootstrap на ноду), а целевой
  generation прогона едет в payload как `reconcile_gen` (стэмпится в
  `_handle_task_outcome`). Меньше колонок, тот же эффект.
- **Acceptance:** правка во время прогона → автоматический повтор; нет тасок на
  каждое действие; UI прогресса жив.

### Phase 4 — Extend + polish ⏳ (частично)
- ✅ **Cap + ordering** в `reconcile_due_nodes`: `RECONCILE_MAX_PER_TICK`
  (default 15) нод за тик, ORDER BY `reconcile_due_at` ASC (FIFO,
  дольше-ждавшие первыми), back-pressure против thundering herd при bulk-правке.
- ✅ **Backoff на фейлах**: `_handle_task_outcome` re-arm'ит `due_at = now +
  RECONCILE_RETRY_S` (default 60s) при неуспешном reconcile-bootstrap'е.
- ⏳ **Deferred (явно не в этом заходе):**
  - **Reconciler'ы для exit/relay** — тот же desired/reconciled-паттерн для
    `WGExitNode` / `RelayExitLink`. Сейчас exit/relay по-прежнему императивные
    (taskи-на-действие). Отдельная фаза: свои generation-колонки + тик.
  - **Diff-skip** — пропуск прогона, если ansible реально нечего применять
    (dry-run/`--check` или хэш желаемого состояния). Сейчас прогоняем всегда
    (идемпотентный site.yml — корректно, но не бесплатно).
  - **Стриминг вывода ansible** — live-tail прогресса в UI. Сейчас лог
    появляется по завершении прогона.

---

## Порядок и риск
0 → 1 → 2 → 3 → 4. Phase 0 даёт ~80% облегчения при минимальном риске (worst case
— вырожденно в старое поведение, не в тихую несходимость). Каждая фаза шипится и
верифицируется отдельно (py_compile + ruff + adversarial review на крупных).

---

## Состояние реализации и как включить (Phase 2+3)

Код Phase 2+3 собран, отревьюен и **проброшен через ansible**. Флаг
`RECONCILER_ENABLED` переключает точку входа оркестрации с «правка →
bootstrap» на «правка → bump desired → reconcile-тик». Role-default OFF
(`deploy_app_stack/defaults/main.yml`); **в проде ВКЛючён 2026-06-15**
(`group_vars/web/main.yml` `deploy_app_stack_reconciler_enabled: "1"`).

Предусловие включения (выполнено 2026-06-15): ручные/операторские
bootstrap-эндпоинты теперь зовут `defer_to_reconciler=False` → создают
немедленную таску, не дебаунсятся (раньше при флаге дефолт `True` они молча
возвращали `(None, False)` и кнопка «висела» — из-за чего флаг и был
откатан в `4acddab`). Список переведённых на `False`: `create_node`,
`create_node_with_configs`, `rebootstrap_node`, `refresh_reality_dest`
(ordering-critical: bootstrap до device-apply) в `api/nodes.py`, плюс
spawn-finalize в `node_spawner.py`. **Config-edit-сайты** (create/update/
delete config) сознательно ОСТАЮТСЯ на дефолтном `defer=True` — их
коалесинг реконсайлером это и есть фича. Откат всего: флаг `"0"` + redeploy
(тег `pre-reconciler-enable-*`).

### Видимость и скорость (вариант C — оставить reconcile-модель, убрать «тишину»)
Чтобы операторское действие при включённом флаге не выглядело как «ничего не
произошло», dirty-состояние ноды **видно в API/админке**, а тик — быстрее:
- `VPNNodeOut.reconcile_pending` (= `desired_generation > reconciled_generation`)
  + `reconcile_due_at` — отдаются в `GET /nodes`; админка рисует бейдж
  «⏳ reconcile через Ns» (`Nodes.tsx:ReconcilePendingBadge`). Фантомную
  `pending`-таску при `mark_node_dirty` НЕ создаём: она попала бы под
  `NOT EXISTS(active bootstrap)` в тике и зависла бы навсегда — поэтому видимость
  это **состояние ноды**, а не строка таски.
- Тик ускорен: `RECONCILE_INTERVAL` 10→**3s**, `RECONCILE_MAX_PER_TICK` 5→**15**.
- **Watchdog**: `run_reconcile_tick` экспортит гейджи `vpn_reconcile_pending_nodes`
  и `vpn_reconcile_oldest_overdue_seconds`; при `oldest_overdue > RECONCILE_OVERDUE_WARN_S`
  (default 120s) — WARNING в лог. Завис scheduler → гейджи протухают →
  external staleness-alert ловит то, что изнутри тика не видно (страховка сверху
  к self-heal stale-лока на старте воркера, `7bf697a`).

| Файл | Что лежит |
|------|-----------|
| `0043_node_reconcile_generations.py` | колонки `desired_generation` / `reconciled_generation` / `reconcile_due_at` на `vpn_nodes` (idempotent через `has_column`) |
| `models.py` | те же 3 колонки |
| `provisioning.py` | `_reconciler_enabled`, `mark_node_dirty`, гейт `defer_to_reconciler` в `create_or_coalesce_node_bootstrap`, `reconcile_due_nodes()`, стэмпинг `reconciled_generation` в `_handle_task_outcome` (успех→converge/re-arm supersession, фейл→backoff) |
| `worker.py` | `run_reconcile_tick()` + bootstrap тика в `main()` |
| `queue.py` | `tick-reconcile` в `TICK_IDS` + `TICK_TIMEOUTS` (60s) |

### Env vars (проброс: `docker-compose.yml` backend+worker-env, `env.j2`, `defaults/main.yml`)

| Env var | Ansible var (`deploy_app_stack_*`) | Default | Назначение |
|---------|-----------------------------------|---------|-----------|
| `RECONCILER_ENABLED` | `…reconciler_enabled` | `0` (role) / `1` (web) | мастер-тумблер. `1/true/yes` → reconcile-модель |
| `RECONCILE_INTERVAL` | `…reconcile_interval` | `3` | период reconcile-тика, сек (только worker-scheduler) |
| `RECONCILE_DEBOUNCE_S` | `…reconcile_debounce_s` | `5` | debounce: правка ставит `due_at = now + N` |
| `RECONCILE_MAX_PER_TICK` | `…reconcile_max_per_tick` | `15` | нод за тик (back-pressure, FIFO по due_at) |
| `RECONCILE_RETRY_S` | `…reconcile_retry_s` | `60` | backoff: re-arm `due_at` после фейла reconcile-bootstrap'а |
| `RECONCILE_OVERDUE_WARN_S` | `…reconcile_overdue_warn_s` | `120` | watchdog: WARNING если самая старая dirty-нода просрочена дольше |

Кто читает: `RECONCILER_ENABLED`/`RECONCILE_DEBOUNCE_S` — backend (mark_node_dirty)
+ worker. `RECONCILE_INTERVAL`/`MAX_PER_TICK`/`RECONCILE_OVERDUE_WARN_S` —
worker-scheduler (тик). `RECONCILE_RETRY_S` — worker (outcome). Поэтому флаг
проброшен и в backend, и в `&worker-env` (worker + worker-scheduler).

### Rollout
Один деплой через ansible (`site.yml --tags web`) выкатывает код И включает
флаг (`group_vars/web`). Что происходит:
1. **Миграция авто** — backend на старте гонит `alembic upgrade head`
   (`main.py:23`; воркеры `SKIP_MIGRATIONS=1`). 0040–0043 накатываются сами.
2. **Бэкфилл безопасен** — 0043 ставит `desired=reconciled=0` всем нодам →
   включение НЕ триггерит mass-rebuild; реконсайлятся только ноды, правленные
   ПОСЛЕ деплоя. Светофор спокойный.
3. **Наблюдать** `due`/`dispatched`/`capped` в результате `tick-reconcile`
   (RQ result backend) + логи worker-scheduler `Reconcile tick bootstrapped`.

**Откат:** `deploy_app_stack_reconciler_enabled: "0"` + redeploy (или
`RECONCILER_ENABLED=` в `.env` на хосте + `docker compose up -d backend worker
worker-scheduler`). Worst case вырождается в Phase-0 immediate, не в тихую
несходимость.
