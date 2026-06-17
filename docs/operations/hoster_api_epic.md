# Эпик: автоматизация нод через API хостера (4vps.su)

**Цель.** Админ из панели «дозаказывает» VPN-ноду в пару кликов: выбрал
провайдера/локацию/тариф/ОС → бэк заказывает VPS через API хостера → ждёт IP →
создаёт `VPNNode` → катит `site.yml` → нода сама становится `active`. Плюс
переустановка ОС и удаление ноды «одной кнопкой». В пределе — два независимых
агента (см. Фаза 4): саппорт-агент отвечает юзерам, ops-агент управляет флотом
через этот же API.

Первый провайдер — **4vps.su** (он же 4vds; RU-хостер, где уже живут ноды).
Доступы и список методов: <https://4vps.su/page/api>.

---

## Архитектура (вписываемся в существующую cloud-абстракцию)

Абстракция уже есть и расширяется без переделок:

| Слой | Файл | Что |
|------|------|-----|
| Driver-контракт | `backend/app/services/cloud/base.py` | `CloudDriver` Protocol + `CloudServer` dataclass + `get_driver()` factory |
| Провайдеры | `backend/app/services/cloud/{hetzner,vultr,digitalocean,aeza,manual}.py` | каждый реализует Protocol (duck-typed) |
| **Новый** | `backend/app/services/cloud/fourvps.py` | `FourVpsDriver` |
| Enum | `backend/app/models.py` `CloudProviderKind` | + `fourvps = "4vps"` |
| Спавн end-to-end (autoscale-тик) | `backend/app/services/node_spawner.py` `spawn_node()` | блокирующий: order → wait IP → VPNNode(registering) → ensure_reality_config → bootstrap site.yml (RQ-воркер, без HTTP-таймаута) |
| Спавн из админ-панели | `backend/app/services/node_spawner.py` `spawn_node_async()` | неблокирующий: `order_server` (sync, быстро) → VPNNode(registering, **is_active=False**, host=`0.0.0.0`, external_id) → ensure_reality_config → **фон** `_finalize_spawn`: `wait_for_ipv4` → host + is_active=True → bootstrap |
| Кредсы провайдера | `backend/app/models.py` `CloudProvider` | `api_token_enc` (Fernet) |
| Admin API | `backend/app/api/cloud.py`, `backend/app/api/nodes.py` | CRUD провайдеров, `/nodes/spawn`, `/nodes/{id}/destroy` |

### Расширения контракта (этот эпик)
- `CloudDriver.reinstall_server(external_id, image, *, password=None)` — переустановка ОС (опционально; не все провайдеры умеют).
- `CloudDriver.list_plans()` / `list_images()` — для наполнения дропдаунов в админке (рядом с уже существующим `list_regions()`).
- `GET /api/cloud/providers/{id}/offerings` — отдаёт `{datacenters, tariffs, images}` для UI-формы заказа.
- `POST /api/nodes/{id}/reinstall` — переустановка ОС на ноде + повторный bootstrap.

### Учётка 4vps: два значения в одном токене
4vps требует `apikey` **и** `panel_id` в каждом запросе. Чтобы не менять схему
`cloud_providers`, храним их в `api_token_enc` как **`panel_id:apikey`** (Fernet).
Драйвер сплитит по первому `:`. Если двоеточия нет — значение целиком трактуется
как `apikey` (panel_id пуст). См. `fourvps.py:_split_token`.

