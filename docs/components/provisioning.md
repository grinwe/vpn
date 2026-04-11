# Provisioning

Слой, который превращает «пользователь купил план» в «на ноде появился новый user, и клиент может подключиться». Живёт в `backend/app/services/provisioning.py` (~1355 строк) и `backend/app/services/ansible_runner.py` (~90 строк).

## Две части задачи

```
                          provision_subscription(user, plan)
                                          │
                                          ▼
                                 choose_node(plan)
                                          │
                                          ▼
                    ┌─────────────────────────────────────┐
                    │   warm_pool.try_assign_bundle(...)  │ ── hit ──► _wire_warm_bundle
                    └─────────────────────────────────────┘              (DB only, no ansible)
                                          │ miss
                                          ▼
                             cold path: build creds, ProvisioningTask,
                             run_task_async → RQ → ansible-playbook
                                          │
                                          ▼
                              _handle_task_outcome → device.status=active
                                          │
                                          ▼
                          _notify_bot_config_ready → audit log "config_ready"
                                          │
                                          ▼
                              bot notification poller доставляет
```

Внутри модуля есть три заметных подсистемы: **выбор ноды**, **создание credential URI** (строители per-protocol) и **orchestrator** (класс, который дергает ansible и обновляет статусы в БД).

## Выбор ноды — `choose_node`

`provisioning.py:37-137`. Не просто `ORDER BY load LIMIT 1`: компонуется шесть фильтров одним запросом.

1. **Pool membership** — если у плана заданы `server_pools`, нода должна принадлежать одному из них. Пустой список пулов = «любая нода подходит».
2. **Explicit node_id** — если caller знает, какую ноду хочет (редко, только админский путь `POST /api/subscriptions` с параметром), все остальные фильтры пропускаются.
3. **Cooldown** — `cooldown_until IS NULL OR cooldown_until < now()`. Нода, только что деградировавшая по здоровью, сидит в cooldown'е, пока воркер не снимет.
4. **Status** — `active` или `registering`. `registering` попала сюда, чтобы bootstrap-task на свежевыспавнутой ноде мог ссылаться сам на себя (до промоушна в `active`).
5. **Health score** — `health_score IS NULL OR health_score >= MIN_HEALTHY_SCORE` (default 50, env). Нода без пришедших health probes считается здоровой по умолчанию — чтобы новая нода не отваливалась из пула до первого пробы.
6. **Exclude list** — caller может попросить «не возвращай эти node_id». Используется миграцией подписки с draining-ноды: `migrate_subscription_to_new_node` подмешивает туда текущую и соседние draining-ноды того же пула.

Дальше — **сортировка по нагрузке**:

```python
# provisioning.py:85-103 (сокращённо)
active_device_count = func.count(models.Device.id).label("active_devices")
rows = (
    query.outerjoin(models.Subscription, ...)
         .outerjoin(models.Device, ...)
         .group_by(models.VPNNode.id)
         .order_by(active_device_count.asc())
         .with_entities(models.VPNNode, active_device_count)
         .all()
)
```

Важный тонкий момент: **нагрузка меряется в устройствах, не в подписках**. Family-подписка с 3 активными устройствами даёт ноде 3× нагрузку относительно Solo с одним устройством. `VPNNode.max_users` (название историческое) — это потолок именно по `Device`, не по `Subscription`. Revoked/disabled устройства исключены.

Дальше — цикл с `SKIP LOCKED` для защиты от конкурентного overcommit'а:

```python
# provisioning.py:105-135
for node, devs in rows:
    if node.max_users is not None and devs >= node.max_users:
        continue
    locked = (
        db.query(models.VPNNode)
        .filter(models.VPNNode.id == node.id)
        .with_for_update(skip_locked=True)
        .one_or_none()
    )
    if locked is None:
        continue  # другой провижинер уже держит блокировку
    # re-check live device count внутри транзакции
    if live_devices >= locked.max_users:
        continue
    return locked
```

Два провижинера, стартовавшие одновременно и увидевшие «у ноды N свободен 1 слот», **не могут** оба его занять — один из них провалится в `SKIP LOCKED` и пойдёт дальше по списку. Re-check после лока защищает от «пока мы сортировали, кто-то уже добавил devices».

Если ни одна нода не прошла фильтры — `RuntimeError("No healthy VPN nodes available for plan")`, и caller отдаёт пользователю HTTP 503.

## Credential builders

`_build_shadowtls_credential`, `_build_vless_reality_credential`, `_build_vless_ws_cdn_credential`, `_build_hysteria2_credential`, `_build_vless_xhttp_credential` (`provisioning.py:155-260`). Все возвращают **строку URI** — готовый к импорту в Hiddify/v2rayNG формат, специфичный для протокола:

```
ss://...         # для ShadowTLS+SS
vless://...      # для VLESS Reality / WS-CDN / xHTTP
hy2://...        # для Hysteria2
```

