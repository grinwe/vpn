# Ansible

Всё, что делается на реальном железе — поднятие VPN-ноды, установка протоколов, деплой приложения, мониторинг — оркеструется Ansible'ом из `infra/ansible/`. Backend не имеет альтернативного пути настраивать ноды; каждое действие на ноде идёт через `subprocess.run(["ansible-playbook", ...])` из `backend/app/services/ansible_runner.py`.

> **Deprecation notice (0.2/0.3, April 2026):** роли `install_shadowtls_stack` и `install_hysteria2` закомментированы в [site.yml](../../infra/ansible/site.yml).  Упоминания ShadowTLS и Hysteria2 ниже оставлены для легаси-нод; role directories сами по себе пока существуют.  Полное удаление — 0.4.

## Структура директории

```
infra/ansible/
├── ansible.cfg                   ← defaults + ssh multiplex
├── site.yml                      ← главный playbook, вызывается на bootstrap/rebootstrap
├── requirements.yml              ← галакси-зависимости (если есть)
├── group_vars/
│   ├── vpn_nodes.yml             ← роли: ansible_user, firewall_allowed_ports,
│   │                                    vpn_system_user, ssh_public_keys
│   ├── db.yml
│   ├── monitoring.yml
│   ├── web.yml
│   └── web/main.yml              ← hostvars для nl-web
├── inventories/
│   └── prod/hosts.yml            ← статический inventory (mgmt-1, nl-monitoring, nl-web)
├── playbooks/
│   ├── provision_device.yml      ← per-device add/remove (основной, backend зовёт часто)
│   ├── resync_node.yml           ← re-push VLESS clients после site.yml
│   ├── diagnose_node.yml         ← диагностика, read-only
│   ├── deploy_app_stack.yml      ← deploy на web-host (docker-compose + nginx)
│   ├── deploy_web_frontend.yml   ← SPA build + nginx site conf
│   ├── deploy_monitoring.yml     ← Grafana/Prometheus на monitoring-host
│   └── mgmt_mirror.yml           ← standalone-раскат mgmt-зеркала upstream (см. ниже)
└── roles/
    ├── base_node                 ← общие системные настройки (не vpn-спец.)
    ├── bootstrap_node            ← pre-install: user, ufw (+ rate-limit 22/tcp), sshd-hardening, fail2ban, unattended-upgrades, apt
    ├── install_shadowtls_stack   ← ShadowTLS v3 + shadowsocks-rust
    ├── install_vless_reality     ← xray + VLESS Reality (xtls-rprx-vision)
    ├── install_vless_ws_cdn      ← xray + VLESS/WS, прямой TLS (LE), DNS-only (без CF-прокси)
    ├── install_vless_xhttp       ← xray + VLESS/xHTTP
    ├── install_hysteria2         ← hysteria2 (UDP)
    ├── install_probe_agent       ← опциональный node-to-node health probe
    ├── install_traffic_collector ← опциональный traffic accounting
    ├── relay_jump_node           ← WG tunnel client → чужая exit-нода
    ├── wg_exit_node              ← non-RU exit нода для relay'ев
    ├── check_node_health         ← post-install assertion: порты LISTEN
    ├── xray_geoip                ← geoip.dat fetcher с CDN-fallback (mirror→jsdelivr→ghproxy→github) + weekly timer
    ├── xray_core                 ← xray-core install через CDN-fallback (mirror→ghproxy→github), pinned version
    ├── mgmt_mirror               ← nginx-зеркало upstream-ресурсов на web-host'е (см. ниже)
    ├── db_host                   ← PostgreSQL + volume на mgmt-хосте
    ├── deploy_app_stack          ← backend, bot, redis, db compose-stack на web
    ├── deploy_web_frontend       ← nginx site для SPA и sub-ссылок
    ├── monitoring_stack          ← Grafana/Prometheus compose на nl-monitoring
    └── node_exporter             ← prom node_exporter (запускается поверх vpn_nodes)
```

## mgmt-mirror — собственное зеркало upstream

Часть RU-провайдеров троттлит outbound к `github.com` и его зеркалам
настолько, что `curl` стоит 5+ минут SSL-handshake и фейлится: видели на
`ru-cloud-web-02` при попытке скачать `Xray-linux-64.zip` (даже через
`ghproxy.com`). Чтобы ноды не зависели от capricious-доступности
upstream'а, у нас есть свой mini-mirror на web-host'е (`nl-web` /
`mgmt-1` — один IP).