### Второй провайдер — VDSina ✅ (driver, 2026-06-12)
`services/cloud/vdsina.py` (`CloudProviderKind.vdsina`, миграция `0048`, ветка в
`get_driver`, kind в `CloudProviders.tsx`). **ДВЕ разновидности (один драйвер,
param `base`):** `kind=vdsina` → `userapi.vdsina.com/v1` (дефолт `_BASE`, override
env `VDSINA_API_BASE`), `kind=vdsina_ru` → `userapi.vdsina.ru/v1` (миграция 0051).
.ru и .com — разные аккаунты/токены/балансы; токен доменно-специфичен. Заводи
отдельный `CloudProvider` на каждый домен. **auth — ГОЛЫЙ `Authorization: <token>` (без Bearer!)**, конверт
`{status,status_msg,data}` (пустой список = `status:error`+`"No X information"` →
трактуем как `[]`). Async order→poll как 4vps: `order_server` (POST /server,
дефисные `server-plan`/`ssh-key`) → `wait_for_ipv4` (poll GET /server/{id} до
`active`, `data.ip` — **МАССИВ** `[{ip,type}]`). Offerings: `/server-group`+
`/server-plan/{g}`, `/datacenter`, `/template`. Баланс: `/account.balance.real`.
**Ключевое отличие от 4vps: VDSina ИНЖЕКТИТ ssh-ключ при заказе** (`ssh-key` id) →
бокс поднимается с нашим ключом, парольный bootstrap НЕ нужен (как hetzner). Ключ
берётся так: явный `provider.ssh_key_ids` → иначе **АВТО-регистрация** нашего
provisioning-pubkey на VDSina (`_ensure_key_id`: ищет ssh-key по имени
`vpn-provisioning`, иначе `POST /ssh-key`). Путь «без ключа» ВЫПИЛЕН (пароль VDSina
генерит сам, эндпоинт пароля бывает не сразу готов → ломало bootstrap на оплаченном
боксе; ревью поймало). `reinstall` тоже переинжектит ключ (его сигнатура ключи не
получает → `_ensure_key_id`). id (datacenter/server-plan/template) ЧИСЛОВЫЕ —
валидируем ДО оплаты (fast-fail на строковом `ubuntu-22.04`-дефолте). Имя==value==
"vdsina" (рассинхрона 0046 нет). Спека сверена по двум community-клиентам + офиц.
PDF; драйвер прошёл 3-линзовый адверсариал-ревью (no-key майоры устранены авто-ключом).
**Токен (2026-06-15):** НЕ через `POST /v1/auth` (email+пароль) — он **deprecated**
(`Method deprecated`, data null). Берётся **постоянный токен из панели**
(`cp.vdsina.com` → Пользователь → «Токен для доступа через публичный API», все права).
**ДОМЕН КРИТИЧЕН:** токен из `.com`-аккаунта на `.ru`-эндпоинте даёт `401 "Incorrect
token"` — это и был баг (драйвер хардкодил `.ru`). Дефолт теперь `.com`, аккаунт
adept38 там; баланс через `.com` подтверждён (`real: 9.79`).

**Боевой smoke (нужен токен+баланс):** завести `CloudProvider(kind=vdsina,
api_token=<постоянный токен из cp.vdsina.com>)` (ssh-ключ авто-зарегается при первом
заказе, если воркеру примонтирован `ANSIBLE_PRIVATE_KEY_FILE`) → offerings → заказать
тест-ноду (числовые id ДЦ/тариф/ОС из offerings) → `active` → снести. NB: VDSina
исторически штормит (видели полный 504) — на автоскейл одних не сажать, пара к 4vps.

### Третий провайдер — generic BILLmanager ✅ (driver, 2026-06-12)
`services/cloud/billmgr.py` (`CloudProviderKind.billmgr`, миграция `0049`, ветка в
`get_driver`, kind+hint в `CloudProviders.tsx`). **ОДИН драйвер на пачку RU-хостеров
на ISPsystem BILLmanager** (DataCheap/UFO/AdminVPS): хост+креды в `api_token_enc` как
JSON `{"base_url","username","password"}` (целиком Fernet). Контракт сверен по офиц.
ISPsystem b6sa/v6-докам + рабочим примерам PQ.Hosting/the.hosting.
- Auth: `authinfo=user:password` параметром каждого запроса (stateless). `out=json` →
  `{doc}`, ошибка в `doc.error`.
- Заказ — **одним выстрелом**: `func=vds.order.param …&skipbasket=on&sok=ok` →
  **СПИСЫВАЕТ С БАЛАНСА** сразу. id услуги в ответе нет → находим по `domain` (=имя
  ноды) в `func=vds`. Статус+IP: `func=vds` → `elem[].{ip,item_status}` (поллим до
  `item_status==2`+ip). Delete `vds.delete`, reinstall `vds.edit ostempl` (нет
  `vds.reinstall`).
- **Нет инъекции SSH-ключа → root-пароль** (как 4vps): VMmanager генерит свой, мы
  ставим СВОЙ известный через `service.changepassword` после active → возвращаем как
  `root_password`. Форма — **блокирующий `create_server` БЕЗ order_server** (заказ+
  поллинг в фоне `_finalize_spawn` — демон-тред, НЕ в HTTP-запросе → 502 не грозит;
  он же сохранит пароль; как hetzner по форме). Заказ шлёт `autoprolong=1` (иначе
  нода удалится в конце периода).
