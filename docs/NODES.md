# Node Management

Как живут VPN-ноды в системе: из каких состояний, как бэкенд выбирает конфиг, pool'ы, health, drain.

## Lifecycle states

`VPNNode.status` (enum `VPNNodeStatus`):

| Status | Что значит | Кто переводит |
|--------|------------|---------------|
| `registering` | Создана в БД, bootstrap-таска enqueue'нута или выполняется | `POST /api/nodes` → ProvisioningOrchestrator |
| `active` | Bootstrap прошёл, ansible отработал, ноду можно ассайнить | worker при успехе `site.yml` |
| `error` | Bootstrap упал — нужно смотреть `ProvisioningTask.stderr` | worker при фейле |
| `disabled` | Админ временно вывел из ротации, на existing subs не влияет | Admin UI |
| `draining` | Идёт миграция подписок на другие ноды, после completion VM удаляется | Downscale drain tick |

Флаги `is_active: bool` — master-switch поверх статуса. `False` выключает ноду из любых выборов (warm pool, choose_node для покупок, health migration target) независимо от status. Переключается из Admin UI кнопкой **исключить** / **вернуть в пул** (см. [§ Admin actions](#admin-actions)) или напрямую через `POST /api/nodes/{id}/active {is_active: bool}`. Существующие подписки на исключённой ноде **продолжают работать** — нода выпадает только из будущих назначений.

## Supported Protocols Per Node

Каждая нода может нести любую комбинацию протоколов. Конфиг протокола — строка в таблице `vpn_configs`, привязанная к `node_id`. Одна нода = до 4 конфигов.

| Protocol | Ansible role | Default port | Default SNI | Service |
|----------|--------------|--------------|-------------|---------|
| `vless-reality` | [install_vless_reality](../infra/ansible/roles/install_vless_reality) | 9443 | `www.asus.com` | xray |
| `vless-xhttp` | [install_vless_xhttp](../infra/ansible/roles/install_vless_xhttp) | 443 | (TLS fronting domain) | xray |
| `vless-ws-cdn` | [install_vless_ws_cdn](../infra/ansible/roles/install_vless_ws_cdn) | 443 | (CDN domain) | xray + Cloudflare proxy |
| `hysteria2` | [install_hysteria2](../infra/ansible/roles/install_hysteria2) | 8443/UDP | (none) | hysteria-server |

> `shadowtls+shadowsocks` — **deprecated** (0.2, April 2026). Роль `install_shadowtls_stack` закомментирована в [site.yml](../infra/ansible/site.yml), UI не даёт создавать новые конфиги.  Enum-значение `shadowtls_ss` оставлено в `VPNConfigProtocol` и бэкенд-branch'и — until 0.4 — на случай легаси-нод.

Источник дефолтов: [admin/src/pages/Nodes.tsx `PROTOCOL_DEFAULTS`](../admin/src/pages/Nodes.tsx). Если меняешь значения в ansible-ролях — синхронизируй оба места, иначе форма в Admin UI будет предлагать не то, что реально поднимется на ноде.

## Recommended protocol stack

По состоянию на April 2026 (см. таблицу TSPU status в [README.md](../README.md#supported-protocols)):

1. **Primary: VLESS Reality** — основной протокол, per-user isolation + sharing enforcer. Порт 9443 (или high-port 47000+).
2. **VLESS XHTTP** — основной TCP-протокол, обход 16KB curtain ТСПУ.
3. **Fallback: Hysteria2** — UDP/QUIC. На мобильных нестабилен (пакет-лосс режет соединение), на broadband отлично.
4. **Fallback: VLESS+WS+CDN** — через Cloudflare. Работает, пока CF IP'шники в whitelist'е, но это moving target и каждая нода требует отдельного domain-setup'а.

**Минимум на каждой ноде:** vless-reality. Остальные — по потребности/региону.

## Config selection (code truth)

Функция [`choose_config(node, preferred_protocol=None)`](../backend/app/services/provisioning.py#L139):

```python
configs = [cfg for cfg in node.configs if cfg.is_enabled]
if preferred_protocol:
    filtered = [cfg for cfg in configs if cfg.protocol == preferred_protocol]
    if filtered:
        return filtered[0]
if not configs:
    raise RuntimeError("No enabled VPN configs found for node")
return configs[0]
```

То есть:

1. Если caller явно попросил протокол — отдаём первый enabled того же типа (без preference — по порядку в `node.configs`, который = ORM-дефолт = `id ASC`).
2. Иначе — первый enabled вообще.

**Заметка (0.2):** в трёх местах провижининга (`_finalize_warm_assignment`, `provision_subscription`, `add_device_to_subscription`) ранее был hard-coded `next(... shadowtls_ss, bundle[0])` для anchor-конфига девайса.  После 0.2 anchor-preference переключён на `vless_reality` (ShadowTLS deprecated).  Fallback `bundle[0]` страхует от `StopIteration` в любом случае.

## Pools (`server_pools`)

Опциональная группировка нод для autoscale. Нода без `pool_id` — «ручная», autoscale её не трогает. Нода с `pool_id` — член пула, попадает под:

- **Upscale**: при `utilization > AUTOSCALE_HIGH_WATERMARK` — bootstrap новой ноды через провайдер API (Hetzner и т.д.)
- **Downscale** (опционально, `AUTOSCALE_DOWNSCALE_ENABLED=1`): при `utilization < AUTOSCALE_LOW_WATERMARK` — флип младшей auto-spawned ноды в `draining`, миграция подписок, destroy VM через `AUTOSCALE_DRAIN_GRACE_HOURS`.

Env vars — см. [README.md](../README.md#environment-variables).

## Health monitoring

Probe-agent ([probes/](../probes)) бежит из нескольких регионов каждые 5 минут и пишет в `health_probes`. Backend агрегирует в `VPNNode.health_score` (0-100).

Триггеры:
- `health_score < 50` **И** `health_score < 50` стабильно → auto-migration: active subscriptions мигрируют на здоровую ноду того же pool/региона, sub_token сохраняется (установленные клиенты auto-update'ятся через sub-link).
- Старые devices revoke'аются best-effort (на мёртвой ноде это может и не сработать).
- Юзер получает нотификацию через бот с новым config link.

`blocked_regions: jsonb` — список регионов, из которых нода unreachable. Используется при выборе ноды для нового юзера (если юзер в заблокированном регионе — нода не предлагается).

## Admin actions

Через [Admin UI `/admin/nodes`](../admin/src/pages/Nodes.tsx):

- **+ Добавить ноду** — форма, enqueue `site.yml`.
- **Клик по строке** — expand конфигов, добавление/удаление. Удаление конфига каскадно подчищает терминальные девайсы + их креденшелы, чтобы FK-constraint не блокировал.
- **исключить** / **вернуть в пул** — `POST /api/nodes/{id}/active {is_active: bool}`. Выводит ноду из `_pick_node` не ломая активные подписки. Типичный use-case: свежая нода, которую хочется обкатать на тест-аккаунтах до боевого трафика; либо подготовка к миграции перед баном/миграцией в другой регион.
- **переселить** — `POST /api/nodes/{id}/migrate` → `services.health.migrate_subscriptions_off`. Все активные подписки получают новую ноду через `_pick_node` (который уже уважает `is_active=False`), девайсы перепровижиниваются через ansible в фоне. Миграция **in-place**: `sub.node_id` флипается на существующей строке, `sub_token` сохраняется, дубликатов в webapp не появляется (раньше рождалась вторая sub в статусе `blocked`). Рекомендуемый flow при бане ноды: **сначала `исключить`, потом `переселить`** — иначе планировщик может случайно вернуть подписки на ту же умирающую ноду.
- **resync** — `POST /api/nodes/{id}/resync` → `ProvisioningOrchestrator.resync_node_vless_clients`. Force-retry всех активных VLESS+Reality клиентов на ноде: бэк собирает UUID'ы из зашифрованных credentials, парсит их через `_extract_vless_uuid()`, создаёт таску `action=resync_vless` и гонит `playbooks/resync_node.yml`, которая цикличит `manage_vless_user.sh add` по списку. Идемпотентно (`manage_vless_user.sh` дропает запись по email перед re-append), безопасно запускать в любом состоянии. See [§ VLESS client resync](#vless-client-resync).

Через API (скрипты и легаси):

- `POST /api/nodes/{id}/active` — toggle `is_active` (аудит-лог `node_set_active`).
- `POST /api/nodes/{id}/migrate` — ручная миграция всех subs с ноды (аудит-лог `node_migrated`). Возвращает `{node_id, migrated_subscriptions: [int]}`.
- `POST /api/nodes/{id}/resync` — ручной resync VLESS клиентов (аудит-лог `node_resync`). Возвращает `{node_id, task_id, clients}`, `task_id=null` если на ноде нет активных vless подписок.
- `POST /api/autoscale/tick` — триггернуть autoscale pass руками.

## VLESS client resync

Исторический баг: роль [install_vless_reality](../infra/ansible/roles/install_vless_reality) рендерила `/usr/local/etc/xray/config.json` с литеральным `"clients": []`, потому что authoritative view живёт в БД бэкенда и клиенты добавляются динамически через `manage_vless_user.sh` из `playbooks/provision_device.yml`. Проблема: любой повторный `site.yml` на ноде (routine re-deploy, обновление SNI, rerun после фейла) **затирал всех юзеров**. Они начинали получать `invalid request user id` и переставали коннектиться, пока бэкенд не перепровижинил каждого руками.

Решение (двухслойное):

1. **Ансибл-слой — preserve on re-render.** Перед рендером роль `slurp`'ит существующий `config.json` (если есть), парсит `.inbounds[] | select(.tag=="vless-reality") | .settings.clients`, и прокидывает в j2 как `vless_reality_existing_clients`. Шаблон теперь рендерит `"clients": {{ vless_reality_existing_clients | default([]) | to_json }}` вместо `[]`. First bootstrap / unparseable old file → пустой список, fallback покрывает бэкенд-слой ниже.

2. **Бэкенд-слой — auto-resync after site.yml.** После **любого** успешного node-level `site.yml` в `ProvisioningOrchestrator._handle_task_outcome()` авто-триггерится `resync_node_vless_clients(node)` — при условии что на ноде есть enabled vless_reality конфиг (`_node_has_vless_reality()`). Метод:
   - Запрашивает все `Credential` где `Subscription.node_id == node.id`, `Subscription.status == active`, `proto == vless_reality`, `is_active == true`. Идёт через `Subscription.node_id`, а не `Credential.node_id` — последний заполняется только warm-pool путём, cold-path credentials имеют `node_id=NULL` и иначе выпадают из выборки.
   - `access_username` берётся с `Credential` (warm) или fallback на `Device` (cold) — смешанные вин­тажи на одной ноде resync'ятся чисто.
   - Парсит UUID из `decrypt(cred.config_text)` регуляркой `vless://([0-9a-f-]{36})@`. Отдельной колонки UUID в БД нет.
   - Создаёт `ProvisioningTask(target_type=node, action=resync_vless, payload={clients: [{username, uuid}]})`, запускает `playbooks/resync_node.yml`.
   - Resync-таски **не флипают** `node.status` в `_handle_task_outcome` — ранний return по `task.action == "resync_vless"`, иначе upscale/bootstrap transition мог бы сломаться.

Операторский flow при подозрении на drift (ручная правка конфига, restore из бэкапа, половинчатый bootstrap, клиенты ловят `invalid request user id`):
1. Нажать **resync** в Admin UI `/admin/nodes` (либо `curl -X POST /api/nodes/{id}/resync`).
2. Смотреть прогресс в `/admin/tasks` (фильтр `target=node, action=resync_vless`). Успех → все UUID из БД снова в `config.json` на ноде.

Плейбук: [playbooks/resync_node.yml](../infra/ansible/playbooks/resync_node.yml). Помощник на ноде: [files/manage_vless_user.sh](../infra/ansible/roles/install_vless_reality/files/manage_vless_user.sh) — после `mv` из `/tmp` делает `chown root:nogroup` + `chmod 0640`, иначе xray (запущенный как `nobody:nogroup`) ловит permission denied и сервис экзитает кодом 23.

## Troubleshooting

| Симптом | Чекпоинт |
|---------|----------|
| Нода в `error` сразу после создания | `docker compose logs worker --tail 200` — ищи ansible traceback. 90% случаев: pub-key не в `authorized_keys`, или `PROVISIONING_SSH_KEY` монтирован в `/dev/null`. |
| Нода в `active`, но новые подписки не выдаются | Проверь, что у ноды есть **enabled** `vless-reality` конфиг — это основной протокол. |
| `/api/nodes` даёт 500 | Недавно ловили такой случай: Pydantic v2 + `from_orm()` без `from_attributes=True` в schema config. Если появится снова — см. [backend/app/schemas.py](../backend/app/schemas.py) на предмет новых `class Config: orm_mode = True`. |
| Health score на 100, но юзеры жалуются | Probe-agent, возможно, бежит только из здорового региона. Проверь `health_probes.region` distribution — нужны probes из RU/KZ, чтобы ловить TSPU-блоки. |
| Warm pool не наполняется после добавления конфига | Это by design: при смене набора протоколов на ноде warm-bundles инвалидируются (`invalidate_node_warm_pool`), warmer'у нужно несколько тиков, чтобы пересобрать. Проверь `vpn_warm_pool_depth{node="..."}` в Grafana. |
| VLESS клиенты на ноде получают `invalid request user id` после `site.yml` | Конфиг ренедрится пустым и затирает `manage_vless_user.sh` add'ы. Роль теперь preserve'ит клиентов при re-render, плюс после каждого успешного `site.yml` бэк авто-триггерит resync. Если всё же — жми **resync** на ноде в `/admin/nodes`. См. [§ VLESS client resync](#vless-client-resync). |
| VLESS URI не парсится в v2rayNG/Hiddify (`Expected BEGIN_OBJECT but was STRING`) | В query-string отсутствовал `encryption=none` — клиенты fallback'ом парсили тело как JSON и падали на gson. Пофикшено в `_build_vless_reality_credential` ([provisioning.py L189](../backend/app/services/provisioning.py#L189)); старые сохранённые credentials обновятся при ближайшем reprovision/add-device/migrate. |
