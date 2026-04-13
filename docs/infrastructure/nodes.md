# VPN ноды

Документ про **ноды-exit'ы**: кто их регистрирует, какой у них жизненный цикл, какие на них роли выполняются и что значит каждая колонка в `VPNNode`. Детали про оркестратор ansible — в `infrastructure/ansible.md`, про орchestration из backend'а — в `components/provisioning.md`. Здесь — перспектива самих нод.

## Модель: `VPNNode` и её колонки

`backend/app/models.py:183-220`. Ключевые поля и зачем они существуют:

```
id, name, region, host, ssh_port     — идентичность и сеть
status (enum VPNNodeStatus)          — жизненный цикл (см. ниже)
is_active (bool)                     — ручной kill switch
pool_id → ServerPool                 — логическая группировка (план → пул → нода)

max_users, max_bandwidth_mbps        — ёмкость (используется autoscale)
health_score (int, nullable)         — агрегат от health-probe'ов; NULL = нет данных (см. ниже)
last_health_check_at                 — timestamp последнего check
cooldown_until                       — DateTime, до которой NodeSelector не берёт ноду
blocked_regions (JSONB)              — регионы, из которых ноду НЕ отдавать
relay_config (JSONB)                 — если set, нода — jump-node, нужна role relay_jump_node

provider_id → CloudProvider          — автор спавна (или NULL для ручной)
provider_external_id                 — id у cloud provider'а (hetzner server id и т.п.)
provider_region, provider_plan       — для autoscale math и для destroy_server
monthly_cost (Numeric)               — для отчётов / cost tracking
```

`relay_config` — это то, чем различаются «обычная» exit-нода и RU jump-нода в relay-схеме (см. секцию «Архитектуры трафика» ниже).

### VPNConfig — что нода умеет отдавать

Каждая нода имеет 0..N строк в `vpn_configs`:

```python
# models.py:223-241
class VPNConfig(Base):
    node_id, name, protocol (enum), port, sni, public_key, fallback,
    settings (JSONB — зашифрованные ключи/пароли через Fernet),
    is_enabled (bool)
```

Протоколы в enum'е `VPNConfigProtocol`:

- `vless_reality` — VLESS + Reality (основной)
- `vless_xhttp` — VLESS + XHTTP (основной TCP, обход 16KB curtain ТСПУ)
- `vless_ws_cdn` — VLESS + WebSocket за Cloudflare
- `hysteria2` — UDP через QUIC (fallback на стабильных сетях)
- `shadowtls_ss` — ShadowTLS v3 + shadowsocks-rust (legacy, без per-user isolation)

**Одна нода может отдавать несколько протоколов одновременно.** `_collect_site_extra_vars` (`provisioning.py`) читает все `VPNConfig` ноды, расшифровывает их `settings` и передаёт всем протокольным ролям как `extra_vars` за один прогон `site.yml`. Если VPNConfig нет — роль встречает пустой `*_password`/`*_domain` переменную, срабатывает guard `meta: end_role` и тихо пропускает себя.

## Жизненный цикл ноды

```
enum VPNNodeStatus (models.py:50-61):

  registering ──(site.yml OK + health OK)──▶  active
       │                                         │
       │                                         ├──(manual admin flip)──▶ disabled
       │                                         │
       │                                         ├──(downscale mark)─────▶ draining ──▶ destroy ──▶ disabled
       │                                         │
       │                                         └──(probe fail N раз)──▶ error (NOT promoted back automatically)
       │
       └──(bootstrap failed)─────────────────▶ error
```

### `registering`

Начальное состояние, когда строка только-только создана:

- **ручная регистрация**: `POST /api/nodes` — админ заводит запись в БД. Статус `registering`, `is_active=True`, `health_score=NULL` (нет проб → `choose_node` всё равно берёт, т.к. `IS NULL` проходит фильтр).
- **автоспавн**: `node_spawner.spawn_node` (`backend/app/services/node_spawner.py:154-221`). Порядок:
  1. `driver.create_server(...)` — вызов у Hetzner/Vultr/DO/Aeza/manual. Блокирующий (30–90с).
  2. `VPNNode(status=registering, provider_id=..., provider_external_id=...)` в БД.
  3. `ensure_reality_config(node)` — сгенерировать Reality ключи и сохранить `VPNConfig`.
  4. `ProvisioningOrchestrator.create_task("node", node.id, "bootstrap")` + `run_task_async` — в воркер.

После этого воркер запускает `playbooks/site.yml` против этой ноды и ждёт успеха + health pass. Промоут `registering → active` делается в `ProvisioningOrchestrator._handle_task_outcome` (см. `components/provisioning.md`).

### `active`

Рабочее состояние. `NodeSelector.choose_node` (`provisioning.py:37`) рассматривает **только** ноды с:

```python
is_active == True
status in {active, registering}    # registering тоже берётся, см. ниже
(cooldown_until is None or cooldown_until <= now)
health_score >= MIN_HEALTHY_SCORE
id not in exclude_node_ids
```

`registering` в выборке — намеренный компромисс: у свежей ноды может быть короткое окно, когда статус ещё не флипнулся в `active`, но ансибл уже закончил и health прошёл. Пустить туда платного пользователя можно, если всё остальное OK.

### `disabled`

Нода выключена — либо руками админом через SPA (`/admin/nodes/{id}` → flip `is_active`), либо автоматически после `destroy_node` по завершении draining. В `choose_node` не попадает вообще. Строку **не удаляем**: на ней могут всё ещё висеть `Subscription.node_id`, `Credential.node_id`, `HealthProbe.node_id` — удаление строки без `cascade` уронит FK.

### `draining`

Отдельный статус для downscale-дорожки (stage 5, `autoscale.evaluate_pool_downscale` + `worker.run_drain_tick`). Семантика:

- **Не** возвращается в `_eligible_nodes` — autoscale не считает ни capacity, ни utilization draining-ноды.
- **Не** возвращается в `choose_node` — новые подписки сюда не едут.
- Старые подписки продолжают работать — `Credential.is_active` не меняется, `Device` остаётся apply'нутым.
- `run_drain_tick` (`worker.py:72-204`) батчами по `DRAIN_MIGRATE_BATCH` (default 10) мигрирует подписки на другие ноды того же пула через `migrate_subscription_to_new_node`.
- Когда `live_subs == 0` **и** `(now - updated_at) >= AUTOSCALE_DRAIN_GRACE_HOURS`, вызывается `destroy_node` — cloud API destroy + `status=disabled`.

Grace использует `updated_at`, не момент marking'а — каждый `migrate_subscription_to_new_node` трогает ноду, так что таймер перезапускается. Это не баг: мы хотим `grace_hours` **тишины** после последней миграции, не с момента marking'а.

### `error`

Нода в ошибочном состоянии — bootstrap упал, health стабильно плохой, `destroy_node` получил exception от провайдера. `_eligible_nodes` специально исключает `error` из подсчёта `counted_nodes` при autoscale-решении «есть ли ещё места», **иначе** одна сломанная нода вечно держала бы пул на `max_nodes` и новые уже не спавнились бы (`autoscale.py:244-246`).

**Из `error` нода никуда автоматически не выходит.** Требуется либо `/admin/nodes/{id}` flip, либо ручной `destroy_node`.

## Health score и cooldown

`health_score` — целое 0–100, **или NULL** (нет данных). Source of truth — `services/health.py:recompute_node_health`, которая агрегирует `health_probes` за последние 15 минут (`LOOKBACK`).

Формула: `health_score = round(ok_count / total_count * 100)`. Если проб за окно нет — score сбрасывается в `NULL` (в UI «нет данных»). `choose_node` трактует `NULL` как eligible (`IS NULL` проходит фильтр), чтобы свежая нода не отваливалась из пула до первого проба.

