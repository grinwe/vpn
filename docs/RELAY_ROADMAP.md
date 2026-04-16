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

**D.1 — Идемпотентный disable в relay_jump_node**

Сейчас роль gated `when: relay_wg_private_key is defined`. Если убрать vars — роль скипается, wg0/патч остаются. Надо: роль идёт всегда, два режима.

- Режим "enable" (relay_wg_private_key есть): как сейчас — поднимает wg0, патчит Xray.
- Режим "disable" (нет vars): `wg-quick down wg0 || true`, `systemctl disable wg-quick@wg0 || true`, jq‑unpatch `sockopt.interface` в Xray конфигах — но только если sockopt там есть (pre‑check через jq `has(...)`).

**Impact на прод:** здесь риск. Надо протестировать на stage‑ноде, что на свежей direct‑нод без wg0/sockopt cleanup‑ветка полностью no‑op (никаких `failed`, никаких перезапусков Xray). Acceptance — `ansible-playbook site.yml --limit <direct-node> --diff` даёт zero changes.

**D.2 — Bulk migrate endpoint**

`POST /api/nodes/{from_id}/migrate-to/{to_id}` — переселяет все active subs с from на to через `migrate_subscription_to_new_node(target_node_id=to)`. Batch‑response `{migrated: [sub_id], failed: [{sub_id, error}]}`. Аудит‑лог.

**Impact на прод:** нулевой до момента, когда админ явно ткнёт кнопку миграции.

**D.3 — Production миграция (операционно)**

Фазово, на каждой из 8 foreign нод. Запускается ТОЛЬКО по команде пользователя, по одной ноде за раз.

1. **P1:** создать `wg_exit_nodes` row для существующей ноды, bootstrap exit‑роли через `site.yml --tags wg-exit --limit foreign-01`. VLESS на ноде продолжает работать параллельно.
2. **P2:** создать RU‑relay в админке с `exit_id=<foreign-01>`, дождаться `active`.
3. **P3:** `POST /nodes/{foreign-01.id}/migrate-to/{ru-relay.id}`. `sub_token` сохраняется, клиенты на следующем profile‑update получают `vless://...@ru-relay`.
4. **P4:** когда `active subs == 0` на foreign‑01 — disable VLESS‑роли на ней (флаг в админке "это теперь pure exit", или удаление `vpn_nodes` row).

Между шагами — выдержка (сутки) для проверки что юзеры перерефетчили sub‑link.

**Acceptance:** после прохода всех 8 нод: в админке "8 exit, N relay", все active subs на relay, зарубежные ноды без VLESS‑сервисов.

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