- **Orphan-guard (из ревью):** заказ СПИСЫВАЕТ баланс до того, как известен id
  услуги (его находим по `domain` в `func=vds`). Если услуга не поднялась за таймаут
  / не встал пароль — `create_server` СНОСИТ оплаченную залипшую услугу (`vds.delete`)
  + ERROR-лог с id, чтобы ретраи (особенно autoscale) не плодили оплаченных сирот.
  `_wait_active`/`_extract_ip` читают $-обёрнутый billmgr-JSON и список IP корректно.
- **✅ UFO captcha-блок СНЯТ (2026-06-17).** Раньше (тест 2026-06-12) UFO
  (`bill.ufo.hosting`) с IP воркера `185.242.87.250` отдавал `captcha_verification_failed`
  (Яндекс-капча/DDoS-Guard флагует DC-IP). На 2026-06-17 IP расфлагнут: dummy-авторизация
  с nl-web вернула обычный billmgr-JSON `badpassword` (`remote_addr [185.242.87.250]`, БЕЗ
  капчи). Снятие капчи вскрыло следующий баг: `base_url` в форме вводят С `/billmgr`, а
  `_call` дописывает `/billmgr` ещё раз → `/billmgr/billmgr` → HTML 404 (DDoS-Guard раньше
  маскировал это капчей). Пофикшено в `_parse_token` (снимаем хвостовой `/billmgr` —
  работает при любом вводе). UFO auto-order теперь ждёт живого offerings/order-smoke.
