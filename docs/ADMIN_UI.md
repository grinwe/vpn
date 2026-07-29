# Admin UI reference

Справочник по тому, что реально умеет `/admin/` — какие страницы, какие действия, какие API-эндпоинты они дёргают. Если ты ищешь «как сделать X через UI» и не находишь здесь — значит кнопки ещё нет; не изобретай curl-workaround, заведи тикет (см. [feedback policy](../CLAUDE.md)).

Стек: Vite + React 18 + TS + TanStack Query + Tailwind. Роутинг — `react-router-dom`. Все запросы уходят с заголовком `X-Admin-Token`, который сетится при логине и хранится в `localStorage`. Прод деплой — отдельный alpine-nginx контейнер на `127.0.0.1:8080`, публичный nginx проксирует `/admin/*` → туда (см. [admin/README.md](../admin/README.md)).

## Pages

### `/admin/login` — [Login.tsx](../admin/src/pages/Login.tsx)

Единственный вход. Принимает токен — либо общий `ADMIN_API_TOKEN` из `.env`, либо scoped token, созданный на странице API tokens. Валидация: `GET /api/users?limit=1` — если 200, токен запомнен. Если 401 — форма остаётся открытой. На каждый админ-экшен в `audit_log` пишется `actor`, и если токен scoped — это развязывает действия человека от серверных.

### `/admin/` — Dashboard ([Dashboard.tsx](../admin/src/pages/Dashboard.tsx))

Сводные счётчики. Поллится раз в 15 секунд (`refetchInterval: 15_000`). Данные из `GET /api/stats`:

- **Пользователи**: `users_total`, `subscriptions_active`/`subscriptions_total`, `devices_active`
- **Активность за 24ч** (по РЕАЛЬНОМУ трафику): `users_active_24h` / `devices_active_24h` / `orphans_active_24h`. Считается из `NodeTrafficSample.details[*]["users"]` за 24ч — distinct `access_username` резолвится в `Credential→Device→User` (xray видит имя креда, не Device — поэтому считаем и вторичные устройства, не только праймари). Сироты (recovery-плейсхолдер `user_id=999999`, инцидент 2026-05) исключены из `users_active_24h` и показаны отдельной (жёлтой при >0) карточкой. Дороже остальных COUNT(*) — скан окна сэмплов + IN-join по индексированному `access_username`; ок для малого флота на 15с-поллинге.
- **Ноды**: `nodes_total`, `nodes_active`, `nodes_down = total − active` (красная карточка, если > 0)
- **Очередь / платежи**: `invoices_pending` (жёлтая, если > 0), `provisioning_tasks_pending`, `provisioning_tasks_failed` (красная, если > 0)

Графиков нет — для time-series смотреть Grafana (`docs/dashboards/business-metrics.json`).

### `/admin/users` — Users ([Users.tsx](../admin/src/pages/Users.tsx))

Таблица юзеров + боковая панель с деталями и подписками выбранного.

