# VPN ноды

Документ про **ноды-exit'ы**: кто их регистрирует, какой у них жизненный цикл, какие на них роли выполняются и что значит каждая колонка в `VPNNode`. Детали про оркестратор ansible — в `infrastructure/ansible.md`, про орchestration из backend'а — в `components/provisioning.md`. Здесь — перспектива самих нод.

> **Статус протоколов (обновлено 2026-07-28):** `shadowtls_ss` — legacy, роль `install_shadowtls_stack` закомментирована в [site.yml](../../infra/ansible/site.yml). `hysteria2` **реанимирован 22.07.2026**: роль активна в `site.yml` (гейт — `hysteria2_port` из БД); с 28.07.2026 протокол снова доступен и в admin-UI (форма создания ноды + добавление конфига к существующей). С **28.07.2026** hysteria2 умеет split-tunnel наравне с vless-флаворами (`outbounds[].direct.bindDevice` + `acl.inline` — см. матрицу «протокол × split-tunnel» ниже). До этой даты он на relay-ноде выпускал весь трафик с российского IP, то есть давал коннект без VPN.

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
suspect_since (DateTime NULL)        — Phase D traffic-drop detector: момент, когда active_users
                                       упал с ≥TRAFFIC_DROP_MIN_USERS до 0. Очищается на
                                       следующем тике (подтверждение → error или false-alarm → NULL)
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
- **автоспавн (autoscale-тик)**: `node_spawner.spawn_node` — вызывается из `autoscale.evaluate_pool` в RQ-воркере (без HTTP-таймаута). Блокирующий: `driver.create_server(...)` (30–600с, ждёт IP) → `VPNNode(registering, is_active=True, provider_external_id=...)` → `ensure_reality_config` → bootstrap-task создаётся сразу, но её запуск дожидается SSH в фоновом daemon-потоке `_deferred_bootstrap_after_ssh` (**`_wait_for_ssh`**, аудит-фикс #77: раньше синхронный путь стартовал `site.yml` без ожидания → на свежем VPS `No route to host` → нода `error`, а автоскейл в следующий тик покупал ещё сервер).
- **спавн из админ-панели**: `POST /api/nodes/spawn` → `node_spawner.spawn_node_async`. **Неблокирующий** (нельзя держать HTTP-запрос на 600s-поллинге — nginx `proxy_read_timeout` 60s убьёт воркер → CF 502, а оплаченный VPS осиротеет). Порядок:
  1. `driver.order_server(...)` — синхронно, БЫСТРО (только `buyServer`, ~секунды) → `(external_id, root_password)`. Драйверы без `order_server` (vultr/DO/aeza) — заказ целиком уходит в фон на шаге 4. У hetzner сплит есть (аудит-фикс #83); его блокирующий `create_server` (autoscale-путь) при провале после успешного POST best-effort сносит оплаченный сервер (orphan-guard), а поллинг статуса переживает транзиентные ошибки API (аудит-фикс #82).
  2. `VPNNode(status=registering, is_active=False, host="0.0.0.0", provider_external_id=...)` сразу в БД — сервер привязан к строке с момента заказа (**сирот нет**). `is_active=False` ⇒ `choose_node` не назначает на неё юзеров, пока нет реального IP.
  3. `ensure_reality_config(node)` (синхронно — host не нужен) → ответ админу с готовой нодой.
  4. фоновый daemon-поток `_finalize_spawn`: `driver.wait_for_ipv4(external_id)` (поллинг до 600s) → проставить `host` + `is_active=True` → `set_autoprolong` (best-effort) → **`_wait_for_ssh`** (ждём, пока свежий VPS поднимет SSH, окно `NODE_SSH_WAIT_TIMEOUT`=480s — иначе bootstrap падает на `No route to host`) → bootstrap-task (`defer_to_reconciler=False`). При провале поллинга IP — нода `error`+`is_active=False` (external_id уже в строке → оператор сносит/переустанавливает). Перед `site.yml` воркер кладёт `provisioning`-ключ по root-паролю (`ssh_bootstrap.ensure_provisioning_key`, см. эпик Фаза 1.5). Reinstall идёт тем же путём через `_reinstall_finalize` (ждёт SSH после ребута).

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

Статус остался от прежней downscale-дорожки. Auto-drain тика выпилена (2026-04-17); нода в `draining` ведёт себя пассивно:

- **Не** возвращается в `_eligible_nodes` — autoscale не считает её в capacity/utilization.
- **Не** возвращается в `choose_node` — новые подписки сюда не едут.
- Старые подписки продолжают работать — `Credential.is_active` не меняется.
- Миграция и destroy делаются **руками** через admin SPA (migrate + `/destroy`).

### `error`

Нода в ошибочном состоянии — bootstrap упал, health стабильно плохой, `destroy_node` получил exception от провайдера. `_eligible_nodes` специально исключает `error` из подсчёта `counted_nodes` при autoscale-решении «есть ли ещё места», **иначе** одна сломанная нода вечно держала бы пул на `max_nodes` и новые уже не спавнились бы (`autoscale.py:244-246`).

**Из `error` нода никуда автоматически не выходит.** Требуется либо `/admin/nodes/{id}` flip, либо ручной `destroy_node`, либо **`PATCH /api/nodes/{id}/status`** со значением `active` — admin-эндпоинт для ручного override'а (принимает `active`/`error`/`disabled`; `registering`/`draining` только через автоматику). При переводе в `active` чистятся `cooldown_until`, `suspect_since`, `blocked_regions`; пишется `AuditLog(action="node_status_changed")`. Admin SPA выставляет этот эндпоинт через dropdown статуса в детальной панели ноды.

Тот же side-effect у **`POST /api/nodes/{id}/active`** с `is_active=true` (кнопка «вернуть в пул» в admin SPA): когда ноду включают обратно, `cooldown_until`/`suspect_since`/`blocked_regions` чистятся в той же транзакции. Без этого после traffic-drop-инцидента или auto-migrate'а нода с `is_active=True` + `cooldown_until` в будущем выглядела для админа рабочей, но `choose_node` её игнорил — вся новая нагрузка уходила на одну единственную нелокдаун-ноду (инцидент 2026-04-15). `AuditLog(action="node_active_changed").extra.cleared` содержит список очищенных полей.

## Health score и cooldown

`health_score` — целое 0–100, **или NULL** (нет данных). Source of truth — `services/health.py:recompute_node_health`, которая агрегирует `health_probes` за последние 15 минут (`LOOKBACK`).

Формула: `health_score = round(ok_count / total_count * 100)`. Если проб за окно нет — score сбрасывается в `NULL` (в UI «нет данных»). `choose_node` трактует `NULL` как eligible (`IS NULL` проходит фильтр), чтобы свежая нода не отваливалась из пула до первого проба.

Если site.yml (bootstrap) упал на **уже active** ноде, `health_score` ставится в 0 (без демоута статуса, чтобы сохранить существующих пользователей), и autoscale перестаёт на неё сажать новых.

`cooldown_until` — отдельный механизм для случая «нода недавно что-то натворила, дадим ей отдохнуть, но не выключаем». Проставляется, например, после fail'а provisioning-таска или временной ошибки API. До истечения ноду не возьмут ни пользовательский `choose_node`, ни autoscale.

### Phase D — traffic-drop detector (TSPU-signals)

Active-пробы из ДЦ не ловят ТСПУ-блокировки, резидентные пробы ненадёжны (одна проба с одного ISP ничего не скажет про другого оператора). Единственный надёжный пассивный сигнал — **резкое падение `active_users`** на ноде: если было ≥5, стало 0 между двумя traffic_stats тиками (5-мин интервал) — нода вероятно заблокирована для пользователей.

Детектор живёт в `services/traffic_stats.py::detect_traffic_drops`, вызывается из `run_traffic_stats_tick` после `collect_all_active_nodes`. Алгоритм:

1. **Детект**: на каждом тике для нод с `curr.active_users == 0` и `prev.active_users >= TRAFFIC_DROP_MIN_USERS` (default 5) → ставим `suspect_since = now()`, запускаем **проверочную миграцию** через `migrate_subscriptions_off(exclude_same_region=True)` (в ноду другого региона, т.к. ТСПУ блочит регионально). Пишем `AuditLog(action="traffic_drop_detected")`.
2. **Подтверждение** (следующий тик): если на ноде-получателе уже есть трафик — это подтверждение блока: `status=error`, `is_active=False`, `cooldown_until=now+3d`, `suspect_since=NULL`. `AuditLog(action="traffic_drop_confirmed")`.
3. **Ложная тревога**: если на новой ноде тоже 0 — пользователи просто не сидят; `suspect_since=NULL`, юзеры **остаются** на новой ноде (откат сломал бы sub_token'ы). `AuditLog(action="traffic_drop_cleared")`.

Мастер-switch `TRAFFIC_DROP_ENABLED=1` (default). Параметры — см. `operations/env-reference.md`.

### Админская видимость

`NodeTrafficSample.details` с 2026-04 содержит per-protocol **список `access_username`**, а не только count. Это позволяет админке показывать "кто сейчас на ноде" по последнему tick'у без отдельного SSH-запроса. Эндпойнт — `GET /api/nodes/{id}/users`: отдаёт `observed_at`, флаг `stale` (sample старше 15 мин), и по каждому юзеру джойнит `Device → Subscription → User/Plan`. Orphan-username'ы (на ноде есть, в БД нет — наследие миграции) помечаются отдельно.

Для визуального отслеживания момента blocking'а (и вообще нагрузки) есть `GET /api/nodes/{id}/traffic-history?hours=24` — тонкий запрос, возвращающий временной ряд `NodeTrafficSample` для sparkline-графика в карточке ноды. `hours` ограничен [1, 168].

Редактирование `VPNConfig` на ноде — `PUT /api/nodes/{id}/configs/{cfg_id}`: правит всё кроме `protocol` (смена протокола — delete+create), shallow-merge'ит `settings` чтобы UI не нужно было пересылать зашифрованные секреты. После успеха инвалидирует warm-pool + ставит bootstrap-таску с `config_change=True`.

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

- Source of truth — таблица `relay_exit_links` (N:N, по одному `wgN` на линк). `VPNNode.relay_config` = legacy-форма, оставшаяся от эпохи одного `wg0`: `{"wg_private_key": "...", "wg_address_v4": "10.77.0.2/24", "wg_endpoint": "<exit_ip>:51820", "wg_exit_public_key": "...", ...}`.
- В `site.yml` (см. `infrastructure/ansible.md`) на неё выполняются роли `install_vless_*` **плюс** `relay_jump_node` — последняя ставит WG-клиентов (по одному на линк) и **патчит через jq** существующие `config*.json` xray, добавляя `sockopt.interface: wgN` в freedom-outbound. Тем самым весь пользовательский трафик уходит в туннель, а SSH/ansible — через основной интерфейс.
- Hysteria2 **не патчится** этим способом (UDP route через kernel routing table — комментарий в `relay_jump_node/tasks/main.yml`).

**Exit-нода**:

- Голый WireGuard-сервер. Ansible-роль `wg_exit_node/tasks/main.yml`:
  - `apt install wireguard wireguard-tools`;
  - `sysctl net.ipv4.ip_forward=1`, `net.ipv6.conf.all.forwarding=1`;
  - render `/etc/wireguard/wg0.conf` из `wg0.conf.j2` с peer-листом (список jump-нод, каждая со своим pubkey и allowed_ips);
  - `iptables -t nat POSTROUTING MASQUERADE` на `default_ipv4.interface`;
  - `iptables FORWARD ACCEPT` в обе стороны (in/out wg0).
- **Не** ставит xray, shadow-tls, hysteria. **Не** живёт в `vpn_nodes` таблице (обычно) — это «inventory-only» машина, про которую БД ничего не знает. Её `ansible_host` прописан в group `wg_exit_nodes` статического inventory.
- ~~На момент написания группа `wg_exit_nodes` пустая~~ — **неверно с 21.04.2026**: в `inventories/prod/hosts.yml` 7 exit-хостов (TR/FR×2/UK×2/CZ/NL), relay-схема в проде работает и является нормой для РУ-нод.

### RU-обход (split-tunnel на geoip:ru)

Поверх обеих схем работает **разделение трафика по назначению**: запросы к РУ-зоне выходят с собственного IP ноды (direct), всё остальное идёт в туннель/exit. Это держит клиенту доступ к РУ-банкам/госуслугам (которые геоблочат зарубежные IP) и снижает внимание ТСПУ к подключению.

Реализовано **server-side в xray** на двух протоколах — `vless_reality` ([install_vless_reality/templates/config.json.j2](../../infra/ansible/roles/install_vless_reality/templates/config.json.j2)) и `vless_xhttp` ([install_vless_xhttp/templates/config_xhttp.json.j2](../../infra/ansible/roles/install_vless_xhttp/templates/config_xhttp.json.j2)). В обоих `routing` (`domainStrategy: IPIfNonMatch`) два правила, стоящие **перед** per-user relay-fan-out (first-match-wins):

```jsonc
{ "domain": ["regexp:\\.ru$", "regexp:\\.su$", "regexp:\\.xn--p1ai$", "regexp:^(yandex|...|dtf)\\..+$"], "outboundTag": "direct-local" },
{ "ip": ["geoip:ru", "geoip:private"], "outboundTag": "direct-local" }
```

`direct-local` — freedom-outbound **без** `sockopt.interface`, поэтому даже на relay-ноде РУ-трафик выходит с её родного (РУ) IP, а не уходит в WireGuard к exit'у. Список доменов — **намеренно самописный** (курированный список РУ-хостов), а не `geosite:category-ru`: зоны заданы regexp'ами (`\.ru$`, `\.su$`, `\.xn--p1ai$`, `.moscow`, `.tatar`, `.дети`, `.рус`), конкретные бренды и CDN — записями `domain:` (суффиксный матч: домен + все поддомены).

> **Почему бренды — `domain:`, а не regexp (2026-07-28).** До этого стоял `regexp:^(yandex|mail|vk|ok|…)\..+$`, анкоренный на **первый лейбл**, а не на регистрируемый суффикс. Он ловил `mail.google.com`, `mail.proton.me`, `mail.yahoo.com`, `ok.google.com`, `hh.com` — они уходили напрямую с РУ-IP; для Proton это была жёсткая поломка (в РФ заблокирован → домен просто не открывался у юзера с включённым VPN). Обратная сторона того же якоря: `www.vk.com` / `m.vk.com` первым лейблом не матчились вовсе. Бренды в зоне `.ru` покрыты `\.ru$` и в списке не нужны — там только не-`.ru` зоны и CDN (`userapi.com`, `mycdn.me`, `yastatic.net`), на которые приходится основной объём байт. Паритет трёх шаблонов закреплён тестом `backend/tests/test_split_routing_parity.py`.

`geoip.dat` (v2fly community, MIT) ставится обеими ролями в `/usr/local/share/xray/geoip.dat` (`get_url force:no` — один раз на bootstrap, идемпотентно на комбинированной ноде) и обновляется еженедельным `geoip-update.timer`; refresh-сервис делает `try-restart` обоих флаворов (`xray` и `xray-xhttp`) best-effort (`-` префикс — трогает только активные юниты). Без файла xray падает с `failed to load geoip` — поэтому обе роли валидируют рендер через `xray -test` до старта.

**Почему именно так — 5 выстраданных правок** (рационал жил в сообщениях коммитов, не было отдельной доки — этот раздел её заменяет). Каждый пункт — отдельный fix после реального бага, проверенного по access-логам xray:

1. **`direct-local` без `sockopt` отдельным outbound'ом** (`678fb3b`). На relay-ноде дефолтный `direct` outbound патчится `sockopt.interface = wgN` → весь «direct» трафик уходит в WG к exit'у. Первая версия правила слала `geoip:ru → direct` — и РУ-трафик послушно утекал на exit (2ip.ru с клиента показывал IP exit-ноды). Нужен **отдельный** freedom-outbound без sockopt = чистый egress через main-iface ноды.
2. **`domainStrategy: IPIfNonMatch`** (`9353a76`). Дефолтный `AsIs` не резолвит domain-назначения в IP → правило `ip:[geoip:ru]` по domain-коннектам **никогда не матчит**. `IPIfNonMatch` резолвит domain в IP и делает второй проход по правилам — но **только если в первом не сматчилось НИ ОДНО правило**, включая fan-out по `user`. ⚠️ Практическое следствие (аудит 2026-07-28): у кредa с `exit_id` правило `{"user": […], "outboundTag": "direct-wgN"}` матчится в первом проходе всегда (email известен без резолва), поэтому второй проход не наступает и **geoip-подстраховка для него мертва** — работает только доменный список. У легаси-кредов без `exit_id` user-правила нет, и geoip отрабатывает. Отсюда «у одних работает, у других нет».
3. **Секция `dns` (Yandex первым)** (`a6d7c58`). Без `dns` xray не резолвит domain→IP для routing'а, и `IPIfNonMatch` молча не срабатывает. Лог подтверждал: `accepted tcp:2ip.ru:443 [vless-reality -> direct-wg2]` (назначение — domain, geoip-правило не применилось). Порядок DNS: **Yandex `77.88.8.8` первым** — отдаёт РУ-IP для РУ-сайтов даже за CDN (foreign DNS часто возвращает Cloudflare). Сам DNS-трафик к 77.88.8.8 попадает под `geoip:ru → direct-local`.
4. **Domain-правило ДО geoip-правила** (`48ae3bd`). Даже со всем выше geoip-путь оказался ненадёжен (в той Reality-сборке geoip.dat либо не грузился, либо `IPIfNonMatch` не резолвил sniffed-SNI до routing'а — в логах старта не было строк про geoip). Domain-правило матчит **прямо по sniffed SNI, без резолва** — покрывает `.ru/.su/.рф` + явный список `.com`-доменов РУ-гигантов. Geoip-правило остаётся **ниже как safety-net** для прямых IP-коннектов (когда клиент обходит sniffing).
5. **Требует `sniffing.enabled: true` + `destOverride: [http, tls]`** на inbound — иначе пункт 4 (matching по SNI) не работает. Оба конфига (Reality и XHTTP) это имеют.

> **XHTTP-специфика:** XHTTP-inbound слушает loopback за nginx (Stage-4 camo), но sniffing работает по **внутреннему** TLS-stream'у проксируемого коннекта, не по внешнему транспорту — поэтому RU-обход на XHTTP идентичен Reality. Reconcile relay-линков (`xray_reconcile.jq`/`xray_unpatch.jq`) ходит по маске `config*.json` — то есть по **всем трём** протоколам (маска появилась в `c926838`, апрель 2026; утверждение «только config.json» было неверным с тех пор). Трогает он только `direct-wg*` и `sockopt` у `direct`, а правила `direct-local` и RU-блок не затрагивает — обход переживает attach/detach.
>
> **Где это вообще работает:** RU-обход осмыслен только на **relay/РУ-нодах**, где `direct` уходит в WG (≠ `direct-local`). На standalone-зарубежной ноде `_primary` пуст → `direct` и `direct-local` оба egress'ят локально → правило безвредный no-op (РУ-сайты всё равно видят зарубежный IP, выгоды нет).

> **Матрица «протокол × split-tunnel» (2026-07-28).** Все четыре протокола на relay-ноде ходят одинаково: РУ — напрямую с самой ноды, остальное — через WG в зарубежный exit.
>
> | Протокол | Механизм RU-обхода | Привязка к WG | С какой даты |
> |---|---|---|---|
> | `vless_reality` | `routing.rules` → `direct-local` | `sockopt.interface` | 21.04.2026 |
> | `vless_xhttp` | то же | то же | 05.06.2026 |
> | `vless_ws_cdn` | то же | то же | **28.07.2026** |
> | `hysteria2` | `acl.inline` → `local(...)` | `outbounds[].direct.bindDevice` | **28.07.2026** |
>
> Список РУ-зон и доменов — общий, из роли [`ru_direct_list`](../../infra/ansible/roles/ru_direct_list/defaults/main.yml); каждый шаблон рендерит его в свой синтаксис (xray: `regexp:`/`domain:`, hysteria: `suffix:`). Так правка физически не может приземлиться в один протокол и разъехаться с остальными — ровно это и происходило три раза подряд. Паритет проверяется `backend/tests/test_split_routing_parity.py`.
>
> **Почему hy2 понадобился отдельный механизм.** Это не xray, а самостоятельный демон: `routing.rules` ему не указ, и `sockopt.interface` он не использует. Ключевое — WG-маршрут на relay-ноде намеренно хуже основного (`relay_jump_node`: `Table = off` + PostUp с `metric 200`), поэтому в туннель попадает **только то, что процесс явно забиндил на интерфейс**. До 28.07.2026 hy2 не биндил ничего и выпускал весь трафик с российского IP — клиент показывал «подключено», РУ-сайты работали, а заблокированное оставалось заблокированным. Теперь `outbounds[0]` — `direct` с `bindDevice: wgN`, и он стоит **первым** намеренно: при неприменившемся ACL hysteria шлёт всё в первый outbound, то есть деградация идёт в сторону «РУ-сайты видят зарубежный IP», а не «VPN не работает». `geoip.dat` переиспользуется из `/usr/local/share/xray` (формат общий, второй копии и второго таймера не нужно).
>
> На нодах **без** relay-линков (зарубежные) секции `outbounds`/`acl` не рендерятся вовсе — там весь трафик и так выходит с IP самой ноды, что и требуется.
>
> Разбор того, как это сломалось и жило незамеченным, — в `operations/ru_split_routing_audit_2026_07_28.md`.

## Роли, запускаемые на ноде

Полная цепочка из `site.yml` для группы `vpn_nodes`:

```
bootstrap_node               — user/group, SSH hardening, UFW + rate-limit 22/tcp, fail2ban sshd, unattended-upgrades, базовые пакеты
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

- ~~**`wg_exit_nodes` группа пустая, relay-схема в проде не используется.**~~ **Снято 2026-07-28:** в инвентаре 7 exit-хостов, relay-схема — норма для РУ-нод. Открытый вопрос по ней теперь один: geoip-подстраховка мертва у кредов с `exit_id` (двухпроходный `IPIfNonMatch` — см. выше). У hysteria2 split-tunnel появился 28.07.2026; там geoip-правило живое, потому что ACL матчится одним проходом.
- **`relay_config` у jump-ноды хранится как JSONB plaintext.** В отличие от паролей `VPNConfig.settings`, которые зашифрованы Fernet, WG-приватник jump-ноды лежит в БД в открытом виде. Компрометация дампа БД = компрометация туннеля.
- **Health score агрегация не зафиксирована в одном месте.** Декремент/инкремент раскиданы по worker-тикам и handler'ам `HealthProbe`. Порог `MIN_HEALTHY_SCORE` — константа в `provisioning.py`, но откуда берётся «что именно декрементит» — читается только в коде, не в документе.
- **Автовосстановления из `error` нет.** Нода, попавшая в error (единичный сбой API провайдера во время destroy, например), остаётся там до ручного вмешательства (`PATCH /api/nodes/{id}/status → active` или `destroy_node`). Нет self-heal'а, который бы через X часов попробовал снова.
- **Promote `registering → active` требует и ansible-success, и health pass.** Если ansible прошёл, а health-probe стабильно падает (например, UFW неправильно настроен), нода остаётся в `registering` надолго. `choose_node` её всё ещё берёт (registering в whitelist). Это компромисс «лучше отдать свежую ноду, чем задержать подписку», но клинические случаи возможны.
- **Grace-таймер draining'а использует `updated_at`.** Любая операция, которая трогает ноду (даже миграция одной подписки), перезапускает таймер. В пуле с постоянным drip'ом миграций destroy может не случиться никогда.
- **ShadowTLS `manage_vpn_user.sh` — no-op.** Единственный общий пароль per node. Revoke одного устройства **не удаляет его фактический доступ** — пользователь продолжает ходить, пока не ротируется node password для всех сразу. Legacy-протокол, sharing enforcer его не покрывает.

## Sharing enforcer — защита от расшаривания (ОТКЛЮЧЁН 2026-04-15)

**Status:** Отключён через `sharing_enforcer_enabled: false` в `group_vars/vpn_nodes.yml`. Роль `install_sharing_enforcer` по-прежнему ставит `xray_enforcer.py` + systemd unit, но unit `xray-enforcer.service` в состоянии `stopped + disabled`.

**Причина отключения:** V2 enforcer `rmuser→sleep→adduser` рвал коннект легитимным пользователям:
- Телефон + ноутбук под одним аккаунтом CGNAT → два разных "видимых" IP в одном SLOT_SECONDS окне → detections растут → через 3 concurrent → warning → kick.
- `ENFORCER_KICK_COOLDOWN=120s` — между kicks того же email минимум 2 минуты. На практике UX это воспринималось как **разрыв коннекта каждые 5-10 минут** для юзеров со вторым устройством на нестабильном IP.
- `rmuser` через gRPC — точечный, другие юзеры на ноде не страдают, но для жертвы это полный disconnect до `adduser` + reconnect клиента.

**Как применить отключение на уже-раскатанных нодах:**
```
ansible-playbook -i inventories/prod/hosts.yml site.yml --tags sharing_enforcer --ask-vault-pass
```
Роль вычитает `sharing_enforcer_enabled: false` из group_vars и переведёт юнит в stopped+disabled state.

**Как работал enforcer (для возможной реактивации):**

1. Каждые 10с (`ENFORCER_CHECK_INTERVAL`) парсит access-логи xray (`/var/log/xray/access-*.log`) за последние 120с (`ENFORCER_SLOT_SECONDS`).
2. Собирает уникальные IP по `email` (= `Device.access_username`, напр. `user-1-2`).
3. Tier 1 (warning): 3 concurrent-IP detections в течение `WARNING_WINDOW=3600s` → notification юзеру, без kick.
4. Tier 2 (kick): `rmuser` → sleep → `adduser` через gRPC, но не чаще `KICK_COOLDOWN=120s` для того же email.
5. Tier 3 (block): 3 kicks в `BLOCK_WINDOW=43200s` → `rmuser` без `adduser`, пока админ не `sharing_unblock`.
6. Каждое событие пишется в `/var/log/xray/sharing_violations.jsonl`.

**Мониторинг (всё ещё работает как пассивный код):** backend-воркер через `traffic_stats` SSH-тик вычитывает + truncate'ит `sharing_violations.jsonl`, пишет `AuditLog(action="sharing_violation")` → видимость в admin UI. Но поскольку enforcer не запущен, файл всегда пустой. Бэкенд-гейт `SHARING_ENFORCEMENT_ENABLED=0` дополнительно убирает сам чтение этого файла.

**Перед реактивацией нужно:**
- Повысить `ENFORCER_WARNING_THRESHOLD` до значения, которое не ловит CGNAT-эффект (реально встречающееся 2-3 IP для одного аккаунта в час).
- Или научить enforcer учитывать "родственные" IP (одинаковые /24 или одинаковый ASN).
- Или требовать kick только после `WARNING_THRESHOLD * 2-3` detections — дать явное предупреждение раньше, чем разрывать.
