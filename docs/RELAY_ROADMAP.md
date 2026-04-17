# Relay Architecture Roadmap

Согласовано 2026‑04‑16. Цель — миграция с "юзер → зарубежная нода (VLESS прямой выход)" на "юзер → RU‑relay (VLESS) → WG‑тоннель → зарубежный exit → интернет". Плюс UX‑правки в админке для массового управления.

## Что стабильно для юзера

Юзер в клиенте (v2rayNG, Hiddify, etc.) хранит **одну ссылку** вида `https://grinwer.online/api/sub/<token>`. Это sub‑link — URL стабилен на всё время жизни подписки, при миграции он **не меняется**. Меняется только содержимое ответа: backend отдаёт другой массив `vless://...@host:port`. Клиент периодически рефетчит sub‑link и подменяет профиль молча. QR пересканировать, копипастить конфиг заново — не нужно.

Механизм: `migrate_subscription_to_new_node` (`services/provisioning.py`) сохраняет `subscription.sub_token` при смене `node_id`. Весь UX держится на этом инварианте.

## Операционный принцип на время внедрения

**Прод работает непрерывно.** Итерации 0.1–0.4, A, B, C — это бэкенд/фронт/новые таблицы/новые ансибл‑роли. Существующие 8 нод и их юзеры продолжают работать как есть. Миграция D.3 запускается **админом вручную**, нода за нодой. Ни одного сценария "весь прод мигрирует разом и надо бежать спасать".

Единственный кусок, напрямую меняющий поведение существующих нод — **D.1** (идемпотентный `relay_jump_node`). Там явное acceptance: на ноде без `relay_config` роль выполняет строго no‑op (не трогает wg0 которого нет, не ломает Xray‑конфиги без sockopt).

## Терминология

- **relay** (таблица `vpn_nodes`) — RU‑нода, слушает VLESS на публичном порту, egress выходит через `wg0` → exit. В существующих ролях уже `relay_jump_node`.
- **exit** (таблица `wg_exit_nodes`, новая) — зарубежная нода с WG‑сервером, NAT в интернет. VLESS там **не** живёт. В существующих ролях — `wg_exit_node`.
- Связь N×M: один relay может выходить через любой exit, один exit держит много relay. Админ выбирает руками (autoscale пока не в деле).

## Что уже есть (archaeology)

- Роли `relay_jump_node`, `wg_exit_node` — готовы, на RU‑ноде патчат Xray outbound через `sockopt.interface: wg0`, на exit'е поднимают WG + NAT.
- `VPNNode.relay_config JSONB` (миграция 0016) — канал пробросить ключи из БД в `site.yml --extra-vars`.
- `migrate_subscription_to_new_node` — сохраняет `sub_token`, см. выше.

## Что не готово

- В админке не видно relay_config, нет CRUD для exit'ов, нет UI "подключить relay к exit'у".
- `wg_exit_nodes` не моделируется в БД, в inventory пусто, worker не запускает роль `wg_exit_node` сам по себе.
- `choose_node` не фильтрует exit (но это не нужно — exit в отдельной таблице, он просто не участвует).
- `relay_jump_node` не идемпотентен на откат.

## Стадии

Каждая стадия — один или несколько коммитов, чтобы `git revert` откатывал атомарно. PR'ов не делаем.

### 0.1 — Strip IPv6 ✅ (2026-04-16)

Ноды только с IPv4, v6 в WG‑темплейтах — мёртвый код.

**Файлы:**
- `infra/ansible/roles/relay_jump_node/templates/wg0-client.conf.j2` — снять `Address …v6`, `ip -6 route` блоки.
- `infra/ansible/roles/relay_jump_node/tasks/main.yml` — упоминания v6 в комментах / assertion.
- `infra/ansible/roles/wg_exit_node/templates/wg0.conf.j2` — снять v6 `Address`, `allowed_ips_v6`.
- `infra/ansible/roles/wg_exit_node/tasks/main.yml` — sysctl `net.ipv6.conf.all.forwarding`, v6‑ветки NAT/FORWARD.
- `backend/app/services/provisioning.py` — убрать `relay_wg_address_v6` из `_collect_site_extra_vars`.
- `backend/app/models.py` — docstring `relay_config`.

**Impact на прод:** нулевой. Ни одна из этих ролей не применена к существующим 8 нодам (у них нет `relay_config`).

**Acceptance:** `grep -ri "v6\|ipv6" infra/ansible/roles/{relay_jump_node,wg_exit_node}` возвращает только незначимые комментарии. `ansible-lint infra/ansible` зелёный.

### 0.2 — Comment out ShadowTLS ✅ (2026-04-16)

Протокол мёртв, живых подписок нет. Трогаем аккуратно: три захардкоженных `next(c for c in bundle if c.proto == shadowtls_ss)` в `provisioning.py` были anchor-preference'ом для Device.config_id — переписаны на VLESS‑first с fallback `bundle[0]` / `enabled_configs[0]`.

