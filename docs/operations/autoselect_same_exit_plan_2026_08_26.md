# План: автовыбор через один exit, hy2 по exit'ам, авто-эвакуация exit'а

Статус: **план, ничего не реализовано** (2026-08-26, ревизия 2 после
адверсариального ревью по коду). Разбор проблемы —
`memory: project_autoselect_multi_exit_2026_08_26`, кратко в §0.

## 0. Что чиним и почему

Профиль «🎯 Автовыбор» (Xray-JSON, `services/xray_client_config.py`) собирает
леги одного устройства, которые выходят в интернет через **разные exit'ы**:
у device-17 владельца reality@4vds-ru-01 и xhttp@tw-ru-01 → dc-nl-01 (NL),
hy2@ufo-ru-03 и ws@ufo-ru-03 → ufo-tr-01 (TR), hy2@vsin-ru-01 → ufo-fr-01 (FR).
`leastPing` без гистерезиса выбирает лег на каждое новое соединение → смена
публичного IP и страны на ровном месте (googlevideo `ip=`-locked → 403,
сессии/комментарии рвутся). На ручном xhttp симптомов нет: один лег = один exit.

Корни (по коду):

1. exit креда выбирается `choose_exit_for_relay(db, node)` **per node** при
   провижне/прогреве (`provisioning.py:3244`, `warm_pool.py:248`); ноды одного
   устройства решают независимо → 3-4 exit'а на устройство. `leg_scheme.plan_legs`
   про exit не знает (`leg_scheme.py:110-118`: кандидат роли = «на ноде есть
   протокол»).
2. hysteria на ноде — node-level `bindDevice` = `xray_primary_interface` = линк с
   **минимальным** `wg_interface_name` (`relay.py:296-314`,
   `install_hysteria2/templates/config.yaml.j2:1`); переменная
   `hysteria2_bind_device` нигде не задаётся. Раскладка hy2 по флоту случайна
   (снято `wg show` на нодах 26.08): TR×4 (aeza-ru-01, ufo-ru-02, ufo-ru-03,
   4vds-ru-01), CZ×3 (tw-ru-01, dc-ru-01, ufo-ru-01), FR×1 (vsin-ru-01), **NL×0**.
   При этом `Credential.exit_id` hy2-кредов = `bundle_exit_id` (least-loaded),
   т.е. **врёт** относительно фактического egress; `switch_subscription_exit` это
   признаёт (docstring `provisioning.py:5637-5642`), `evacuate_exit_to` — нет и
   молча переписывает `exit_id` hy2-кредам.
3. `relay_jump_node` при любом `relay_tunnel apply` (attach/detach/reconnect/
   switch-exit/evacuate/PATCH exit) реконсайлит hysteria своим путём:
   `HY2_BIND_DEV = relay_wg_links | map(interface) | sort | first`
   (`roles/relay_jump_node/tasks/main.yml:314-328`) — override через `site.yml`
   не переживёт первый же apply.
4. Сборщик JSON получает `SubLinkConfig` только с `protocol, uri`
   (`api_extensions.py:54-56`); `node_id=` в `_decrypt_configs` (:1147) молча
   отбрасывается pydantic'ом (extra=ignore). Exit/нода лега при сборке недоступны.
5. Скрытая грабля, найденная ревью: hy2-кред **делит `access_username` (email)**
   с vless-кредами бандла (`provisioning.py:3262-3272`, `warm_pool.py:266-275`), а
   `build_xray_relay_outbounds` (`relay.py:262-288`) группирует email'ы по
   `exit_id` без фильтра по proto. Любая попытка сделать `exit_id` hy2-креда
   «честным» без proto-фильтра даст один email в двух routing-правилах и уведёт
   vless-трафик устройства в hy2-exit.

Целевое состояние: **все леги балансировщика одного устройства выходят через
один egress**; hy2 в наборе остаётся (у каждого exit'а ≥2 ноды, где hysteria
смотрит в него); смерть exit'а переживается без ожидания эвакуации
(запасной лег вне селектора, §2.3) и лечится эвакуацией, а не скачками страны.

Сознательно НЕ делаем: `leastLoad` с tolerance (паллиатив — реже флапает, но
страна всё равно меняется); per-user маршрутизацию в hysteria (её нет в
продукте).

## 1. Фундамент (Ф)