Если site.yml (bootstrap) упал на **уже active** ноде, `health_score` ставится в 0 (без демоута статуса, чтобы сохранить существующих пользователей), и autoscale перестаёт на неё сажать новых.

`cooldown_until` — отдельный механизм для случая «нода недавно что-то натворила, дадим ей отдохнуть, но не выключаем». Проставляется, например, после fail'а provisioning-таска или временной ошибки API. До истечения ноду не возьмут ни пользовательский `choose_node`, ни autoscale.

## Архитектуры трафика

Код поддерживает **две** модели, выбираемые на уровне ноды через наличие/отсутствие `relay_config`.

### Прямая (standalone exit)

```
client ──TLS (ShadowTLS/Reality/XHTTP/WS+CDN/Hy2)──▶ node :443 ──▶ Internet
```

Одна нода, один процесс xray/shadow-tls/hysteria, прямой выход в интернет. Трафик идёт через `default_ipv4.interface` по умолчанию — никакого WireGuard, никакого роутинга.

Большинство нод в обычной зарубежной локации (NL, DE, PL) — standalone exit'ы.

### Relay (RU jump → foreign exit)

```
client ──TLS (Reality, RU :443)──▶ jump node (RU) ──WireGuard tunnel──▶ exit node (NL) ──▶ Internet
```

Схема нужна, когда клиенту важно подключаться к RU-IP (TCP handshake к российскому адресу меньше вызывает внимания ТСПУ), но исходящий трафик должен выходить из «чистой» локации.

**Jump-нода**:

- `VPNNode.relay_config` = `{"wg_private_key": "...", "wg_address_v4": "10.77.0.2/24", "wg_endpoint": "<exit_ip>:51820", "wg_exit_public_key": "...", ...}`.
- В `site.yml` (см. `infrastructure/ansible.md`) на неё выполняется роль `install_vless_reality` **плюс** `relay_jump_node` — последняя ставит WG-клиента и **патчит через jq** существующий `config.json` xray, добавляя `sockopt.interface: wg0` в freedom-outbound. Тем самым весь пользовательский трафик уходит в wg0, а SSH/ansible — через основной интерфейс.
- Hysteria2 **не патчится** этим способом (UDP route через kernel routing table — комментарий в `relay_jump_node/tasks/main.yml`).

**Exit-нода**:

- Голый WireGuard-сервер. Ansible-роль `wg_exit_node/tasks/main.yml`:
  - `apt install wireguard wireguard-tools`;
  - `sysctl net.ipv4.ip_forward=1`, `net.ipv6.conf.all.forwarding=1`;
  - render `/etc/wireguard/wg0.conf` из `wg0.conf.j2` с peer-листом (список jump-нод, каждая со своим pubkey и allowed_ips);
  - `iptables -t nat POSTROUTING MASQUERADE` на `default_ipv4.interface`;
  - `iptables FORWARD ACCEPT` в обе стороны (in/out wg0).
- **Не** ставит xray, shadow-tls, hysteria. **Не** живёт в `vpn_nodes` таблице (обычно) — это «inventory-only» машина, про которую БД ничего не знает. Её `ansible_host` прописан в group `wg_exit_nodes` статического inventory.
- На момент написания группа `wg_exit_nodes` в `inventories/prod/hosts.yml` **пустая** — relay-cхема описана в коде, но в проде не работает. См. ⚠️ ниже.

## Роли, запускаемые на ноде

Полная цепочка из `site.yml` для группы `vpn_nodes`:

```
bootstrap_node               — user/group, SSH hardening, UFW, базовые пакеты
install_shadowtls_stack      — всегда в списке, gated на shadowtls_password
install_vless_reality        — всегда в списке, gated на vless_reality_private_key
install_vless_xhttp          — всегда в списке, gated на vless_xhttp_domain
install_vless_ws_cdn         — всегда в списке, gated на vless_ws_cdn_domain
install_hysteria2            — всегда в списке, gated на hysteria2_port
relay_jump_node              — только если relay_config проставлен
install_probe_agent          — health agent, пушит наружу
install_traffic_collector    — xray stats API scraper (per-user bytes)
install_sharing_enforcer     — локальный демон: access log → detect >1 IP/UUID → xray gRPC rmuser/adduser cycle
node_exporter                — prometheus node_exporter
check_node_health            — ассертит, что все ожидаемые порты слушают
```

Каждая протокольная роль реализует **единый паттерн guard'а**:

```yaml
# пример из install_shadowtls_stack/tasks/main.yml:31-42
- name: Stop ShadowTLS services when protocol is removed
  systemd:
    name: "{{ item }}"
    state: stopped
    enabled: no
  loop: [shadow-tls, ssserver]
  when: shadowtls_password is not defined or shadowtls_password | length == 0
  failed_when: false

- name: Skip role when ShadowTLS is not configured
  meta: end_role
  when: shadowtls_password is not defined or shadowtls_password | length == 0
```

Идея: роль **сама** решает, нужна ли она на этой ноде, по наличию своей переменной. Если backend отдаёт только `shadowtls_password` — `install_vless_reality` и остальные упадут в guard и `end_role`. Если на ноде был старый VLESS и его убрали из `VPNConfig`, роль сама **останавливает systemd unit'ы** вместо того, чтобы игнорировать.

**Auto-recovery из failed-state.** Каждая xray-роль (`install_vless_reality`, `install_vless_xhttp`, `install_vless_ws_cdn`) в конце play'а проверяет, что её systemd unit действительно `active`. Если нет — `reset-failed` + `restarted`. Это страхует сценарий: предыдущий запуск упал (permission denied / bad config), unit в `failed` state, `RestartPreventExitStatus=23` запрещает auto-restart, а на следующем прогоне config template не изменился → handler не стреляет → `state: started` не восстанавливает unit, попавший в StartLimitBurst. Safety-net ловит и рестартит.

### Per-user управление

Helper-скрипты на нодах (устанавливаются соответствующими ролями в `/usr/local/sbin/`):

| скрипт | протокол | механизм |
|---|---|---|
| `manage_vpn_user.sh` | ShadowTLS+SS | **no-op с audit-логом** — все девайсы делят один ss-rust password. Persist нет, логирование в `/var/log/shadowtls-users.log`. Legacy-протокол, без per-user isolation. |
| `manage_vless_user.sh` | VLESS Reality | `jq` patch `/usr/local/etc/xray/config.json` → `settings.clients` (add/del by email) → `systemctl restart xray`. Поддерживает `NO_RESTART=1` для batch-операций. |
| `manage_vless_ws_user.sh` | VLESS WS+CDN | аналогично, но для `config_ws_cdn.json` + restart unit `xray-ws-cdn`. `NO_RESTART=1`. |
| `manage_vless_xhttp_user.sh` | VLESS XHTTP | аналогично, `config_xhttp.json` + `xray-xhttp`. `NO_RESTART=1`. |
| `manage_hy2_user.sh` | Hysteria2 | edit `/etc/hysteria/config.yaml` → restart `hysteria-server`. |

**`NO_RESTART=1`** — переменная окружения, пропускает `systemctl restart` внутри manage-скрипта. Используется в `resync_node.yml` для batch-добавления клиентов: все add'ы проходят без рестарта, единый restart в конце батча. Без этого при ≥6 клиентах systemd rate-limit (`StartLimitBurst`) блокирует перезапуск.

ShadowTLS — единственный, где helper **не** меняет состояние. Причина в `files/manage_vpn_user.sh:9-27`: v1 использует один shared password per node, per-user separation требует SS2022 EIH и ещё не реализован. Helper остаётся в pipeline для audit-trail'а и чтобы `provision_device.yml` мог единообразно звать `add`/`del` для любого протокола.