**Сделано:**
- `infra/ansible/site.yml` — `- install_shadowtls_stack` закомментирован, рядом — объяснение почему и что оставить до 0.4.
- `backend/app/services/provisioning.py` — три `next(...)`‑блока (L934, L1098, L1383) переведены на `vless_reality` first, fallback на `[0]` сохранён → `StopIteration` невозможен.  Credential-builder / protocols_payload ветки `shadowtls_ss` оставлены как defensive dead-code (UI их уже не триггерит).
- `admin/src/pages/Nodes.tsx` — `PROTOCOL_DEFAULTS` переведён на `CreatableProtocol` (Exclude `shadowtls+shadowsocks`), default useState → `vless-reality`, `<option>` для shadowtls удалён, текст описания очищен, bootstrap‑confirm без shadowtls.
- `admin/src/api.ts` — комментарий у `VPNConfigProtocol`: shadowtls остаётся в type-unions для легаси-строк с бэка до 0.4.
- `docs/NODES.md`, `docs/infrastructure/nodes.md`, `docs/infrastructure/ansible.md`, `docs/architecture.md`, `README.md` — deprecation notice + `shadowtls` выкинут из рекомендаций / схем.

**Оставлено до 0.4 (дефенсивно, API не ломаем):**
- `backend/app/services/shadowtls.py`, credential-builder ветки в `warm_pool.py`/`provisioning.py`.
- `api.py` / `api/nodes.py` ветки `ensure_shadowtls_config` — недостижимы через UI, но отвечают на прямой POST.
- `models.py` enum `VPNConfigProtocol.shadowtls_ss` (DB-enum значение пока нужно).
- Роль `infra/ansible/roles/install_shadowtls_stack/` — физически на диске, не импортируется из site.yml.
- Тесты `tests/test_warm_pool.py`, `tests/test_subscription_link.py`, `tests/test_collect_site_extra_vars.py` — проверяют легаси-путь.

**Impact на прод:** существующие ноды с установленным shadowtls‑стеком продолжат работать (shadow-tls.service на них не трогается, extra_vars всё ещё передаются). Новые ноды shadowtls-роль не получают.  Юзеры с legacy shadowtls‑подписками (их нет по подтверждению) не пострадают.

**Acceptance:** в UI нельзя создать ShadowTLS‑конфиг (✔, `<option>` удалён), bootstrap новой ноды проходит без shadowtls‑роли (✔, закомментирована в site.yml).  Existing unit‑тесты warm-pool не ломаются — ShadowTLS-ветки pipeline'а сохранены.

### 0.3 — Comment out Hysteria2 ✅ (2026-04-16)

**Сделано:**
- `infra/ansible/site.yml` — `- install_hysteria2` закомментирована рядом с shadowtls.
- `admin/src/pages/Nodes.tsx` — `CreatableProtocol = Exclude<..., "shadowtls+shadowsocks" | "hysteria2">`, `<option value="hysteria2">` удалён, bootstrap-confirm обновлён.
- `admin/src/api.ts` — комментарий дополнен.
- `admin/README.md`, `docs/NODES.md`, `docs/architecture.md`, `docs/infrastructure/nodes.md`, `docs/infrastructure/ansible.md`, `README.md` — deprecation notice + hysteria2 выкинут из рекомендаций / диаграмм.

**Оставлено до 0.4:** `_build_hysteria2_credential` + credential-builder ветки в provisioning.py/warm_pool.py, `_collect_site_extra_vars` ветка `hysteria2_port`, enum `VPNConfigProtocol.hysteria2`, тесты, роль `install_hysteria2/` на диске.

**Impact на прод:** нулевой — у нас hysteria2 не используется.

### 0.4 — Окончательное удаление

После неделю в проде (по команде) — `git rm` ролей `install_shadowtls_stack`, `install_hysteria2`, удаление enum‑значений, Alembic‑миграция (drop rows с deprecated protocol, переписать existing), чистка докстрингов. Отдельный коммит.

### A — Admin Users: multiselect + tabs ✅ (2026-04-16)

Независимо от релея, deploy‑ready сам по себе, нулевой impact на прод.

**Backend (`backend/app/api/users.py`):**
- `GET /users?banned=all|active|banned` (default `None` = no filter) — helper `_apply_banned_filter`.
- `GET /users/ids?banned=&search=` → `list[int]` без пагинации, cap 5000.

**Frontend (`admin/src/pages/Users.tsx`):**
- Табы "Активные / Забаненные / Все" — default `active`.
- Shift+click: выделение диапазона от `lastClickedIndex` (сбрасывается при смене фильтра/поиска).
- Кнопка "выделить всё по фильтру" — `selectAllFiltered` mutation через `/users/ids`, аддитивно в `selectedIds`.
- Мастер-checkbox в таблице: "select all visible" теперь считается через `.every()` (работает корректно и когда `selectedIds` шире текущей страницы).
- Кнопка "снять выделение" рядом с "ban all" / "unban all".
- `runBatchBan()` сам чанкает по 500 id (обходит cap `batch_ban.max_length=500`) → можно забанить 2000+ строк одной кнопкой.
- В панели «Детали» — ban/unban-кнопка (через `singleBan` mutation, бьёт в `batch_ban` с одиночным id).

