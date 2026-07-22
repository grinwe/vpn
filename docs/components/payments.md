# Платежи

Платёжная подсистема решает одну задачу: превратить клик «оплатить» в паспорт `Invoice.status=paid` + запись в ledger балансов. Всё, что дальше — провижининг, активация подписки, уведомления — делают другие сервисы.

Живёт в `backend/app/services/payments/` и в двух роутах `api.py`:
`/api/invoices/{id}/checkout` (создать pay_url) и `/api/payments/webhook/{provider}` (приём callback'а).

## Provider abstraction

Все провайдеры подчиняются одному протоколу — `services/payments/base.py`:

```python
# base.py:33-46
class PaymentProvider(Protocol):
    name: str
    def create_invoice(self, *, invoice_id, amount, currency,
                       description=None, return_url=None) -> ProviderInvoice: ...
    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent: ...
```

Два data-class'а — `ProviderInvoice` (что возвращается при создании: `external_id`, `pay_url`, `amount`, `currency`) и `WebhookEvent` (что возвращается при верификации: `external_id`, нормализованный `status` ∈ `{"paid", "failed", "expired", "other"}`, `amount`). Всё остальное (идемпотентность, запись в БД, _mark_invoice_paid) — за пределами интерфейса.

Добавить нового провайдера — это написать класс, реализующий протокол, и зарегистрировать его в `get_provider(name)` (`base.py:87-119`). Никакого реестра через entry-points, никаких плагинов — явный `if name == "...":`. Это сознательно.

## Ротация провайдеров

`PAYMENT_PROVIDERS` — comma-separated список имён. Пример: `cryptobot,sbp:robokassa,sbp:sbpfoo`. Ротация — **случайная**:

```python
# base.py:71-84
def pick_provider_name(rng=None) -> str:
    pool = list_available_providers()
    if len(pool) == 1:
        return pool[0]
    return (rng or random).choice(pool)
```

Ключевой момент — пул читается **из env на каждом вызове** (`list_available_providers` не кэширует). Оператор может добавить/убрать провайдера правкой `.env` + `docker compose restart backend` — без миграции кода. Никакого hot-reload внутри процесса нет.

Выбранное имя **персистится** в `Invoice.provider` и `Payment.provider` в момент `/checkout` — чтобы webhook'у не приходилось угадывать, какой провайдер прислал callback: URL сам несёт `provider_name` в path, backend тянет тот же класс, и HMAC проверяется тем же секретом, которым был «выпущен» invoice.

Legacy fallback: если `PAYMENT_PROVIDERS` не задан, читается `PAYMENT_PROVIDER` (single-value), а если и его нет — `cryptobot`. Это важно для апгрейда старых инсталляций.

## Три реализации

### CryptoBot (`cryptobot.py`)

Единственный honest-to-god внешний провайдер. API `@CryptoBot` (Crypto Pay API).

- `create_invoice`: POST `/createInvoice`, `payload: str(invoice_id)`. Валюта различается по коду (аудит #115): крипто-ассеты (`USDT`/`TON`/`BTC`/…, и исторический `USD`→`USDT`) уходят как `currency_type=crypto` + `asset`; всё остальное (`RUB`/`EUR`/…) — как `currency_type=fiat` + `fiat=<код>` (топап-путь `webapp_topup` шлёт `RUB` напрямую без конвертации). Возвращает `pay_url` и `invoice_id` провайдера → в `ProviderInvoice`.
- `verify_webhook`: подпись в заголовке `Crypto-Pay-Api-Signature`. Секрет считается как `sha256(token)` (не сам токен!), затем `HMAC-SHA256(raw_body)`. `cryptobot.py:88-92`. Распаковывается `payload.payload` — это наш же `invoice_id`, отправленный на этапе create.
- Статусы: `invoice_paid → "paid"`, `invoice_expired → "expired"`, всё остальное → `"other"`.
- Токен передаётся в `Crypto-Pay-API-Token` header, **не в query** (`cryptobot.py:33-34`). Явный комментарий: «never in query strings».

### Telegram Stars (`telegram_stars.py`)

Особый случай: платёж идёт **внутри** Telegram, без внешнего pay-page.

- `create_invoice`: POST `api.telegram.org/bot<token>/createInvoiceLink` с `currency="XTR"`, `payload=str(invoice_id)`, `prices=[{amount: stars}]`. Возвращает `t.me/$…` ссылку как `pay_url`. `external_id` — наш же `invoice_id`, потому что Stars не выдаёт отдельного payment_id при создании.
- **Округление вверх**: `stars = max(1, int(amount + 0.999))` (`telegram_stars.py:58-59`). Stars — целые юниты, ни 0.5 XTR, ни 0 XTR быть не может.
- **Title обрезается до 32 символов** (`telegram_stars.py:62-63`) — Bot API жёстко лимитирует, иначе call возвращает 400.

#### Режим 1 (deprecated): shared-secret relay через бота

Старый поток (audit #62 — shared-bearer как единственная аутентификация):

```
user pays → TG шлёт successful_payment → bot handler
     → bot POST backend:/api/payments/webhook/telegram_stars
        + X-Telegram-Stars-Secret: <shared>
```

`verify_webhook` проверял `X-Telegram-Stars-Secret` через `hmac.compare_digest` — классический shared-bearer. Если секрет утёк, фейковый payment проходил.

#### Режим 2 (рекомендуемый): native Telegram webhook

После #62 backend регистрирует себя как Telegram webhook через `setWebhook(url=.../tg-webhook, secret_token=...)`. Telegram сам подтверждает аутентичность обновлений заголовком `X-Telegram-Bot-Api-Secret-Token`.

```
user pays → TG шлёт update на /tg-webhook (backend напрямую)
     → backend проверяет secret_token header
     → если successful_payment (XTR): _mark_invoice_paid_core
       (сбой = error-лог + алерт админам stars_payment_failed; 5xx → не-200, Telegram ретраит — аудит #209)
     → если pre_checkout_query (XTR): валидация инвойса (существует, pending,
       сумма в Stars совпадает) → answerPreCheckoutQuery(ok|ok=False) — аудит #2
     → иначе: forward в bot через BOT_INTERNAL_WEBHOOK_URL
```

Env для активации: `TELEGRAM_WEBHOOK_SECRET_TOKEN`, `TELEGRAM_WEBHOOK_URL`, `BOT_INTERNAL_WEBHOOK_URL`, `BOT_WEBHOOK_PORT`. Бот переходит из polling в webhook-режим (aiohttp-сервер на внутреннем порте).

Старый shared-secret путь (`telegram_stars.py:verify_webhook`) остаётся для backward compat, но `TELEGRAM_STARS_WEBHOOK_SECRET` теперь необязательна.

Bot → backend forward описан в `components/bot.md`.

### Generic SBP (`generic_sbp.py`)

Один драйвер для пула «мелких российских SBP/card-агрегаторов», конфигурируемый через env.

Адресация: `sbp:<slug>`. `get_provider("sbp:robokassa")` читает env-переменные с префиксом `SBP_ROBOKASSA_*`:

```
SBP_<SLUG>_HMAC_SECRET           ← обязательно
SBP_<SLUG>_PAY_URL_TEMPLATE      ← или CREATE_URL, одно из двух обязательно
SBP_<SLUG>_CREATE_URL            ← HTTP create-режим
SBP_<SLUG>_DISPLAY_NAME          ← имя в Payment.provider, default "sbp:<slug>"
SBP_<SLUG>_AUTH_HEADER           ← "Name:Value", для create-call
SBP_<SLUG>_SIGNATURE_HEADER      ← default "x-sbp-signature"
SBP_<SLUG>_PAID_STATUSES         ← default "paid,success,succeeded"
```

Есть **два режима создания инвойса**:

1. **Template mode** — `PAY_URL_TEMPLATE` используется как Python `str.format(invoice_id=..., amount=..., currency=...)`. HTTP-запросов **не делает** — для агрегаторов, которые выдают one-shot static URL'ы out-of-band. Комментарий в коде: «useful for aggregators that hand out one-shot static URLs».
2. **Create-URL mode** — `POST {invoice_id, amount, currency}` на `CREATE_URL`, из ответа достаётся `pay_url` / `external_id`. Из соображений нейтральности `description` **не пересылается** (`generic_sbp.py:94-98`) — агрегатор должен выставлять свою собственную подпись в интерфейсе оплаты.

Верификация webhook'а единообразна для обоих режимов:
- Заголовок `SIGNATURE_HEADER` (case-insensitive lookup).
- `HMAC-SHA256(raw_body, secret).hexdigest()` — сравнение через `hmac.compare_digest`.
- Дальше ищется `payload.invoice_id` или `payload.order_id`, **один** из них обязан совпадать с тем, что мы клали на create.
- Маппинг статусов: `paid_statuses → "paid"`, `{failed, canceled, cancelled, declined} → "failed"`, `{expired, timeout} → "expired"`, остальное → `"other"`.

«Mock aggregator» в тестах использует именно этот driver — `tests/test_payment_providers.py` гоняет generic_sbp с поддельными env'ами. Сквозной конвейер «webhook → `_mark_invoice_paid_core` → подписка/баланс» покрыт интеграционно в `tests/test_auditfix_api_invoices_py.py` (аудит #186): реальные POST на `/api/payments/webhook/sbp:*` с HMAC-подписью тела, идемпотентность повторной доставки, mark_paid, topup + реферальный бонус, ветки ошибок 400/401/404.

### Lava.top (`lava_top.py`, Stage 9b)

Карты РФ (МИР/Visa/MC) + СБП через lava.top (LAVALANE LTD). Полный
контекст выбора и рисков — `docs/PLAN_LAVA_TOP.md`.

- `create_invoice`: POST `{LAVA_TOP_API_BASE}/api/v3/invoice`, auth-заголовок `X-Api-Key`. Динамическая сумма работает только у продукта с включённым в кабинете режимом «Цена по запросу через API» (`LAVA_TOP_OFFER_ID`); лимиты платформы 50–1 000 000 ₽. Metadata-поля у платформы нет — наш `invoice_id` едет в `clientUtm.utm_content`, а обязательный email покупателя синтезируется как `inv{invoice_id}@{LAVA_TOP_EMAIL_DOMAIN}`. Из ответа: `id` (contractId) → `external_id`, `paymentUrl` → `pay_url`.
- `verify_webhook`: HMAC у платформы **нет** — она шлёт наш статический секрет `LAVA_TOP_WEBHOOK_SECRET` в заголовке `X-Api-Key` (настраивается в кабинете при добавлении вебхука, тип «API key»). `paid` = `eventType=payment.success` **и** `status ∈ {completed, subscription-active}`; `payment.failed`/`subscription.recurring.payment.failed` → `failed`; остальное → `other`. Событие без `clientUtm.utm_content` (покупка не из нашего backend'а) — warning + `other`/`external_id="0"`, чтобы платформа не ретраила вечно (до 20 попыток).
- Env-цепочка: `LAVA_TOP_API_KEY`/`_OFFER_ID`/`_WEBHOOK_SECRET` из vault (`vault_lava_top_*`), `LAVA_TOP_EMAIL_DOMAIN` — открытый (default: домен фронта).
- **Авто-сверка (вебхук-независимо).** Доставка вебхуков lava — best-effort (до 20 ретраев по докам; в проде наблюдалось, что POST не приходит вовсе — счёт остаётся pending, деньги списаны). Воркер-тик `run_lava_reconcile_tick` (`worker.py`, интервал `LAVA_TOP_RECONCILE_INTERVAL`, default 60с; TICK_IDS/TIMEOUTS в `queue.py`) раз в минуту зовёт `LavaTopProvider.list_recent_invoices()` (`GET /api/v2/invoices`) и зачисляет любой pending-счёт, чья продажа у lava `COMPLETED` (матч по `clientUtm.utm_content` = наш invoice_id), через тот же `_mark_invoice_paid_core`. Идемпотентно: уже-paid счета пропускаются, а если вебхук всё-таки долетит — дедуп по `reference=invoice:{id}`. Сумма сверяется (продажа должна покрывать счёт). Так карта пополняет баланс даже при полностью нерабочем вебхуке. No-op без `LAVA_TOP_API_KEY`. **Воркеру для этого прокинуты все `LAVA_TOP_*` в `worker-env`** (docker-compose), иначе `get_provider("lava_top")` в тике вернёт not_configured.

### Tribute (`tribute.py`, Stage 9b)

Tribute Shop API (tribute.tg, TRBT Limited): карты (браузерная ссылка), СБП, Stars.

- `create_invoice`: POST `{TRIBUTE_API_BASE}/shop/orders`, заголовок `Api-Key`. Сумма — **int в копейках/центах** (драйвер конвертирует из рублёвого float). `title`/`description` обязательны у платформы — берутся нейтральные строки из `TRIBUTE_ORDER_TITLE`/`_DESCRIPTION` (дефолт «Пополнение баланса», Stage 9d-нейтральность), описание счёта не пересылается. `customerId` = наш `invoice_id` (round-trip), `uuid` заказа → `external_id`. `pay_url` = `paymentUrl` (браузер; приоритетнее `webappPaymentUrl` — карты за цифровые услуги внутри Telegram нарушают Stars-only правило Bot ToS §6.2).
- `verify_webhook`: заголовок `trbt-signature` = HMAC-SHA256 сырого тела, ключ — сам `TRIBUTE_API_KEY` (отдельного секрета нет). Кодировка в доке не зафиксирована — принимаются hex и base64. `paid` = **только** событие `shop_order` со `status=paid`; промежуточный `shop_order_payment_received` («фиат получен, ждём финала») намеренно → `other`; `shop_order_payment_failed`/`_cancelled`/`_refunded` → `failed`. Событие без `customerId` — warning + `other`/`"0"` (заказ не из нашего backend'а).

Оба драйвера рублёвые: `_convert_for_provider` пропускает их суммы без конвертации, сверка суммы вебхука работает из коробки. `_provider_invoice_id_from_event` понимает их raw (lava: `contractId` на верхнем уровне; tribute: `payload.uuid`). Юнит-тесты — `tests/test_payments_lava_top.py` (оба драйвера: построение запроса, подписи/секреты, маппинг событий, get_provider-диспатч).

### Выбор способа оплаты в боте (Stage 9b)

`PAYMENT_PROVIDER_CHOICES` (env бота, comma-separated имена провайдеров): при 2+ значениях бот после создания счёта показывает меню способов («⭐ Telegram Stars / 💳 Карта РФ / …»), checkout происходит в callback'е `payvia:{kind}:{invoice_id}:{provider}` выбранным провайдером. Кнопки способов остаются в клавиатуре после выдачи pay-ссылки — неудавшийся способ (антифрод агрегатора) можно сменить, каждый выбор создаёт свою Payment-строку (#117 матчит оплаченную). Пусто/одно имя — старое поведение (`PAYMENT_PROVIDER` без меню).

## `/api/invoices/{id}/checkout` — создание инвойса

`backend/app/api.py:2578-2636`. Минимальный путь:

1. Найти `Invoice`, проверить `status == pending`.
2. `provider = get_provider(body.provider or None)` — `None` означает «выбери из пула».
3. Конвертация валюты (#108): RUB-счёт приводится к валюте провайдера **до** `create_invoice` — для `telegram_stars` через `_rub_to_stars` (курс `WEBAPP_STARS_PER_RUB`, тот же, что в WebApp), для `cryptobot` через `CRYPTOBOT_RUB_PER_USDT` (не задан → 503, счёт не создаётся). SBP и уже сконвертированные счета (XTR/USDT) проходят как есть. `Payment`-строка при этом хранится в валюте `Invoice` (RUB).
4. `provider.create_invoice(invoice_id, amount, currency, return_url)`.
5. **Перед** ответом клиенту создать `Payment(status=pending, provider=provider.name, external_id=provider_invoice.external_id)`. Это — то, что webhook потом найдёт по `(invoice_id, provider)`.
6. `db.commit()`, вернуть `pay_url`.

`return_url` передаётся только в CryptoBot и только как `paid_btn_url` (кнопка «Return to bot» после оплаты). Stars игнорирует, SBP — тоже, потому что его UX мы не контролируем.

Особенность: **один invoice может получить несколько Payment-строк**. Если пользователь дважды нажал checkout, каждый вызов создаст свой `Payment` с новым `external_id` (id счёта на стороне провайдера). Webhook помечает `paid` именно ту строку, которую реально оплатили: среди `pending`-строк по `(invoice_id, provider)` ищется та, чей `external_id` совпал с provider invoice id из события (извлекается из `event.raw`, т.к. `event.external_id` — это НАШ внутренний invoice id), с фолбэком на последнюю `pending`, а если pending-строк нет — на последнюю любую (#117, см. ниже).

## `/api/payments/webhook/{provider_name}` — приём callback'а

`backend/app/api.py:2639-2697`. Единственный unauthenticated route в admin-surface'е (по FastAPI). Эндпоинт `async`, но вся работа с БД (запрос `Payment` + `_mark_invoice_paid_core` с `with_for_update`) уходит в threadpool через `asyncio.to_thread` (#199) — иначе row-lock на инвойсе замораживал бы event loop всего процесса:

```
POST /api/payments/webhook/cryptobot
POST /api/payments/webhook/telegram_stars
POST /api/payments/webhook/sbp:robokassa
```

Поток обработки:

```
                           body: bytes
                           headers
                                │
                                ▼
               get_provider(provider_name)     ← raises 404 on unknown
                                │
                                ▼
               provider.verify_webhook(body)   ← raises 401 on bad sig
                                │
                                ▼
            WebhookEvent{status, external_id}
                                │
               ┌────────────────┼──────────────┐
               │                │              │
               ▼                ▼              ▼
         status=="paid"  status=="expired"  status=="other"
               │            log & return  log & return
               ▼
       invoice_id = int(external_id)
               │
               ▼
       сверка суммы/валюты (#111):
         expected = _convert_for_provider(invoice.amount, invoice.currency, provider)
         if event.amount is not None and (недоплата или валюта≠) →
             notify_admins + HTTPException 409, счёт остаётся pending
               │
               ▼
       pending_payment = SELECT p FROM payments             (#117)
             WHERE invoice_id=? AND provider=? AND status='pending'
             ORDER BY id DESC
             → предпочесть p.external_id == provider_invoice_id(event.raw)
             → иначе последнюю pending
             → иначе (нет pending) последнюю любую
               │
               ▼
       _mark_invoice_paid_core(
         invoice_id,
         actor=f"{provider.name}:webhook",
         actor_type=system,
         payment_id=pending_payment.id,
       )
```

Ключевые моменты:

- **401 на плохую подпись.** `ProviderError` из `verify_webhook` превращается в HTTPException 401. Провайдеры не различают «нет секрета» и «плохая подпись» — оба кейса это «не пускаем».
- **Idempotency защитой инвойса.** `_mark_invoice_paid_core` берёт `SELECT ... FOR UPDATE` на `Invoice`, и если `status == paid` — возвращает уже готовый результат без повторного провижининга. Повтор webhook'а (CryptoBot иногда шлёт два раза при таймауте) не создаёт дубликата credential'ов.
- **Только `"paid"` обрабатывается.** `expired` / `other` логируются и возвращают 200 — это нужно, иначе провайдер решит, что webhook не доставлен, и будет ретраить до бесконечности.
- **Actor зашивается как `<provider>:webhook`** — чтобы в `audit_logs` было видно, какой провайдер инициировал переход в paid. Actor type = `system`, не `admin`/`bot`/`user`.
- **Сверка суммы и валюты (#111).** Перед зачислением webhook сравнивает `event.amount`/`event.currency` с ожидаемой суммой счёта. Счёт хранится в рублях, а провайдер присылает свою валюту (XTR/USDT), поэтому ожидание считается тем же `_convert_for_provider`, что и в `/checkout`, и сравнивается уже в валюте провайдера (синоним `RUR`≡`RUB`). Недоплата (сверх допуска в копейку) или несовпадение валюты → `HTTPException 409` + `notify_admins(kind="payment_amount_mismatch")`, счёт остаётся `pending`. Переплата зачисляется, но пишет `warning`. Если `event.amount` не пришёл (template-режим SBP не фиксирует сумму) — сверять нечего, зачисляем как раньше.

## Взаимодействие с `_mark_invoice_paid_core`

Эта функция — единственный способ перевести инвойс в `paid`. Её вызывают:

| Caller | Actor | Actor type |
|---|---|---|
| `/api/invoices/{id}/mark_paid` (admin UI / `/invoices` в боте) | `X-Admin-Actor` header | `admin` / `bot` |
| `/api/invoices/batch/mark_paid` | то же | то же |
| `/api/payments/webhook/*` | `"<provider>:webhook"` | `system` |

Внутри есть три ветки (по `invoice.kind` и `invoice.action`):

1. **`kind = "topup"`** (`api.py:1845-1917`) — кредитнуть `balance_kopecks`, никакого провижининга. Дополнительно срабатывает **referral payout**: если это первый `kind=topup` у пользователя, и у него есть `referred_by_id`, — начисляем `REFERRAL_BONUS_KOPECKS` реферреру. Защита от double-pay — по `BalanceTransaction.reference = "referral_payout:{user_id}"`. Referral-payout wrapped в `try/except` (`api.py:1882-1890`): если начисление упало, топап пользователя всё равно пройдёт.

2. **`action = "renewal"`** — продлить существующую подписку: `expires_at += plan.duration_days`, статус → `active`. Провижининг не нужен — credential'ы уже есть.

3. **`action = "new_subscription"` (default)** — вызов `_create_subscription_for_user(user, plan)`, который уходит в `ProvisioningOrchestrator.create_subscription` (попытка через warm pool, fallback на cold path).

Fallback на «latest subscription by (user_id, plan_id)» при уже-paid инвойсе описан в `components/backend-api.md` — и упомянут как неясное место (может отдать чужую подписку при collision'е на уровне plan+user).

## Balance ledger — где лежат деньги

Платежи — одна часть биллинга; вторая — **пользовательский wallet** (`balance_kopecks`) в `users` + таблица `balance_transactions`. Провайдеры напрямую wallet **не трогают**: всегда через `services/balance.py` (`topup`, `activate_subscription`, `renew_subscription`).

- `topup(user_id, amount_kopecks, reference)` — `SELECT user FOR UPDATE`, `user.balance_kopecks += amount`, вставка `BalanceTransaction(kind=topup)` (`balance.py:134-158`). Идемпотентность — **нет**, уникальности на `reference` нет, caller обязан проверять сам (как делает `_mark_invoice_paid_core` через `SELECT FOR UPDATE` на Invoice).
- `activate_subscription(user, sub, reference)` — `SELECT user FOR UPDATE`, проверка `balance >= plan_price`, дебет + запись `BalanceTransaction(kind=spend)`, установка `sub.expires_at = now + duration_days`, `auto_renew = True` (`balance.py:220-264`). На insufficient balance — `raise ValueError`, caller переводит в HTTP 402.
- `renew_subscription(sub)` — вызывается воркером из `run_balance_charge_tick`. Успех — `expires_at += duration_days`, сбой — возвращает `False` (caller помечает sub `expired`).

Все мутации баланса — через `_lock_user` (row-level lock). Гонка «topup + renew в один момент» невозможна: один из них подождёт.

Метрики биллинга:
```
vpn_balance_charges_total{result="success|insufficient"}   Counter
vpn_balance_topups_total{kind}                              Counter
vpn_balance_charge_amount_kopecks_total                     Counter
vpn_balance_subs_frozen                                     Gauge
```

## Freeze / unfreeze

Отдельная ручка в `balance.py:382-483` и два метода, но это не столько про деньги, сколько про пользовательский UX. Правила зафиксированы в коде:

- Один freeze на календарный год (`has_frozen_this_year` + `frozen_year`, сбрасывается при смене года).
- Длительность — `FREEZE_DAYS` (default 7, env).
- `expires_at += FREEZE_DAYS` — пользователь не теряет оплаченное время.
- **Devices физически revoked** через orchestrator: нода перестанет принимать эти credential'ы.
- Early unfreeze разрешён — `reprovision_subscription` создаёт новые devices, но `has_frozen_this_year` **не сбрасывается**, второй freeze в этом же году отклоняется.

## Модель доверия

```
                 attacker
                    │
                    │ forges POST /api/payments/webhook/cryptobot
                    ▼
       ┌─────────────────────────────┐
       │ provider.verify_webhook(...)│
       │   HMAC-SHA256 check          │
       │   401 на mismatch            │
       └─────────────────────────────┘
                    │ valid signature
                    ▼
       _mark_invoice_paid_core (row lock)
                    │
                    │ idempotency by invoice status
                    ▼
               balance.topup / provisioning
```

Единственная защита — HMAC-секрет. Его нельзя логировать (`CryptoBotProvider` кладёт его в header session, не в аргументы route; `telegram_stars` использует compare_digest). Утечка секрета = возможность заливать себе баланс без реальной оплаты, так как backend поверит в любой «paid» webhook с правильной подписью.

## Схемы per-provider за одной диаграммой

```
┌──────────┐   HMAC-SHA256(body, sha256(token))
│ CryptoBot│──────────────────────────────────────┐
└──────────┘   header: Crypto-Pay-Api-Signature   │
                                                  │
┌──────────┐   hmac.compare_digest(shared_secret) │
│   Stars  │──────────────────────────────────────┤
└──────────┘   header: X-Telegram-Stars-Secret    │
   ↑                                              ▼
   │  forward                              /api/payments/
   │  successful_payment                    webhook/<prov>
   │                                              ▲
bot/handlers.py                                   │
                                                  │
┌──────────┐   HMAC-SHA256(raw_body, SBP_X_HMAC)  │
│generic_sbp│─────────────────────────────────────┘
└──────────┘   header: SBP_X_SIGNATURE_HEADER
                        (default x-sbp-signature)
```

## ⚠️ Неясные места

- **Random rotation без веса.** `pick_provider_name` — чистый `random.choice`. Нельзя настроить «80% CryptoBot, 20% SBP», нельзя выключить провайдер для конкретного плана, нельзя упасть обратно на резерв при сбое. Если CryptoBot лёг — `/checkout` будет рандомно успех/502 пока оператор не поправит `.env`.
- **Дубли `Payment` при двойном checkout.** Один `Invoice` может получить несколько `Payment`-строк (каждый checkout создаёт свою с уникальным provider `external_id`). Webhook помечает `paid` ту строку, чей `external_id` совпал с provider invoice id из события (#117), поэтому сверка с провайдером сходится; но неоплаченные дубли так и остаются `pending` навсегда — отдельного чистильщика нет.
- **Webhook rate-limit 30/min** (`api.py:2640`) — общий на все провайдеры. Если CryptoBot начнёт агрессивно ретраить, он съест budget SBP'шных уведомлений. Индивидуальных лимитов нет.
- **Generic SBP template mode.** `PAY_URL_TEMPLATE` — чистый `str.format`, без проверки, что полученный URL вообще валиден для HTTP. Опечатка в env → пользователь получит битую ссылку без ошибки на стороне backend'а.
- **`verify_webhook` у Stars принимает любой currency только через ручную проверку.** `raise` срабатывает только если `sp.currency != "XTR"` — а если поле отсутствует, используется fallback `"XTR"` (`telegram_stars.py:114`). Это нужно, потому что forward от бота иногда не содержит currency, но делает провайдер чуть слепее, чем хотелось бы.
- **Referral-payout на `kind=topup` не атомарен с самим топапом.** Оба идут внутри одной транзакции `_mark_invoice_paid_core`, но обёрнуты разными `try/except`: ошибка бонуса не откатывает топап, но ошибка топапа откатывает бонус через общий rollback. Комментарий в коде сам это признаёт: «Payout will be retried by a nightly reconciliation if we ever add one; for now it's fire-and-forget».

> ✅ Исправлено (#62): native Telegram webhook (`/tg-webhook`) заменяет shared-bearer. Старый polling-relay deprecated.
