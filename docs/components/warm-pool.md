# Warm credential pool

Механизм, который убирает `ansible-playbook` из горячего пути покупки подписки. Живёт в `backend/app/services/warm_pool.py` (~503 строки).

## Проблема, которую решает

Cold-path провижининг означает: пользователь нажал «оплатить» → backend создал Subscription → worker запустил ansible-playbook → дождались 15–40 секунд → отдали конфиги. Для ansible-роли, которая редактирует xray-конфиг и делает `systemctl reload`, это нормальное время, но пользователь в боте видит «⏳ готовим подписку» и уходит.

Warm pool переносит провижининг **вперёд** во времени: backend периодически догоняет пул заранее сгенерированных «безхозных» credential'ов на каждой ноде. Покупка становится DB-only операцией: flip `pool_state: warm → assigned` + `subscription_id = X`, commit. Никакого ansible в pre-checkout / post-webhook коде.

## Анатомия «warm bundle»

**Bundle** — это набор строк `Credential`, у которых совпадает `(node_id, access_username)` и `pool_state = warm`. На каждую ноду создаётся столько bundle'ов, сколько включённых протоколов: если нода отдаёт ShadowTLS+SS и VLESS Reality одновременно, bundle состоит из двух `Credential`-строк с одним и тем же `access_username`, разными `proto` и `config_id`.

Инвариант: внутри одной ноды `access_username` уникален на bundle. Это критично для атомарного assignment'а (см. ниже).

Username генерится как `warm-<node_id>-<8 hex>` (`warm_pool.py:100-109`). Формат специально отличается от реальных user-bound имён (`user-<uid>-<sub_id>`), чтобы warm-записи были греппабельны в xray-логах. При assignment'е **переименования не происходит** — warm-name остаётся с подпиской до конца её жизни. Наружу он не утекает (пользователь видит только config URI).

## Lifecycle

```
   ┌───────────────────────┐
   │   pool_state = warm   │   создаётся warmer'ом периодически,
   │   subscription_id=NULL│   pushed на ноду через ansible state=present
   └──────────┬────────────┘
              │  try_assign_bundle (atomic)
              ▼
   ┌───────────────────────┐
   │ pool_state = assigned │   DB-only flip, бинд на Subscription/Device,
   │ subscription_id = X   │   нода про это ничего не знает
   └──────────┬────────────┘
              │  unassign_bundle (stage 1 revoke)
              ▼
   ┌───────────────────────┐
   │ pool_state = revoked  │   DB-only flip, пользователь ещё может
   │ is_active = False     │   ходить до реального reload'а на ноде
   └──────────┬────────────┘
              │  physical_revoke_credential_bundle (stage 2)
              ▼
   ┌───────────────────────┐
   │     row deleted       │   после успешного ansible state=absent
   └───────────────────────┘
```

Все переходы идут **только вперёд**: warm → assigned → revoked → deleted. Нет возврата warm→assigned→warm: повторно использовать username небезопасно (старый конфиг мог ещё где-то жить в кэше клиента).

## Warming — как пул заполняется

`ensure_pool(db)` (`warm_pool.py:261-299`) — точка входа, вызывается тиком `run_warm_pool_check` в воркере. Алгоритм:

1. Берёт все ноды с `is_active=true AND status='active'`.
2. Для каждой считает `depth = pool_depth(node.id)` — `COUNT(DISTINCT access_username)` по warm-credential'ам.
3. Deficit = `WARM_POOL_TARGET - depth` (default 10, env `WARM_POOL_TARGET`).
4. Cap per tick: `min(deficit, WARM_POOL_BATCH_PER_TICK)` (default 3) — свежедобавленная нода не получает 10 последовательных ansible-запусков в одну тику.
5. Цикл вызовов `warm_one_bundle(node)`, с early exit при первом `None` — если провайдер SSH упал, нет смысла жечь оставшийся budget на тех же ошибках.

`warm_one_bundle` (`warm_pool.py:143-240`):

1. Генерирует `username`, `password = secrets.token_urlsafe(12)`, `uuid = uuid.uuid4()`.
2. Для каждого включённого `VPNConfig` ноды вызывает соответствующий `_build_*_credential` из `services/provisioning.py` (lazy import во избежание circular) и складывает `Credential` ORM-объекты в in-memory список — **ещё не в сессию**.
3. Запускает `playbooks/provision_device.yml` с `extra_vars={state: present, username, uuid, password, protocols: [...]}` под семафором `_warmer_semaphore`.
4. **Только после** успешного `returncode == 0` добавляет `Credential`-объекты в сессию и коммитит.

Порядок принципиальный (см. комментарий `warm_pool.py:228-230`): если писать в БД до ansible'а, падение роли оставит в warm-пуле «мертвецов», которых assignment потом отдаст платящему пользователю. Симметрия нарушена в двух других местах (см. ⚠️ Неясные места).

## Atomic assignment — hot path