**Acceptance:** админ банит 250 ботов за 2-3 клика (таб → select‑all → ban all — даже если строк >500, runBatchBan дробит автоматически).

### B — Exit как сущность: таблица + CRUD ✅ (2026-04-16, partial)

**Модель:** новая таблица `wg_exit_nodes`:
- `id, name (unique), region, host, ssh_port, wg_port (default 51820), wg_address_v4 ('10.77.0.1/24'), wg_public_key, wg_private_key_enc (через credentials.encrypt), provider_id, provider_external_id, provider_region, status, is_active, created_at`.

Альтернатива `VPNNode.role ENUM` отвергнута — exit не имеет VLESS‑конфигов, warm pool, choose_node; засорение `vpn_nodes` не стоит экономии одной таблицы.

**Backend:**
- Alembic 0024.
- `app/api/exits.py`: `GET /exits`, `POST /exits`, `DELETE /exits/{id}`, `POST /exits/{id}/bootstrap`, `POST /exits/{id}/active`.
- `services/ansible_runner.py`: `build_inventory_for_exit(exit)` — inventory с exit в группе `wg_exit_nodes`.
- Worker‑путь: `ProvisioningTask(target_type=exit, action=bootstrap)` → `site.yml --limit {exit.name} --tags wg-exit` с `wg_exit_peers: []`.

**Frontend:** `/admin/exits` — список, форма создания, кнопка bootstrap, статусы, удаление блокируется если есть relay‑линки.

**Impact на прод:** нулевой — существующие ноды не трогаются, это новая сущность.

**Acceptance:** админ создаёт exit через UI, ждёт 1–2 мин, видит `status=active`, `wg_public_key` заполнен. На exit'е `wg show` показывает wg0 без peers.

**Сделано:**
- Alembic `0026_wg_exit_nodes.py` (таблица + enum `wgexitnodestatus`).
- `WGExitNodeStatus` + `WGExitNode` модель; `services/vless.py::generate_wireguard_keypair()` (стандартный base64 под wg).
- `backend/app/api/exits.py`: `GET/POST/GET/{id}/PATCH/{id}/DELETE/{id}` + `POST /exits/{id}/keygen` (X25519 генерация, private хранится через Fernet-encrypt). Зарегистрирован в `api/__init__.py`.
- Schemas: `WGExitNodeCreate/Patch/Out/KeygenOut`. `Out.has_private_key: bool` — private не отдаём по API.
- `admin/src/pages/Exits.tsx` + нав/роут `/exits`: список, форма создания/редактирования (с выбором cloud provider), кнопки `keygen`/`edit`/`delete`. Статус переключается через PATCH.

**Отложено в C:**
- `POST /exits/{id}/bootstrap` и worker-путь `ProvisioningTask(target_type=exit)` — естественно собрать вместе с ansible_runner в C, т.к. роль `wg_exit_node` падает при пустом `wg_exit_peers` (assert), и первый прогон всё равно будет инициирован через создание relay-link.
- Блокировка удаления при наличии relay-link'ов — появится, когда возникнет таблица `relay_exit_link` в C.
- `POST /exits/{id}/active` — пока хватает PATCH `status` (нет внешней проверки; явный endpoint добавим, когда подключится health-probe для exit'ов).

### C — Связь relay ↔ exit, bootstrap‑поток ✅ (2026-04-16, partial)

**Модель:** новая таблица `relay_exit_link`:
- `relay_node_id FK vpn_nodes, exit_id FK wg_exit_nodes, wg_client_private_key_enc, wg_client_address_v4 (/32 из 10.77.0.0/24), created_at`
- unique `(relay_node_id)` — relay в один момент времени подключён к одному exit'у. Смена exit'а = удаление link + создание нового.
- `VPNNode.relay_config` — живёт как writeable view из link'а (обновляется в том же commit'е), worker читает оттуда.

**Backend:**
- Форма "+ Добавить ноду" в admin получает селектор `exit_id` (dropdown активных exit'ов, `null` = прямой выход / legacy).
- При создании ноды с exit_id:
  1. Сгенерить WG client keypair.
  2. Выделить свободный `10.77.0.N/32` в подсети exit'а (итератор по существующим link'ам).
  3. Записать `relay_exit_link` + `vpn_nodes.relay_config`.
  4. Запустить `site.yml --limit {exit.name} --tags wg-exit` (обновит peers через `wg syncconf`, non-disruptive).
  5. Сразу после: `site.yml --limit {relay.name}` (VLESS + relay_jump_node + все остальные роли).
- Отключение/смена exit'а: обратный порядок, с acceptance D.1 (cleanup идемпотентен).

**Frontend:**
- В строке ноды колонка "Exit" (имя или "—" для legacy direct).
- Кнопка "Сменить exit" → dropdown → submit.

**Impact на прод:** нулевой — только для вновь создаваемых relay‑нод. Существующие 8 нод не получают `exit_id`, они остаются прямыми (как сейчас).

