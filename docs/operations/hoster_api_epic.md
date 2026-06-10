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

### Фаза 1 — API-клиент + заказ/переустановка из админ-API ⏳ (в работе)
- [x] `CloudProviderKind.fourvps`, миграция `0044` (`ALTER TYPE … ADD VALUE '4vps'`).
- [x] `FourVpsDriver` — каркас: session, `_request` (4vps-конверт `{error,data,errorMessage}`), poll-IP, маппинг в `CloudServer`. Точные пути/имена параметров вынесены в **один блок `_API`** (см. «Чек-лист спека» — нужно подтвердить по доке).
- [x] `reinstall_server` (известен точно: `POST /api/action/reinstall` `{serverid, ostempl, password}`).
- [x] `get_driver()` → `FourVpsDriver`.
- [x] `reinstall_node()` в `node_spawner` + `POST /nodes/{id}/reinstall`.
- [x] `list_plans/list_images` + `GET /cloud/providers/{id}/offerings`.
- [ ] **Подтвердить wire-спек по доке** (см. чек-лист) и довести `order`/`getTariffs`/`getDatacenters`/`getImages`/`delete`.
- [ ] Боевой smoke: завести `CloudProvider(kind=4vps)`, дёрнуть offerings, заказать тест-ноду, дождаться `active`, снести.

### Фаза 2 — Admin UI «Заказать ноду» (UI-часть, явно одобрена)
- Форма в `admin/src/pages/Nodes.tsx`: провайдер → (offerings) локация/тариф/ОС → pool/SNI → «Заказать». Прогресс через существующий tasks-поллинг; нода появляется в списке как `registering` → `active`.
- Кнопки в строке ноды: «Переустановить ОС» (выбор образа), «Уничтожить» (destroy).

### Фаза 3 — Жизненный цикл и автоскейл
- Подключить 4vps как `autoscale_provider_id`/fallback в пулах (autoscale уже умеет провайдер-цепочку).
- Биллинг: `monthly_cost` с заказа в `VPNNode.monthly_cost` (уже пишется), агрегаты по флоту.
- Продление (4vps `renewal`) — тик, который продлевает VPS до истечения, если нода живая.

### Фаза 4 — Два агента (видение, отдельный эпик)
- **Support-agent**: отвечает юзерам в поддержке (read-only к биллингу/подпискам).
- **Ops-agent**: управляет флотом через этот API + диагностику (добавить/снести/переустановить/мигрировать), за гейтом подтверждений. Независимые сущности, общая БД-правда.

---

## Чек-лист спека (нужно подтвердить по доке — фетчер 4vps.su заблокирован)

Достоверно известно из публичного поиска:
- Переустановка: `POST https://4vps.su/api/action/reinstall` `{serverid:int, ostempl:int, password:str(≥6)}` → `{"error":false,"data":"ok"}`; ошибка → `{"error":true,"errorMessage":"…","data":false}`.
- Заказ: параметр `period ∈ [720,2160,4320,8640]` (1/3/6/12 мес), опц. домен. Ответ содержит `serverInfo`, `dcInfo`, `ipPrice`, `ipList`.
- Auth: `apikey` + `panel_id` в каждом запросе (GET/POST-параметр или JSON-тело).
- Методы: `getBalance`, `getDatacenters`, `getTariffs`, `getImages`, `order`, заказ IP, `enable/disable/reboot`, `renewal`, `delete`, `reinstall`, смена тарифа.

**Нужно подтвердить (иначе `order`/list-методы — на допущениях):**
1. Точное имя auth-параметра ключа (`apikey`? `api_key`?) и нужен ли `panel_id` всегда.
2. Базовый префикс info-методов: `/api/info/getTariffs` vs `/api/getTariffs` vs `/api/action/getTariffs`.
3. `order` — полный список параметров (имена `tariff`/`datacenter`/`ostempl`/`password`/`period`/SSH-ключ/домен?) и **где в ответе `serverid` и IP** (в `serverInfo`? `ipList`?), приходит ли IP сразу или нужен поллинг `getServers`.
4. `getTariffs`/`getDatacenters`/`getImages` — форма ответа (имена полей id/name/price/cpu/ram).
5. `delete` — путь и параметры (`POST /api/action/delete {serverid}`?).
6. `getServers`/детали сервера — как получить IP/статус по `serverid` после заказа.

Всё перечисленное локализовано в `fourvps.py` в блоке `_API` и помечено `# VERIFY`.