### check_node_health — диагностика-first

`check_node_health/tasks/main.yml` намеренно переупорядочен так, что ассерт на порты идёт **последним**:

1. `wait_for port=...` с `failed_when: false` — непадающий probe.
2. `ss -tulpn` — сбор слушающих сокетов.
3. `systemctl status xray shadow-tls ssserver hysteria-server` + `journalctl -n 20` — unit status + свежий лог.
4. `debug` оба вывода.
5. **Только после этого** — `assert` что все `port_probe.results` не failed.

Причина (inline-комментарий `check_node_health/tasks/main.yml:3-16`): при старом порядке fail на `wait_for` обрывал play до того, как диагностика собиралась, и оператор видел только `Timeout waiting for 127.0.0.1:443` без понимания почему. При journald в volatile-режиме (а VPN-ноды часто такие) после fail'а и вовсе нечего смотреть. Новый порядок гарантирует, что дамп состояния уже в логах playbook'а **выше** строки fail.

## Autoscale pool math

`autoscale.evaluate_pool` (`services/autoscale.py`). При входе в функцию берётся `pg_try_advisory_xact_lock` (#61) — если другой worker уже оценивает этот пул, вызов возвращает `skipped` без побочных эффектов. Это предотвращает двойной spawn при горизонтальном масштабировании worker'ов. Ключевые формулы:

```
eligible_nodes   = [n for n in pool.nodes if n.is_active
                    and n.status in {active, registering}
                    and not in_cooldown(n)
                    and n.health_score >= MIN_HEALTHY_SCORE]

capacity         = sum(n.max_users or FALLBACK_NODE_CAPACITY for n in eligible_nodes)
active           = COUNT(Device where node in eligible_nodes and Device.status not in {revoked, disabled})
utilization      = active / capacity   # 1.0 если capacity == 0

if utilization >= high_watermark (default 0.8) and len(counted_nodes) < max_nodes:
    spawn_node(primary_provider → fallback_chain → ...)
elif utilization < low_watermark (default 0.3) and len(eligible) > min_nodes:
    mark_one_node draining
```

**`active` — это COUNT(Device), не COUNT(Subscription).** (`autoscale.py:176-199`.) Причина: stage 7 — family-планы с несколькими устройствами. Одна подписка может занимать 3 «слота», и без per-device подсчёта autoscale под-провижинит, как только пользователи начнут добавлять extra devices.

**`counted_nodes = [n for n in pool.nodes if n.status != error]`.** Сломанные ноды не заваливают cap, иначе пул вечно был бы «на максимуме» без рабочих серверов.

### Fallback-провайдеры

`ServerPool.autoscale_fallback_provider_ids` (JSONB list). Когда primary провайдер поднимает `NodeSpawnError` (Hetzner abuse-lock, Vultr quota, DO region out of stock, ...), `evaluate_pool` **идёт по цепочке**:

```python
# services/autoscale.py:306-341 (сокращённо)
for provider_id in [primary] + fallbacks:
    try:
        node, _ = spawn_node(db, provider_id=provider_id, ...)
        break
    except NodeSpawnError:
        continue
```

Если **все** провалились — `_spawn_backoff[pool.id] = now + 600s`, пул на 10 минут выходит из решения (in-memory, не персистится). На рестарт — retry сразу.

## Ручное добавление ноды

Минимальный путь без cloud provider'а — для тестов или dedicated-машины, которую нельзя спавнить через API:

1. `POST /api/nodes` с `{name, region, host, ssh_port, pool_id, max_users}`. Backend создаёт `VPNNode(status=registering, provider_id=null)`.
2. `POST /api/nodes/{id}/configs` — завести хотя бы один `VPNConfig` (обычно через admin endpoint, который дёргает `ensure_reality_config` / `ensure_shadowtls_config`).
3. Положить публичный ключ `provisioning_key.pub` в `~root/.ssh/authorized_keys` на самой ноде (на этом этапе руками — у нас нет канала иначе).
4. `POST /api/nodes/{id}/bootstrap` — backend создаёт `ProvisioningTask` и кидает в воркер.
5. Воркер запускает `site.yml -l <node.name>` с extra_vars из `_collect_site_extra_vars`.
6. При успехе `_handle_task_outcome` флипает `status: registering → active`.

Для автоспавна первый шаг — завести `CloudProvider` через `/api/cloud/providers` (админ в SPA), положить туда api_token (будет encrypted через Fernet), привязать к `ServerPool.autoscale_provider_id`, и дальше autoscale сам будет спавнить, когда utilization пойдёт выше.

## ⚠️ Неясные места

- **`wg_exit_nodes` группа в `inventories/prod/hosts.yml` пустая.** Код роли `wg_exit_node` готов, `relay_jump_node` готова, но ни одна нода не описана — фактически relay-схема в проде не используется. Неясно, есть ли она хоть где-то в inventory вне git.
- **`relay_config` у jump-ноды хранится как JSONB plaintext.** В отличие от паролей `VPNConfig.settings`, которые зашифрованы Fernet, WG-приватник jump-ноды лежит в БД в открытом виде. Компрометация дампа БД = компрометация туннеля.
- **Health score агрегация не зафиксирована в одном месте.** Декремент/инкремент раскиданы по worker-тикам и handler'ам `HealthProbe`. Порог `MIN_HEALTHY_SCORE` — константа в `provisioning.py`, но откуда берётся «что именно декрементит» — читается только в коде, не в документе.
- **Автовосстановления из `error` нет.** Нода, попавшая в error (единичный сбой API провайдера во время destroy, например), остаётся там до ручного вмешательства. Нет self-heal'а, который бы через X часов попробовал снова.
- **Promote `registering → active` требует и ansible-success, и health pass.** Если ansible прошёл, а health-probe стабильно падает (например, UFW неправильно настроен), нода остаётся в `registering` надолго. `choose_node` её всё ещё берёт (registering в whitelist). Это компромисс «лучше отдать свежую ноду, чем задержать подписку», но клинические случаи возможны.
- **Grace-таймер draining'а использует `updated_at`.** Любая операция, которая трогает ноду (даже миграция одной подписки), перезапускает таймер. В пуле с постоянным drip'ом миграций destroy может не случиться никогда.
- **ShadowTLS `manage_vpn_user.sh` — no-op.** Единственный общий пароль per node. Revoke одного устройства **не удаляет его фактический доступ** — пользователь продолжает ходить, пока не ротируется node password для всех сразу. Legacy-протокол, sharing enforcer его не покрывает.

## Sharing enforcer — защита от расшаривания

На каждой ноде работает `xray-enforcer` systemd-сервис (`install_sharing_enforcer`), который:

1. Каждые 10с (`ENFORCER_CHECK_INTERVAL`) парсит access-логи xray (`/var/log/xray/access-*.log`) за последние 120с (`ENFORCER_WINDOW_SECONDS`).
2. Собирает уникальные IP по `email` (= `Device.access_username`, напр. `user-1-2`).
3. Если у UUID обнаружено >1 IP (`ENFORCER_MAX_IPS`) — цикл: `xray api rmuser` (мгновенный disconnect) → sleep 2с (`ENFORCER_RECONNECT_DELAY`) → `xray api adduser` (легитимный пользователь переподключится, шарящий — нет).
4. Каждое нарушение пишется в `/var/log/xray/sharing_violations.jsonl`.

Мониторинг: backend-воркер через `traffic_stats` SSH-тик вычитывает + truncate'ит `sharing_violations.jsonl`, пишет `AuditLog(action="sharing_violation")` → видимость в admin UI.