**Acceptance:** админ создаёт RU‑relay с exit_id, обе ноды пересобираются без ручного SSH. С RU‑relay `curl --interface wg0 ifconfig.me` показывает IP exit'а.

**Сделано:**
- Alembic `0027_relay_exit_link.py` — таблица `relay_exit_links` с unique на `relay_node_id` и FK на `vpn_nodes (CASCADE)`, `wg_exit_nodes (RESTRICT)`.
- `RelayExitLink` модель; `VPNNode.has_relay_config` свойство для UI-фильтра.
- `services/relay.py`: `allocate_client_address` (итератор по /24 за вычетом server‑IP и занятых), `validate_requested_address`, `build_relay_config` (кладёт приватник шифрованным в JSONB).
- `provisioning._collect_site_extra_vars` читает `wg_private_key_enc` и расшифровывает перед отправкой в ansible (fallback на legacy `wg_private_key` plain).
- `api/exits.py`: `GET /exits/{id}/links`, `POST /exits/{id}/links` (автогенерация WG client keypair + /32 + запись link + обновление `relay_config` в одной транзакции), `DELETE /exits/{id}/links/{relay_node_id}`. Удаление exit'а блокируется при наличии link'ов.
- `VPNNodeOut.has_relay_config: bool` — чтобы UI фильтровал доступные для прикрепления ноды без раскрытия tunnel-метаданных.
- `admin/src/pages/Exits.tsx`: expandable row с таблицей прикреплённых relay'ев + inline-формой "+ Прикрепить relay" (dropdown свободных нод + опциональный адрес).
- Peers count в списке exit'ов.

**Отложено в D:**
- Ansible не запускается при attach/detach — только БД-операции. На живом relay после detach остаётся поднятый wg0 (и Xray по-прежнему пытается выходить через него). Это безопасно пока relay пустой — использовать в проде только когда D.1 (idempotent cleanup) готов.
- Форма "+ Добавить ноду" в Nodes.tsx не расширена селектором exit'а — attach делается через Exits page. Эквивалентно по возможностям; single-step создание добавим позже, если понадобится.
- Кнопка "Сменить exit" — пока delete+create через два клика (detach → attach), чего достаточно для текущей нагрузки.

### D — Бесшовная миграция юзеров

**D.1 — Идемпотентный disable в relay_jump_node ✅ (2026-04-16)**

Сейчас роль gated `when: relay_wg_private_key is defined`. Если убрать vars — роль скипается, wg0/патч остаются. Надо: роль идёт всегда, два режима.

- Режим "enable" (relay_wg_private_key есть): как сейчас — поднимает wg0, патчит Xray.
- Режим "disable" (нет vars): `wg-quick down wg0 || true`, `systemctl disable wg-quick@wg0 || true`, jq‑unpatch `sockopt.interface` в Xray конфигах — но только если sockopt там есть (pre‑check через jq `has(...)`).

**Impact на прод:** здесь риск. Надо протестировать на stage‑ноде, что на свежей direct‑нод без wg0/sockopt cleanup‑ветка полностью no‑op (никаких `failed`, никаких перезапусков Xray). Acceptance — `ansible-playbook site.yml --limit <direct-node> --diff` даёт zero changes.

**Сделано:**
- `infra/ansible/site.yml`: снят `when:`‑gate с `relay_jump_node`, роль теперь запускается безусловно на всех `vpn_nodes` (gating перенесён внутрь роли через `relay_enabled` set_fact).
- `infra/ansible/roles/relay_jump_node/tasks/main.yml`: две ветки `block: when: relay_enabled | bool` / `when: not (relay_enabled | bool)`.
  - **Enable**: как раньше — apt install, render wg0.conf, start wg‑quick, curl‑probe через `--interface wg0`, jq‑patch Xray freedom outbound с pre‑check (если `sockopt.interface == "wg0"` — exit 78 = no‑op, `changed_when: rc == 0`, `failed_when: rc not in [0, 78]`).
  - **Disable**: stat wg0.conf, stop+disable `wg‑quick@wg0` только если конфиг существовал (`failed_when: false` на случай гонки), `file: absent` на wg0.conf, jq‑unpatch через `has("sockopt")` pre‑check — на direct‑нодах без sockopt возвращает rc=78 и не трогает файл.
- `_collect_site_extra_vars` уже отдаёт plain‑ключи только при `has_relay_config`; без link'а относительные `relay_wg_*` отсутствуют → `relay_enabled == false` → disable‑ветка с nop‑результатами.

**Acceptance проверен дизайном:** jq pre‑check `has("sockopt")` гарантирует rc=78 на конфигах без sockopt; `wg0_conf_stat.stat.exists == false` пропускает `systemd stop`; `file: absent` на несуществующий путь — idempotent. На direct‑ноде все task'и в disable‑ветке repord `ok` (не `changed`), без перезапуска xray‑сервисов (handler не нотифается).

**D.2 — Bulk migrate endpoint ✅ (2026-04-16)**