- **⚠️ DataCheap + AdminVPS** по-прежнему режут TLS с DC-IP — нужен чистый egress
  (резидентный прокси) или whitelist; либо держать флот на REST-хостерах (4vps; VDSina —
  свой блокер по scrubbing'у). offerings при блоке дегрейдят в пустой каталог + WARNING-лог.
- **UNCONFIRMED (проверить боевым smoke на UFO до прода):** точные имена slist-полей
  offerings; поведение `skipbasket` при нехватке баланса (спишет vs создаст unpaid);
  доступен ли `vds.edit ostempl` под клиентским токеном; не IP-whitelist'нут ли
  `authinfo`; не мешает ли login-captcha. Драйвер прошёл 3-линзовый ревью.

### Node-management расширения (2026-06-17)
- **Читаемый регион.** `node_spawner._display_region` резолвит id ДЦ → country/name
  через `list_datacenters` (фолбэк на id), чтобы в `VPNNode.region` была «Russia», а
  не сырая «3»; сырой id остаётся в `provider_region`.
- **Авто-имя ноды.** `resolve_spawn_name` → `<хостер>-<cc>-<NN>` при пустом поле имени
  (префикс из kind; для billmgr — из хоста `base_url`; cc из региона; NN — следующий
  по relay+exit). `name` в `NodeSpawnRequest`/`ExitSpawnRequest` опционален.
- **Reboot без панели хостера.** `reboot_server` в драйверах (4vps/VDSina `PUT
  /server.reboot/{id}`/billmgr `vds.reboot`) + `ssh_bootstrap.reboot_via_ssh` (graceful
  по ключу). `node_spawner._reboot_target`: API hard-reboot → SSH-фолбэк. Эндпоинты
  `POST /nodes/{id}/reboot` и `/exits/{id}/reboot` + кнопки «↻ reboot» в админке.
  ⚠️ VDSina-путь `server.reboot` — по аналогии с reinstall, не сверен боевым; SSH-фолбэк
  страхует достижимые ноды.

---

## Фазы

### Фаза 1 — API-клиент + заказ/переустановка из админ-API ✅ (spec подтверждён по доке)
- [x] `CloudProviderKind.fourvps`, миграция `0044` (`ALTER TYPE … ADD VALUE '4vps'`).
- [x] `FourVpsDriver` финализирован по официальной доке (PDF): Bearer-хедер + `panel_id`, конверт `{error,data,errorMessage}`, реальные пути (`/getDcList`, `/getTarifList`, `/getImages/{tarif}/{dc}`, `/action/buyServer`, `/myservers`, `/action/deleteServer`, `/action/reinstall`, `/action/reboot`, `/action/continueServer`).
- [x] `create_server`: `buyServer` отдаёт только `{serverid, password}` (БЕЗ IP) → поллим `/myservers` до `status=active`+`ipv4`. Пароль (4vps не инжектит SSH-ключ) → `CloudServer.root_password` → `VPNNode.provider_root_password_enc` (Fernet, миграция `0045`).
- [x] **Async-spawn (фикс 502 + сирот):** `create_server` блокирует до 600s (поллинг IP). Прямой вызов в HTTP-роуте `POST /nodes/spawn` убивал uvicorn-воркер по nginx `proxy_read_timeout` 60s → CF 502; хуже — `buyServer` оплачивал VPS ДО создания `VPNNode`, и убитый запрос оставлял осиротевший сервер. Разбили `create_server` на `order_server` (быстрый `buyServer`) + `wait_for_ipv4` (поллинг). Роут идёт через `spawn_node_async`: sync-заказ + фиксация `VPNNode(is_active=False, host=0.0.0.0, external_id)` сразу (сервер привязан → сирот нет), поллинг IP + bootstrap — в фоновом daemon-потоке. `is_active=False` ⇒ `choose_node` не отдаёт placeholder-ноду юзерам. Autoscale-тик (RQ, без HTTP-таймаута) остался на блокирующем `spawn_node`.
- [x] `reinstall_server` (`POST /api/action/reinstall {serverid, ostempl, password}`).
- [x] `get_driver()` → `FourVpsDriver`; `reinstall_node()` + `POST /nodes/{id}/reinstall`.
- [x] `list_datacenters/list_plans/list_images` + `GET /cloud/providers/{id}/offerings`.
- [x] **Парсинг offerings исправлен (заказ не проходил, 4vps резал buyServer).** Реальная структура `getTarifList`: `tarifList[<locId>] = {clusterInfo:{id,dc_name,flag,presets:[13..25],need_verification}, presets:{13:{id,name:"cx01",cpu_number,ram_mib,rom,commentParsed.price}}}`. Ключи `tarifList` — это **локации** (= id из `getDcList` = `datacenter` для buyServer), а тарифы — это **пресеты** (cx01/cx11/…, id 13–25) внутри. Образы: `getImages/{PRESET}/{LOCATION}` → `{imgId:name}` (напр. `/getImages/13/10` для Финляндии → `{14:"Ubuntu 22.04",1098:"Ubuntu 24.04",…}`). Итого валидный заказ = `tarif=<пресет id>`, `datacenter=<локация id>`, `ostempl=<img id>`. Раньше `list_plans` отдавал id локаций как тарифы, а `osNames`/`list_images()` без аргументов были пустыми → форма слала `tarif=<локация>` + `ostempl="ubuntu-22.04"` (строка) → buyServer падал. Теперь `list_plans` = каталог пресетов, `list_images()` без аргументов дёргает репрезентативный `getImages` (каталог ОС общий) → заполняет `offerings.images`, форма авто-выбирает Ubuntu 22.04.
- [x] **Боевой smoke (2026-06-15): пройден.** Реальный заказ 4vps-ноды через админ-форму «☁ Заказать в облаке» (`POST /nodes/spawn`): VPS заказался, поднялся до `active`, забутстрапился — всё корректно (подтверждено оператором). Авто-заказ 4vps боеспособен в проде.

### Фаза 1.5 — SSH-bootstrap ✅ РЕШЕНО password-инъекцией ключа (код)
`buyServer` не принимает SSH-ключ, аккаунтный ключ 4vps **на практике не
инжектится** (проверено боем — нода поднялась без ключа → ansible `Permission
denied (publickey,password)`). Поэтому используем то, что API ОТДАЁТ —
**root-пароль** (`provider_root_password_enc`):

- `services/ssh_bootstrap.py::ensure_provisioning_key(host, password)` — заходит
  по root-паролю (paramiko) и дописывает наш **provisioning-pubkey** (деривится
  из `ANSIBLE_PRIVATE_KEY_FILE` или `<key>.pub`) в `~/.ssh/authorized_keys`.
  Идемпотентно, ретраит подключение (свежая нода/после reinstall поднимает SSH
  не сразу), терпит провал (если password-auth уже отключён `bootstrap_node` —
  ключ и так стоит).
- Вызывается в worker'е из `provisioning._execute_task` **перед `site.yml`** для
  любой ноды с `provider_root_password_enc` (worker'у примонтирован приватный
  ключ; backend — нет, поэтому инъекция именно тут, не в `_finalize_spawn`).
- `reinstall_node` теперь **генерит и СОХРАНЯЕТ** новый root-пароль (reinstall
  его сбрасывает) — иначе после переустановки зайти было бы нечем.
- Host-key: `ANSIBLE_SSH_ARGS` += `UserKnownHostsFile=/dev/null
  StrictHostKeyChecking=accept-new` — переустановка/реюз IP больше не вешает
  bootstrap на «REMOTE HOST IDENTIFICATION HAS CHANGED».
- **Ждём SSH перед bootstrap'ом** (`node_spawner._wait_for_ssh`, TCP-поллинг
  порта, окно `NODE_SSH_WAIT_TIMEOUT`, дефолт 480с). Свежий VPS / нода после
  reinstall грузятся минутами — ранний `site.yml` падал на `No route to host`.
  Ждём в фоновом daemon-потоке backend'а (нет RQ-таймаута; RQ job_timeout=900с
  уже равен timeout site.yml, так что внутри bootstrap-джобы ждать нельзя):
  `_finalize_spawn` (заказ) и `_reinstall_finalize` (reinstall) дожидаются SSH
  и только потом ставят bootstrap-таску. Уже-живые ноды проходят проверку
  мгновенно. reinstall теперь возвращает ноду сразу, bootstrap уходит в фон.