### Что лежит в зеркале

`/srv/assets/` на nginx-контейнере отдаёт по `http://<web-host>:8090/`:

| URL | Что |
|---|---|
| `/geoip.dat` | v2fly geoip database (latest, qweekly refresh) |
| `/geosite.dat` | v2fly geosite list |
| `/xray/Xray-linux-64-<version>.zip` | pinned-версии xray-core |
| `/healthz` | docker healthcheck endpoint |

Источник правды pinned-версий — `XRAY_VERSIONS` массив в
[refresh-assets.sh](../../infra/ansible/roles/mgmt_mirror/files/refresh-assets.sh).
Bump xray — два места: тут + `xray_core_version` в
[roles/xray_core/defaults/main.yml](../../infra/ansible/roles/xray_core/defaults/main.yml).

### Как ноды его находят

[group_vars/all.yml](../../infra/ansible/inventories/prod/group_vars/all.yml)
задаёт `xray_mirror_url: "http://{{ hostvars['mgmt-1']['ansible_host'] }}:8090"`.
`bootstrap_node` рендерит `/etc/default/xray-mirror` на каждой ноде, оттуда
wrapper'ы `xray-geoip-fetch.sh` и `xray-core-fetch.sh` подхватывают `MIRROR_URL`
и пробуют его **первым** в цепочке. При недоступности — fallback на
ghproxy/jsdelivr/github как раньше.

### Раскатка

В составе общего `site.yml`:

```bash
ansible-playbook -i inventories/prod/hosts.yml site.yml --tags web,mirror
```

Точечно, только mirror без перетряхивания backend/admin:

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/mgmt_mirror.yml
```

Принудительный refresh upstream'а (после bump'а xray-version, например):

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/mgmt_mirror.yml \
    -e mgmt_mirror_force_refresh=true
```

Cron на mgmt'е сам обновляет geoip раз в неделю (понедельник 04:00,
лог `/var/log/mgmt-mirror-refresh.log`).

## `ansible.cfg` — важные дефолты

```ini
[defaults]
roles_path = roles:playbooks/../roles
inventory  = inventories/prod/hosts.yml
host_key_checking = False
stdout_callback = default
forks = 20

[ssh_connection]
ssh_args = -o ControlMaster=auto -o ControlPersist=10m -o ControlPath=~/.ansible/cp/%h-%p-%r
pipelining = True
```

Два тонких момента:

1. **`roles_path = roles:playbooks/../roles`** — без него playbook'и из `playbooks/` не видят сестринский каталог `roles/`. Комментарий в cfg прямо фиксирует причину: сайт-файл `site.yml` лежит в корне, а `provision_device.yml` — в подкаталоге, и без явного path подкаталог ломается.
2. **ControlPersist + pipelining** — чтобы один SSH-коннект переиспользовался между тасками. Важно, когда ключ провижининга passphrase-protected: без `ControlMaster` каждый task просит passphrase заново.
3. **`host_key_checking = False`** — ноды добавляются динамически, backend никогда не имеет их в `known_hosts`, MITM-защиты нет. См. ⚠️ в конце файла.

## Inventory

**Статический inventory** — `inventories/prod/hosts.yml` — содержит инфраструктуру **и snapshot fleet'а для static plays**:

```yaml
db_host:        { mgmt-1       → 45.14.244.140 }
monitoring:     { nl-monitoring → 45.14.244.140 }
web:            { nl-web       → 45.14.244.140 }
vpn_nodes:      { ru-*         → RU relay-ноды }
wg_exit_nodes:  { kr-*, fr-*, tq-*, uk-*, ur-*, cz-*, nl-* → non-RU exit-ноды }
```