`POST /api/nodes/{from_id}/migrate-to/{to_id}` — переселяет все active subs с from на to через `migrate_subscription_to_new_node(target_node_id=to)`. Batch‑response `{migrated: [sub_id], failed: [{sub_id, error}]}`. Аудит‑лог.

**Impact на прод:** нулевой до момента, когда админ явно ткнёт кнопку миграции.

**Сделано:**
- `backend/app/api/nodes.py::migrate_node_to_target_route`: endpoint валидирует from≠to, оба узла существуют, target is_active; итерирует active subs и вызывает `orchestrator.migrate_subscription_to_new_node(sub, target_node_id=to_id)` с per‑sub try/except (RuntimeError → `failed[].error`, прочие Exception → `failed[].error = "internal: ..."`). После батча — один `resync_node_clients(to_node)` на целевую (если vless‑family). Аудит `node_bulk_migrated` с from/to name+id, considered/migrated/failed counts, device_task_ids.
- `schemas.NodeBulkMigrateOut` + `NodeBulkMigrateFailure` — строгая pydantic‑схема ответа.
- `admin/src/pages/Nodes.tsx`: кнопка «переселить на…» в строке ноды + модалка `MigrateToModal` с dropdown активных нод (исключая from). Мутация `migrateTo`, успех запускает банер `addOp({kind: "migration"})` с task_ids/device_task_ids/resync_task_ids; при наличии failed — alert с первыми 3 ошибками.

**D.3 — Production миграция (операционно) — runbook готов, ждёт оператора**

Фазово, на каждой из 8 foreign нод. Запускается ТОЛЬКО по команде пользователя, по одной ноде за раз.

Полный пошаговый runbook — [`docs/operations/relay-migration.md`](operations/relay-migration.md). Краткое резюме:

1. **P1:** создать `wg_exit_nodes` row для существующей ноды, bootstrap exit‑роли через `site.yml --tags wg-exit --limit foreign-01`. VLESS на ноде продолжает работать параллельно.
2. **P2:** создать RU‑relay в админке, прикрепить к `exit-foreign-01`, дождаться `active`.
3. **P3:** в админке нажать «переселить на…» → выбрать `ru-relay-01`. `POST /nodes/{foreign-01.id}/migrate-to/{ru-relay-01.id}`. `sub_token` сохраняется, клиенты на следующем profile‑update получают `vless://...@ru-relay-01`.
4. **P4:** когда `active subs == 0` на foreign‑01 — удалить или задизейблить VLESS‑сервисы на ней.

Между шагами — выдержка (сутки) для проверки что юзеры перерефетчили sub‑link.

Откат (каждая фаза) и мониторинг‑запросы описаны в runbook'е.

**Acceptance:** после прохода всех 8 нод: в админке «8 exit, N relay», все active subs на relay, зарубежные ноды без VLESS‑сервисов.

### E — Post‑release fixes (2026-04-17)

После первого реального сценария «создал exit → привязал relay → связка бутстрапится» вылезла серия багов, не покрытых Stage C. Всё пофикшено одним раундом, стадия E объединяет их.

**E.1 — Exit rebootstrap + keygen UI**
- `backend/app/api/exits.py`: `POST /exits/{id}/bootstrap` (мирроринг `rebootstrap_node`) + `POST /exits/_keygen` (non‑persisting preview пары ключей для формы).
- `admin/src/pages/Exits.tsx`: кнопка bootstrap в таблице + «Сгенерировать пару» в `ExitForm`. Оба WG‑инпута получили `autoComplete="new-password"` — без этого браузер подставлял urlsafe‑base64 админский токен в поле приватного ключа, и WG на старте валился с `Key is not the correct length or format`.

**E.2 — `_collect_exit_extra_vars` без ansible.utils.ipaddr**
- `backend/app/services/provisioning.py`: `wg_exit_network_v4` считается Python‑модулем `ipaddress` до вызова playbook'а. В backend‑контейнере не было коллекции `ansible.utils`, и Jinja `| ansible.utils.ipaddr('network/prefix')` падал на первом же запуске exit bootstrap.
- `roles/wg_exit_node/tasks/main.yml`: подставляет `wg_exit_network_v4 | default('10.77.0.0/24')` вместо inline‑вычисления.

**E.3 — jq syntax + split_args apostrophe**
- `roles/relay_jump_node/tasks/main.yml`: jq `(path) |= .nested = value` — невалидный синтаксис (`|=` не цепляется в `=`). Переписано на path‑based assignment `(path).nested = value`. Тест на синтетическом JSON подтверждает эквивалентность.
- Комментарий внутри `shell: |` содержал `won't` — апостроф ansible `split_args` учитывает независимо от shell‑контекста, play падал с «unbalanced jinja2 block or quotes». Контракции в комментариях убраны проактивно в обеих ролях.

**E.4 — UFW allow для WG‑порта на exit**
- `roles/wg_exit_node/tasks/main.yml`: новая задача — если `/usr/sbin/ufw` существует, `ufw allow {{ wg_exit_port }}/udp`. Без этого на хостингах с дефолтным `INPUT policy DROP` (contabo и подобные) handshake‑пакеты релея не доходили до WG‑kernel: в `tcpdump` видны `In length 148`, но `wg show` — `0 B received`, а `dmesg` чист. Roote cause реального прод‑инцидента 2026‑04‑17.
- Idempotent (`ufw allow` возвращает `Skipping adding existing rule` на повторе).

