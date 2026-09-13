# Диагностика relay→exit WireGuard линков

Спец-страница для оператора: что какая проверка делает, при каких
симптомах её гонять, как интерпретировать результат. Phase 1 (manual)
+ Phase 2 (smart-trigger) задокументированы вместе, потому что
оператору важно понимать, *кто* и *по какому поводу* запустил
конкретную проверку.

## Архитектура

```
                          ┌────────────────────────────┐
   ручная кнопка          │ admin /nodes (expanded) →   │
   ───────────────────►   │ RelayLinksSection            │
                          │   [Диагностировать] кнопка   │
                          └─────────────┬───────────────┘
                                        │ POST /exits/links/{id}/diagnose
                                        ▼
                          ┌────────────────────────────┐
                          │ ProvisioningOrchestrator    │
   smart-trigger          │ task('relay_tunnel',         │
   ───────────────────►   │      relay.id, 'diagnose')   │
   (worker tick           └─────────────┬───────────────┘
    каждые 5 мин)                       │
                                        ▼
                          ┌────────────────────────────┐
                          │ ansible-playbook            │
                          │ diagnose_relay_link.yml      │
                          │ → diagnose_relay_link role   │
                          │ → checks: [...]              │
                          │ → /tmp/diagnose-result-N.json│
                          └─────────────┬───────────────┘
                                        │
                                        ▼ orchestrator парсит JSON
                          ┌────────────────────────────┐
                          │ ProvisioningTask.result =    │
                          │   {stdout, stderr, rc,       │
                          │    checks: [...],            │
                          │    diagnose_meta: {...}}     │
                          └─────────────┬───────────────┘
                                        │
                                        ▼
                          ┌────────────────────────────┐
                          │ admin UI banner             │
                          │   DiagnoseResult карточки    │
                          │   OK/WARN/FAIL/SKIP          │
                          └────────────────────────────┘
```

## Чек-листы

Все checks — read-only на jump-ноде. Запускаются параллельно через
ansible-роль `diagnose_relay_link`, результат каждого = словарь с
полями `name`, `status` (`ok|warn|fail|skip|info`), `latency_ms`,
`message`, `details`.

| check_type             | Что делает                                                                                 | Триггер симптома                                                                                        |
|------------------------|--------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| `peer_on_jump`         | `wg show <iface> dump` на JUMP, ищет `exit.wg_public_key` среди peer'ов                    | Полное отсутствие коннекта; attach отработал, но `wg-quick@wgN` не запустился                          |
| `handshake_age`        | Парсит handshake-epoch из того же dump → возраст в минутах. ok ≤5m, warn 5-15m, fail >15m | Peer есть, но handshake не идёт. Самый частый симптом — UDP firewall на exit, неправильный wg_port    |
| `ping_endpoint`        | `ping -c3 -W2 -I <iface> <exit_wg_addr>` (inner WG IP)                                     | Handshake свежий, но IP не отвечает → kernel routing / iptables на exit'е                              |
| `ping_internet_through`| `curl --interface <iface> https://1.1.1.1` (HTTPS через WG)                                | Ping OK, но юзер не выходит в инет → NAT / forwarding на exit, MTU                                     |
| `xray_port`            | `ss -tlnp \| grep :<diag_xray_port>` (default 9443)                                        | Xray Reality слушает? Юзер коннектится через xray на jump, потом xray уходит в WG-туннель              |
| `listening_sockets`    | `ss -tulpn` — общий снапшот для контекста, всегда `status=info`                            | Никогда автоматом — только когда оператор хочет видеть полную картину listen-сокетов рядом              |
| `peer_on_exit`         | `wg show wg0 dump` на EXIT, ищет `link.wg_client_public_key` среди peer'ов                  | Зеркало `peer_on_jump` с другой стороны. Fail = bootstrap_exit не накатил peer-line в /etc/wireguard/wg0.conf, либо ключи разошлись. |
| `iptables_forward`     | `iptables -L FORWARD -nv` (fallback `nft list chain inet filter FORWARD`) на EXIT, считает ACCEPT-правила с упоминанием `wg0` | Peer есть с обеих сторон, handshake тоже, но трафик в инет не идёт. Часто — default policy `DROP` без явного ACCEPT для wg0. Status `warn` если правила нет, `fail` если iptables вообще не отвечает. |

