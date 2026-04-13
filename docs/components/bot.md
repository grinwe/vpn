# Telegram-бот

Живёт в `bot/`. Стек — aiogram 3, один процесс, один poll-loop, в контейнере.

## Структура модулей

```
bot/
├── bot.py        — точка входа: Dispatcher, роутеры, notification poller, graceful shutdown
├── handlers.py   — ~970 строк, все основные команды (user + admin /invoices)
├── support.py    — FSM support-тикетов, подключается отдельным роутером
├── keyboards.py  — все ReplyKeyboard / InlineKeyboard
├── config.py     — чтение env (BOT_TOKEN, BACKEND_URL, ADMIN_API_TOKEN, ADMIN_IDS, ...)
└── requirements.txt
```

Диспетчер собирается в `bot/bot.py:78-84`:

```python
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(support_router)  # FIRST — чтобы FSM-стейт перехватывал раньше
dp.include_router(router)
```

`MemoryStorage` — осознанный выбор: single-instance бот, нет нужды в Redis storage. Переход на multi-instance потребует RedisStorage (комментарий `bot.py:75-77`).

## Почему aiogram long-poll, а не webhook

В коде напрямую это не зафиксировано, но эффекты видны: единственная точка связи бота с TG — `dp.start_polling(bot)` (`bot.py:101`). Long-poll избавляет от необходимости публичного endpoint'а для бота и позволяет сидеть за Cloudflare + nginx на том же хосте, что и backend, не шаря webhook-URL в TG.

## Поток пользовательских команд

Команды делятся на три группы.

### 1. Обычные команды пользователя

```
/start [ref_XXXXX | support]   — onboarding, регистрация с реферальным кодом
/plans                          — список тарифов (через /api/plans)
/config                         — отдать конфиги активной подписки
/newconfig                      — регенерация (/api/users/by_telegram/{id}/regenerate)
/status                         — статусы всех подписок
/renew                          — продлить
/balance                        — показать balance_kopecks + runway
/referral                       — показать реферальный код
/help                           — инлайн-помощь
```

Все они ходят в backend через `_fetch_json(method, url, **kwargs)` (`handlers.py:69-91`) — обёртка над `aiohttp.ClientSession` с:

- общим keep-alive session (`_SESSION`);
- таймаутом `total=10s` (`_HTTP_TIMEOUT`);
- ретраями `_RETRIES=2` на `aiohttp.ClientError` и `5xx` с бэкоффом `0.3 * (attempt + 1)`;
- возвратом `(status_code, payload)` тупла, с деградацией на `(0, {"message": "backend unreachable"})` вместо исключения — поэтому хэндлеры всегда могут отдать пользователю адекватный текст, даже если backend упал.

### 2. Админские команды

```
/invoices                       — список pending-инвойсов, inline-кнопки mark_paid
/invoice_paid:<id>              — callback-обработчик для кнопки
```

Доступ гейтится через `_is_admin(user_id) = user_id in ADMIN_IDS` (`handlers.py:51`). `ADMIN_IDS` приходит из env (comma-separated), парсится в `bot/config.py`. Это проверка **на уровне Telegram user id**, не через отдельный auth на backend'е — backend всё равно принимает запросы по единому `ADMIN_API_TOKEN`.

### 3. Stars-платёжный поток

Telegram Stars — единственный способ принять оплату *внутри* TG, без внешнего pay_url.

#### Polling-режим (legacy)

Два хэндлера в `handlers.py` — `_stars_pre_checkout` (отвечает OK на pre_checkout_query) и `_stars_successful_payment` (форвардит на backend через `X-Telegram-Stars-Secret` shared-secret). Бот выступал relay'ом — shared-secret был единственной аутентификацией (audit #62).

#### Webhook-режим (рекомендуемый, #62)

При `BOT_WEBHOOK_PORT > 0` бот переходит из polling в aiohttp-сервер на внутреннем порте. Backend регистрируется как Telegram webhook (`setWebhook`) и:

- `pre_checkout_query` (XTR) — backend отвечает OK через Bot API напрямую
- `successful_payment` (XTR) — backend вызывает `_mark_invoice_paid_core` напрямую
- Все остальные update'ы — forward в бот через `BOT_INTERNAL_WEBHOOK_URL`

Stars-хэндлеры в `handlers.py` в webhook-режиме не вызываются (backend перехватывает payment-update'ы до пересылки боту). Хэндлеры остаются в коде для backward compat с polling-режимом.

Endpoint: `POST /tg-webhook` (`app/telegram_webhook.py`). Детали — `components/payments.md`.

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
- **Фильтрация, кого можно действовать.** На стороне бота: `ADMIN_IDS` плюс явные ownership-проверки внутри хэндлеров (`cmd_config` использует `message.from_user.id` как owner'а, не берёт input от пользователя — `handlers.py:424`).

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

Это и есть «очередь уведомлений»: воркер пишет строки в `audit_logs` со специальными `action` (`renewal_reminder`, `config_ready`, `migration_notice`, `low_balance_warning`, `trial_expiry_warning`), бэкенд их рендерит в человекочитаемый текст (`backend/app/api_extensions.py:359-439`), бот опрашивает и доставляет.

Тонкости:

- Интервал — `NOTIFICATION_POLL_INTERVAL` (default 10с). Ставить `<=0` — отключить поллер.
- Timeout 5с на GET — если backend молчит, просто пропускаем итерацию.
- Ack — best-effort: ошибка ack'а не прерывает цикл, но может привести к повторной доставке одного и того же уведомления.
- Для `type=config_ready` прикрепляется `onboarding_keyboard()` — чтобы сразу показать инструкции по настройке клиента.

Таск — background, стартует в `main()` как `asyncio.create_task(...)`, отменяется в finally при shutdown.

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

`cmd_config` (`handlers.py:420-482`):

1. Запрашивает `/api/users/by_telegram/{id}` (используя свой же user id — не user input).
2. Находит первую active subscription с хотя бы одним credential.
3. Рендерит каждый credential как `<b>{proto}:</b>\n<code>{config_text}</code>` — inline HTML.
4. Если есть `sub_token` + `SUB_LINK_BASE_URL` — добавляет динамическую ссылку, которую клиент (Hiddify/v2rayNG) может вставить как subscription URL.

> ⚠️ Plaintext конфиги уходят в Telegram-чат (пользователь + TG-сервер + TG-бэкап клиента). См. audit/...

## Deep-link reference

Бот понимает три типа `/start <arg>`:

- `/start support` → сразу перебрасывает в support-FSM (`handlers.py:167-179`).
- `/start ref_XXXXX` → передаёт `referral_code=XXXXX` в `/api/users/register` (`handlers.py:181-200`).
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