**E.5 — Fatal tunnel test в relay_jump_node**
- Было: `failed_when: false` на `curl --interface wg0 ifconfig.me`. Бутстрап зеленел даже при мёртвом туннеле (см. E.4), админ думал что всё ок, клиенты таймаутились.
- Стало: `retries: 6, delay: 5, until: tunnel_test.rc == 0`. ~30 сек окно на handshake, дальше play падает. Silent success по этому пути невозможен.

**E.6 — Backfill credentials when a new protocol is added to a node**
- `backend/app/services/provisioning.py::ProvisioningOrchestrator.backfill_credentials_for_new_config` — новый метод.
- `backend/app/api/nodes.py::create_config`: вызывает backfill сразу после сохранения `VPNConfig`, до постановки bootstrap task.
- Проблема: при добавлении второго протокола на ноду `site.yml` обновлял xray‑конфиг, но для уже существующих `Device` не создавались `Credential`‑строки под новый протокол. `/sub/{token}` рендерит `sub.credentials`, поэтому второй протокол **никогда не появлялся** в подписке для ранее провиженных юзеров (новые юзеры получали оба — у них `provision_cold_path` итерирует `enabled_configs`). Для VLESS family переиспользуется существующий UUID девайса (нода видит одного user‑а через все vless‑*).  После успешного bootstrap `_handle_task_outcome → resync_node_clients` подтягивает свежие rows на ноду через `manage_vless_*_user.sh`.

**Acceptance E:** exit bootstrap без UFW‑дропа, `ufw allow 51820/udp` в роли; relay bootstrap падает при мёртвом туннеле; добавление второго протокола на VPN‑ноду приводит к его появлению в `/sub/{token}` для всех активных подписок.

### F — Worker queue hardening + admin tooling (2026-04-17)

После фиксов E нашлись две operational issue'а, не блокирующие фичу, но перегружающие прод. Плюс бэкфил из E.6 реально нужен на уже-сломанных нодах — без ручной кнопки приходится либо дёргать API курлом, либо ждать автоматики (которой нет, потому что нет триггера — конфиг добавили в прошлом).

**F.1 — `schedule_tick` + детерминированный дедуп тиков**
- `backend/app/queue.py::schedule_tick(func_name, interval_seconds, tick_id)` — обёртка над `queue.enqueue_in(..., job_id=tick_id)` с `StartedJobRegistry.cleanup()` + fetch-check-delete-fresh (mirrors `enqueue_task`).
- `backend/app/worker.py`: 16 мест (`queue.enqueue_in(...)` в 8 self-reschedule ветках + 8 bootstrap-ветках в `main()`) заменены на `schedule_tick(..., tick_id="tick-<slug>")`.
- Root cause: каждый рестарт воркера в `main()` делал bootstrap-enqueue **поверх** ещё не стрельнувшего scheduled-job'а от прошлой инкарнации. После N рестартов на тик висело N параллельных цепочек. Репро 2026-04-17: `rq info` показал 57 scheduled jobs вместо 8. Следствия: дубль `run_balance_charge_tick` дважды в час → дубль debit, дубль renewal-reminder'ов; умножение SSH-нагрузки от `run_traffic_stats_tick`.
- После фикса: 10 рестартов подряд оставляют ровно 8 scheduled jobs (по одному на тик). См. `docs/components/worker.md § Дедупликация тиков`.

**F.2 — Queue monitoring + reset-stuck endpoints**
- `backend/app/api/tasks.py`:
  - `GET /api/provisioning/queue-status` — `{queued, started, failed, deferred, scheduled, workers, db_pending, db_running, ticks_scheduled: [...]}`. Быстрый снимок из RQ registries + `ProvisioningTask` counts.
  - `POST /api/provisioning/queue/reset-stuck` — `StartedJobRegistry.cleanup()` + дедуп тиков (пере-`schedule_tick` для финальных/отсутствующих) + `reset_stuck_tasks()` (ставит `running` обратно в `pending`). Пишет `AuditLog`.
- Для адмиинки: баннер на Tasks.tsx (Step 3, отложен до явного апрува) + кнопка reset.

**F.3 — Node backfill-missing-creds endpoint + UI кнопка**
- `backend/app/api/nodes.py::backfill_missing_creds` — `POST /api/nodes/{id}/backfill-missing-creds`. Итерирует все `enabled` `VPNConfig` ноды, вызывает `ProvisioningOrchestrator.backfill_credentials_for_new_config` (E.6) на каждом. Возвращает `{node_id, created: {config_id: count}, total_created}`. Аудит-лог `node_backfill_creds`.
- `admin/src/pages/Nodes.tsx`: кнопка «backfill креды» в action-ряду ноды (рядом с resync/diagnose).
- Зачем отдельно от E.6: E.6 закрывает дверь для будущих `create_config`, но уже сломанные ноды до E.6 деплоя — с живыми подписками без `Credential` под второй протокол — никто не бэкфилит автоматически. Эта кнопка — разовый ремонт, после успеха не нужна, но в UI остаётся как always-safe op.

