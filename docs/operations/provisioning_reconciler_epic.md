# EPIC: Provisioning reconciler — от «задача-на-действие» к desired-state convergence

**Статус:** Phase 0 ✅ (отревьюен, 5 багов закрыты) · Phase 1 ✅ · Phase 2+3 ✅
собраны и flag-gated OFF (`RECONCILER_ENABLED`, по умолчанию выключено — без
флага поведение = Phase 0) · Phase 4 ⏳ частично (cap+ordering, backoff на
фейлах — есть; diff-skip, стриминг ansible, reconciler'ы exit/relay — deferred,
см. ниже) · **Создан:** 2026-06-08 · **Обновлён:** 2026-06-08

## Зачем
Сейчас каждое действие (правка/добавление/удаление конфига, ручной bootstrap)
создаёт `ProvisioningTask` и сразу гонит ansible. Последствия (по коду):
- **Нет дедупа на создании** (`provisioning.py:830` `create_task` — просто `db.add`).
  N правок → N тасок → нода в петле re-bootstrap'ов (наблюдали 2026-06-08).
- **Нельзя отменить**: в `ProvisioningTaskStatus` нет `cancelled`, нет cancel-пути,
  ansible зовётся блокирующим `subprocess.run` (`ansible_runner.py:365`).
- **`defer_bootstrap` — ручной костыль**: UI сам помнит флаг и в конце дёргает
  bootstrap. Коалесинг руками, гонки если забыл флаг.
- **Семафор глобальный** (`MAX_CONCURRENT_ANSIBLE=3`), НЕ per-node → возможны два
  bootstrap'а на одну ноду параллельно.

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
  (default 5) нод за тик, ORDER BY `reconcile_due_at` ASC (FIFO,
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

Код Phase 2+3 собран и **flag-gated OFF**. Без `RECONCILER_ENABLED` система
работает по Phase-0 immediate-модели — правка сразу коалесится в bootstrap.
Включение — один env var (`RECONCILER_ENABLED=1`), переключает точку входа
оркестрации с «правка → bootstrap» на «правка → bump desired → reconcile-тик».

| Файл | Что лежит |
|------|-----------|
| `0043_node_reconcile_generations.py` | колонки `desired_generation` / `reconciled_generation` / `reconcile_due_at` на `vpn_nodes` (idempotent через `has_column`) |
| `models.py` | те же 3 колонки |
| `provisioning.py` | `_reconciler_enabled`, `mark_node_dirty`, гейт `defer_to_reconciler` в `create_or_coalesce_node_bootstrap`, `reconcile_due_nodes()`, стэмпинг `reconciled_generation` в `_handle_task_outcome` (успех→converge/re-arm supersession, фейл→backoff) |
| `worker.py` | `run_reconcile_tick()` + bootstrap тика в `main()` |
| `queue.py` | `tick-reconcile` в `TICK_IDS` + `TICK_TIMEOUTS` (60s) |

### Env vars

| Var | Default | Назначение |
|-----|---------|-----------|
| `RECONCILER_ENABLED` | `` (off) | мастер-тумблер. `1/true/yes` → reconcile-модель |
| `RECONCILE_INTERVAL` | `10` | период reconcile-тика, сек (0 = тик не bootstrap'ится) |
| `RECONCILE_DEBOUNCE_S` | `5` | debounce: правка ставит `due_at = now + N` |
| `RECONCILE_MAX_PER_TICK` | `5` | сколько нод диспатчим за один тик (back-pressure) |
| `RECONCILE_RETRY_S` | `60` | backoff: re-arm `due_at` после фейла reconcile-bootstrap'а |

### Rollout
Деплой через ansible (`site.yml --tags web`) с `RECONCILER_ENABLED` пустым
выкатывает весь код **без смены поведения** (миграция накатывается, тик
крутится, но short-circuit'ит на `disabled`). Включать флагом отдельным шагом
после прогона миграции 0043, наблюдая `due` / `dispatched` / `capped` в
результате тика (RQ result backend).