- Поиск по `telegram_id` / `email` с debounce 300ms (`GET /api/users?limit=50&offset=…&search=…`).
- **Пагинация через `useInfiniteQuery`** с кнопкой «Загрузить ещё» в подвале таблицы. Страница — 50 записей (backend cap — 200). Счётчик «Загружено: N» всегда на виду. Ордер с бэка — `id DESC`, новые регистрации всегда сверху, пагинация стабильна.
- Колонка «Создан» показывает **дату И время** (`toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })`) — раньше было только число, и на DDoS-инцидентах невозможно было отличить «эти 250 регистраций сыпанулись за минуту» от «за сутки».
- **Мультивыбор через чекбоксы** (`selectedIds: Set<number>`, независим от single-row `selected` для сайдбара — можно чекнуть строку для bulk-операции, не открывая детали). В заголовке — checkbox «select-all на текущей загруженной странице».
- **Bulk `ban all` / `unban all`** появляется в хедере при непустом выборе. Шлёт `POST /api/users/batch_ban` с `{user_ids, action: 'ban'|'unban'}`. Ответ — `{done, skipped, not_found}` (skipped = уже в нужном состоянии, ранним return'ом в backend). Алерт-саммари после операции, кэш юзеров инвалидируется. Аудит — отдельная строка `user_banned`/`user_unbanned` на каждого юзера (с `extra.batch=true`), чтобы per-user атрибуция сохранялась.
- **Bulk `🔁 регенерация sub-link` / `🚚 переезд на сервер`** — две массовые операции по выделению (рядом с `ban all`). **Разные вещи, не путать:**
  - `🔁 регенерация sub-link` → `POST /api/subscriptions/bulk-regenerate-sublink` с `{user_ids}`. Каждому **живому устройству** активных подписок выбранных юзеров выдаётся НОВАЯ ссылка (`reprovision_subscription` на той же ноде: fresh `sub_token`+UUID+creds). Старое устройство уходит в `disabled` **без ansible-revoke** (`_disable_device_keep_on_node`) — его UUID остаётся на ноде, юзер продолжает подключаться по старой ссылке до перехода, старый токен алиасится на новый в `/api/sub/{token}`. Юзеру уходит Telegram `sublink_rotated` («возьми новую ссылку в ЛК»). `sub_token` **меняется**, но **стоимость прежняя**: `extra_device_slots` не трогаем, live-счётчик устройств сохраняется 1:1 (`balance.py` считает месячную цену от слотов, а не от числа Device-строк). Для хвостов аварии 2026-05 (часть sub-link поломалась при восстановлении).
  - `🚚 переезд на сервер` → `POST /api/subscriptions/bulk-migrate-auto` с `{user_ids}` — bulk-версия карточной «обновить подписку»: авто-выбор свободной healthy-ноды (`migrate_subscription_to_free_node`) + бан старой ноды для юзера. `sub_token` **сохраняется**, уведомления нет (профиль обновляется через sibling-alias).
  - Обе **тяжёлые по ansible** (regenerate: 1 apply/устройство; migrate: revoke+apply/устройство) и идут через один серийный RQ-воркер — поэтому backend капит на **25 юзеров/запрос** (а не 500 как `batch_ban`: тот pure-DB), UI чанкует выделение по 25. Перед большими пачками — `scripts/workers.sh 5`. Ответ — `{done, skipped, not_found, failed, …}`, алерт-саммари; прогресс провижининга — на странице Tasks.
- **Banned-бэдж** — красная плашка «banned» рядом с `telegram_id` + vermilion-оттенок всей строки (`text-red-300`). Источник — `UserOut.banned_at`, проставляется бэкендом в `list_users`.
- Клик на строку — загружает `GET /api/users/{id}` и показывает список подписок с девайсами.
- **Пополнить баланс** — форма в детали-панели (только если у юзера есть `telegram_id`). `POST /api/users/by_telegram/{tg_id}/topup` пишет `kind=adjust` с note `admin_topup`. В ledger'е появляется отдельная строка, user balance обновляется оптимистично в UI.
- **revoke now** на подписке (если `status != blocked|expired`) — `POST /api/subscriptions/{id}/disable`. Юзер отключается от ноды через Ansible за 1-2 минуты, sub переходит в `blocked`.
- **enable** на подписке (если `status != active`) — `POST /api/subscriptions/{id}/enable`. Если sub был `frozen` — прозрачно зовёт `balance_svc.unfreeze_subscription`. Если `blocked`/`expired` — флипит статус в `active`, ресет `notes`, `next_charge_at = now`, перепровижнивает один девайс через orchestrator. Если репровижининг упал — подписка всё равно остаётся `active`, но в ответе выставляется `reprovision_failed: true` (конфига нет, sub-link отдаст 503); UI должен показать оператору предупреждение.
- **+ add device** на подписке (только когда `status=active`) — `POST /api/subscriptions/{id}/devices`, админский обход prepaid-гейта. Считает текущие live-девайсы, создаёт новый с именем `device-{N+1}`, запускает ansible. Используется, когда юзер нагрешил руками или нужен тест-девайс на его аккаунте.
- **unbind** на конкретном девайсе (если `status != revoked|disabled`) — `POST /api/devices/{id}/revoke`. Отвязывает от ноды через ansible, sub остаётся живой — удобно когда один юзер просит освободить слот на ноде.
- **переселить** per-sub (только когда `status=active`) — dropdown со всеми active-нодами + кнопка. `POST /api/subscriptions/{id}/migrate` с `{target_node_id}`. Это **ручной admin-override**: пул/health/cooldown-фильтры обходятся, проверяется только `is_active=True` на целевой ноде. Старые девайсы revoke'аются в фоне, новый проводится через `reprovision_subscription`. `sub_token` сохраняется, поэтому sub-link у клиента продолжает работать. Отличается от node-wide `POST /nodes/{id}/migrate` тем, что переселяет ровно одну подписку на заданную ноду — альтернатива ушедшей в 2026-04 webapp-кнопке «поменять ноду» (она осознанно не возвращается).

Чего нет: adjust кредита с произвольным знаком, прямого просмотра ledger транзакций из UI. Пока только через psql.

### `/admin/invoices` — Invoices ([Invoices.tsx](../admin/src/pages/Invoices.tsx))

Список инвойсов с фильтром по статусу (default: `pending`) и лимиту (20/50/100/200). Поллится раз в 10 секунд. `GET /api/invoices?status=…&limit=…`.

Действия:
- **mark paid** на `pending` → `POST /api/invoices/{id}/mark_paid`. Триггерит тот же `_mark_invoice_paid_core` что и вебхуки: для `kind=topup` начисляет баланс (и при первом топапе — реферер payout, см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md)); для `kind=subscription` создаёт/продлевает подписку и enqueue'ит провижининг.
- **mark unpaid** на `paid` → `POST /api/invoices/{id}/mark_unpaid`. **Только bookkeeping** — подписка/девайсы НЕ отзываются. Нужен, если руками помеченный инвойс оказался ошибкой; снятие денег/ревок делать отдельно. **`kind=topup` откатить нельзя** (400): баланс уже зачислен, для коррекции — balance adjustment. Batch-вариант такие инвойсы пропускает (`skipped`).