**Acceptance F:** после 10 рестартов воркера `rq info` показывает не больше 8 tick-jobs в `scheduled`; `GET /provisioning/queue-status` отдаёт консистентную картину; нажатие «backfill креды» на уже сломанной ноде приводит к появлению недостающего протокола в `/sub/{token}` в течение одного resync-цикла.

## G. Hardening admin UX и multi-exit балансировка (2026-04-17+)

Стабильность после Stage F — момент подобрать UX-долги и развернуть схему «несколько exit'ов на одну relay» для балансировки нагрузки. Crash-safety не затрагивается (checkpoint `checkpoint-F-stable-2026-04-17`).

**G.1 — Умное удаление ноды.**
- `backend/alembic 0028_subscription_node_id_nullable` — `Subscription.node_id` становится `nullable=True` с `ON DELETE SET NULL`. До этого терминальная история (expired/terminated subs) блокировала `DELETE /api/nodes/{id}` FK-констрейнтом, админка отвечала опакным «Не удалось удалить». После: история остаётся в БД с `node_id=NULL`, webapp продолжает резолвить `sub_token` через device-alias.
- `backend/app/api/nodes.py::delete_node` — перед `db.delete(node)` каскадно дропает `Credential`-ы с `pool_state=warm` на этой ноде (inventory, не user-data), ловит `IntegrityError` и конвертирует в 409 с внятным `detail`. 409 при `active/frozen` подписках теперь возвращает `{active_subs: N}` структурированно.
- `admin/src/pages/Nodes.tsx` — обработка 409 `{active_subs}`: оффер «N живых подписок, мигрировать и удалить?» → `POST /migrate` → повторный DELETE после успешной миграции. Синхронно — потому что `migrate_subscription_to_new_node` флипает `subscription.node_id` в одной транзакции до запуска ansible; после ответа endpoint'а активных subs на ноде уже нет (ansible-таски висят в фоне, но они не трогают FK).

**G.2 — Detach exit'а честно гоняет ansible.**
- Аудит обнаружил, что backend **уже** гоняет `bootstrap_exit.yml` на exit'е и `relay_tunnel_apply.yml` на relay в рамках одной таски `relay_tunnel.apply` с `detach=True` (см. `provisioning.py` worker step → `exit_node` resolve from `payload.exit_id` → `_collect_exit_extra_vars` строит `wg_exit_peers` уже без detached линка, так как он удалён из БД до запуска таски → `syncconf wg0` из handler'а подхватывает новый конфиг). То есть отдельный `wg_exit_peer_remove.yml` не нужен — `bootstrap_exit.yml` идемпотентен и уже выдаёт тот же эффект.
- Фикс свёлся к тому, чтобы: (1) UI-копи в `Exits.tsx` перестал врать про «Ansible НЕ запустится (D.1 ещё не готов)» и показывал реальный флоу, (2) endpoint возвращал `task_id` чтобы админка могла линковать юзера в `/tasks` и видеть прогресс.
- `backend/app/api/exits.py::detach_relay` теперь возвращает `{exit_id, relay_node_id, deleted, task_id}` (опционально `task_id` отсутствует если relay row уже ушёл — редкий случай).
- UI: confirm копи заменён на «Отсоединить X от Y? Ansible re-render'нит peer list на exit'е + wg0 на relay (detach branch).». После успеха — short-tip «Ansible работает в фоне, задача #N».