### Ф1. Резолверы (services/relay.py, рядом с `primary_wg_interface`)

- `hy2_exit_id(db, node_id) -> int | None` — фактический exit hysteria ноды:
  `VPNNode.hy2_exit_id` (Ф2), если задан **и** на ноде есть линк на него;
  иначе exit линка с минимальным `wg_interface_name` **той же строковой
  сортировкой** и **теми же фильтрами**, что у роли: только линки с exit'ом и
  непустым `wg_public_key` (как `_build_relay_wg_links`,
  `provisioning.py:1001-1008`; сейчас `primary_wg_interface` :308-313 фильтра
  не имеет — при «битом» линке на wg0 роль привяжет wg1, а БД скажет wg0).
  Паритет `wg10 < wg2` с `tasks/main.yml:322` держим, не «исправляем» в одном
  месте.
- `egress_of(cred) -> tuple`: для relay-кредов `('exit', exit_id)`, для
  direct-нод (NL/DK, `exit_id IS NULL`) `('node', node_id)`. Сравнивать egress,
  а не `exit_id`: иначе direct-нода NL и exit NL считаются «разным» или
  primary на direct-ноде даёт `ref=None`.
- `effective_egress(cred, hy2_exit_by_node)` — для `hysteria2` → по
  `hy2_exit_id(node)`, для vless → `cred.exit_id`; без обращения к БД (карту
  `node_id → hy2_exit_id` считает вызывающий один раз).
- `device_egress(db, device)` — «egress устройства»: egress опубликованного лега
  с `leg_role='primary'` (`leg_published=True`); fallback — egress большинства
  активных vless-кредов; иначе `None`. **Не** `device.config.node`: `config_id`
  не переназначается после `swap_node_out` (`provisioning.py:4906-4931`) и
  роль primary при 4×1 уезжает раскладкой на другую ноду (`leg_scheme.py:117`,
  `rotation.py:111-115 forbid=before`). Одна формула на выдачу (А), подбор (Б′)
  и `DeviceOut.exit_name` (`api/users.py:143-148`, сейчас «первый попавшийся
  кред»).

### Ф2. Миграция 0069 + API-поверхность

- `vpn_nodes.hy2_exit_id` (nullable FK `wg_exit_nodes`, SET NULL).
- `wg_exit_nodes.fallback_exit_id`, `auto_evacuate_enabled` (default false),
  `evacuated_to_exit_id`, `evacuated_at`.
- `credentials.evacuated_from_exit_id` (nullable FK, SET NULL).
- Бэкфилл `hy2_exit_id` **вне** гарда `has_column` (иначе на create_all-БД
  0001 он не выполнится и тест ничего не проверит): отдельная идемпотентная
  функция `app/alembic/_backfills.py::backfill_hy2_exit_id(conn)` (`WHERE
  hy2_exit_id IS NULL`, по правилу Ф1), тест на неё напрямую + прогон миграции
  на симулированной инкрементальной БД (рецепт в памяти `reference_test_harness`).