`try_assign_bundle(db, node_id, subscription_id)` (`warm_pool.py:304-366`) — функция, которую вызывает orchestrator в момент активации подписки.

```python
# warm_pool.py:323-330 (сокращённо)
anchor = (
    db.query(models.Credential)
    .filter(node_id == node_id, pool_state == warm)
    .order_by(Credential.id.asc())
    .with_for_update(skip_locked=True)
    .first()
)
```

**Почему SKIP LOCKED, а не advisory lock или отдельная таблица:**

- `access_username` уникален per-node на один bundle.
- Анкер — строка с минимальным `id` среди warm'ов ноды.
- Два параллельных воркера, взявших разные анкеры, гарантированно смотрят на разные bundle'ы (потому что у анкеров разные `access_username`).
- Третий воркер, попытавшийся взять тот же анкер, что и первый, провалится в `SKIP LOCKED` и увидит следующий по порядку (или `None`, если пул пустой).

Результат: «two workers can ever pick the same bundle even if they hit the table at the exact same instant» (комментарий `warm_pool.py:25-30`), **без** отдельного app-level мьютекса, **без** advisory lock, **без** двухфазного claim.

После анкера — второй `SELECT ... FOR UPDATE` (уже **без** SKIP LOCKED) на всех credential'ах bundle'а по `access_username`:

```python
# warm_pool.py:340-347
bundle = (
    db.query(models.Credential)
    .filter(node_id == node_id,
            access_username == anchor.access_username,
            pool_state == warm)
    .with_for_update()
    .all()
)
```

Второй лок реально никогда не ждёт, потому что инвариант «unique access_username per node» гарантирует отсутствие конкурента. Но комментарий кода честно фиксирует цель (`warm_pool.py:334-339`): «if a future refactor breaks that invariant, the explicit lock makes it safe».

Дальше — голый DB-flip:

```python
for cred in bundle:
    cred.pool_state = CredentialPoolState.assigned
    cred.subscription_id = subscription_id
    cred.assigned_at = now
    cred.is_active = True
db.flush()
```

**Flush, не commit.** Это позволяет caller'у rollback'нуться, если что-то дальше в его транзакции упадёт. `ProvisioningOrchestrator` пользуется этим: warm-assign происходит внутри той же транзакции, что и создание `Device`, так что «частично активированная подписка» невозможна.

Метрики: инкрементится `vpn_warm_pool_hits_total{node}`, обновляется `vpn_warm_pool_depth{node}`. При miss'е caller руками вызывает `record_pool_miss(db, node_id)` и идёт cold-path'ом.

## Two-stage revoke

Главная причина раздвоения — **API-запрос на revoke не должен блокироваться на ansible**. Админ нажал «отозвать устройство» → должен сразу увидеть «готово», а реальное удаление с ноды пусть догонит background'ом.

### Stage 1: `unassign_bundle` (синхронно в API)

`warm_pool.py:378-395`:

```python
for cred in credentials:
    cred.pool_state = CredentialPoolState.revoked
    cred.is_active = False
    cred.revoked_at = cred.revoked_at or now
    pending[(cred.node_id, cred.access_username)] = None
db.flush()
return [f"{n}:{u}" for (n, u) in pending.keys()]
```

Возвращается список `"node_id:username"`-строк, которые caller enqueue'ит в RQ. Всё, API отвечает пользователю.

**Важно:** пользователь может ещё какое-то время ходить через эти креды, пока xray на ноде не получит обновлённый конфиг. Потому что сам reload — stage 2.

### Stage 2: `physical_revoke_credential_bundle` (в воркере)

`warm_pool.py:398-478`. Запускается как RQ-job. Шаги:

1. Ищет строки по `(node_id, access_username, pool_state=revoked)`.
2. Если их **нет** — возвращает `True` (идемпотентно: retry не должен фэйлить job).
3. Если нода удалена (`node is None`) — удаляет записи и возвращает `True`. Нельзя запустить ansible на несуществующей ноде.
4. Собирает `protocols_payload` через dedup по `proto` (bundle может иметь несколько credential'ов с одним и тем же `proto`, если была multi-config ситуация).
5. Запускает `playbooks/provision_device.yml` с `state=absent` под тем же `_warmer_semaphore`.
6. После успешного exit'а — `db.delete()` на все credential'ы bundle'а и `commit`.
7. При non-zero exit возвращает `False` — RQ retry policy перезапустит job.

Это — единственная функция warm_pool, которая реально удаляет строки из таблицы `credentials`.

### `invalidate_node_warm_pool`

`warm_pool.py:481-502`. Вызывается, когда у ноды меняется набор `VPNConfig` (добавили/убрали протокол). Старые warm-bundle'ы собраны под прежнюю конфигурацию xray и уже не соответствуют реальности на ноде, поэтому все warm'ы ноды помечаются revoked. Потом обычный воркер (`physical_revoke_credential_bundle`) их догонит и снимет через ansible.

## Concurrency и семафор

`_warmer_semaphore = threading.Semaphore(WARM_POOL_MAX_CONCURRENT)` (`warm_pool.py:71`, default 2).

Ограничивает одновременные ansible-runs **как при warming, так и при physical revoke** — единый семафор на обе операции. Отдельный от оркестратора (`services.provisioning`), чтобы warming не «ел» параллелизм пользовательского провижининга, а пользовательский — наоборот.

Комментарий `warm_pool.py:67-70` прямо фиксирует: семафор **process-local**. При горизонтальном масштабировании воркеров эффективный параллелизм становится `WARM_POOL_MAX_CONCURRENT × N_workers`. Для однопроцессного прод-деплоя (см. `infrastructure/deployment.md`) это не актуально, но точка зафиксирована.

## Master switch и конфигурация

| env | default | эффект |
|---|---|---|
| `WARM_POOL_ENABLED` | `1` | `0`/`false`/`no` — `ensure_pool` возвращает `{}`, `try_assign_bundle` возвращает `None`. Cold path работает всегда. |
| `WARM_POOL_TARGET` | `10` | глубина пула на одну ноду |
| `WARM_POOL_BATCH_PER_TICK` | `3` | максимум новых warm'ов за одну тику на одну ноду |
| `WARM_POOL_MAX_CONCURRENT` | `2` | размер `_warmer_semaphore` |

Интервал тика warmer'а задаётся через `WARM_POOL_INTERVAL_SECONDS` в воркере (см. `components/worker.md`).

## Метрики

```
vpn_warm_pool_depth{node}        Gauge    — warm bundle count per node
vpn_warm_pool_hits_total{node}   Counter  — successful warm assignments
vpn_warm_pool_misses_total{node} Counter  — fallbacks to cold path
vpn_warm_credential_provision_seconds{node} Histogram — single warm_one_bundle wall-clock
```

Hits/misses — прямой индикатор того, справляется ли warmer. Если `rate(misses) > 0` на стабильной ноде, значит либо `WARM_POOL_TARGET` слишком низкий, либо тик warmer'а слишком редкий для актуального трафика покупок.

## Что warm_pool НЕ делает

- **Не решает, какую ноду выбрать.** Это дело `NodeSelector` в `services/provisioning.py` — выбор ноды под план и нагрузку происходит **до** вызова `try_assign_bundle(node_id, ...)`.
- **Не генерирует sub_token.** `sub_token` принадлежит `Subscription`, а не credential'у. Warm'ы привязываются к уже созданному `Subscription`.
- **Не знает про freeze / renew / trial.** Это biz-layer в `services/balance.py`.
- **Не меняет `access_username` при передаче пользователю.** Имя остаётся warm-*, никакого rename'а.
- **Не управляет provisioning-задачами.** Для hot path `ProvisioningTask` вообще не создаётся — assign не пишет в эту таблицу.

## ⚠️ Неясные места

- **Идемпотентность `warm_one_bundle` при partial failure.** Если ansible прошёл (нода уже имеет user'а), а `db.commit()` упал (например, конфликт на уникальном индексе, OOM в PG), то запись на ноде останется, а в БД — нет. При следующем тике сгенерится **другой** `access_username` и на ноду pushнётся ещё один user. Ни очистки, ни проверки на orphan'ов не видно.
- **`physical_revoke_credential_bundle` ↔ `threading.Semaphore` ↔ RQ concurrency.** RQ по умолчанию гоняет job'ы в одном worker-процессе последовательно. Но если кто-то поднимет `RQ_WORKER_COUNT>1` (через `supervisor`/compose replicas), физический revoke сможет войти в семафор одновременно с warming'ом на разных процессах — см. комментарий к process-local ограничению.
- **Inconsistency в ordering «DB first vs ansible first».** `warm_one_bundle` делает `ansible → db.commit()`. `physical_revoke_credential_bundle` тоже: `ansible → db.delete()`. А `unassign_bundle` — напротив, `db.flush()` без ansible. Это намеренно (revoke делится на два стадии), но читатель видит один модуль с двумя несимметричными порядками операций — явно это нигде не объяснено.
- **`try_assign_bundle` при частично warm bundle'е.** Если по какой-то причине в bundle'е осталась только одна warm-строка (скажем, ручной DELETE в админке), anchor-lock найдёт её, а `bundle = [...]` вернёт только её — функция назначит подписке credential с неполным набором протоколов. Нет валидации «bundle должен содержать N credential'ов, где N = количество активных конфигов ноды».
- **`invalidate_node_warm_pool` коммитит сама.** В отличие от `unassign_bundle` (только `flush`), она делает `db.commit()` в конце. Несимметрично с общим принципом «caller commits», причина не задокументирована.

> ⚠️ См. audit/... — процессно-локальный `threading.Semaphore` в модели concurrency пула.