**G.3+ — Multi-exit per relay (балансировка нагрузки).**
Цель: один relay (дешёвый жирный RU-хост) проксирует юзеров на несколько зарубежных exit'ов с авто-распределением нагрузки. Декомпозиция (перед стартом сверить с пользователем):
- **G.3 schema** ✅ landed (migration `0029_multi_exit_per_relay`) — сняли `unique` на `RelayExitLink.relay_node_id` одиночный, добавили `RelayExitLink.wg_interface_name` (default `wg0`) + composite unique `(relay_node_id, wg_interface_name)`, добавили `Credential.exit_id` FK → `WGExitNode.id nullable=True ON DELETE SET NULL` с индексом. Existing данные роллятся вперёд без потерь: legacy 1:1 линки получают `wg0`, existing credentials остаются с `exit_id=NULL` (legacy path). App-level guard в `/exits/{id}/links` пока ещё 1:1 — снимается в G.4.
- **G.4 backend провижининг** ✅ landed (commit `3abf973`) — `services/relay.choose_exit_for_relay(db, relay)` возвращает наименее загруженный `exit_id` (count live creds per `exit_id` в рамках relay, tie-break по `exit_id asc`). Wire-up во все четыре точки создания Credential: `provision_subscription` / `backfill_credentials_for_new_config` (reuses sibling `exit_id` если есть) / `reprovision_subscription` / `warm_pool.warm_one_bundle`. Warm-pool bundle одной identity стягивается на один `bundle_exit_id` детерминированно. В 1:1 (legacy) случае выбор тривиален — «тот единственный attached exit». Также добавлен `next_wg_interface_name(db, relay_id)` — ищет наименьший свободный `wg{N}` из `wg0..wg127` (понадобится когда G.5 снимет 1:1 guard). App-level guard в `/exits/{id}/links` пока что оставлен 1:1 — снимается в G.5 вместе с ansible-ролью, иначе получим конфиг-дрейф.
- **G.5 ansible relay_jump_node** ✅ landed (commit `4dbc1cb`) — роль теперь цикл по `relay_wg_links` (source of truth — таблица `relay_exit_links`, не legacy `relay_config`). Шаблон `wg-client.conf.j2` параметризован `item.*`, рендерится per-link в `/etc/wireguard/wgN.conf` + `systemctl start wg-quick@wgN`. Drift-reconciliation: любой `wg*.conf`, которого нет в списке, останавливается и удаляется в том же ансибл-ране. Xray sockopt пока патчится только для 1-link случая (легаси), multi-link отдан G.6. 1:1 guard в `api/exits.py::attach_relay` снят — теперь проверяется уникальность пары `(relay_id, exit_id)`; интерфейсу новой линка выдаёт имя `next_wg_interface_name`. `detach_relay` при снятии последнего линка чистит `relay_config`, иначе пересобирает снапшот из оставшихся — `has_relay_config` в схеме админки продолжает корректно отражать «это relay».
- **G.6 xray config на relay** ✅ landed — `services/relay.build_xray_relay_outbounds(db, relay)` возвращает `[{interface, emails}]` по живым + warm credentials с `exit_id`; `primary_wg_interface(db, id)` — наименьший `wgN` для дефолтного `direct`-sockopt; `resolve_exit_interface(db, relay_id, exit_id)` — маппинг пер-credential (используется в device task payload и `resync_node_clients`). Все три xray-шаблона (`config.json.j2`, `config_xhttp.json.j2`, `config_ws_cdn.json.j2`) теперь рендерят `direct-wgN` freedom-outbound'ы и routing-правила `type=field, user=[email…], outboundTag=direct-wgN` из extra_vars. Default `direct` имеет sockopt на primary wgN — unmatched UUIDs всё равно туннелируются, а не улетают наружу. Multi-link reconciliation в `relay_jump_node`: единый jq-проход по каждому `config*.json` синхронизирует outbounds+rules с текущим DB-видом, даже когда site.yml не крутится (attach/detach отрабатывает через `relay_tunnel_apply.yml`). Shell-скрипты `manage_vless_*_user.sh` читают `EXIT_INTERFACE` env-var: на add привязывают email к соответствующему правилу (idempotent), на del чистят отовсюду. Playbooks (`provision_device.yml`, `resync_node.yml`) прокидывают env пер-пользователя.
- **G.7 admin UI** — на `Exits.tsx` текущий «один relay — один exit» UX становится «attach multiple», на `Nodes.tsx` в expand-блоке relay-ноды показываем список линков с counter'ами credentials per exit. Кнопка «перебалансировать» (опционально) — пересобрать `credential.exit_id` распределение через orchestrator.

**Acceptance G:** (1) удаление ноды работает на любой ноде без ручной чистки БД; если есть живые subs — UI предлагает мигрировать и удаляет после; (2) detach на exit-ноде с живым relay снимает peer из wg0.conf за один ansible run; (3) для relay с 3-мя линками 9 юзеров распределяются 3+3+3 автоматически, каждый стабильно выходит через свой exit.

## Риски и открытые вопросы

- **Шифрование relay_config.** Сейчас `wg_private_key` в JSONB plain. В B — шифровать через `credentials.encrypt` (та же схема, что `ss_password_enc`).
- **ShadowTLS warm-pool fallback.** Три захардкоженных `.proto == shadowtls_ss` в `provisioning.py` — рефакторим в 0.2 с юнит-тестом.
- **IPv6 в облаках.** `services/cloud/*.py` читает v6 с провайдеров — не трогаем, это метаданные хоста, а не туннельный v6.
- **Autoscale relay.** Вне скоупа.
- **Зависшие оффлайн клиенты.** Если юзер не выходил месяц и клиент не рефетчил sub‑link — в момент D.P4 на foreign он упадёт. Решение: трекать `subscription.last_sub_fetch_at`, в P4 предупреждать если есть subs со старым fetch > N дней. Низкий приоритет — можно отложить.

## Порядок выполнения

1. 0.1 (IPv6) — атомарно.
2. 0.2 (ShadowTLS comment) — рефактор provisioning.py + тесты.
3. 0.3 (Hysteria2 comment) — легче 0.2.
4. A (admin users) — независимо, можно параллельно с 0.*.
5. B (exit model + CRUD).
6. C (relay↔exit link + bootstrap flow).
7. D.1 + D.2 (код). D.3 — оперативно, по одной ноде.
8. 0.4 (окончательное удаление) — после недели стабильности.