- API: поля в `VPNNodeUpdate`/`VPNNodeOut` и `WGExitNodeUpdate`/`WGExitNodeOut`
  (`api/nodes.py` PATCH, `api/exits.py:505-577 update_exit`) с валидацией
  «на ноде есть линк на этот exit» (409). Без этого пилот Б2 = прямой UPDATE в
  проде, что вне правил. UI (поле в карточке ноды/exit'а) — отдельным аппрувом
  владельца.
- Плоскость конфигурации новых флагов (`SUB_XRAY_SAME_EXIT`, `EXIT_AUTO_EVACUATE`,
  `EXIT_EVAC_*`, `EXIT_EVACUATION_INTERVAL`, `EXIT_RETURN_HEALTHY_MIN`):
  `docker-compose.yml` для **backend и worker** (переменные дублируются по
  сервисам, у backend нет env_file — забытая строка = мёртвый флаг), `env.j2`,
  `group_vars/web/main.yml`, `docs/operations/env-reference.md` + grep-тест
  «каждый `os.getenv('EXIT_…'/'SUB_XRAY_…')` есть в compose».

### Ф3. Честный `Credential.exit_id` у hy2 — только вместе с proto-фильтром

- Вставка hy2-креда: `exit_id = hy2_exit_id(node)` вместо `bundle_exit_id`:
  `provisioning.py:3270`, `:4463`, `warm_pool.py:261`; в backfill-ветке
  `:3853-3865` — вместо `sibling_exit`.
- **Обязательно одновременно** фильтр `proto IN _VLESS_FAMILY_PROTOS`
  (`provisioning.py:1087-1091`) в: `build_xray_relay_outbounds`
  (`relay.py:262-280`); `choose_exit_for_relay` (`relay.py:150-158`, иначе
  hy2-креды перекашивают least-loaded); `_active_subs_by_exit`
  (`api/exits.py:71-102`); выборке `evacuate` (`:1749-1762`) и `switch_*_exit`
  (`provisioning.py:5674-5684`, `:5757-5767`); orphan-перепине `_detach_link_core`
  (`api/exits.py:1145-1202`).
- `_detach_link_core`: сирот перепинивать **группой по device_id**, а не по
  одному креду round-robin (сейчас устройство после detach снова
  мульти-exit); hy2-креды ставить в `hy2_exit_id(node)`; если
  `relay.hy2_exit_id == link.exit_id` — сбросить в NULL до `db.delete(link)`
  (:1218), записать в `migration_summary`.
- Разовый DB-бэкфилл существующих hy2-кредов (включая `pool_state=warm`) по
  резолверу — только БД, ноды не трогаем.
- Паритет-тест: после Ф3 каждый email встречается ровно в одном iface-бакете
  `build_xray_relay_outbounds`.

Тесты Ф: резолверы (паритет сортировки на `['wg0','wg1','wg10','wg2']` против
`sort | first`; линк без pubkey); `device_egress` для warm/cold, после
`swap_node_out` primary-ноды (ожидание — egress нового опубликованного лега) и
после `reshuffle_legs`; бэкфилл-функция; detach с двумя устройствами.

## 2. Часть А — балансировщик только из легов одного egress

### А1. Данные в сборщик

`SubLinkConfig` += `node_id`, `egress` (кортеж/строка), с `Field(exclude=True)`
— модель входит в `SubLinkResponse.configs` (`api_extensions.py:63`), новые
поля в ответ эндпоинта ехать не должны. `_decrypt_configs` (:1095-1148)
получает `hy2_exit_by_node: dict | None` и заполняет `egress` без БД (чистый
тест `test_leg_scheme.py:238-243` живёт). Карту считает `dynamic_sub_link` один
раз по нодам кредов.

### А2. Референс и фильтр

- `ref = device_egress(db, found.serve_device)` считается в `dynamic_sub_link`
  (`:1321-1331`, там есть db и device) и передаётся в `_sub_payload` kwarg'ом
  `ref_egress` (в `_sub_payload` :316-318 ни db, ни device нет; legacy-ветка
  :1466-1469 без device — ref = egress первого vless-лега по `_PROTO_ORDER`,
  hy2 в кандидаты на ref не брать). `configs[0]` — только диагностическое поле
  аудита, не референс.
- Сборщик: `build_profiles(configs, token, *, balancer_configs=None)` —
  балансировщик из подмножества, **одиночные профили из полного списка**
  (сейчас `:472-492` строит и то и другое из одних outbounds — передав только
  `same`, выкинули бы из клиента hy2/dup-леги чужих exit'ов целиком).
  `test_xray_client_config.py` расширяется.
- Гейт по **выразимым outbound'ам**, не по числу configs: `body =
  build_body(same)`; `None` → fallback-политика.
- Гейт `SUB_XRAY_SAME_EXIT = off | allowlist | on` (форма как `SUB_XRAY_JSON`,
  токены из `SUB_XRAY_JSON_TOKENS`), дефолт `off`. `SUB_XRAY_JSON=on` уже у
  всех Happ/v2rayTun — А затрагивает всех, не только владельца.
- Аудит `subscription_fetch.extra` (формируется в `dynamic_sub_link`
  :1401-1411): `legs_total`, `legs_same_egress`, `egress_ref`, `json_mode`
  (`same|mixed|flat`). Fail-open `_sub_payload` :333-342 глотает исключения —
  новый код логирует token/sub.

### А3. Запасной лег вне селектора (`spare`) — рекомендуемый дизайн

Смерть exit'а при «всё через один egress» = полный обрыв у его устройств до
эвакуации (15-20 мин confirm + тик + очередь apply). Сегодняшняя смесь даёт
уход на другую страну за ≤300 с. Компромисс без смены страны в норме: в JSON
оставить **один** лег другого egress с тегом `spare` (вне префикса `main`),
`balancers[].fallbackTag = "spare"` вместо `first` (`xray_client_config.py:447`).
`leastPing` выбирает только из `main*`; `fallbackTag` берётся, когда по
observatory живых `main` нет → переход на spare за ≤1 probeInterval, страна
меняется только при реальной смерти exit'а. Кандидат в spare — dup-лег или
любой лег чужого egress (тот же протокол, что и живой primary, предпочтительно
reality/xhttp). Требует проверки на живом Happ (что `fallbackTag` вне selector
работает у их ядра) — пилот на токене владельца.

Fallback при `< 2` выразимых легов одного egress — решение владельца (§7):
`mixed` (как сегодня) или `flat` (плоский список). Рекомендация: `mixed` до
завершения Б′, затем `flat`.

### А4. Измерение (read-only, ДО пилота)

Скрипт по БД: для каждого активного устройства — число легов и число легов с
`egress == device_egress`, плюс наличие кандидата в spare. Ожидание при нынешнем
per-node подборе: у большинства 1-2 → без Б′ включать А на всех бессмысленно.
Цифру и список нод/relay'ев для Б′3 зафиксировать в этом документе.

### А5. Тесты

Чистые (`_decrypt_configs` + `_sub_payload` с `ref_egress`, образец
`test_leg_scheme.py:219-249`; hy2-кред с «врущим» `exit_id`, но картой → другой
egress; spare/fallbackTag; fallback-ветки; direct-нода как egress) и E2E
(`test_sub_tunnel_blind_filter.py` `_mk_exit_link`/`_mk_device_with_creds` →
`GET /api/sub/{token}` UA Happ → `main*` одного egress, `spare` есть, одиночные
профили — все леги).

## 3. Часть Б — hy2 по exit'ам (раскладка bindDevice)

### Б1. Инфраструктура (предпосылка, иначе откатится)

1. `_collect_site_extra_vars` (`provisioning.py:836-851`, hy2-блок) и
   `_collect_relay_tunnel_extra_vars` (:1040-1075): `iface =
   resolve_exit_interface(db, node.id, node.hy2_exit_id)`; ключ
   `hysteria2_bind_device` передавать **только если `iface` не None** (stale
   колонка при отцепленном линке → default в шаблоне и в :322 + warning), иначе
   пустая строка = direct-ветка шаблона без tunnel/acl.
2. `roles/relay_jump_node/tasks/main.yml:322`: `HY2_BIND_DEV:
   {{ hysteria2_bind_device | default(relay_wg_links | map(attribute='interface') | sort | first) }}`.
3. `roles/relay_jump_node/files/reconcile_hy2_split.py:80-88`: проверка
   `/sys/class/net/<dev>` и что dev ∈ `relay_wg_links` перед записью (в роли
   install_hysteria2 проверка есть — `tasks/main.yml:222-226`; в скрипте нет →
   bindDevice на несуществующий wgN = Fatal + краш-луп hysteria).
4. Detach — см. Ф3 (`_detach_link_core` сбрасывает `hy2_exit_id`).
5. Паритет-тест: `test_split_routing_parity.py` уже покрывает hy2 (:306-420,
   :478-496 реконсайлер vs шаблон) — параметризовать `test_hy2_reconciler_matches_template`
   по `bind in ('wg0','wg3')` с `hysteria2_bind_device` в шаблон и `HY2_BIND_DEV`
   в реконсайлер; негативный кейс: bind не из `relay_wg_links` → отказ.

### Б2. Целевая раскладка (по 2 ноды на exit)

Источник таблицы — `wg show all endpoints` на нодах 26.08 (приложить к плану
выборку `GET /exits/{id}/links` перед раскаткой — это состояние БД, из репо не
проверяется).

| exit | ноды | что меняется |
|---|---|---|
| dc-nl-01 (NL) | tw-ru-01, dc-ru-01 | оба CZ → NL (у обоих wg3=NL) |
| ufo-tr-01 (TR) | ufo-ru-02, 4vds-ru-01 | без изменений |
| dc-cz-01 (CZ) | ufo-ru-01, aeza-ru-01 | aeza TR → CZ (wg1=CZ) |
| ufo-fr-01 (FR) | ufo-ru-03, vsin-ru-01 | ufo-ru-03 TR → FR (wg1=FR); vsin уже FR |

4 ноды, у каждой один рестарт hysteria (обрыв hy2-сессий ноды). Раскатка —
`PATCH /nodes/{id}` (`hy2_exit_id`) → `POST /exits/{exit}/links/{relay}/reconnect`
(relay_tunnel apply: WG-конфиги не меняются, xray reconcile байт-в-байт → rc 78
без рестарта, hy2 reconcile → changed → условный `restart hysteria2`). **Не**
`site.yml -l <нода>`: статик-инвентарь без backend extra_vars останавливает
hysteria (`install_hysteria2/tasks/main.yml:10-19`, `hysteria2_port` не задан) и
снимает sockopt у xray. Полный API-bootstrap даёт лишний безусловный рестарт из
`resync_node_hy2.yml:80-86`.

Пилот: **tw-ru-01 → NL** первой (совпадает с egress устройства владельца;
hysteria на не-primary wgN в проде ещё не работала — проверяем именно это).
Чеклист на ноде: `grep bindDevice /etc/hysteria/config.yaml`; tunnel первый,
`tunnel(all)` последний; `curl --interface wg3 https://ifconfig.me` = IP NL;
`systemctl is-active hysteria-server`, journal без `Fatal`;
`curl 127.0.0.1:10088/traffic`; `journalctl -u xray --since` — xray не
рестартовал; с телефона одиночный профиль «🚀 Быстрый» → 2ip = NL. Затем
остальные три.

### Б3. Побочные эффекты и откат

- `Credential.exit_id` hy2-кредов затронутых нод пересчитать по резолверу сразу
  после раскатки (иначе БД снова врёт).
- Откат = `hy2_exit_id = NULL` + reconnect (вернётся min(wgN)).
- DNAT hy2 port-hopping, traffic collector (10088), systemd ordering
  `After=wg-quick@*` (`relay_jump_node:121-140`, все линки) — от смены
  интерфейса не зависят.

## 4. Часть Б′ — exit-aware подбор/раскладка и ремедиэйшн

Без этого А почти всегда в fallback.

### Б′1. Прогрев и выбор exit'а

`warm_one_bundle` (`warm_pool.py:248`): на multi-link relay раскладывать бандлы
по exit'ам round-robin с учётом warm-кредов (сейчас `choose_exit_for_relay`
считает только `is_active=True` → весь пул ноды на одном exit'е).
`choose_exit_for_relay`: proto-фильтр (Ф3) + пропускать exit'ы с
`last_probe_status != 'ok'`, `evacuated_to_exit_id IS NOT NULL`, не `is_active`
(новых юзеров не сажать на мёртвый/эвакуированный exit — нужно и для В).

### Б′2. Диверс-подбор и раскладка с предпочтением egress

- `_maybe_attach_diverse` (`provisioning.py:3384-3532`): `preferred =
  device_egress(device)` (cold-путь: креды ещё `is_active=False` — брать без
  фильтра); в `_try_one` после `choose_node` требовать `RelayExitLink(node,
  preferred)` и `warm_pool.try_assign_bundle(db, node.id, sub.id,
  exit_id=preferred)` — фильтр через EXISTS по **vless-кредам** бандла, не по
  анкору `min(id)` (`warm_pool.py:446-451`; после Ф3 анкором может быть
  hy2-кред с другим exit_id). Третий пасс без фильтра, как сейчас два
  (:3506/:3509) — сухой пул по exit-срезу не должен оставлять устройство без
  диверс-легов.
- Роль hy2 назначается **не** в `_try_one`, а в `plan_legs` (паросочетание,
  `leg_scheme.py:137-200`) и переприменяется на каждом reprovision/failover/
  reshuffle/`_handle_task_outcome` (`provisioning.py:3169, 3319, 4355, 4513,
  4711, 4931, 5094`). Поэтому exit-aware делать саму раскладку: `plan_legs`/
  `apply_leg_scheme` получают `device_egress`, в `candidates('fast')` ноды с
  `hy2_exit_id == device_egress` первыми (или `forbid` пар `(fast, node)` с
  чужим exit'ом при наличии альтернативы, `forbid` уже есть :201-206);
  фолбэк без forbid, если роль иначе не закрывается. Тест в
  `test_leg_scheme.py`.
- `grant_duplicate_leg` (`rotation.py:203-227`): предпочитать кандидатов с
  `egress == device_egress`; чужой egress брать только как spare (А3) с
  пометкой в аудите.

### Б′3. Ремедиэйшн уже выданных устройств

`align_device_legs_egress(db, device) -> tasks` (существующий
`switch_device_exit` трогает только креды `device.config.node`, :5757-5767):
per-(device, relay) UPDATE vless-кредов на `device_egress` (если у relay есть
линк) + один `relay_tunnel apply` per relay батчем как в `evacuate` (один apply
перерендеривает всех). hy2-леги не трогаем (после Б exit фиксирован нодой; при
несовпадении лег просто не в балансировщике). Масштаб: ~8 relay'ев с линками,
~42 устройства (39×4 + 3 по памяти); apply = `systemctl restart` xray×3 на relay
только там, где хоть один email сменил wgN (иначе rc 78); hot-reload у xray нет;
nginx перед xhttp/ws не трогается (клиенты ловят краткий обрыв и
переподключаются); **hysteria не рестартует** (её секции не меняются). Ночью, по
1-2 relay'я; метрика — доля apply с rc 78 (идемпотентность).

Метрика «до/после»: доля устройств с `legs_same_egress >= 2` (А4) и число смен
egress у устройства в день по access-логам нод (метод разбора 26.08).

## 5. Часть В — детект смерти exit'а и авто-эвакуация

### В1. Сигналы и предикат

Что есть:

- `run_node_reachability_tick` (`worker.py:3051`): ping → tcp:ssh → ssh-pong с
  контроллера (NL); для exit'ов `vpn_ports=[]` (:3229) → DOWN означает ровно
  «ssh не ответил» (фильтр ssh, fail2ban, скруббинг SSH-баннеров у VDSina из
  памяти, повисший sshd) — WG при этом может быть жив. Поля `WGExitNode`:
  `last_probe_status`, `unreachable_since`, `diagnose_incident_open_at`,
  `alerts_muted_until`, `diagnostics_disabled_at` (`models.py:1050-1061`);
  confirm через `unreachable_since` ≥ `NODE_ALERT_CONFIRM_MIN=20`
  (:3098, :3296-3318). Mass-down suppression в проде **выключен**
  (`NODE_MASS_DOWN_FRACTION=0`, в env.j2 нет) и в любом случае пишет
  `unreachable_since` до подавления.
- `run_relay_link_health_tick` (:2858) → `RelayExitLink.last_handshake_at /
  last_observed_at` (`models.py:1129-1132`); `PersistentKeepalive=25`
  (`wg-client.conf.j2:17`, все линки этим шаблоном) → handshake каждые ~2 мин
  и без юзеров. Это единственный сигнал «мёртв со стороны РФ» (скруббинг по
  src-IP, блок РКН).

Предикат «эвакуировать exit X» — сигнал (2) **обязателен**, (1) только
усилитель:

- (2) на **всех** линках X с `last_observed_at` свежим (< 8 мин) на ≥2 relay'ях
  `last_handshake_at` старше `EXIT_EVAC_STALE_MIN=15` **и** был свежим до окна
  (переход fresh→stale, не «никогда не было»; no-peer-match оставляет старый
  handshake при свежем observed — тоже не считать);
- гварды: `X.is_active`, `status=active`; `_controller_has_network()`;
  relay_link_health успешно обошёл ≥2 relay'я за 10 мин; **нет** pending/running
  `relay_tunnel`-тасок по линкам X и нет аудита `wg_exit_keygen` /
  `wg_exit_updated` / `relay_exit_reconnect` за 60 мин (keygen/PATCH exit дают
  stale на живом exit'е, пока N тасок по семафору переприменяют линки);
  собственный mass-down: если `≥2` exit'ов или ≥50% нод одновременно не `ok` —
  только пуш; `auto_evacuate_enabled` на X и `EXIT_AUTO_EVACUATE=1` (дефолт 0);
  не чаще 1 эвакуации X за `EXIT_EVAC_COOLDOWN_H=6`, дедуп по AuditLog.
- (1) reachability confirmed DOWN сокращает окно (2) до `EXIT_EVAC_STALE_FAST_MIN=8`.
  Операторские `alerts_muted/diagnostics_disabled` гейтят пуши, не эвакуацию.

### В2. Действие: `services/exit_evacuation.py::evacuate_exit(...)`

Тело `evacuate_exit_to` (`api/exits.py:1699-1890`) выносится в сервис, эндпоинт
становится обёрткой (прецедент `_detach_link_core`). Зависимости:
`_run_task_best_effort` (`exits.py:263-290`) переносится в services/метод
оркестратора; `_audit` — ленивый импорт как в `self_repair.py:97`;
actor/actor_type параметрами (тик — `auto-evacuate`/system). Семантика
**меняется** (унаследованный баг: сегодня группировка по `Subscription.node_id`
:1770-1776, а UPDATE по `subscription_id + exit_id` без `node_id` :1808-1816 →
диверс-креды чужих relay'ев переписываются без линка и без apply → email
выпадает из fan-out на default `direct` = чужой exit, молча):

- выбирать **креды**, не сабы: `exit_id == from AND is_active AND proto IN
  vless-family`; группировать по `Credential.node_id`;
- цель **per relay**: `from.fallback_exit_id`, если у relay есть линк и exit
  здоров (`last_probe_status=='ok'`, свежий handshake на этом relay); иначе
  least-loaded здоровый из линков relay; relay без здорового линка →
  `failed_relays` + пуш;
- UPDATE по id кредов группы, `evacuated_from_exit_id = from`; один apply на
  relay; `exit_id` целевого exit'а в payload **только первой** таске
  (`_run_relay_tunnel_apply` :2480-2508 гонит `bootstrap_exit.yml` до 600 с при
  любом `exit_id` — иначе 8 relay'ев = 8 последовательных бутстрапов одного
  exit'а под семафором, последний relay переедет через десятки минут);
- `WGExitNode.evacuated_to_exit_id/evacuated_at`, `AuditLog exit_bulk_evacuated`
  с `reason='auto'`, отчёт по кредам/relay'ям + поле «hy2-легов на мёртвом
  exit'е: N (не переезжают)»;
- пуш `admin_alert_exit_evacuated` (добавить в `ADMIN_NOTIFICATION_ACTIONS`,
  `api_extensions.py:2314`) с inline-кнопками «вернуть»/«оставить»: новый
  префикс callback + `POST /api/exits/{id}/evacuation/{action}` по образцу
  `diag:*` (`bot/handlers.py:2298`, `api/diagnostics.py`).

hy2 **не эвакуируется** (node-level bind). Леги hy2 нод с `hy2_exit_id == X`
выпадают из балансировщика (А2 + здоровье exit'а в резолвере) — клиент уходит на
vless-леги/spare. Перебинд hysteria (`hy2_exit_id` → другой + reconnect) —
только по кнопке: рестарт демона, минуты на ноду.

Что видит Xray-JSON-клиент: UUID/ноды/sub_token не меняются, меняется серверный
outbound (тот же механизм, что у switch-exit, `provisioning.py:5635-5637`);
профиль пересобирать не нужно. До эвакуации связь держит spare (А3).

### В3. Тик

`run_exit_evacuation_tick` (отдельный тик, не ветка reachability):
self-reschedule в начале тела, `EXIT_EVACUATION_INTERVAL=300`, регистрация в
`TICK_IDS`/`TICK_TIMEOUTS` (`queue.py:399/429`), bootstrap в `main()`, строка в
`docs/components/worker.md`; максимум 1 exit за тик; env в compose/env.j2 (Ф2).

### В4. Возврат

По умолчанию **ручной** (кнопка): `evacuate_exit(to→from)` только для кредов с
`evacuated_from_exit_id == from`, затем сброс маркеров. Авто-возврат —
отдельным флагом: X `ok` ≥ `EXIT_RETURN_HEALTHY_MIN=60` мин подряд и свежие
handshake на его линках, не чаще 1 раза в сутки. Каждый переезд = рестарт xray
на relay'ях; флап без гистерезиса недопустим.

### В5. Тесты

- Сервис (сегодня `evacuate` не покрыт вовсе): per-relay цель; саб с кредами
  на двух relay'ях, у одного нет линка на fallback — второй получает свою цель;
  hy2-креды не тронуты; `exit_id` только в первой таске; маркеры; возврат
  только помеченных.
- Тик (образец `test_reachability_confirm.py`): stale без fresh-истории → нет;
  stale при pending relay_tunnel/keygen-аудите → нет; ≥2 exit'ов down → только
  пуш; полный предикат → одна эвакуация; повтор в cooldown → нет;
  `auto_evacuate_enabled=false` → только пуш; reachability DOWN без stale → нет.
- Структурный тест регистрации тика (`test_audit_tails_2026_07.py:124-131`);
  grep-тест env↔compose.

## 6. Порядок, риски, доки

| шаг | что | риск | откат | docs |
|---|---|---|---|---|
| 1 | Ф1-Ф3 + фикс группировки evacuate/detach + API колонок (без UI) | низкий: БД/код, поведение нод не меняется | миграция вниз; бэкфилл обратим | data-model, nodes.md:227-231, RELAY_ROADMAP:305-307, backend-api |
| 2 | А4-измерение (read-only) | — | — | этот план |
| 3 | Б1 + паритет-тест | средний: правка relay_jump_node | git revert роли + reconnect | provisioning.md:108, nodes.md, runbook |
| 4 | А allowlist на токен владельца: `balancer_configs`, spare/fallbackTag, fallback=mixed | низкий | `SUB_XRAY_SAME_EXIT=off` | env-reference:220-238, backend-api:166 |
| 5 | Б2 пилот tw-ru-01 → NL, затем 3 ноды | средний: рестарт hysteria ×4 | `hy2_exit_id=NULL` + reconnect | nodes.md |
| 6 | Б′1-Б′2 (прогрев, подбор, exit-aware `plan_legs`, dup) | средний: SAVEPOINT в `_maybe_attach_diverse`, сухой пул по exit-срезу | третий пасс без фильтра = старое поведение | warm-pool.md, diverse_subscription_epic |
| 7 | Б′3 ремедиэйшн ночами по 1-2 relay'я | высокий: рестарты xray по флоту | батчи, не всем сразу | relay-migration.md:84 |
| 8 | А → `on`, fallback → flat (по метрике А4) | средний | флаг off | env-reference |
| 9 | В (сервис, тик, бот-callback, эндпоинт), `EXIT_AUTO_EVACUATE=0` → per-exit | средний: ложные срабатывания = рестарты xray | флаг/колонка off | worker.md, diagnostics.md:183/267, env-reference:84, backend-api, bot.md |

Помнить:

- Любая раскатка на ноды — только через API (bootstrap/reconnect), не `site.yml -l`.
- Сортировка `wg_interface_name` строковая в трёх местах (`relay.py:251,311`,
  `tasks/main.yml:322`); при ≥10 линках `wg10 < wg2` — чинить синхронно или не
  чинить вообще.
- `PATCH /nodes/{id}/configs/{cfg}` инвалидирует warm-пул ноды (`nodes.py:1435`)
  — потому `hy2_exit_id` в колонке VPNNode, не в `VPNConfig.settings`.
- `_incident_auto_close_blocked`/`reconcile_healthy_incident` живут своей
  жизнью — состояние эвакуации только в своих колонках, не в `diagnose_*`.

## 7. Решения владельца

1. Режим отказа: принять spare/fallbackTag (А3) как штатный ответ на смерть
   exit'а (страна меняется только при реальной смерти) — или без spare и ждать
   эвакуацию (15-20+ мин полного обрыва у устройств exit'а).
2. Fallback А2 при `< 2` легов одного egress: `mixed` (как сейчас) или `flat`.
3. Direct-нода той же страны (dc-nl-01 vs NL-exit): один egress «по стране» или
   разный «по IP» (для googlevideo ip-lock — разный). Рекомендация: разный.
4. Окно (2) для эвакуации: 15 мин, с reachability-усилителем до 8.
5. Авто-возврат: только кнопка или авто через 60 мин.
6. Целевая раскладка hy2 (Б2) — подтвердить пары нод/exit'ов.
7. UI для `hy2_exit_id`/`fallback_exit_id`/`auto_evacuate_enabled` — нужен ли
   сейчас (иначе только API/curl).
8. Оценка (с учётом ревью): Ф ≈ 2-3 дня (включая фикс evacuate/detach и API),
   А ≈ 2 дня (spare + сборщик + тесты), Б ≈ 1-2 дня + пилот, Б′ ≈ 3-4 дня,
   В ≈ 4-5 дней. Итого **12-15 рабочих дней**; без Б′ включать А на всех нельзя.