Первые три — один и тот же IP. Разделение только логическое: когда появится вторая машина, это тривиальный inventory-edit. Группы `vpn_nodes`/`wg_exit_nodes` нужны для **bulk/static operator-plays** (`monitoring`, ручной rollout bootstrap'а, fleet-wide audit). Это **snapshot**, не источник истины.

**Provisioning runtime inventory — всё ещё dynamic из БД.** Реальные ноды для ad-hoc `run_playbook`-вызовов из backend'а материализуются в **temp-inventory на лету** (функция `build_inventory_for_node` в `backend/app/services/ansible_runner.py`, рендерит через `.format()` шаблон вида):

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

Это один файл с **одной нодой внутри**. `--limit <node.name>` даёт ещё одно гарантирующее «не ходи никуда больше». При одновременной работе двух провижинеров — два разных temp-файла с разными процессами ansible, без взаимного влияния на общий inventory.

**Валидация идентификаторов (fix #55).** `.format()` на сыром шаблоне — YAML-injection'шный вектор: `\n` или `:` в `node.name` позволяли бы перезаписать `ansible_host`/`ansible_user`/`--private-key` и увести плейбук на чужую машину с реальным SSH-ключом. Чтобы это закрыть, перед рендером вызывается `_validate_node_for_inventory()` → `validate_node_identity_fields()`:

- `node.name` обязан матчить `^[a-z0-9][a-z0-9-]{0,62}$` (DNS-safe, без ведущего дефиса, ≤63 символа)
- `node.host` — `^[A-Za-z0-9.\-:\[\]]{1,253}$` (IPv4/IPv6/DNS, без пробелов, кавычек и переводов строк)
- `node.ssh_port` — `int` в диапазоне `[1, 65535]`

Нарушение → `InvalidNodeIdentity` (подкласс `ValueError`) **до** любых файловых операций, то есть отбитый запрос не оставляет мусора в `/tmp`. Одноимённый хелпер публичен и вызывается также из `api/nodes.py::create_node` и `services/node_spawner.py::spawn_node` — трёхслойная защита (API → service → render) на случай, если кто-то обойдёт один из фронтов. Регрессии закреплены в `backend/tests/test_ansible_runner_inventory.py`.

**Cleanup контракт.** `build_inventory_for_node` возвращает путь к файлу с `delete=False`, и **каллер обязан** вызвать `inventory.unlink()` в `finally`-блоке — иначе `/tmp` забивается по одному файлу на каждый прогон. Текущие вызовы (`provisioning.py::_run_ansible`, `warm_pool.py::_warm_bundle`/`_physical_revoke`) это делают; перед добавлением нового caller'а проверьте grep'ом.

Для ручных операторских прогонов (`ansible-playbook site.yml`) используется snapshot из `hosts.yml`. **Дрейф-риск:** fleet ведётся в БД (`VPNNode`-таблица) как источник истины; `hosts.yml` — ручной snapshot, обновляется оператором при добавлении/выводе нод. Если после спавна новой ноды забыть добавить её в `hosts.yml`, monitoring play её не накроет (node_exporter не встанет, Prometheus target не появится). Pre-flight check отсутствует — жить с этим, пока fleet маленький; при росте — либо dynamic inventory script из БД, либо обязательный step в runbook'е спавна ноды.

## `site.yml` — главный playbook

`infra/ansible/site.yml`:

```yaml
- hosts: vpn_nodes
  become: yes
  roles:
    - bootstrap_node
    - install_shadowtls_stack
    - install_vless_reality
    - install_vless_ws_cdn
    - install_vless_xhttp
    - install_hysteria2
    - role: relay_jump_node
      when: relay_wg_private_key is defined and relay_wg_private_key | length > 0
    - role: install_traffic_collector
      when: traffic_collector_backend_url | default('') | length > 0
    - role: install_probe_agent
      when: probe_agent_backend_url | default('') | length > 0
    - check_node_health

- hosts: wg_exit_nodes
  roles:
    - role: wg_exit_node
      when: wg_exit_private_key is defined and wg_exit_private_key | length > 0

- hosts: db_host         — [db_host]
- hosts: vpn_nodes       — [node_exporter]  (tags: monitoring)
- hosts: monitoring      — [monitoring_stack] (tags: monitoring)
- hosts: web             — [deploy_app_stack] (tags: web,app)
- hosts: web             — [deploy_web_frontend] (tags: web,frontend)
```

Ключевое архитектурное решение: **все протокол-роли всегда включены**, даже если нода раньше не имела этого протокола. Комментарий в `site.yml:6-8` объясняет — роль внутри себя гейтится по `*_password is defined and length > 0`. Если переменная отсутствует, роль не устанавливает сервис, а наоборот — **останавливает и выключает** (если он был). Это позволяет удалять протокол с ноды простым `site.yml`-ом без `state=absent`: просто не передавать `shadowtls_password_enc` в `_collect_site_extra_vars`.

`check_node_health` идёт **последним** в списке vpn_nodes плей-секции. Если какая-то роль не подняла свой порт — check упадёт с полным дампом `ss -tulpn` и журналов systemd (см. ниже).

## Gating роли на variable-based enable

Пример — `install_shadowtls_stack/tasks/main.yml:31-42`:

```yaml
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

Идиома повторяется во всех `install_*` ролях:

1. Если переменная пустая — останови/выключи сервисы (идемпотентно, `failed_when: false` на случай, если их вообще нет).
2. `meta: end_role` — выход из роли, никаких installation-тасков не выполняется.
3. Иначе — `assert` что все обязательные переменные есть и валидны, дальше apt+binary+config+systemd.

Секреты (`shadowtls_password`, `shadowtls_ss_password`, `relay_wg_private_key`, ...) попадают в ansible **не из vault'а**, а **через `--extra-vars '{"..": "..."}'`**, которые формирует backend (`_collect_site_extra_vars` в `services/provisioning.py`). Все секреты расшифровываются Fernet-ом на стороне backend'а и кладутся в командную строку ansible'а plaintext'ом. Это упрощает контур (нет vault-password в воркере), но расширяет контур доверия (см. ⚠️).

## `provision_device.yml` — hot path

`playbooks/provision_device.yml`. Это playbook, который запускается на **каждую покупку / revoke / miragrate** в cold path. Он read-write, но **не трогает** systemd-юниты и binaries — он только добавляет/удаляет строчки клиентов через существующие helper-скрипты на ноде:

```
/usr/local/sbin/manage_vpn_user.sh       ← ShadowTLS+SS (сейчас no-op logger)
/usr/local/sbin/manage_vless_user.sh     ← VLESS Reality (add/del email+UUID)
/usr/local/sbin/manage_vless_ws_user.sh  ← VLESS+WS+CDN
/usr/local/sbin/manage_vless_xhttp_user.sh
/usr/local/sbin/manage_hy2_user.sh       ← Hysteria2
```

Helper'ы устанавливаются соответствующими `install_*` ролями. Backend передаёт в playbook:

```
username, uuid, password, protocols: [{proto, port[, method]}], state: present|absent
```

Playbook циклит `protocols` и для каждого вызывает правильный скрипт. Идемпотентен: `add` → уже есть → skip, `del` → отсутствует → skip.

**Forward-compat тонкость для ShadowTLS+SS.** Сейчас `manage_vpn_user.sh` — no-op логгер (один SS password на ноду, multi-user ещё не заведён на SS2022 EIH). Но скрипт жёстко требует 4 позиционных аргумента, и ansible `command:` silently drops empty-string args. Чтобы на revoke (без password'а) счётчик аргументов не сбивался, в playbook явно передаётся литерал `'x'`:

```yaml
# provision_device.yml:40-46
command: >-
  /usr/local/sbin/manage_vpn_user.sh
  {{ 'add-shadowtls-ss' if state == 'present' else 'del-shadowtls-ss' }}
  {{ username }}
  {{ password | default('x') }}
  ...
```

Комментарий в playbook'е (строки 33-39) прямо фиксирует причину.

## `check_node_health` — post-install gate

`roles/check_node_health/tasks/main.yml`. Старая версия была «`wait_for :443 → :8443 → :9443`, fatal at first timeout» — что при любой проблеме давало пустой error без диагностики. Новая версия:

1. **`wait_for` на каждом ожидаемом порте с `failed_when: false`** — **не** прерывает play, результат копится в `port_probe`.
2. **Сбор диагностики даже при успехе**: `ss -tulpn`, `systemctl status` + `journalctl -u ... -n 20` для всех известных unit'ов (`xray`, `shadow-tls`, `ssserver`, `hysteria-server`).
3. **`debug:` — печать в stdout** безусловно.
4. **`assert:` — фейл, если хоть один порт не поднялся** — в самом конце, чтобы дамп выше был всегда виден оператору / в `/admin/tasks` UI.

Порты приходят из `vpn_health_ports` extra_vars, который backend собирает в `_collect_site_extra_vars`. Если роль с Hysteria2 подключена, порт **не** добавляется в список — `wait_for` умеет только TCP, а hy2 — UDP. Комментарий в `provisioning.py:359-361`.

## Handler flush — почему в `install_shadowtls_stack:197`

В конце роли — `meta: flush_handlers`. Причина (комментарий в cfg): внутри одной play `check_node_health` идёт сразу после протокольных ролей, и если `shadowtls.service` ещё не перезапущен (handler в очереди) — `wait_for :8443` откроет сокет **старого** процесса, который может давать ложное «порт ок» при сломанной новой конфигурации. `flush_handlers` перед выходом из роли гарантирует, что `check_node_health` видит уже новый процесс. Та же идиома — в `install_vless_reality`.

## Relay-роль (jump node → WG tunnel → exit)

`roles/relay_jump_node/tasks/main.yml`. Opt-in (гейтится через `when: relay_wg_private_key is defined`). Логически:

```
   client ──VLESS Reality──► RU VPN node (jump)
                                    │
                                Xray ──direct outbound──► wg0 (WireGuard client)
                                    │
                                    └──► exit node (non-RU, раскручен ролью wg_exit_node)
                                            │
                                            ▼
                                         internet
```

Ключевой трюк — **patching xray outbound через jq**:

```bash
# roles/relay_jump_node/tasks/main.yml:78-97
jq '
  (.outbounds[] | select(.tag == "direct" and .protocol == "freedom")) |=
    .streamSettings.sockopt = {
      "interface": "wg0",
      "tcpKeepAliveInterval": 30
    }
' "${CONFIG}" > "${TMP}"
```

Xray биндит исходящий freedom-socket на интерфейс `wg0`, так что весь пользовательский трафик уходит через тоннель, а management-трафик (SSH, ansible) — через main interface (который не знает про wg0 вообще). Идемпотентно: второй прогон перезапишет тот же ключ тем же значением.

**Hysteria2 пропускается** (`main.yml:99-115`): outbound routing у него управляется ядерной routing-таблицей, а не конфигом. На relay-ноде hy2-трафик выходит напрямую. Комментарий: «UDP protocols are rarely blocked and don't need relay».

## `deploy_app_stack` и `deploy_web_frontend` — web host

Работают поверх `[web]`-группы в inventory (`nl-web`). Две отдельные роли, но обе дёргаются последовательно в `site.yml` и в `playbooks/deploy_app_stack.yml` / `deploy_web_frontend.yml`.

- `deploy_app_stack` — кладёт `docker-compose.yml` + `.env` на хост, делает `docker compose up -d`. Контейнеры: `postgres`, `redis`, `backend`, `worker`, `bot`, `admin` (SPA), `webapp` (SPA). Подробности — в `infrastructure/deployment.md`.
- `deploy_web_frontend` — установка/рендер nginx site-конфига для `grinwer.online`, certbot DNS-01 через Cloudflare API (см. `infrastructure/deployment.md`).

Обе роли **не** трогают VPN-ноды и могут быть прогнаны отдельно (`--tags web` или `--limit web`).

## `wg_exit_node` — чужая non-RU exit нода

`roles/wg_exit_node/tasks/main.yml`. Настраивает WireGuard **сервер** на чужой (не-RU) машине: устанавливает wg-tools, рендерит `wg0.conf` с peers, включает NAT masquerade и IP forwarding. Эта нода — terminus трафика относительно relay'ев.

Отдельная hosts-секция в `site.yml` (`- hosts: wg_exit_nodes`). Группа **описана** в `inventories/prod/hosts.yml` — это статический snapshot для operator-plays (monitoring, rollout node_exporter, fleet audit). Для ad-hoc provisioning из backend'а используется dynamic inventory из БД.

## Как backend использует ansible

Со стороны backend'а точкой входа является `run_playbook(playbook, inventory, limit, extra_vars)` в `backend/app/services/ansible_runner.py` (функция `run_playbook`):

```python
cmd = [
    "ansible-playbook",
    str(playbook_path),
    "-i", str(inventory),
]
if ANSIBLE_PRIVATE_KEY_FILE:
    cmd.extend(["--private-key", private_key])
if limit:
    cmd.extend(["--limit", limit])
if extra_vars:
    cmd.extend(["--extra-vars", json.dumps(extra_vars)])

return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=300)
```

- `ANSIBLE_ROOT` — по умолчанию `infra/ansible` относительно кода backend'а, переопределяется env.
- `ANSIBLE_PRIVATE_KEY_FILE` — путь к приватному ключу, пробрасываемому из `docker-compose.yml`. Если ключ не задан — ansible попытается использовать системный `~/.ssh/id_*` (что в контейнере worker'а означает «ничего не работает»).
- `timeout=300` — 5 минут, жёсткий. Никакого retry на уровне ansible_runner'а: retry-логика живёт в orchestrator'е (`_handle_task_outcome` → `reset_failed_task`).
- `check=False` — non-zero exit не бросает исключение. Caller (`_execute_task`) разбирает rc и stdout отдельно.

Matching вызовов backend → ansible (из `provisioning.py:692-773` и `warm_pool.py:204-219`):

| Кто зовёт | playbook | target |
|---|---|---|
| `orchestrator.run_task(target=node, action=bootstrap/rebootstrap)` | `site.yml` | `[vpn_nodes]` |
| `orchestrator.run_task(target=device, action=apply/revoke)` | `playbooks/provision_device.yml` | `[vpn_nodes]` |
| `orchestrator.run_task(target=node, action=resync_vless)` | `playbooks/resync_node.yml` | один vpn_node |
| `orchestrator.run_task(target=node, action=diagnose)` | `playbooks/diagnose_node.yml` | один vpn_node |
| `warm_pool.warm_one_bundle` | `playbooks/provision_device.yml` | один vpn_node, `state=present` |
| `warm_pool.physical_revoke_credential_bundle` | `playbooks/provision_device.yml` | один vpn_node, `state=absent` |

Для non-VPN-ноды (деплой бекенда на web, Grafana на monitoring) **backend не вызывает ansible**. Эти деплои делаются оператором с ноутбука командой `ansible-playbook site.yml` (или `--tags web`). Единственный code-path, который что-то делает на `web` в runtime — это сам docker-compose внутри backend/worker.

## ⚠️ Неясные места

- **`host_key_checking = False` + динамический inventory.** Каждый новый ansible-run backend'а открывает SSH в ноду, взятую из БД, без какой-либо записи в `known_hosts`. MITM между backend'ом и нодой полностью незаметен ansible'у. Защита остаётся только «доверие к IPv4-адресу в `vpn_nodes.host`».
- **Секреты в CLI-строке `--extra-vars`.** `shadowtls_password`, `shadowtls_ss_password`, `relay_wg_private_key`, `vless_reality_private_key` — все попадают в ansible через `json.dumps(extra_vars)` как аргумент команды. В `ps auxf` на worker-хосте это видно любому пользователю, имеющему читать `/proc/*/cmdline`. В контейнере worker'а root — только process owner'а, но host-level инспектор (если кто-то получит host) увидит секреты в аргументах.
- **`hosts.yml` vs БД — дрейф-риск.** Fleet ведётся в БД (`VPNNode` / `WGExitNode`) как источник истины для provisioning'а; `hosts.yml` — ручной snapshot для operator-plays (monitoring, bulk rollout). Если после спавна ноды забыть добавить её в `hosts.yml`, monitoring play её не накроет: node_exporter не встанет, Prometheus target не появится, нода «пропадёт» из Grafana.
- **Playbook `diagnose_node.yml` не упомянут в `deployment.md`-роутах.** В `playbooks/` есть, orchestrator его зовёт (`action=diagnose`), но как именно оператор триггерит диагностику — через API-эндпоинт `/api/nodes/{id}/diagnose` или напрямую `ansible-playbook` — не документировано в коде однозначно.
- **`forks = 20` в defaults** — но backend процесс ограничивает параллельность через `MAX_CONCURRENT_ANSIBLE=3` (и warm_pool — ещё два). Эффективно используется только один fork на запуск (одна нода в temp-inventory). Высокий `forks` — это наследие, когда site.yml мог бить по нескольким нодам сразу вручную; сейчас никогда не стреляет.

> ⚠️ См. audit/... — plaintext-секреты в `--extra-vars` CLI.