### `/admin/plans` — Plans ([Plans.tsx](../admin/src/pages/Plans.tsx))

CRUD по тарифам. Поля: `name`, `duration_days`, `max_devices`, `price` (рубли), `traffic_limit_mb` (nullable), `is_visible`.

- Создание: `POST /api/plans`
- Редактирование: `PUT /api/plans/{id}`
- Удаление: `DELETE /api/plans/{id}` — 409, если есть живые подписки (алерт всплывает над формой)
- Toggle visibility — отдельный shortcut, тоже `PUT /api/plans/{id}` с `{is_visible}`

**Критично:** trial-система читает цену самого дешёвого `is_visible` 30-day плана как сумму trial-бонуса (см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md#activation-flow)). Если у тебя нет ни одного 30-day visible плана — trial-баннер в WebApp не рендерится, активация отдаёт 503.

### `/admin/nodes` — Nodes ([Nodes.tsx](../admin/src/pages/Nodes.tsx))

Полный lifecycle VPN-нод. Поллится раз в 5 секунд (чтобы ловить переход `registering → active` во время bootstrap), отключается при ошибке запроса (`retry: false`).

- **+ Добавить ноду** — форма (имя, регион, host, ssh_port, pool_id). На submit — `POST /api/nodes`, бэкенд enqueue'ит provisioning-таску в worker.
- **Клик на строку** — expand с панелью «Конфиги протоколов». Показывает все `VPNConfig` ноды.
  - **+ Добавить конфиг** — `POST /api/nodes/{id}/configs`. Префиллы (порт/SNI) по выбранному протоколу из константы `PROTOCOL_DEFAULTS` в Nodes.tsx — синхронизируй с ansible-ролями если меняешь дефолты.
  - **Удалить конфиг** — `DELETE /api/nodes/{id}/configs/{config_id}`. 409, если на конфиге живые девайсы (статус ≠ `revoked|disabled`). Терминальные девайсы бэкенд удаляет каскадом вместе с их креденшелами — это фикс от 2026-04, до этого FK-constraint блокировал удаление даже после того, как юзер поотвязывал всё.
- **исключить** / **вернуть в пул** — `POST /api/nodes/{id}/active` с `{is_active: bool}`. Исключённая нода не попадает в `_pick_node` в [services/provisioning.py:49](../backend/app/services/provisioning.py#L49), то есть новые активации её не увидят. Существующие подписки продолжают работать. Используется когда хочешь обкатать свежую ноду до боевого трафика или готовишь её к миграции. См. [NODES.md § admin actions](NODES.md#admin-actions).
- **переселить** — `POST /api/nodes/{id}/migrate` → `services.health.migrate_subscriptions_off`. Все активные подписки с этой ноды получают новую через `_pick_node`, девайсы перепровижиниваются ansible'ом в фоне. Рекомендуемый flow при бане: сначала `исключить`, потом `переселить`. Результат показывается алертом со списком `subscription_id`s; прогресс — на странице Tasks с фильтром `target=subscription`.
- **✎ правка** — `PATCH /api/nodes/{id}` (модалка `EditNodeModal`): имя / регион / `pool_id` / notes. Имя валидируется как inventory-хост (`[a-z0-9][a-z0-9-]{0,62}` + уникальность). Переименование **без re-bootstrap**: `build_inventory_for_node` рендерит `name` как alias, ansible коннектится по `ansible_host=host` — IP не меняется, развёрнутые клиенты не рвутся. `region`/`pool_id` влияют только на будущий `choose_node`; `notes` — текст. `host`/`ssh_port` тут НЕ меняются (это identity у провайдера → reinstall/renew). Пулы для дропдауна — `GET /api/pools` (id+name; может быть пусто, если пулы не заведены).
- **удалить из панели** / **уничтожить у хостера** — две раздельные кнопки. `удалить из панели` = `DELETE /api/nodes/{id}` (убирает запись, хостер не трогает; 409→migrate→retry для активных подписок, `?force=true` для живого VPS); `уничтожить у хостера` (только cloud-ноды, `provider_id`) = `POST /api/nodes/{id}/destroy` (сносит VPS у провайдера, запись остаётся).
- **🛠 диагностика off/on** + **🔕 alerts mute/unmute** (diagnostics overhaul) — ДВА независимых тумблера на строке ноды, не путать: `🛠` (`POST /api/diagnostics/node/{id}/disable|enable`, поле `diagnostics_disabled_at`) **жёстко гасит ВСЕ диаг-таски** ноды (авто + ручные); `🔕` (`POST /api/diagnostics/node/{id}/mute {hours}`, поле `alerts_muted_until`) только **глушит Telegram-алёрты** на TTL (по умолчанию 24ч / unmute). Старый объединённый `NodeMuteToggle` (`auto_diagnose_disabled_at`) оставлен для совместимости. Бейджи: `🔴 инцидент` (открытый `diagnose_incident_open_at`) и `probe: {status}` (`last_probe_status`). Подробнее — [diagnostics.md § Overhaul](operations/diagnostics.md).
- **Error state** — если `/api/nodes` даёт 500, показывается красная панель с reason и кнопкой «Повторить». Poll останавливается, чтобы не сыпать логами. Стандартный подсказка: `docker compose logs backend --tail 100`.

Чего **нет**:
- Enable/disable отдельного config (только удаление)
- Заведение/редактирование пулов из UI (pool_id у ноды править можно через ✎ правку, но сами пулы создаются отдельно)

Для диагностики застрявшего bootstrap'а — см. страницу **Tasks** ниже.

### `/admin/tasks` — Provisioning tasks ([Tasks.tsx](../admin/src/pages/Tasks.tsx))

Список последних `ProvisioningTask` записей. Поллится раз в 5 секунд (останавливается на ошибке). Фильтры: status (`pending`/`running`/`success`/`failed`), target_type (`node`/`device`/`subscription`), limit (20/50/100/200), `telegram_id` (серверный, ходит по цепочке device→sub→user → возвращает только таски владельца), и client-side fuzzy поиск по `action`/`target`/`id`/`telegram_id`.

- `GET /api/provisioning/tasks?status=…&target_type=…&limit=…&telegram_id=…`
- Колонка **Telegram** показывает `telegram_id` владельца таски (бэкенд дорезолвит его через [`_enrich_task_telegram`](../backend/app/api.py) батчевыми 2 запросами — device→sub→user и sub→user). Для node-тасков и осиротевших строк — `—`.
- Клик по строке — expand с `error_message`, `payload`, `result` (JSONB с stdout/stderr ansible-runner'а).
- **rerun** на `failed`/`pending` — `POST /api/provisioning/tasks/{id}/rerun`. Триггерит тот же executor, не создаёт новую запись.
- **del** на любой не-`running` таске — `DELETE /api/provisioning/tasks/{id}` → 204. Нужен чтобы вычистить шум из failed-тасков после дебага. Running-таски удалять нельзя — 409.

Это та самая страница, куда ссылается footer под таблицей Nodes, когда юзер хочет узнать «почему нода застряла в `registering`». Раньше там был curl-хинт — теперь ссылка на Tasks + фильтр failed.

### `/admin/tokens` — API tokens ([ApiTokens.tsx](../admin/src/pages/ApiTokens.tsx))

Управление scoped токенами (модель — `api_tokens`, префикс `X-Api-Token` в запросах). Хранится только SHA-256 хеш, plaintext показывается один раз при создании.

- `GET /api/api-tokens` — список (id, name, scopes, is_active, last_used_at)
- `POST /api/api-tokens` — создаёт новый; body `{name, scopes}`. Scopes выбираются чекбоксами из `AVAILABLE_SCOPES = ["probe:read", "probe:write"]` — держи в sync с `ALL_SCOPES` в [auth.py](../backend/app/auth.py). `traffic:write` удалён 2026-07-29 вместе с блокирующим ингестом.
- `DELETE /api/api-tokens/{id}` — revoke (ставит `is_active=False`, запись не удаляется, `last_used_at` остаётся для аудита).

**Кому это нужно:** probe-rigs (сканеры здоровья нод, `probe:read` + `probe:write`) и отдельные токены для людей-админов чтобы в `audit_log` их действия были отличимы от generic `"admin"` actor'а.

## Что чего тригерит — cheat sheet

| Что нажал | Что уходит | Side effects |
|-----------|-----------|--------------|
| `mark paid` на subscription-инвойсе | `POST /api/invoices/{id}/mark_paid` | провижининг в worker → ansible на ноде, юзер получает config в боте |
| `mark paid` на topup-инвойсе | то же | `balance_kopecks += amount`; если это первый топап и есть referrer — payout рефереру ([TRIAL_SYSTEM.md](TRIAL_SYSTEM.md#stage-3)) |
| `revoke now` на подписке | `POST /api/subscriptions/{id}/disable` | subscription → `blocked`, ansible-таска снимает девайсы с ноды |
| `enable` на подписке | `POST /api/subscriptions/{id}/enable` | `frozen` → `unfreeze_subscription`; `blocked/expired` → `active` + reprovision одного девайса |
| `+ add device` на подписке | `POST /api/subscriptions/{id}/devices` | Создаёт новый `Device`, ставит ansible-таску. Обходит prepaid-гейт (админский override). |
| `unbind` на девайсе | `POST /api/devices/{id}/revoke` | Статус девайса → `revoked`, ansible снимает с ноды. Sub остаётся живой. |
| `переселить` на подписке | `POST /api/subscriptions/{id}/migrate` с `{target_node_id}` | Одна подписка переезжает на выбранную ноду; пул/health/cooldown НЕ проверяются (только `is_active=True`). Старые девайсы revoke'аются в фоне, новый поднимается через `reprovision_subscription`. `sub_token` сохраняется. |
| `пополнить баланс` | `POST /api/users/by_telegram/{tg}/topup` | `kind=adjust, note=admin_topup`, в ledger'е новая строка |
| `+ Добавить ноду` | `POST /api/nodes` | enqueue `site.yml` в worker; статус `registering` → `active`/`error` |
| `+ Добавить конфиг` на ноде | `POST /api/nodes/{id}/configs` | warm-pool ноды инвалидируется, warmer пересобирает bundles за несколько тиков |
| `Удалить конфиг` | `DELETE /api/nodes/{id}/configs/{config_id}` | Каскадно удаляет терминальные девайсы + их креденшелы, обнуляет `Credential.config_id` у осиротевших. 409, если есть живые. |
| `исключить` / `вернуть в пул` на ноде | `POST /api/nodes/{id}/active` | Флипит `is_active`. Планировщик `_pick_node` сразу перестаёт видеть исключённую. |
| `переселить` на ноде | `POST /api/nodes/{id}/migrate` | Все активные подписки получают новую ноду через `_pick_node`, ansible-таски в фоне. Миграция in-place: `sub_token` и строка сохраняются, дубликатов в webapp нет. |
| `resync` на ноде | `POST /api/nodes/{id}/resync` | Force-retry всех активных VLESS UUID'ов на ноде через `playbooks/resync_node.yml` (цикл `manage_vless_user.sh add`). Идемпотентно. Safety-net при drift'е клиентов vs БД. См. [NODES.md § VLESS client resync](NODES.md#vless-client-resync). |
| `backfill креды` на ноде | `POST /api/nodes/{id}/backfill-missing-creds` | Создаёт `Credential` для уже провижённых девайсов под enabled-протоколы ноды. Фиксит случай, когда добавили протокол на ноду, но в `/sub/{token}` у старых юзеров он не появился. Идемпотентно. |
| `удалить` на ноде | `DELETE /api/nodes/{id}` (manual) или `POST /api/nodes/{id}/destroy` (cloud) | При 409 `{active_subs: N}` — UI оффер: «N живых подписок, мигрировать и удалить?» → `POST /migrate` → повтор DELETE. Cloud-нода убивается через provider API (`destroy_node`), manual — строка в БД. Терминальная история (expired/terminated subs) не блокирует: `Subscription.node_id` `SET NULL` с миграции `0028`. |
| `del` на таске | `DELETE /api/provisioning/tasks/{id}` | Жёсткое удаление записи (нельзя для `running`). |

## Navigation permission

Сейчас — **бинарно**: либо у тебя есть admin-token и ты видишь всё, либо ты не залогинен. Scoped токенам UI не ограничивает пункты меню (они в UI работать всё равно не будут, т.к. admin-роуты требуют `X-Admin-Token`). Tree-shake по scope'ам — в backlog (`admin/README.md` → «Что дальше»).
