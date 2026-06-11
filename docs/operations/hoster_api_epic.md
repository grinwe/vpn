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
| Спавн end-to-end | `backend/app/services/node_spawner.py` `spawn_node()` | order → wait IP → VPNNode(registering) → ensure_reality_config → bootstrap site.yml |
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

---

## Фазы

### Фаза 1 — API-клиент + заказ/переустановка из админ-API ✅ (spec подтверждён по доке)
- [x] `CloudProviderKind.fourvps`, миграция `0044` (`ALTER TYPE … ADD VALUE '4vps'`).
- [x] `FourVpsDriver` финализирован по официальной доке (PDF): Bearer-хедер + `panel_id`, конверт `{error,data,errorMessage}`, реальные пути (`/getDcList`, `/getTarifList`, `/getImages/{tarif}/{dc}`, `/action/buyServer`, `/myservers`, `/action/deleteServer`, `/action/reinstall`, `/action/reboot`, `/action/continueServer`).
- [x] `create_server`: `buyServer` отдаёт только `{serverid, password}` (БЕЗ IP) → поллим `/myservers` до `status=active`+`ipv4`. Пароль (4vps не инжектит SSH-ключ) → `CloudServer.root_password` → `VPNNode.provider_root_password_enc` (Fernet, миграция `0045`).
- [x] `reinstall_server` (`POST /api/action/reinstall {serverid, ostempl, password}`).
- [x] `get_driver()` → `FourVpsDriver`; `reinstall_node()` + `POST /nodes/{id}/reinstall`.
- [x] `list_datacenters/list_plans/list_images` + `GET /cloud/providers/{id}/offerings`. NB: у 4vps образы зависят от тарифа+ДЦ → отдаются внутри `list_plans()[].images` (из `osNames`); `list_images()` без аргументов = `[]`.
- [ ] Боевой smoke: завести `CloudProvider(kind=4vps, api_token="panel_id:apikey")`, дёрнуть offerings, заказать тест-ноду, дождаться `active`, снести. (Нужен реальный ключ + баланс; заказ списывает деньги.)

### Фаза 1.5 — SSH-bootstrap ✅ РЕШЕНО account-level ключом (не требует кода)
`buyServer` не принимает SSH-ключ в параметрах, НО у 4vps есть **аккаунтный
SSH-ключ** (кладётся в биллинге/панели) — он авто-инжектится на ВСЕ новые
серверы. Подтверждено вручную: ключ положен, на свежих нодах работает.
Значит API-заказанная нода получит тот же ключ → ansible цепляется по ключу,
как у key-based провайдеров; существующий `spawn_node → bootstrap site.yml`
работает без изменений.

⚠️ **Условие:** ключ в панели 4vps должен быть **публичной половиной нашего
`provisioning_key`** (им ходит worker/ansible), не личным ключом оператора.
Проверить при первом боевом заказе (см. smoke в Фазе 1). `provider_root_password_enc`
остаётся как аварийный доступ.

### Фаза 2 — Admin UI «Заказать ноду» ✅
- `OrderCloudNodeForm` (`admin/src/pages/Nodes.tsx`, тоггл «☁ Заказать в облаке»): провайдер → live offerings (`GET /cloud/providers/{id}/offerings`) → ДЦ/тариф/ОС (образы из `plan.images`/osNames) → pool → `POST /nodes/spawn`. Дегрейд в текст-инпуты, если offerings пусты (не-4vps/без токена). Нода появляется как `registering` → `active` (существующий tasks-поллинг).
- Кнопка «reinstall OS» в строке cloud-ноды (`prompt` ostempl → `POST /nodes/{id}/reinstall`).
- «Уничтожить» — уже был (`deleteNode` → `/destroy` для provider-нод).
- `api.ts`: `listCloudProviders / getProviderOfferings / spawnNode / reinstallNode` + типы.

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

### Фаза 4 — Два агента (видение, отдельный эпик)
- **Support-agent**: отвечает юзерам в поддержке (read-only к биллингу/подпискам).
- **Ops-agent**: управляет флотом через этот API + диагностику (добавить/снести/переустановить/мигрировать), за гейтом подтверждений. Независимые сущности, общая БД-правда.

---

## Спек 4vps (подтверждён по официальной доке, PDF)

База: `https://4vps.su/api`. Auth: `Authorization: Bearer <apikey>` (хедер) +
`panel_id` параметром (где взаимодействие с панелью). Конверт ответа:
`{"error": bool, "data": ..., "errorMessage": str|dict}`.

| Метод | HTTP | URL | Параметры | Ответ (data) |
|-------|------|-----|-----------|--------------|
| Баланс | GET | `/userBalance` | — | `{userBalance}` |
| Дата-центры | GET | `/getDcList` | — | `{dcList:{id:{dc_name,cpu_name,flag,…,id}}}` |
| Тарифы | GET | `/getTarifList` | — | `{tarifList:{id:{…,osList,osNames}}}` |
| Образы | GET | `/getImages/{TARIF}/{DC}` | path | `{images:{id:name}}` |
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
`[720,2160,4320,8640]` = 1/3/6/12 мес (дефолт 720). Образы зависят от тарифа+ДЦ
(`getImages/{tarif}/{dc}`), для UI отдаются внутри `list_plans()[].images`.

**Остаток:** SSH-bootstrap (Фаза 1.5) — `buyServer` не инжектит ключ.