Общие свойства:

- Каждый билдер читает `config.settings` — это JSONB, куда ansible-роль кладёт результаты первоначального provisioning'а ноды (ключи, domain'ы, паттерны). Секреты — зашифрованы на диске через Fernet (`enc:v1:...` prefix), расшифровываются на лету через `decrypt()`.
- Все URI содержат `#<tag>` suffix, где tag — `<protocol>-<region>-<username>`. Это нужно только для отображения в клиенте (пользователь видит «hy2-fra-user-123»).
- Для ShadowTLS+SS билдер берёт **shared** ss_password и shadowtls_password **на уровне ноды** (`config.settings.ss_password_enc`, `shadowtls_password_enc`) — то есть все пользователи одной ноды используют одни и те же пароли на SS/STLS-слое, различаясь через ShadowTLS v3 handshake и Shadowsocks 2022 EIH (пока что не multi-user; см. комментарий `provisioning.py:161-166`).
- Для VLESS-вариаций используется **per-user UUID**, сгенерированный `uuid.uuid4()`.

## `_collect_site_extra_vars` — мост к ansible ролям

`provisioning.py:280-377`. Для `action = bootstrap/rebootstrap` (site.yml) собирается полный dict extra-vars:

- Ключи декриптятся из `config.settings` и переименовываются в соответствии с ansible-ролью (`install_shadowtls_stack`, `install_vless_reality`, `install_vless_ws_cdn`, `install_vless_xhttp`, `install_hysteria2`).
- **Health-порт дайджест**: собирается список TCP-портов, которые `check_node_health` должна увидеть `LISTEN`ящими. **Hysteria2 намеренно исключён**: `ansible wait_for` умеет только TCP, а hy2 — чистый UDP. Liveness UDP нужна отдельная проверка. Комментарий `provisioning.py:359-361`.
- Если у ноды есть `relay_config` (jump → WireGuard → exit), туда добавляются `relay_wg_*` переменные для роли `relay_jump_node`.

Этот dict потом уходит в `run_playbook(...extra_vars=extra)` как `--extra-vars '{...}'`.

## Orchestrator — класс ProvisioningOrchestrator

`provisioning.py:415`. Стейтлесс обёртка над SQL-сессией, создаётся per-request. Методы:

### Life-cycle провижининг-таска

```
create_task(target_type, target_id, action, payload)
    │  INSERT ProvisioningTask(status=pending)
    ▼
run_task_async(task, node)
    │  enqueue в RQ (см. components/worker.md)
    ▼
run_task(task, node)          ← в воркере
    │  started_at=now, status=running, commit
    ▼
_execute_task(task)            ← собственно ansible
    │  acquire _ansible_semaphore
    │  build_inventory_for_node → temp YAML
    │  run_playbook(path, inventory, limit, extra_vars)
    │  release semaphore, unlink temp inventory
    │  return {stdout, stderr, returncode}
    ▼
branch on returncode
    │
    ├─ 0 ──► _mark_task(success) → _handle_task_outcome(success=True)
    │         │
    │         └─ если action=apply → device.status=active, creds.is_active=True
    │            если action=revoke → DELETE device + credentials
    │            если target=node   → promote registering→active
    │            если success       → _notify_bot_config_ready
    │
    └─ ≠0 ──► tail = last 20 lines of (stderr || stdout)
              _mark_task(failed, error=tail) → _handle_task_outcome(success=False)
```

### `_execute_task` — что именно крутится внутри ansible

`provisioning.py:692-773`. Маршрутизация по `target_type + action`:

| target_type | action | playbook | extra_vars |
|---|---|---|---|
| `node` | `bootstrap` / `rebootstrap` | `site.yml` | `_collect_site_extra_vars(node)` |
| `node` | `resync_vless` | `playbooks/resync_node.yml` | `payload` (список `{username, uuid}`) |
| `node` | `diagnose` | `playbooks/diagnose_node.yml` | `payload` |
| `device` | `apply` / `revoke` | `playbooks/provision_device.yml` | `payload` (username, protocols, state) |

Семафор `_ansible_semaphore = threading.Semaphore(MAX_CONCURRENT_ANSIBLE)` (default 3, env) ограничивает параллельность. Process-local — та же история, что и в warm_pool: при масштабировании воркеров лимит множится на количество процессов.

Inventory **генерится в temp-файл** на каждый запуск (`ansible_runner.build_inventory_for_node`, `ansible_runner.py:32-52`):

```yaml
all:
  hosts:
    <node.name>:
      ansible_host: <node.host>
      ansible_port: <node.ssh_port>
      ansible_user: root
  children:
    vpn_nodes:
      hosts:
        <node.name>:
    db_host:
      hosts: {}
```