## Auto-trigger (Phase 2)

`run_relay_link_health_tick` каждые 5 мин (`RELAY_LINK_HEALTH_INTERVAL`)
пишет `RelayExitLink.last_handshake_at` + `last_observed_at`. После
этого вызывается `_auto_diagnose_stale_links(session)`:

Коммит идёт per-relay, а не одним `session.commit()` в конце прохода
(аудит-фикс #102): RQ kill-по-таймауту тика при нескольких лежащих relay
(до ~10с SSH-таймаута на каждый) иначе терял бы обновления ВСЕХ relay,
включая уже успешно опрошенных — `last_observed_at` всего флота протухал
разом и авто-диагностика молча гасла. Relay обходятся в порядке
`last_observed_at` ASC (самый протухший первым), чтобы под повторными
kill не голодал один и тот же хвост.

* **Условие unhealthy**: `last_observed_at` свежий (≤ 8 мин — относится к
  `AUTO_DIAGNOSE_OBSERVED_FRESH_MIN`) И (`last_handshake_at IS NULL` OR
  `last_handshake_at` старше 10 мин — `AUTO_DIAGNOSE_HANDSHAKE_STALE_MIN`).
* **Debounce**: если за последние 30 мин уже был лог `symptom_detected`
  по этому link_id — пропускаем. Контролируется
  `AUTO_DIAGNOSE_DEBOUNCE_MIN`.
* **Что запускается**: `check_types=["peer_on_jump", "handshake_age",
  "ping_endpoint"]` — урезанный набор handshake-side проверок (полный
  ~20s, этот ~5s). Полный набор всегда доступен через ручную кнопку.
* **Audit log**: `action='symptom_detected'`,
  `target_type='relay_exit_link'`, `target_id=link.id`,
  `extra={symptom, last_hs_age_min, action_taken: 'enqueued_task:<id>',
  check_types: [...]}`.

Полностью выключить: `AUTO_DIAGNOSE_ENABLED=false` в env (env.j2 / vault).

## Симптом → ручная диагностика

Что вижу как оператор → какие check_types запросить.

| Симптом                                              | Стартовый набор check_types                                            |
|------------------------------------------------------|------------------------------------------------------------------------|
| «Только что прицепил exit, юзер не работает»         | `peer_on_jump, peer_on_exit, handshake_age, ping_endpoint`              |
| «WG-индикатор в админке красный (handshake stale)»   | `handshake_age, ping_endpoint, ping_internet_through`                  |
| «Connect к VPN устанавливается, но интернет не идёт» | `ping_endpoint, ping_internet_through, iptables_forward`                |
| «Юзер вообще не коннектится»                         | `xray_port, listening_sockets, peer_on_jump`                           |
| «Подозреваю что xray упал»                           | `xray_port, listening_sockets`                                         |
| «Handshake есть, но трафик не ходит»                 | `ping_endpoint, ping_internet_through, peer_on_exit, iptables_forward` |
| «Не понимаю, проверь всё»                            | (пусто — backend возьмёт `DEFAULT_DIAGNOSE_CHECKS` = 8 проверок)        |

## Куда смотреть в коде

| Слой        | Файл                                                                          |
|-------------|-------------------------------------------------------------------------------|
| Ansible role| `infra/ansible/roles/diagnose_relay_link/tasks/main.yml`                       |
| Playbook    | `infra/ansible/playbooks/diagnose_relay_link.yml`                              |
| Backend     | `backend/app/services/provisioning.py:_run_relay_link_diagnose`                |
| Endpoint    | `backend/app/api/exits.py:diagnose_relay_link` (POST `/exits/links/{id}/diagnose`) |
| Smart-trigger | `backend/app/worker.py:_auto_diagnose_stale_links` (вызывается из `run_relay_link_health_tick`) |
| UI control  | `admin/src/pages/Nodes.tsx:LinkDiagnoseButton`                                 |
| UI badge    | `admin/src/pages/Nodes.tsx:AutoDiagnoseBadge`                                  |
| UI render   | `admin/src/diagnoseResult.tsx:DiagnoseResult`                                  |

## Two-play архитектура (Phase 1.5)

Playbook `diagnose_relay_link.yml` теперь содержит два play'я:

1. **Jump-side** (`hosts: vpn_nodes`) — role `diagnose_relay_link`,
   6 проверок (peer_on_jump, handshake_age, ping_endpoint,
   ping_internet_through, xray_port, listening_sockets). Записывает
   JSON в `{diag_result_file}.jump`.
2. **Exit-side** (`hosts: wg_exit_nodes`) — role
   `diagnose_relay_link_exit_side`, 2 проверки (peer_on_exit,
   iptables_forward). Запускается через `include_role` под условием
   `'peer_on_exit' in diag_check_types or 'iptables_forward' in diag_check_types`
   — если ни одной exit-side проверки не запрошено, второго SSH не
   делаем. Записывает JSON в `{diag_result_file}.exit`.

Backend (`provisioning.py:_run_relay_link_diagnose`):
* Создаёт **combined inventory** через `build_inventory_for_relay_link_diagnose`
  (relay в group `vpn_nodes`, exit в `wg_exit_nodes`).
* `--limit` либо `relay.name` (jump only), либо `relay.name,exit.name`
  (с exit-side).
* После прогона читает оба JSON-файла, merge'ит `checks` в один
  список, кладёт `sides: {jump: {...}, exit: {...}}` в `diagnose_meta`.

## Overhaul: staged probe + node/exit reachability + говорящий пуш (2026-06)

Расширение поверх relay-link диагностики выше. Закрывает три боли:
спам диагностиками, отсутствие per-node «выключить», сырой stdout.

### 1. Staged path-to-host probe (`services/diagnostics.py`)

`run_local_path_probe(host, ssh_port, extra_tcp_ports)` гоняется **с
контроллера** (worker-контейнер) ПЕРЕД любым on-host плейбуком:
`ping → tcp:port → ssh-pong` (+ `traceroute`, если ssh не дошёл).
Каждый этап — чек в стандартном контракте `{name,status,latency_ms,
message,details}`. `.ssh_ok` — гейт: **если ssh не дошёл, on-host
плейбук НЕ запускается** (конец бессмысленных ansible UNREACHABLE на
упавшей ноде), on-host этапы помечаются `skip`. Вклинено в
`_execute_task` через `_run_node_diagnose` / `_run_exit_diagnose`.
Worker-образу нужны `iputils-ping` + `traceroute` (см.
`backend/Dockerfile.worker`), иначе эти этапы деградируют в `skip`.
`.ssh_skipped` — отдельный флаг: `True`, когда ssh-стадию НЕ смогли
выполнить (нет `paramiko` / нет файла ключа), а не «проверили —
недоступно». Reachability-тик обязан НЕ метить цель `unreachable` при
`ssh_skipped` — иначе потеря одного ssh-ключа (ротация секрета,
пересборка контейнера, опечатка в `ANSIBLE_PRIVATE_KEY_FILE`) кладёт
весь флот в ложный DOWN и открывает лавину инцидентов (finding #97).

### 2. Структурный чек-лист для node/exit

Роли `check_node_health` (node: `xray_service`, `ports_listening`,
`geoip_loaded`) и `check_exit_health` (exit, 8 чеков: `wg_interface`,
`wg_peers`, `ip_forward`, `wg_listen_port`, `nat_masquerade`,
`internet_egress`, `dns_resolve`, `conntrack`) пишут тот же `{diag_result_file}` JSON, что и relay-роль
(под `when: diag_result_file is defined` — обычный `site.yml` не
затронут). Парсер — `provisioning.py:_parse_diagnose_result_file`.
Probe-этапы префиксуются. Рендер — тот же `<DiagnoseResult>` в Tasks
**и** Nodes (раньше Tasks показывал голый `JSON.stringify`).

### 3. Reachability-tick + анти-спам инцидентом (`worker.py`)

`run_node_reachability_tick` (env `NODE_REACHABILITY_INTERVAL`,
default 300s) пробит **ВСЕ active VPNNode + WGExitNode** (не только
relay'и). Анти-спам — через **инцидент-стейт** (`services/
diagnostics_state.py::should_diagnose`):

* **confirm-окно перед алертом** (`NODE_ALERT_CONFIRM_MIN`, default **20
  мин**; анти-спам на транзиентные блипы): первый непрошедший probe только
  ставит `unreachable_since=now` (suspect), НЕ диагностирует и НЕ пушит.
  Эскалация (диагностика + пуш + открытие инцидента) — лишь когда
  недоступность держится `>= NODE_ALERT_CONFIRM_MIN` (несколько пробов
  подряд при интервале 300с ≈ 4 проба). Единичный 1–2 пропущенных пинга
  recovery очистит `unreachable_since` → серия не накопится, админа не будим.
  `0` = старое поведение (алерт с первого DOWN);
* нода упала и **подтвердилась** → диагностика **ОДИН раз** → говорящий пуш → тишина;
* recovery (`ssh_ok`) → чистит `unreachable_since` + `close_incident`
  сбрасывает стейт → следующее (подтверждённое) падение = новый инцидент.
  Исключение — крауд-инцидент / взятый оператором в работу (см. «Фикс 3 —
  гвард операторского состояния» ниже): его `ssh_ok` НЕ закрывает;
* экспонента (30m→2h→6h) и мьют — **по кнопке из пуша**, не дефолт;
* `should_diagnose` — единый гейт для всех триггеров (заменил три
  раздельные 30-мин AuditLog-дебаунс зоны). Relay-тик больше НЕ делает
  node-down детект и infra_ssh-пуш — это всё ушло сюда.

### 4. Два независимых тумблера (per-node/exit)

* `diagnostics_disabled_at` — **hard-стоп ВСЕХ** диаг-тасок (авто +
  ручные: гейт в `_run_node_diagnose`/`_run_exit_diagnose` + worker);
* `alerts_muted_until` — молчание admin-пушей до TTL («замутить N
  часов / совсем»).

Старый `auto_diagnose_disabled_at` — legacy combined-флаг, забэкфилен в
`diagnostics_disabled_at` миграцией `0039`; `is_diagnostics_disabled`
читает обе колонки.

### 5. Говорящий пуш + inline ack/mute/follow

`admin_notify.notify_node_diagnosis` рендерит резюме чек-листа в текст
(kind `node_diagnosis`, dedup per-target за `ADMIN_ALERT_DIAGNOSIS_
WINDOW_SEC`, default 30m) и кладёт `target_kind`/`target_id` в `extra`.
Бот строит клавиатуру `node_diagnosis_keyboard` и роутит callback
`diag:<action>:<kind>:<id>[:<hours>]` → `POST /api/diagnostics/{kind}/
{id}/{action}` (`api/diagnostics.py`, generic node|exit, `require_admin`):
`ack` (вижу-работаю → стоп до recovery), `mute` (1/4/24ч/совсем),
`follow` (включить экспоненту).

### 6. Краудсорс здоровья нод (user-report-driven)

Юзер жмёт «🆘 VPN не работает» (бот, кабинет Help, страница по саб-токену
или ответ `hping:bad` на плановый пинг) — и backend сразу его **чинит**
через общее ядро `services/self_repair.py` (унификация 2026-09-12,
[vpn_broken_channels_parity_2026_09_12.md](vpn_broken_channels_parity_2026_09_12.md)).
Основной путь — per-device лестница (перетасовка протоколов → перенос ноды →
дубль лега): sub_token сохраняется, соседние устройства не трогаются, ноду
user-wide НЕ банит. Whole-sub переезд («🔁 Все мои устройства», `/newconfig`,
ответ «плохо» при нескольких устройствах) делает **то же, что админская
«обновить подписку»**: `_do_failover` → `migrate_subscription_to_free_node`
переселяет всю подписку на свободную healthy-ноду И **банит проблемную ноду
для этого юзера** (`NodeUserBan`), чтобы auto-pick не вернул его назад.
Спам ограничивает единая политика повторов — `SELF_REPAIR_THROTTLE_SEC=120`
и `SELF_REPAIR_DAILY_MAX=5` **по подписке** (по свежим `OperatorNodeReport`,
`reported_at` индексирован), одна на все каналы вместо прежних разрозненных
5-минутных окон.

Плюс **краудсорс «плохости»** (`_escalate_node_failure_reports`): считаем
DISTINCT подписки, пожаловавшиеся на ноду за окно; по порогу — нода
выводится из пула через `cooldown_until` (`choose_node` её пропускает),
открывается diagnose-инцидент + enqueue диагностики + говорящий
admin-push. Идемпотентно: уже cooled-нода повторно не охлаждается. Так
ноды само-ранжируются по реальным юзер-сигналам, а не только по нашему ssh.
Счётчик берётся из `operator_node_reports` по `failed_node_id` (btree-
индекс), а не JSONB-containment'ом по `audit_logs` — это user-facing путь
под нагрузкой на аварии ноды, полный скан таблицы аудита внутри запроса
недопустим. Голосуют ВСЕ user-driven каналы: бот, кабинет, страница по саб-токену и
control-channel — каждый успешный ПЕРЕНОС (`migrated`) пишет
`OperatorNodeReport` с `failed_node_id` = старая нода и после миграции
зовёт ту же эскалацию. Шаги `reshuffled` / `duplicated` голоса против ноды
не дают намеренно (`failed_node_id=None`): они отвечают на «режут
транспорт», а не «нода мертва».

Авто-баны `NodeUserBan` при этом не вечные: перед каждым user-driven
failover'ом протухшие авто-баны юзера снимаются (TTL, env
`NODE_USER_BAN_TTL_HOURS=48`; ручные админ-баны не трогаются), а при
`NODE_USER_BAN_MAX_PER_USER=3` свежих авто-банах миграция идёт **без**
бана старой ноды — юзер с проблемой на своей стороне (оператор/локальный
RKN) не выжигает себе весь пул нод в вечный `no_target`.

`choose_node` теперь исключает и disable-флагнутые ноды
(`auto_diagnose_disabled_at` / `diagnostics_disabled_at`) — централизованно
для всех путей выбора (new subs, admin migrate-auto, failover).

### Env (все с дефолтами в коде)

`NODE_REACHABILITY_INTERVAL=300`, `NODE_REACHABILITY_ENABLED=true`,
`NODE_ALERT_CONFIRM_MIN=20` (confirm-окно перед алертом; 0 = алерт с первого DOWN),
`NODE_REACHABILITY_MAX_DIAGNOSE_PER_TICK=4`, `NODE_REACHABILITY_BUDGET_SEC=200`,
`DIAGNOSE_SAFETY_RECAP_HOURS=12`, `ADMIN_ALERT_DIAGNOSIS_WINDOW_SEC=1800`,
`NODE_INCIDENT_RECONCILE_MAX_AGE_MIN=30` (бэкстоп авто-закрытия, см. раздел 2026-06-21).
Краудсорс: `NODE_FAILURE_REPORT_WINDOW_MIN=60`, `NODE_FAILURE_BAN_THRESHOLD=4`,
`NODE_FAILURE_COOLDOWN_HOURS=2`. Авто-баны: `NODE_USER_BAN_TTL_HOURS=48`
(0 = TTL выключен), `NODE_USER_BAN_MAX_PER_USER=3` (0 = потолок выключен).

### Код overhaul'а

| Слой | Файл |
|------|------|
| Staged probe | `backend/app/services/diagnostics.py` |
| Инцидент + тумблеры | `backend/app/services/diagnostics_state.py` |
| Tick | `backend/app/worker.py:run_node_reachability_tick` |
| Пуш | `backend/app/services/admin_notify.py:notify_node_diagnosis` |
| Эндпоинты | `backend/app/api/diagnostics.py` |
| On-host роли | `infra/ansible/roles/check_node_health`, `check_exit_health` |
| Bot | `bot/handlers.py:node_diagnosis_keyboard`/`diag_control`, `bot/bot.py` |
| Migration | `backend/app/alembic/versions/0039_node_diagnostics_state.py` |

## Что НЕ сделано

* **Auto-recovery** — диагностика только наблюдает, не «чинит». Авто-
  reconnect (re-attach по симптому) сознательно вне scope, чтобы избежать
  flapping'а на сетевых дёргах.
* **Параллельный пробинг** — `run_node_reachability_tick` пробит ноды
  серийно; при многих упавших (каждая ждёт ssh-таймаут) тик медленный.
  При росте флота — thread-pool. Пока серийно + cap на диагнозы/тик.
* **Симптом «handshake свежий, но трафик не ходит»** — relay-detector
  ловит только stale-handshake. Exit-side auto-trigger можно добавить
  когда накопится статистика. *(частично закрыто: `internet_egress`/
  `nat_masquerade`/`dns_resolve` чеки ниже дают видимость, но авто-триггер
  по ним ещё не подключён.)*

## Паритет exit-диагностики + idle-туннели + self-heal тиков (2026-06-09)

### Exit-чеки до уровня relay-link (пункт 1)
`check_exit_health` теперь 8 чеков (было 3) — exit это терминус egress,
нужна видимость «реально ли уходит трафик». Добавлены:
`wg_listen_port` (UDP wg-порт слушает), `nat_masquerade` (MASQUERADE/SNAT
на egress — без него трафик из туннеля не наатится наружу, юзеры без инета),
`internet_egress` (`curl https://1.1.1.1` с самого exit'а — настоящий
выход в сеть), `dns_resolve` (DNS с exit'а), `conntrack` (заполнение
таблицы). Контракт тот же `{name,status,latency_ms,message,details}`,
рендер — `<DiagnoseResult>`.

### Idle-aware туннели (admin/src/linkHealth.tsx)
`linkHealth()` красил relay↔exit в красный по stale-handshake. Но WG без
трафика handshake не делает → **пустая нода (0 active_users) показывала все
туннели красными, будучи здоровой** (кейс 4vds-ru-kmr: жива, SSH/observed
свежий, юзеров нет → «горело»). Теперь при `activeUsers === 0`
stale/нет-handshake = **`idle` (серый)**; красный остаётся только когда
юзеры ЕСТЬ, а handshake протух (реальный обрыв). `active_users` уже в
`NodeOut`, прокидывается `Nodes.tsx → HealthDots → linkHealth`. SSH-stale
(`observed_at` старый) по-прежнему красный — это реальная недоступность
relay, не idle.

### Self-heal тиков от битого RQ-джоба (queue.py)
`schedule_tick`/`enqueue_task` ловили только `NoSuchJobError`. Битый
job-hash (`KeyError('created_at')` — хэш потерял поля, но registry на него
ссылается) пробивал наружу → тик **не планировался НАВСЕГДА**. Наблюдали:
`tick-node-reachability` висел 12ч → recovery нод не детектился →
инциденты на ожившие ноды замерзали (тот же 4vds-ru-kmr: SSH ожил, а
«probe: unreachable / инцидент» висели намертво). Теперь оба ловят и битый
job → чистят сырой ключ `rq:job:<id>` → свежий enqueue. Один битый запис
больше не вешает тик. Расклинить уже-битый прод-джоб: **deploy** (новый код
вычистит на рестарте воркера) либо вручную `redis del
rq:job:tick-node-reachability` + рестарт `worker-scheduler`.

## Бэкстоп авто-закрытия инцидентов + ручной close (2026-06-21)

Закрывает класс «красный 🔴 инцидент висит, хотя нода уже здорова» (`probe:
ok`, SSH свежий, WG зелёный) — тот же симптом, что замёрзшие инциденты после
12ч-сталла тика (self-heal выше), но по другим причинам.

### Почему инцидент залипает
Штатное закрытие живёт ТОЛЬКО в `run_node_reachability_tick`: ветка
`if probe.ssh_ok:` зовёт `close_incident` немедленно на первом удачном пробе
(`worker.py`). До этой ветки можно НЕ дойти:
* тик обрезан wall-clock бюджетом (`NODE_REACHABILITY_BUDGET_SEC`) — хвост
  списка целей в этот цикл не пробивается, рекавери-проб пропущен. С
  аудит-фикса #95 (2026-07) хвост больше не голодает *систематически*:
  цели обходятся по `last_probe_at` ASC NULLS FIRST (самые давно не
  пробованные — первыми), так что обрезанный хвост идёт первым в следующем
  тике; само голодание видно в гейдже `vpn_reachability_stale_targets`
  (целей без проба дольше `NODE_REACHABILITY_STALE_MIN`, default 30 мин).
  Разовый пропуск рекавери-проба в конкретном тике всё ещё возможен;
* инцидент открыт **крауд-путём** (`client_control.py`, ≥4 жалобы за час) на
  SSH-здоровой ноде — `last_probe_status` и так `ok`, ssh_ok-ветке нечего
  закрывать, рассинхрон крауд-инцидента и проба никто не сводит;
* тик подвисал (битый RQ-джоб).

Асимметрия: открытие — с confirm-окном `NODE_ALERT_CONFIRM_MIN` (20 мин),
закрытие — одним `ssh_ok` без гистерезиса; поэтому любой пропуск этой ветки
оставляет бейдж красным при живой ноде.

### Фикс 1 — бэкстоп-реконсиляция (авто)
`diagnostics_state.reconcile_healthy_incident(target, now, max_age_min)` +
вызов в начале `run_node_reachability_tick` ОТДЕЛЬНЫМ дешёвым проходом по всем
active-целям, ДО бюджетного цикла (без SSH → не голодает под бюджетом).
Закрывает инцидент iff: он открыт, `last_probe_status=='ok'`, серии падений нет
(`unreachable_since is None`) и проб свежий (`last_probe_at` ≤
`NODE_INCIDENT_RECONCILE_MAX_AGE_MIN`, default 30 мин — чтобы не действовать по
протухшей телеметрии подвисшего тика). Закрытые рефы → `summary["reconciled"]`.
Штатное немедленное закрытие на `ssh_ok` остаётся как было. Тест:
`test_reachability_confirm.py::test_reachability_backstop_reconcile`.

### Фикс 2 — ручное закрытие (оператор)
`POST /api/diagnostics/{kind}/{id}/close` (`api/diagnostics.py`, generic
node|exit, `require_admin`). В отличие от `ack` (тот лишь глушит ре-диагностику,
ОСТАВЛЯЯ бейдж) — снимает сам инцидент: `close_incident` + сброс
`unreachable_since`. Если нода реально недоступна, ближайший тик заново
подтвердит за `NODE_ALERT_CONFIRM_MIN` и откроет свежий инцидент. Аудит
`diagnose_incident_closed`, ответ включает `was_open`. Тесты:
`test_diagnostics_close.py`.

UI: кнопка «✕ закрыть» рядом с 🔴-бейджем в строке ноды
(`admin/src/pages/Nodes.tsx`, мутация `closeIncident` → `diagnosticsClose` в
`admin/src/api.ts`); `confirm()` поясняет, что серия сбросится и тик может
открыть заново. Пока только для нод (`kind="node"`).

### Фикс 3 — гвард операторского состояния (#98)
Обратная проблема к «залипанию»: крауд-инцидент на **SSH-здоровой** ноде (юзеров
блокирует DPI/РКН, контроллеру нода доступна) закрывался *слишком* рьяно.
`ssh_ok`-ветка и бэкстоп-реконсиляция звали `close_incident` КАЖДЫЙ тик (ssh_ok
у такой ноды всегда `true`), а он обнуляет `diagnose_acked_at` и
`diagnose_follow_mode`. Итог: нажатый оператором в крауд-пуше `ack`/`follow`
молча сбрасывался максимум через один тик (~5 мин), и после истечения
`NODE_FAILURE_COOLDOWN_HOURS` крауд-порог пушил заново, будто оператор ничего
не жал.

Фикс — `worker._incident_auto_close_blocked(target, now)` перед обоими
авто-закрытиями (`ssh_ok`-ветка и реконсиляция). Возвращает `True` (не
закрывать автоматически), если инцидент открыт И выполнено любое:
* оператор взял в работу — `diagnose_acked_at ≥ open_at` или
  `diagnose_follow_mode=='exponential'`;
* нода выведена крауд-порогом — `cooldown_until` в будущем (только `VPNNode`;
  у `WGExitNode` поля нет → ветка не срабатывает).

Ручной close (Фикс 2, кнопка оператора) идёт мимо гварда — оператор всегда
закрывает сам. Как только `cooldown_until` истёк и оператор не залочил стейт,
авто-закрытие снова работает как раньше. Тесты:
`test_auditfix3_Y_worker_retention.py` (`test_guard_*`,
`test_crowd_incident_survives_ssh_ok_tick`, `test_plain_incident_still_closes_on_ssh_ok`).

### Env
`NODE_INCIDENT_RECONCILE_MAX_AGE_MIN=30` — макс. возраст `last_probe_at`, при
котором бэкстоп доверяет `ok` и закрывает; `<=0` снимает проверку свежести.