После первого `bootstrap_node` нода отключает password-auth и хардится; ключ
уже стоит, дальше ansible ходит по ключу. `provider_root_password_enc` остаётся
аварийным доступом.

### Фаза 2 — Admin UI «Заказать ноду» ✅
- `OrderCloudNodeForm` (`admin/src/pages/Nodes.tsx`, тоггл «☁ Заказать в облаке»): провайдер → live offerings (`GET /cloud/providers/{id}/offerings`) → ДЦ (локация) / тариф (пресет) / ОС (из `offerings.images`, авто-выбор Ubuntu 22.04) → pool → `POST /nodes/spawn`. Дегрейд в текст-инпуты, если offerings пусты (не-4vps/без токена). Нода появляется как `registering` → `active` (существующий tasks-поллинг).
- Кнопка «reinstall OS» в строке cloud-ноды (`prompt` ostempl → `POST /nodes/{id}/reinstall`).
- «Уничтожить» — уже был (`deleteNode` → `/destroy` для provider-нод).
- `api.ts`: `listCloudProviders / getProviderOfferings / spawnNode / reinstallNode` + типы.
- **Заказ EXIT-ноды в облаке** ✅ (`OrderCloudExitForm`, `admin/src/pages/Exits.tsx`, тоггл «☁ Заказать в облаке»): то же offerings-меню → `POST /exits/spawn` → `node_spawner.spawn_exit_async` (заводит `WGExitNode` registering + WG-keypair, фоновая достройка `_finalize_exit_spawn`: poll IP → SSH → `bootstrap_exit`). Требует `wg_exit_nodes.provider_root_password_enc` (миграция `0047`) — worker кладёт ключ по паролю перед `bootstrap_exit` (как у нод). Зарубежные серверы заводят ИМЕННО так — exit за РУ-relay, не прямой нодой (прямой зарубежный endpoint душит DPI; см. диагностику).
- **Дефолтный провайдер в формах заказа:** ноды → провайдер с «ru» в имени (`4vps-ru`, РУ-расходники); exit'ы → провайдер БЕЗ «ru» (`4vps`, зарубежный). Фолбэк — первый активный.

### Фаза 3 — Жизненный цикл, продление и биллинг ✅
- **Автоскейл на 4vps — без кода (конфигурация пула).** Autoscale-тик уже ходит по
  `[autoscale_provider_id] + autoscale_fallback_provider_ids` → `spawn_node`, а 4vps
  теперь валидный провайдер. Оператор задаёт у пула `autoscale_provider_id`=4vps,
  `autoscale_region`=DC-id, `autoscale_plan`=tarif-id, `autoscale_image`=ostempl-id
  (через существующий `PoolAutoscaleConfig`). Спавн пойдёт на 4vps автоматически.
- **Авто-продление.** `spawn_node` включает 4vps `autoprolong` при заказе (best-effort)
  — хостер сам продлевает VPS с баланса, флот не умирает в конце периода. Ручной путь:
  `renew_node()` + `POST /nodes/{id}/renew` + кнопка «renew» в админке (4vps `continueServer`).