После запуска файл unlink'ается в `finally` — чтобы `/tmp` не забивался. Ошибка `unlink` логируется, но не прерывает обработку.

**Важно:** ansible-playbook вызывается через `subprocess.run(..., timeout=300)`. Пять минут — максимум на один playbook-run. Если timeout достигнут — `subprocess.TimeoutExpired` → `RuntimeError("Ansible playbook timed out")`. Это единственная жёсткая граница; сам ansible внутри может иметь свой более короткий `async`/`poll`, но из Python-слоя видим только общее 5-минутное окно.

### `_handle_task_outcome` — что меняется после ansible

`provisioning.py:569-690`. Ветка по `target_type`:

**Node-level tasks:**

- Успех → `status = active`, `last_health_check_at = now()`. Для `registering` это первый промоушн; для уже `active` — refresh health timestamp.
- Успех + нода имеет vless_reality → автоматический **resync_vless**. Причина: `install_vless_reality` роль на первом bootstrap'е render'ит пустой `config.json`, и ранее-активные подписки получают `invalid request user id`. Resync идемпотентен (`manage_vless_user.sh` дроп по email), так что гонять его на каждом успехе дёшево.
- Сбой **только на `registering` ноде** → `status = error`. Уже-`active` ноду **не понижаем** при transient rerun failure — операторский комментарий `provisioning.py:610-614`: «leave it active, surface the task error via /admin/tasks».
- `action == resync_vless` **пропускает** все эти переходы — это follow-up helper, он не должен промоутить/демоутить ноду.

**Device-level tasks:**

- `apply` success → `device.status = active`, все `device.credentials.is_active = True`, `revoked_at = None`.
- `revoke` success → **полное удаление** device и его credentials из БД (`db.delete`). `ProvisioningTask`-строка остаётся для audit'а. Причина (комментарий `provisioning.py:631-635`): revoked-записи засоряли UI и карточку подписки после миграций.
- Сбой любой → `device.status = failed`.

### Bot notification hook

`_notify_bot_config_ready(device)` (`provisioning.py:651-690`). После **успешного** `apply`-task'а orchestrator ищет последний успешный apply-task для того же device'а и рассчитывает, что воркер уведомлений (`worker.run_renewal_check` и коллеги) подберёт его. Реализация — **нестандартная**: уведомление не кладётся в отдельную таблицу, а инкорпорируется в результат task'а, который потом прочтут через notification poller на стороне бота (`bot/bot.py:14-69`). Детали очереди уведомлений через `audit_logs` — в `components/backend-api.md` и `components/bot.md`.

## Warm-pool fast path

Два места, где вызывается `warm_pool.try_assign_bundle`:

1. **`provision_subscription`** (`provisioning.py:863-1018`) — создание новой подписки.
2. **`reprovision_subscription`** (`provisioning.py:1097-1227`) — после unfreeze, когда у подписки нет живых devices.

Структура идентична:

```python
warm_bundle = warm_pool.try_assign_bundle(self.db, node.id, subscription.id)
if warm_bundle:
    try:
        device, task = self._wire_warm_bundle(user, subscription, warm_bundle, device_name)
        ...
        return ...
    except Exception:
        logger.exception("warm-pool wiring failed, rolling back")
        self.db.rollback()
        raise
else:
    warm_pool.record_pool_miss(self.db, node.id)
    # fall through to cold path
```

`_wire_warm_bundle` (`provisioning.py:775-861`) создаёт `Device`, привязывает к нему все credential'ы из bundle (ставит `device_id`), собирает «synthetic» `ProvisioningTask(status=success, action=assign_warm)` — **без** вызова ansible. Task синтетический, чтобы API мог вернуть `SubscriptionProvisionResponse` с task.id, и чтобы WebApp-polling checkout'а сразу видел success. Counter `TASK_STATUS_COUNTER{status=success}` инкрементится вручную — чтобы warm-hits не терялись в метрике «total tasks processed».

В rollback-exception'е есть открытый TODO в комментарии `provisioning.py:923-927`: если `_wire_warm_bundle` упал после того, как `try_assign_bundle` уже пометил bundle `assigned`, rollback вернёт bundle в half-state (assigned без Subscription). Комментарий напрямую говорит «warmer will pick the bundle back up next tick — TODO: GC». GC, соответственно, не написан.

## `migrate_subscription_to_new_node` — поток миграции

`provisioning.py:1229-1306`. Используется `worker.run_drain_tick`, когда нода помечена `draining`.

```
1. choose_node(plan, exclude=[old_node.id, ...other_draining])
2. для каждого живого device старой ноды: revoke_device(background=True)
3. subscription.node_id = target.id; flush; refresh
4. reprovision_subscription(subscription)    ← warm fast path + cold fallback
```

`sub_token` **не меняется** — это единственный persistent identifier, который клиент хранит (в Hiddify-профиле стоит `https://grinwer.online/sub/<token>`). Ротация токена silently сломала бы все установленные конфиги на клиентах.

