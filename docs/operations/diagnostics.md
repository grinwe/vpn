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

## Что НЕ сделано

* **Auto-recovery** — диагностика только наблюдает, не «чинит». Авто-
  reconnect (re-attach по симптому) сознательно вне scope, чтобы избежать
  flapping'а на сетевых дёргах. Если симптом сохраняется N тиков подряд
  — это уже сигнал оператору, не auto-action'а.
* **Telegram-алёрты** — `symptom_detected` пишет только в audit_log.
  Если нужны пуши — расширить `notify_admins` на новый kind.
* **Симптом «handshake свежий, но трафик не ходит»** — пока detector
  ловит только stale-handshake. Логику auto-trigger'а exit-side
  проверок (`peer_on_exit` + `iptables_forward`) можно добавить когда
  накопится статистика, какие именно симптомы стоит за этим следить.