- **Страж баланса + стоимость флота.** Тик `run_cloud_billing_tick` (`CLOUD_BILLING_INTERVAL`,
  default 1ч): по каждому активному cloud-провайдеру тянет `get_balance` → gauge
  `vpn_cloud_provider_balance{provider}` + admin-алерт при балансе ниже
  `CLOUD_BALANCE_ALERT_THRESHOLD` (0=выкл); суммирует `monthly_cost` активных нод →
  gauge `vpn_fleet_monthly_cost`. Так баланс не иссякнет молча.
- Driver += `get_balance`, `set_autoprolong` (тоггл), `renew_server`.

### Фаза 4 — Два агента (см. docs/AI_AGENT_ROADMAP.md)
- **Support-agent**: отвечает юзерам в поддержке (read-only к биллингу/подпискам).
- **Ops-agent**: управляет флотом через этот API + диагностику (добавить/снести/переустановить/мигрировать), за гейтом подтверждений. Независимые сущности, общая БД-правда.
- **Сделано (P1 роадмапа агента):** read-only диагностический триаж ноды —
  `POST /api/agent/triage/{node_id}`, `services/agent/*`, за флагом `AGENT_ENABLED`,
  Claude `claude-sonnet-4-6`. Дальше — support-draft + ops-reversible по тирам.

---

## Спек 4vps (подтверждён по официальной доке, PDF)

База: `https://4vps.su/api`. Auth: `Authorization: Bearer <apikey>` (хедер) +
`panel_id` параметром (где взаимодействие с панелью). Конверт ответа:
`{"error": bool, "data": ..., "errorMessage": str|dict}`.

| Метод | HTTP | URL | Параметры | Ответ (data) |
|-------|------|-----|-----------|--------------|
| Баланс | GET | `/userBalance` | — | `{userBalance}` |
| Дата-центры | GET | `/getDcList` | — | `{dcList:{id:{dc_name,cpu_name,flag,…,id}}}` |
| Тарифы | GET | `/getTarifList` | — | `{tarifList:{<locId>:{clusterInfo:{id,dc_name,flag,presets:[…],need_verification}, presets:{<presetId>:{id,name,cpu_number,ram_mib,rom,commentParsed:{price}}}}}}` — ключи = ЛОКАЦИИ, тарифы = пресеты внутри |
| Образы | GET | `/getImages/{PRESET}/{LOCATION}` | path | `{images:{imgId:name}}` (PRESET=tarif id, LOCATION=datacenter id; пара, не tarif+dc-в-нашем-старом-смысле) |
| **Заказ** | POST | `/action/buyServer` | `tarif,datacenter,ostempl,name,domain?,period?` | `{serverid,password}` (БЕЗ IP) |
| Мои серверы | GET | `/myservers` | — | `{serverlist:[{id,name,ipv4,status,tid,dc,price,…}]}` |
| Инфо о сервере | GET | `/getServerInfo/{ID}` | path | `{serverInfo,dcInfo,ipPrice,ipList}` |
| Переустановка | POST | `/action/reinstall` | `serverid,ostempl,password` | `"ok"` |
| Удаление | POST | `/action/deleteServer` | `serverid` | `"ok"` |
| Перезагрузка | POST | `/action/reboot` | `serverid` | `"ok"` |
| Продление | POST | `/action/continueServer` | `serverid` | `"ok"` |
| Смена тарифа | POST | `/action/changeTarif` | `serverid,preset` | `"ok"` |
| Панели (публ.) | GET | `/public/getPanelIds` / `/public/getPanels` | — | список ID/панелей |

Заказ может вернуть ошибку-объект: верификация профиля (`/Api/verif`) или
нехватка средств (`/Api/deposit/...`) — драйвер разворачивает `errorMessage.message`.

Реализовано в `fourvps.py` (без `# VERIFY` — спек закрыт). Период аренды:
`[720,2160,4320,8640]` = 1/3/6/12 мес (дефолт 720). Образы зависят от пары
(пресет, локация) — `getImages/{PRESET}/{LOCATION}`; для UI общий каталог
отдаётся в `offerings.images` (`list_images()` без аргументов).

**Остаток:** SSH-bootstrap (Фаза 1.5) — `buyServer` не инжектит ключ.
