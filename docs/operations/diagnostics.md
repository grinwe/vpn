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

### 2. Структурный чек-лист для node/exit

Роли `check_node_health` (node: `xray_service`, `ports_listening`,
`geoip_loaded`) и `check_exit_health` (exit: `wg_interface`, `wg_peers`,
`ip_forward`) пишут тот же `{diag_result_file}` JSON, что и relay-роль
(под `when: diag_result_file is defined` — обычный `site.yml` не
затронут). Парсер — `provisioning.py:_parse_diagnose_result_file`.
Probe-этапы префиксуются. Рендер — тот же `<DiagnoseResult>` в Tasks
**и** Nodes (раньше Tasks показывал голый `JSON.stringify`).

### 3. Reachability-tick + анти-спам инцидентом (`worker.py`)

`run_node_reachability_tick` (env `NODE_REACHABILITY_INTERVAL`,
default 300s) пробит **ВСЕ active VPNNode + WGExitNode** (не только
relay'и). Анти-спам — через **инцидент-стейт** (`services/
diagnostics_state.py::should_diagnose`):

* нода упала → диагностика **ОДИН раз** → говорящий пуш → тишина;
* recovery (`ssh_ok`) → `close_incident` сбрасывает стейт → следующее
  падение = новый инцидент;
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

Юзер жмёт «🆘 VPN не работает» (бот `hping:bad` → `/users/health-ping-
response`, или webapp Help → `/webapp/health-ping-report`) — и backend
сразу делает ему **то же, что админская «обновить подписку»**: через
`_do_failover` → `migrate_subscription_to_free_node` переселяет на
свободную healthy-ноду (sub_token сохраняется) И **банит проблемную ноду
для этого юзера** (`NodeUserBan`), чтобы auto-pick не вернул его назад.
5-мин per-sub throttle (по `client_reported_failure` в AuditLog) не даёт
спамить миграциями.

Плюс **краудсорс «плохости»** (`_escalate_node_failure_reports`): считаем
DISTINCT подписки, пожаловавшиеся на ноду за окно; по порогу — нода
выводится из пула через `cooldown_until` (`choose_node` её пропускает),
открывается diagnose-инцидент + enqueue диагностики + говорящий
admin-push. Идемпотентно: уже cooled-нода повторно не охлаждается. Так
ноды само-ранжируются по реальным юзер-сигналам, а не только по нашему ssh.

`choose_node` теперь исключает и disable-флагнутые ноды
(`auto_diagnose_disabled_at` / `diagnostics_disabled_at`) — централизованно
для всех путей выбора (new subs, admin migrate-auto, failover).

### Env (все с дефолтами в коде)

`NODE_REACHABILITY_INTERVAL=300`, `NODE_REACHABILITY_ENABLED=true`,
`NODE_REACHABILITY_MAX_DIAGNOSE_PER_TICK=4`, `NODE_REACHABILITY_BUDGET_SEC=200`,
`DIAGNOSE_SAFETY_RECAP_HOURS=12`, `ADMIN_ALERT_DIAGNOSIS_WINDOW_SEC=1800`.
Краудсорс: `NODE_FAILURE_REPORT_WINDOW_MIN=60`, `NODE_FAILURE_BAN_THRESHOLD=4`,
`NODE_FAILURE_COOLDOWN_HOURS=2`.

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
  когда накопится статистика.
