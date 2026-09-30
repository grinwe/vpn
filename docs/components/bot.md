# Telegram-бот

Живёт в `bot/`. Стек — aiogram 3, один процесс, один poll-loop, в контейнере.

Self-report: в `bot/keyboards.py::start_keyboard()` есть шестая reply-кнопка «🆘 VPN не работает» (константа `BTN_VPN_BROKEN`). Message-handler `self_report_vpn_broken` в `bot/handlers.py` не «шлёт сигнал», а **чинит**: пре-чек → выбор устройства → шаг починки через общее ядро `backend/app/services/self_repair.py` (см. «Флоу «🆘 VPN не работает»» ниже). Нужно потому, что плановый `run_user_health_ping_tick` **приходит юзеру не чаще раза в сутки** (дефолты: `USER_HEALTH_PING_INTERVAL=1800` — интервал самого тика, `USER_HEALTH_PING_DEBOUNCE_HOURS=24` — per-user debounce, плюс окно МСК 11–14) — ждать сутки, чтобы пожаловаться на сломанный VPN, абсурд. Плановый тик воркера **не затронут**; self-report — это дополнительный канал, не замена. Admin-видимость всей телеметрии (plan + self-report) — страница `/health-pings` в админке + компактный виджет в expand-row карточки ноды (`admin/src/pages/Nodes.tsx::NodeHealthPings`). Бэкенд различает два типа в `AuditLog.extra.source` (`"prompted"` vs `"self_reported"`).

## Структура модулей

```
bot/
├── bot.py        — точка входа: Dispatcher, роутеры, notification poller, graceful shutdown
├── handlers.py   — ~970 строк, все основные команды (user + admin /invoices)
├── support.py    — FSM support-тикетов, подключается отдельным роутером
├── middleware.py — outer middleware BanGuard: тихий drop апдейтов от забаненных
├── keyboards.py  — все ReplyKeyboard / InlineKeyboard
├── config.py     — чтение env (BOT_TOKEN, BACKEND_URL, ADMIN_API_TOKEN, ADMIN_IDS, ...)
└── requirements.txt
```

Диспетчер собирается в `bot/bot.py:78-84`:

```python
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(support_router)  # FIRST — чтобы FSM-стейт перехватывал раньше
dp.include_router(router)
dp.errors.register(on_dispatch_error)  # глобальная страховка от падений хендлеров
```

Глобальный errors-хендлер `on_dispatch_error` (`bot/bot.py`) логирует стектрейс любого необработанного исключения и best-effort отвечает пользователю: для callback — `callback_query.answer(...)` (гасит спиннер на кнопке), для message — короткое «Произошла ошибка, попробуйте позже». Сам ответ обёрнут в try/except, чтобы обработчик ошибок не падал вторично.

`MemoryStorage` — осознанный выбор: single-instance бот, нет нужды в Redis storage. Переход на multi-instance потребует RedisStorage (комментарий `bot.py:75-77`).

## BanGuard — тихий drop забаненных

`bot/middleware.py` — outer middleware на `dp.update`, регистрируется ПЕРЕД роутерами (`bot.py::main`). Каждые 30с (`_TTL_SECONDS`) подтягивает `GET /api/users/banned-telegram-ids` в процесс-локальный `set[str]`. На каждый входящий `Update` достаёт `from_user.id` (перебирая `message / edited_message / callback_query / inline_query / pre_checkout_query / chat_member / my_chat_member / chosen_inline_result / shipping_query`), сравнивает со строкой из кэша — если совпало, `return None` без вызова `handler(event, data)`.

Почему это outer middleware, а не фильтр в роутере:

- **DDoS-мотивация.** Инцидент: ~250 throwaway-аккаунтов долбили `/start` и клали процесс. Если бан-чек лежит внутри роутера, каждый mach of `CommandStart` всё равно прогружает FSMContext, тянет UserDB, etc. Outer middleware отрубает update'ы ещё до `FSMContext.get_data()`.
- **Один admin-вызов на TTL, не на update.** Бот при DDoS'е не усиливает нагрузку на backend — вызовов к `/banned-telegram-ids` ровно `60/_TTL_SECONDS` в минуту (2 по текущим дефолтам), независимо от объёма входящих апдейтов.
- **Fail-open при blip'е backend'а.** Если рефреш упал (5xx / таймаут / connection error), `_banned` остаётся в последнем успешно прочитанном состоянии. Лучше однократно пропустить забаненного внутрь, чем зачёрнодырить всю юзерскую базу из-за сетевого сбоя.