Защита: если `choose_node` вернул ту же ноду (defensive bug), — `RuntimeError`. Молча re-provisioning'нить на той же draining-ноде запустил бы drain в бесконечный цикл.

Старые devices revoke'аются в **background** (ансибл state=absent на старой ноде), новое устройство проводится по нормальному пути. Между этими двумя моментами существует короткое окно, когда пользователь может подключиться и к старой (не успели revoke), и к новой (уже active) ноде. Это осознанный trade-off ради того, чтобы не блокировать миграцию на ansible-run'е старой ноды.

## `revoke_device` — обычный revoke

`provisioning.py:1308-1345`. В отличие от warm_pool двухстадийного revoke'а (см. `components/warm-pool.md`), здесь всё в одном вызове:

1. Собрать `protocols_payload` (dedup по `proto`, чтобы multi-proto device не пытался revoke'ить один и тот же TCP-порт дважды).
2. Создать `ProvisioningTask(action=revoke, state=absent)`.
3. **Сразу** обновить DB: `device.status = disabled`, `credentials.is_active = False`, `revoked_at = now()`, `pool_state = revoked`.
4. `run_task_async(...)` — ansible запустится в фоне.
5. После успеха `_handle_task_outcome` **удалит** device и credentials полностью (`db.delete`).

Есть `background=False` опция (синхронный путь) — используется только в тестах и в ручных админских операциях, когда caller хочет видеть ansible stdout сразу.

## Metrics

```
vpn_provisioning_tasks_total{status}  Counter  — pending/running/success/failed
                                                 (warm hits тоже инкрементят success)
```

Per-node метрик провижининга нет — есть только `vpn_warm_pool_*{node}` в warm_pool, см. `components/warm-pool.md`.

## Shared state и re-entrancy

- `_ansible_semaphore` — process-local, 3 параллельных ansible'а по умолчанию. При конкурентных покупках на одной и той же ноде семафор ограничивает нагрузку на SSH.
- `ProvisioningOrchestrator.db` — одна SQLAlchemy-сессия per-orchestrator. Caller обязан создавать новую сессию для нового треда/таска. Воркер использует `_run_task_in_new_session` (`provisioning.py:525-558`), который открывает свежий `SessionLocal()` и гарантированно закрывает в `finally`.
- `SUB_LINK_BASE_URL` env — если задан, дает внешний URL для `sub_token` (e.g. `https://grinwer.online/sub/<token>`). Если не задан — возвращается относительный `/api/sub/<token>`, что работает только когда фронт на том же origin'е.

## ⚠️ Неясные места

- **TODO: GC half-assigned warm bundles.** Комментарий `provisioning.py:923-927` напрямую: если `_wire_warm_bundle` упадёт после `try_assign_bundle`, bundle станет assigned без Subscription. Warmer'у не предписано такие подбирать обратно — нет кода, который бы сканировал «assigned без subscription_id».
- **`_execute_task` timeout = 300s жёсткий.** Если ansible `site.yml` на новой ноде с нестабильным сетевым линком физически не успевает за 5 минут, task становится failed без возможности продлить окно через env. Worker re-enqueue сработает с такой же 5-минуткой.
- **Cold path credential'ы записываются с `is_active = False`.** `_handle_task_outcome` ставит их в `True` только после success. Но между commit'ом credential-row и проходом success/failure есть окно, в течение которого активная подписка имеет *неактивные* credentials. Для warm fast path этого окна нет (creds сразу активные), так что UI/sub-link отдают credentials по-разному в зависимости от пути провижининга — см. фильтрацию `is_active` в `api_extensions.dynamic_sub_link`.
- **`revoke_device` устанавливает `device.status=disabled`, а не `revoked`.** Терминальный статус «revoked» достигается только после удаления строки из БД. Каждый другой код (admin UI, фильтры capacity) вынужден считать `disabled` и `revoked` эквивалентными — дублирование логики.
- **`_notify_bot_config_ready` не изолирует ошибки на уровне БД.** Она ловит `Exception` широко (`provisioning.py:689-690`), но **после** `success`-commit'а; то есть если не получилось записать уведомление — task всё равно success, пользователь будет ждать без уведомления до следующего notification-poll'а... которого может и не быть, если bot в этот момент был down.
- **`access_username` с timestamp suffix только в `reprovision_subscription`.** Cold path reprovision добавляет `-<epoch_second>` к username, чтобы избежать TTL collision на ноде (старый username может ещё жить в кэше ansible/xray после state=absent). Обычный `provision_subscription` такого suffix'а не делает — предполагается, что первый username на ноде всегда свежий.

> ⚠️ См. audit/... — subprocess ansible-playbook с расшифрованными секретами в CLI `--extra-vars`.