«No ACK» — инвариант. На dropped-update мы НЕ отвечаем никак (ни текстом, ни reaction'ом, ни ошибкой). Любой ответ тренирует DDoS-скрипты, что аккаунт достижим. Update просто поглощается молча.

Устанавливает бан только `POST /api/users/{id}/ban` (админ-панель). Снимается `/unban`. Поле — `User.banned_at` (`DateTime NULL`). Ортогонально `Subscription.status=blocked`: бан трогает только Telegram-аккаунт, подписки и их устройства не затрагиваются.

## Почему aiogram long-poll, а не webhook

В коде напрямую это не зафиксировано, но эффекты видны: единственная точка связи бота с TG — `dp.start_polling(bot)` (`bot.py:101`). Long-poll избавляет от необходимости публичного endpoint'а для бота и позволяет сидеть за Cloudflare + nginx на том же хосте, что и backend, не шаря webhook-URL в TG.

## Поток пользовательских команд

Команды делятся на три группы.

### 1. Обычные команды пользователя

```
/start [ref_XXXXX | support]   — onboarding, регистрация с реферальным кодом; два сообщения: приветствие + reply-клавиатура, затем «Начнём? 👇»/«Что дальше? 👇» + inline-кнопки (см. «/start: два сообщения» ниже)
/plans                          — список тарифов (через /api/plans)
/config                         — одна саб-ссылка двумя сообщениями (короткая шапка + голый URL); тот же cmd_config зовут trial:activate и go:config (с intro-заголовком)
/newconfig                      — «переселить всё»: тот же whole-sub ремонт, что «🔁 Все мои устройства» (POST /api/admin/client-control/report-broken)
/status                         — статусы всех подписок
/renew                          — продлить
/balance                        — показать balance_kopecks + runway
/referral                       — показать реферальный код
/help                           — инлайн-помощь
```

Все они ходят в backend через `_fetch_json(method, url, **kwargs)` (`handlers.py:69-91`) — обёртка над `aiohttp.ClientSession` с:

- общим keep-alive session (`_SESSION`);
- таймаутом `total=10s` (`_HTTP_TIMEOUT`);
- ретраями `_RETRIES=2` на `aiohttp.ClientError`, `asyncio.TimeoutError` (total-таймаут aiohttp — не подкласс `ClientError`) и `5xx` с бэкоффом `0.3 * (attempt + 1)`;
- возвратом `(status_code, payload)` тупла, с деградацией на `(0, {"message": "backend unreachable"})` вместо исключения — поэтому хэндлеры всегда могут отдать пользователю адекватный текст, даже если backend упал.

### 2. Админские команды

```
/invoices                       — список pending-инвойсов, inline-кнопки mark_paid
/invoice_paid:<id>              — callback-обработчик для кнопки
```

Доступ гейтится через `_is_admin(user_id) = user_id in ADMIN_IDS` (`handlers.py:51`). `ADMIN_IDS` приходит из env (comma-separated), парсится в `bot/config.py`. Это проверка **на уровне Telegram user id**, не через отдельный auth на backend'е — backend всё равно принимает запросы по единому `ADMIN_API_TOKEN`.

### 2.5. Выбор способа оплаты (Stage 9b)

При заполненном `PAYMENT_PROVIDER_CHOICES` (2+ имён провайдеров,
`bot/config.py`) покупка (`plan:`-callback) и продление (`/renew`) после
создания счёта показывают меню способов оплаты вместо немедленного
checkout'а с `PAYMENT_PROVIDER`. Callback `payvia:{new|ren}:{invoice_id}:{provider}`
делает checkout выбранным провайдером и подставляет pay-кнопку (`edit_reply_markup`),
сохраняя callback-кнопки (способы + автопродление) — неудавшийся способ можно
сменить, каждый выбор создаёт свою Payment-строку, вебхук пометит оплаченную
(#117). Подписи кнопок — `_PROVIDER_LABELS` в `handlers.py`; неизвестное имя
показывается как есть. Пустой/одиночный `PAYMENT_PROVIDER_CHOICES` — старое
поведение без меню. Детали провайдеров — `components/payments.md`.

Прод-набор с 2026-09-19 — `telegram_stars,lava_top_sbp,lava_top`
(`group_vars/web/main.yml`), то есть три кнопки: «⭐ Telegram Stars»,
«🏦 СБП», «💳 Карта РФ». Раньше их было две, и lava-кнопка называлась
«💳 Карта РФ / СБП» — один счёт через агрегатор PAY2ME, где человек выбирал
способ уже на платёжной странице. lava закрыл у PAY2ME карту (счета стали
падать с 400 «Restricted payment method type»), поэтому способ выбирается
до создания счёта: `lava_top_sbp` = СБП, `lava_top` = карта, обе кнопки —
одна и та же интеграция lava. Подробности — `docs/PLAN_LAVA_TOP.md`,
«Инцидент 2026-09-19».

### 2.6. Inline-callback'и навигации

```
go:start / go:plans / go:topup / go:referral / go:help — шорткаты с welcome-клавиатуры (user_id берётся у инициатора, не у message.from_user — это бот)
go:config                       — «🔗 Ссылка для подключения» → cmd_config(user_id=инициатор); кнопка есть на welcome и в /plans у юзера с подпиской active/frozen ИЛИ живыми устройствами (`register.has_subscription or has_devices` — cold-путь с pending-девайсом и заморозка не должны терять путь к ссылке); у новичка с незабранным подарком — сразу после кнопки подарка (2026-08-25)
trial:activate                  — нативный бесплатный месяц: POST /api/trial/activate_full, затем ссылка тем же cmd_config с intro «🎉 Бесплатный месяц активирован до …» — итого ДВА сообщения (заголовок+шапка со ссылкой, голый URL)
plans:help / plan:<id> / payvia — прайс и оплата
onboard:<platform>              — инструкции по платформам (onboarding_keyboard)
```

### 3. Stars-платёжный поток

Telegram Stars — единственный способ принять оплату *внутри* TG, без внешнего pay_url.

#### Polling-режим (legacy)

Два хэндлера в `handlers.py` — `_stars_pre_checkout` (отвечает OK на pre_checkout_query) и `_stars_successful_payment` (форвардит на backend через `X-Telegram-Stars-Secret` shared-secret). Бот выступал relay'ом — shared-secret был единственной аутентификацией (audit #62).

#### Webhook-режим (рекомендуемый, #62)

При `BOT_WEBHOOK_PORT > 0` бот переходит из polling в aiohttp-сервер на внутреннем порте. Backend регистрируется как Telegram webhook (`setWebhook`) и:

- `pre_checkout_query` (XTR) — backend валидирует инвойс (существует, pending, сумма в Stars совпадает) и отвечает ok/ok=False через Bot API (аудит #2)
- `successful_payment` (XTR) — backend вызывает `_mark_invoice_paid_core` напрямую; при сбое — error-лог + алерт админам `stars_payment_failed`, на 5xx вебхук отвечает не-200 для ретрая Telegram (аудит #209)
- Все остальные update'ы — forward в бот через `BOT_INTERNAL_WEBHOOK_URL`

Stars-хэндлеры в `handlers.py` в webhook-режиме не вызываются (backend перехватывает payment-update'ы до пересылки боту). Хэндлеры остаются в коде для backward compat с polling-режимом.

Endpoint: `POST /tg-webhook` (`app/telegram_webhook.py`). Детали — `components/payments.md`.

## Флоу «🆘 VPN не работает»

Унифицирован 2026-09-12: бот, кабинет и страница по токену ходят в одно ядро
(`backend/app/services/self_repair.py`) и говорят одно и то же. Разбор расхождений
до унификации — [operations/vpn_broken_channels_parity_2026_09_12.md](../operations/vpn_broken_channels_parity_2026_09_12.md).

В боте у флоу **четыре входа, и все они кончаются одной функцией**
`_send_repair_outcome(bot, chat_id, data)` — единственным местом с текстами исходов:

1. reply-кнопка «🆘 VPN не работает» (`self_report_vpn_broken`);
2. кнопка пикера «какое устройство» (`brk:<device_id>` / `brk:all`);
3. ответ «не работает» на плановый health-ping (`hping:bad`);
4. `/newconfig`.

### Шаги кнопки «🆘 VPN не работает»

1. **Пре-чек** — `GET /api/admin/client-control/devices-by-telegram?telegram_id=`
   отдаёт `{devices, retry_after_sec, wait_reason, subscription_id}`.
   `wait_reason` (`throttled` / `daily_limit`) → сразу текст ожидания и выход,
   **независимо от числа устройств**: раньше бот сначала спрашивал «какое не
   работает?», а потом отвечал «уже перекидывали недавно» — для человека это
   выглядело как противоречие бота самому себе. Жалобу (`complaint_received`)
   в этом случае пишет сам бэкенд, боту делать ничего не нужно.
2. **Выбор объекта.** `devices` пусто, но `subscription_id` есть → «⏳ Устройства
   ещё настраиваются» (все девайсы pending). Пусто и подписки нет → текст
   `no_subscription` + inline-кнопка «💎 Выбрать тариф» (`go:plans`; команды
   `/buy` в боте нет, тупик недопустим). Ровно одно → чиним сразу. Больше
   одного → пикер `broken_device_keyboard`: кнопка на каждое живое устройство
   (`Device.name`, как записал юзер) + «🔁 Все мои устройства».
3. **Действие.** Одно устройство → `POST /api/admin/client-control/report-broken-device`
   `{telegram_id, device_id}` (`_do_device_failover`) — лестница ротации для
   этого устройства, соседей не трогаем, ноду user-wide не баним.
   «Все мои устройства» и `/newconfig` → `POST /api/admin/client-control/report-broken`
   `{telegram_id}` (`_do_whole_sub_failover`) — переезд всей подписки с баном
   старой ноды.
4. **Исход** — `_send_repair_outcome` по полю `action` ответа.

### Тексты исходов (`_send_repair_outcome`)

| `action` | Что показываем | Что дальше |
|---|---|---|
| `migrated` | «🔄 Поменяли сервер для «{device_name}»» (при `scope="subscription"` — «для всех устройств») + «нажми 🔄 рядом с профилем» | вопрос об операторе + нудж |
| `reshuffled` | «🔀 Переключили тебя на другой способ подключения» | вопрос об операторе + нудж |
| `duplicated` | «➕ Добавили тебе запасной сервер» | вопрос об операторе + нудж |
| `throttled` | «👍 Мы уже переключали тебя пару минут назад… если через {M} мин. всё ещё не работает — нажми кнопку ещё раз», `M = ceil(retry_after_sec/60)` | кнопка доступна сразу |
| `daily_limit` | «Сегодня мы уже несколько раз меняли тебе серверы — дальше нужна помощь человека» | `help_keyboard()` |
| `not_ready` | «⏳ Устройство ещё настраивается» (устройство pending — чинить нечего) | повтор через пару минут |
| `no_subscription` | «Чинить нечего: подписка не активна или это устройство уже отключено» | «💎 Выбрать тариф» |
| `user_not_found` | «Не нашли твой аккаунт — нажми /start» | — |
| `no_target` | «Не смогли автоматически подобрать другой сервер. Попробуй через 10 минут» | `help_keyboard()` |
| нет `action` / не-200 / сеть | «Не получилось обработать — попробуй ещё раз через минуту» | `help_keyboard()` |

Имя устройства приходит от юзера, поэтому идёт через `html.escape` — parse_mode
у бота HTML, и `<` в имени иначе валит отправку целиком (юзер не получил бы ни
клавиатуры оператора, ни нуджа).

После `migrated` / `reshuffled` / `duplicated` с `report_id`: `operator_keyboard(report_id)`
(шесть операторов, ответ необязателен) и отложенный `_delayed_still_broken_prompt`
через `_STILL_BROKEN_DELAY_S = 900` — если бэкенд НЕ видит переподключения,
прилетает «всё ещё не работает?» с кнопками «✅ Всё работает» (`report-ok`) /
«❌ Всё равно не работает» (`report-still-broken`).

### Повторы ограничивает только бэкенд

Клиентского кулдауна в боте нет: `_SELF_REPORT_COOLDOWN_S` удалён (он был мёртв —
серверный пре-чек срабатывал раньше — и терялся при рестарте процесса). Единая
политика живёт в ядре: `SELF_REPAIR_THROTTLE_SEC` (120 с) и `SELF_REPAIR_DAILY_MAX`
(5/сутки), обе **по подписке**, с fallback на `SUB_FIX_*` (см.
[operations/env-reference.md](../operations/env-reference.md)). Повторный тап
разрешён всегда — сервер сам ответит `throttled`.

### Ответ «плохо» на плановый пинг (`hping:bad`)

`POST /api/users/health-ping-response` с `answer=bad` теперь не только пишет
телеметрию, но и чинит тем же ядром (одно живое устройство → per-device лестница,
иначе → переезд подписки) и возвращает тот же контракт с `action`. Бот показывает
короткий ack «🛠 Спасибо! Чиним.» (он автоудаляется через
`HEALTH_PING_ACK_DELETE_DELAY_S`), а затем отдельным сообщением — полноценный
исход через `_send_repair_outcome` с вопросом об операторе и нуджем. Если `action`
в ответе нет (в колбэке не было `subscription_id` — чинить нечего), остаётся старый
текст «получили сигнал… /help».

### FSM переименования больше не глотает кнопки меню

`devices_router` подключён раньше главного, и `DeviceStates.waiting_rename` ловил
любой текст: тап «🆘 VPN не работает» в состоянии переименования становился
**именем устройства**, а жалоба пропадала. Теперь `device_rename_finish` сверяет
текст с `_MENU_TEXTS` (из `bot/support.py`): совпало → `state.clear()`, и если это
был `BTN_VPN_BROKEN` — сразу вызывается `self_report_vpn_broken` (человек с
нерабочим VPN не должен жать дважды), иначе «Ок, переименование отменено. Нажми
кнопку ещё раз».

## X-Admin-Actor — кто дёргает backend от имени бота

Все user-инициированные команды идут в backend с:

```python
# bot/handlers.py:94-99
def _admin_headers(actor_id: int) -> dict[str, str]:
    headers = {}
    if ADMIN_API_TOKEN:
        headers["X-Admin-Token"] = ADMIN_API_TOKEN
    headers["X-Admin-Actor"] = str(actor_id)
    return headers
```

То есть **бот шлёт тот же admin-токен, что и админ-SPA**, но проставляет в `X-Admin-Actor` Telegram user id того пользователя, от имени которого действует. Backend записывает это значение в `AuditLog.actor` без верификации — see `components/backend-api.md` ("Audit logging").

Эта схема предполагает:

- **Бот доверенная машина.** Компрометация контейнера `bot` = shared admin-token наружу.
- **`X-Admin-Actor` — не аутентификация, а контекст аудита.** Поле помогает отличать «это действие инициировал пользователь 12345 через бота» от «это админ нажал кнопку в SPA».
- **Фильтрация, кого можно действовать.** На стороне бота: `ADMIN_IDS` плюс явные ownership-проверки внутри хэндлеров (`cmd_config` берёт owner'а из `message.from_user.id`, а из callback'ов — явный `user_id=callback_query.from_user.id` инициатора; input пользователя в запрос не попадает).

> ⚠️ Самодекларативный actor + shared admin token. См. audit/...

## Notification poller — подбираем уведомления из backend'а

```python
# bot/bot.py:14-69
async def notification_poller(bot: Bot):
    while True:
        await asyncio.sleep(NOTIFICATION_POLL_INTERVAL)
        # GET /api/notifications/pending
        # → для каждой строки: bot.send_message(telegram_id, text)
        # → POST /api/notifications/{id}/ack
```

Это и есть «очередь уведомлений»: воркер пишет строки в `audit_logs` со специальными `action` (`renewal_reminder`, `config_ready`, `migration_notice`, `sublink_rotated`, `low_balance_warning`, `trial_expiry_warning`, `admin_alert_node_diagnosis`), бэкенд их рендерит в человекочитаемый текст (`backend/app/api_extensions.py:359-439`), бот опрашивает и доставляет.

**Алерт «починка не помогла».** `admin_alert_repair_failed` (с 2026-09-30, `backend/app/services/repair_alerts.py`) — единственный пуш по жалобам «VPN не работает»: приходит, только если автоматическая починка не помогла (watcher не увидел переподключения, юзер нажал «всё ещё не работает») или не смогла ничего сделать (нет целевой ноды). Текст: кто, что сделали (перетасовка / перенос / дубль-нога), нода, оператор, исход и канал. Пуша на саму жалобу (`admin_alert_user_report`) больше нет — строка в allowlist осталась только ради недоставленных старых записей.

**Admin-диагностический пуш с кнопками.** `admin_alert_node_diagnosis` (diagnostics overhaul) — говорящий пуш «нода/exit недоступна» с резюме чек-листа (рендерит `services/admin_notify.notify_node_diagnosis`). В отличие от прочих `admin_alert_*`, несёт `target_kind`/`target_id` в `NotificationOut`, по которым `notification_poller` строит `node_diagnosis_keyboard` (`bot/handlers.py`): `👀 Вижу, работаю` / `🔕 1ч·4ч·24ч·совсем` / `📈 Следить (экспонента)`. Callback `diag:<action>:<kind>:<id>[:<hours>]` ловит admin-only `diag_control` и POST'ит в `/api/diagnostics/{kind}/{id}/{action}`. Это первый admin-пуш с интерактивом — раньше все `admin_alert_*` шли без клавиатуры.

Тонкости:

- Интервал — `NOTIFICATION_POLL_INTERVAL` (default 10с). Ставить `<=0` — отключить поллер.
- Timeout 5с на GET — если backend молчит, просто пропускаем итерацию.
- Не-200 ответ от `/pending` (например 401 при рассинхроне `ADMIN_API_TOKEN` после деплоя, или 500) логируется как `warning` с кодом и обрезанным телом, но с дедупом: сразу при смене статуса, дальше не чаще раза в 60с — иначе стабильный отказ гасил бы всю доставку молча. При возврате 200 дедуп сбрасывается.
- Ack — с ретраем и дедупом доставки (аудит #208): `_ack` ретраит POST `/…/ack` до 3 раз с backoff, ошибка ack'а не прерывает цикл. Чтобы сбой ack'а не приводил к повторной отправке в Telegram (юзер/`admin_broadcast` иначе получали бы дубли каждый тик, пока ack не пройдёт), отправленные-но-не-подтверждённые `notif_id` держатся в module-level `_sent_unacked` (id → ts) и повторно в TG не уходят — на следующем тике их только до-ACK'ивают. Успешный ack снимает id с дедупа; TTL-очистка (`_SENT_UNACKED_TTL` ~10 мин) страхует от вечного подавления при перманентном сбое ack'а.
- Ack — не блокирующий (сетевой аудит): `_ack` запускается в фоне через `_spawn_ack` (реестр `_ack_tasks`, id → Task, с дедупом in-flight и удержанием ссылки от GC), а не `await`'ится в основном проходе. Иначе залипший ack бэкенда добавлял до ~4.5с (3 ретрая с backoff) на каждую запись и держал отправку следующих — в т.ч. срочного `config_ready` после оплаты.
- Приоритизация в пределах тика (сетевой аудит, head-of-line): перед отправкой `notifications` стабильно сортируются так, что срочные типы (`config_ready`, `health_ping_request`, `admin_alert_*`) идут первыми, а массовая `admin_broadcast` — в хвост. Плюс лимит `NOTIFICATION_BROADCAST_PER_TICK` (default 50) на число рассылок за тик: хвост сверх лимита не ACK-ается и возвращается из `/pending` на следующем тике. Так одна большая рассылка (сотни юзеров × `sleep 0.05с`) больше не задерживает оплаченный конфиг, попавший в тот же ответ `/pending`.
- Для `type=config_ready` прикрепляется `onboarding_keyboard()` — чтобы сразу показать инструкции по настройке клиента. Первой строкой в ней (при https-WebApp) — «🔐 Открыть личный кабинет»: ЛК — основное место, где ссылка лежит всегда; та же клавиатура висит на /config и на «Оплата получена» после Stars. Текст пуша короткий: «✅ Конфиг VPN готов, можно подключаться! Ссылка: … Не знаешь, как настроить? Выбери платформу 👇» — абзаца про ЛК/config в нём нет (кнопка ЛК уже в клавиатуре), строка про ЛК появляется только когда ссылки в пуше нет. Саму запись `config_ready` пишет бэкенд по завершении провижининга — см. [provisioning.md](provisioning.md) (до 2026-08-25 писателя не было: `_notify_bot_config_ready` клал флаг в `ProvisioningTask.result`, который никто не читал, и после оплаты картой бот молчал). **Warm-пуш только там, где вызывающий сам ссылку не отдаёт: оплата картой** (lava-вебхук → `_create_subscription_for_user`). Бот после `activate_trial_full` и ЛК после `webapp_activate` отдают ссылку сами и сразу, поэтому передают `provision_subscription(..., notify_config_ready=False)` — иначе через ≤10 с приходил пуш с той же ссылкой (жалоба владельца 2026-08-28: 7 сообщений за один тап, ссылка дважды). Cold-путь (девайс pending, бот честно пишет «ещё создаётся») пуш шлёт всегда.
- `type=referral_invite` («Как VPN? Если приведёшь друга, получишь N дней подписки, когда он оплатит. Ссылка для друзей: …») бэкенд отдаёт с задержкой `REFERRAL_INVITE_DELAY_H` (default 24 ч) после первого скачивания конфига: строка пишется сразу, доставка откладывается на стороне `/api/notifications/pending`. Сразу после ссылки это было третьим-четвёртым сообщением подряд.

Таск — background, стартует в `main()` как `asyncio.create_task(...)`, отменяется в finally при shutdown.

## Sub-link UX: где показываем подписочную ссылку, а где нет

Подписочный URL (`{SUB_LINK_BASE_URL}/{sub_token}` — та же ссылка, что висит в карточке подписки в webapp) — штука одноразового копирования. Юзер импортирует её в Hiddify / V2rayNG / Streisand **один раз**, дальше клиент периодически рефрешит профиль по тому же URL и сам подтягивает актуальные VLESS-линки. Поэтому есть ровно два контекста, где URL может понадобиться — и один, где он противопоказан.

- **`/config`** (`bot/handlers.py::cmd_config`) — юзер явно попросил конфиг, ссылка нужна. Отдаём **двумя сообщениями**: первое — короткая шапка («🔗 Твоя ссылка для подключения. Тапни по ней ниже, чтобы скопировать. / Не знаешь, как настроить? Выбери платформу 👇») + клавиатура онбординга, второе — одна строка `<code>{sub_url}</code>` с `disable_web_page_preview=True` и без клавиатуры. Абзацы «импортируй один раз, клиент сам подтянет» (уехал в `ONBOARDING_INSTRUCTIONS`, где он по делу) и «ссылка всегда лежит в личном кабинете … /config» (кнопка ЛК первой строкой в клавиатуре + menu-button у поля ввода) из шапки убраны 2026-08-28: текстом ЛК повторялся до четырёх раз за один тап. **Сырые `vless://…` не показываем** — юзеру они не нужны (клиент сам парсит подписочную ссылку), а длинный список из 5-10 строк путал и переполнял экран. URL берём через `_build_sub_url(token)`: сначала `SUB_LINK_BASE_URL` (если задан — например, CF-воркер), иначе origin из `WEBAPP_BASE_URL` + `/api/sub/{token}`. В проде `WEBAPP_BASE_URL` всегда задан (без него Mini App не запускается), так что fallback гарантированный. Мотивация для split на 2 сообщения: на мобильном тап-копирование `<code>` в многострочном HTML-посте регулярно промахивается, а чистая однострочная реплика копируется одним тапом без сюрпризов. Тот же `cmd_config` зовут `trial_activate_cb` (после «Готово!», через `_try_send_sub_link`) и callback `go:config` (кнопка «🔗 Ссылка для подключения» на welcome и в `/plans` у юзера с устройствами — способ забрать ссылку ПОТОМ); из callback'ов передаётся `user_id` инициатора и, для сообщений старше 48ч, `bot`/`chat_id`. Возвращает `bool` («ссылка ушла»); `quiet_if_missing=True` глушит «нет подписок / /plans» — вызывающий сам решает, что показать («⏳ ещё создаётся» и «❄️ заморожена» шлются всегда). `intro=` — заголовок от вызывающего («🎉 Бесплатный месяц активирован до …», «У тебя уже есть подписка 😉»), который клеится к ПЕРВОМУ сообщению cmd_config, каким бы оно ни было (шапка со ссылкой, «ещё создаётся», «заморожена»); если в тихом режиме слать нечего, заголовок уходит сам по себе. Так «готово» и ссылка — одно сообщение, а не два. Замороженному — «разморозь через /status», а не ложное «нет подписок». **Инцидент 2026-08-25:** вложенный `_say` звал сам себя вместо `message.answer` (RecursionError) — /config, «Мой конфиг» и ссылка после триала не работали ни у кого; после «Готово!» юзер получал тишину, повторный тап — «подарок уже использован» + прайс. Теперь выдача из callback'ов обёрнута в try/except с фолбэком «забери в личном кабинете или /config» + кнопка ЛК (заголовок `intro` едет вместе с фолбэком: факт активации юзер должен увидеть даже при падении выдачи), а 409 с живой подпиской отдаёт ссылку вместо прайса. Развёрнутый абзац про ЛК/config живёт только в этих фолбэках (`_SUB_LINK_FALLBACK_TEXT`, `_SUB_LINK_MISSING_HINT`, 409-ветки без ссылки) и в строке `/plans` у юзера с подпиской — там это навигация, а не повтор.

**Итог за тап по подарку (warm-путь), 2026-08-28:** (1) «🎉 Бесплатный месяц активирован до DD.MM.YYYY. / 🔗 Твоя ссылка для подключения…» + `onboarding_keyboard`, (2) голый URL. Всё. Пуш `config_ready` не приходит (см. выше), `referral_invite` — через сутки. До этого было 7 сообщений: отдельное «Готово!», шапка с тремя абзацами, ссылка, пуш с той же ссылкой и «Готово, VPN работает» по первому фетчу подписки. Структурный тест на самовызов вложенных хелперов — `backend/tests/test_bot_sublink_delivery.py`.
- **`migration_notice`** (после переезда ноды) — URL **не вшиваем**. Старый `sub_token`, который уже лежит в профиле клиента, продолжает резолвиться через sibling-alias в `/sub/{token}` (см. длинный комментарий в `backend/app/api_extensions.py::dynamic_sub_link` — «Seamless-migration alias»). Клиент при ближайшем профиль-апдейте сам подтянет новые VLESS-линки с новой ноды. Всё, что нужно юзеру — тапнуть 🔄 в своём VPN-клиенте. Раньше в сообщении был `/api/sub/…` — это путало людей, они копировали его и импортировали профиль заново вместо того, чтобы использовать refresh.
- **`sublink_rotated`** (после массовой `🔁 регенерация sub-link` в ADMIN_UI) — в отличие от `migration_notice`, здесь `sub_token` **сменился**, и юзера надо мягко увести на новую ссылку. URL тоже **не вшиваем** (одноразовое копирование склонно к ошибкам, и тап-копирование в многострочном посте промахивается) — вместо этого текст просит **открыть ЛК и взять новую ссылку оттуда**. Старый токен пока живёт (алиасится на новый Device, а его UUID не снят с ноды), так что обрыва нет — сообщение это явно проговаривает («старый конфиг ещё работает, но лучше обновиться сейчас»). Текст рендерится в `api_extensions.py::get_pending_notifications`.

Инвариант, на котором всё держится: `/sub/{token}` в бэкенде НИКОГДА не должен отдавать пустой 200 после миграции. Пустые конфиги затирают закешированный профиль клиента — юзер стрэнднутый. Отсюда защитный 503 и sibling-lookup в `dynamic_sub_link` — не трогать без очень хорошего повода.

## Shared HTTP session и shutdown

```python
# bot/handlers.py:32-48
_SESSION: aiohttp.ClientSession | None = None

async def get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT)
    return _SESSION

async def close_session() -> None:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None
```

Lazy-init, один на процесс. Закрывается в `bot/bot.py:109` (`finally` блока `start_polling`).

## FSM для support-тикетов

`support.py` — отдельный роутер. Через `FSMContext` держит состояние:

- `waiting_user_message` — юзер пишет в поддержку;
- дальнейшие состояния — переписка админ↔юзер с relay'ом через бота.

Стейт — `MemoryStorage`, живёт в процессе. Перезапуск бота = потеря активных тикетов.

Подключение — в `bot/bot.py:83`, **первым**, чтобы StateFilter'ы перехватывали сообщения админа раньше, чем общие `F.text` / `Command(...)` хэндлеры в `router`.

## Рендеринг конфигов пользователю

`cmd_config` (`handlers.py::cmd_config`):

1. Запрашивает `/api/users/by_telegram/{id}` (свой же user id — не user input; из callback'ов — id инициатора).
2. Находит первую active subscription с хотя бы одним credential. Если нет — честный статус вместо прайса: «⏳ ещё создаётся» (pending-девайс), «❄️ заморожена — разморозь через /status» (frozen), и только иначе «нет активных подписок / /plans».
3. Отдаёт ОДНУ подписочную ссылку `_build_sub_url(sub_token)` двумя сообщениями (см. «Sub-link UX» выше); сырые `config_text` не рендерит — клиент сам разворачивает подписку.

## /start: два сообщения

Telegram даёт один `reply_markup` на сообщение, а `/start` нужны обе клавиатуры: reply (всегда внизу) и inline (подарок / тарифы / ЛК). `cmd_start` и `go:start` шлют их через `_send_welcome_pair`: **(1)** приветствие `format_welcome(...)` + `start_keyboard(has_devices=)`, **(2)** «Начнём? 👇» (подарок доступен) / «Что дальше? 👇» + `welcome_action_keyboard(trial_available=, is_new=, has_link=has_subscription or has_devices)`. До 2026-08-28 порядок был обратным, и reply-клавиатуру везла отдельная строка «⌨️ Кнопки внизу всегда под рукой» без другой функции; «Выбери действие ниже 👇» из `format_welcome` при этом убрано — призыв к действию теперь и есть второе сообщение. Итог те же два сообщения, но без пустой строки, и кнопки действий — последнее, что юзер видит на экране.

> ⚠️ Саб-ссылка (эквивалент доступа к конфигам) уходит в Telegram-чат (пользователь + TG-сервер + TG-бэкап клиента). См. audit/...

## Deep-link reference

Бот понимает несколько типов `/start <arg>` (хендлер `cmd_start`):

- `/start support` → сразу перебрасывает в support-FSM.
- `/start ref_XXXXX` → передаёт `referral_code=XXXXX` в `/api/users/register` (реферал человека → `User.referred_by_id`).
- `/start <МЕТКА>` (любой другой непустой payload) → **рекламная метка** `source` (first-touch),
  передаётся в `/api/users/register`. См. [operations/ad_source_attribution.md](../operations/ad_source_attribution.md).
- `/start` без аргумента → обычный welcome.

WebApp Mini App открывается не через `/start` аргумент, а через inline-кнопку с `web_app` action (см. `bot/keyboards.py::webapp_inline_keyboard`).

## Что бот НЕ делает

- **Не шифрует/не расшифровывает credentials.** Backend отдаёт уже расшифрованный `config_text` (через `schemas.CredentialOut.from_orm` → `decrypt`), бот просто показывает.
- **Не ходит напрямую в БД.** Все данные — через `/api/*`.
- **Не знает про warm pool, провижининг-таски и т.п.** Бот — тонкий фронт для backend'а.
- **Не обрабатывает payment webhook'и из CryptoBot/SBP.** Те идут напрямую в backend по `/api/payments/webhook/{provider}`. Только Stars forward'ится через бота, потому что Stars доставляется боту самим Telegram'ом.

## ⚠️ Неясные места

- `ADMIN_IDS` хранится в env и в `bot/config.py`; изменить список = перезапустить контейнер бота. Backend про эти id ничего не знает — только бот фильтрует `/invoices` и support-FSM по ним. Возможны расхождения «админ в боте ≠ тот, кто в SPA», без одного источника истины.
- `NOTIFICATION_POLL_INTERVAL=10` по дефолту означает доставку уведомлений с задержкой до 10с. Для warning'ов это не критично, для `config_ready` — воспринимается как лаг провижининга. Компромисс нагрузки vs latency не зафиксирован.
- `_stars_successful_payment` возвращается юзеру «оплата получена» только после успешного forward'а в backend (`handlers.py:409-413`). Если backend вдруг подтвердил 200 по verified-seen, но операция на уровне БД упала — юзеру скажут «всё ок», а реального provisioning'а не будет. Гарантии «happy path fall-through» стоит проверять на уровне backend'а, не бота.
