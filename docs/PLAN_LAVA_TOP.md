# Plan: Приём карт РФ / СБП через Lava.top + Tribute (закрытие Stage 9b)

> **Status (2026-07-21, вечер): КОД РЕАЛИЗОВАН** — по решению владельца
> подключаются СРАЗУ ДВА провайдера (`lava_top` + `tribute`) с меню выбора
> способа оплаты в боте (`PAYMENT_PROVIDER_CHOICES`). Драйверы:
> `backend/app/services/payments/{lava_top,tribute}.py`, тесты
> `backend/tests/test_payments_lava_top.py`, актуальное описание —
> `docs/components/payments.md` (разделы Lava.top / Tribute / выбор способа).
> API-ключи обоих провайдеров проверены живыми read-only запросами: валидны.
>
> **Блокеры деплоя (кабинеты, руками владельца):**
> 1. lava.top: создать продукт с «Ценой по запросу через API» (продуктов в
>    аккаунте 0 — проверено GET /api/v2/products) → offerId; настроить
>    вебхук (URL + секрет, тип «API key»); включить RUB в платёжных
>    настройках; KYC для вывода.
> 2. Tribute: создать магазин (API отвечает 404 "shop not found" — проверено);
>    настроить вебхук на /api/payments/webhook/tribute.
>
> Ресёрч ниже адверсариально верифицирован по первоисточникам (оферта,
> OpenAPI-спека, FAQ) и сохранён как обоснование решений.
>
> **Прод-инцидент 2026-07-22 (первый боевой платёж):** карта прошла (lava:
> «успешная продажа», sale COMPLETED, `clientUtm.utm_content="48"` корректен),
> но баланс не пополнился — **вебхук lava не долетел** (счёт остался pending,
> в логах 0 строк). Наша сторона доказанно рабочая: зонд с секретом `P3HO…` →
> 200, ручная доставка `payment.success` → зачислила счёт 48. Причина —
> доставка вебхука на стороне lava: вебхук привязан к API-ключу `RU06p…`, а
> счета создаются ключом `Egojo…` (наш `LAVA_TOP_API_KEY`); у lava вебхуки
> висят под конкретным ключом → на «чужой» инвойс не шлётся (ведущая версия).
>
> **Фикс (одобрен юзером):** авто-сверка — воркер-тик `run_lava_reconcile_tick`
> (`LAVA_TOP_RECONCILE_INTERVAL`, default 60с) опрашивает `GET /api/v2/invoices`
> ключом `Egojo…` и зачисляет pending-счета с COMPLETED-продажей. Карта теперь
> работает НЕЗАВИСИМО от вебхука. Вебхук как мгновенный канал чинится отдельно
> (привязать под ключ `Egojo`), но больше не блокирует. См.
> `docs/components/payments.md` § «Авто-сверка».
>
> **Ревью (multi-agent, 2026-07-21):** 8 подтверждённых находок, все
> починены до коммита:
> 1. [major] payvia падал с AttributeError на сообщениях >48ч
>    (`InaccessibleMessage` без `edit_reply_markup`) → guard + отправка
>    ссылки новым сообщением.
> 2. Нечисловой round-trip id (чужой `utm_content`/`customerId` на том же
>    аккаунте) уходил как `paid` → `int()` 400 → вечные ретраи. Теперь
>    ack как `other`/`"0"` (оба драйвера).
> 3. `verify_webhook` падал 500 на валидном-JSON-не-объекте (`[]`/`"ping"`)
>    → `isinstance(payload, dict)` guard → ProviderError/401.
> 4. IDOR: payvia чекаутил любой `invoice_id` из подделываемой
>    callback_data → allowlist провайдера + backend ownership-проверка по
>    `telegram_id` (новое поле `InvoiceCheckoutRequest.telegram_id`).
> 5. Двойная оплата (два способа за один счёт) молча зачислялась → алерт
>    админам `payment_double_paid` в webhook-хендлере.
> 6. `*_API_BASE` были в env-reference, но не прокинуты в
>    compose/env.j2 → прокинуты.
> Тесты на все фиксы — в `test_payments_lava_top.py` /
> `test_auditfix_api_payments_py.py`.

## Инцидент 2026-09-19: карта у PAY2ME закрыта → две кнопки вместо одной

**Симптом.** Все счета lava падали: create отвечал HTTP 400 «Restricted
payment method type», человек в кабинете видел «Не удалось создать счёт:
502». Последний успешный платёж lava — **24.08**, за весь сентябрь платежей
картой/СБП нет ни одного.

**Причина.** Мы слали единый счёт «Карта РФ / СБП»: `paymentProvider=PAY2ME`
и намеренно **без** `paymentMethod` — агрегатор сам давал выбрать способ на
своей странице. lava закрыл у PAY2ME приём карт, а без явного метода PAY2ME
берёт карту по умолчанию → отбой на каждом счёте, включая тех, кто хотел СБП.

**Матрица, снятая живыми запросами с прод-ключом:**

| тело create | ответ |
|---|---|
| `paymentProvider=PAY2ME`, метод не указан | 400 «Restricted payment method type» |
| `PAY2ME` + `paymentMethod=CARD` | 400 «Restricted payment method type» |
| `PAY2ME` + `paymentMethod=SBP` | 201 |
| `SMART_GLOCAL` (+ `paymentMethod=CARD`) | 201 |

**Решение (вариант владельца «две кнопки»).** Интеграция lava остаётся одна —
один API-ключ, один `offerId`, один секрет и один вебхук-URL
`/api/payments/webhook/lava_top`. Разведены только **имена провайдера**, и
способ теперь шлётся ЯВНО:

| имя провайдера | эквайрер (env) | `paymentMethod` | подпись кнопки |
|---|---|---|---|
| `lava_top` | `LAVA_TOP_CARD_PROVIDER`, дефолт `SMART_GLOCAL` | `CARD` | 💳 Карта РФ |
| `lava_top_sbp` | `LAVA_TOP_SBP_PROVIDER`, дефолт `PAY2ME` | `SBP` | 🏦 СБП |

Что из этого следует по коду:

- `get_provider` (`services/payments/base.py`) знает оба имени и поднимает
  один и тот же `LavaTopProvider` через `load_lava_top_env("card"|"sbp")`;
  имя инстанса подменяется на выбранное (`Payment.provider`).
- `LAVA_TOP_PAYMENT_PROVIDER` **больше не читается** — удалена из
  `docker-compose.yml` и `env.j2`. Старые строки `Payment` с именем
  `lava_top` остаются валидными: имя карточной кнопки не менялось.
- Вебхук приходит на `/webhook/lava_top`, а строка СБП-платежа записана как
  `lava_top_sbp` → `api/payments.py` ищет pending-платежи по **семейству**
  имён `LavaTopProvider.family = ("lava_top", "lava_top_sbp")`, а тик сверки
  в `worker.py` фильтрует по тому же кортежу (`_LAVA_PROVIDERS`).
- Точки выбора способа: бот — `PAYMENT_PROVIDER_CHOICES`
  (`telegram_stars,lava_top_sbp,lava_top`, `group_vars/web/main.yml`);
  кабинет — `TopupModal` (`Home.tsx`) и `TopupHintSheet` (`Plans.tsx`);
  страница `?fix=1` — «Продлить по СБП…» (`&pay=sbp` → `SUB_FIX_SBP_PROVIDER`,
  дефолт `lava_top_sbp`) и «Продлить картой…» (`&pay=1` → `SUB_FIX_PROVIDER`,
  дефолт `lava_top`).

**Диагностика на будущее** (runbook, §6): 400 «Restricted payment method
type» = у эквайрера закрыт этот способ, а не проблема ключа/суммы. Проверять
`POST /api/v3/invoice` прод-ключом с ЯВНЫМ `paymentMethod` по каждому
эквайреру и смотреть, какая пара ещё отвечает 201.

## Контекст

Платёжная подсистема уже принимает автоматические платежи: CryptoBot (USDT),
Telegram Stars, и есть универсальный слот `GenericSBPProvider` (Stage 9c),
написанный «под будущий рублёвый агрегатор». Сам выбор агрегатора («Stage 9b —
the actual aggregator hunt», docstring `generic_sbp.py`) был отложен как
non-code задача и не завершён. Этот эпик закрывает его: карты РФ (МИР/Visa/MC)
и СБП для пользователей — через Lava.top.

Lava.top не ложится в generic_sbp (другая схема create: `offerId` + `email`,
вебхук без HMAC, нестандартный round-trip invoice_id) → пишем полноценный
драйвер `lava_top.py` по образцу `cryptobot.py`.

## Верифицированные факты о Lava.top (июль 2026)

Источники: [OpenAPI-спека](https://gate.lava.top/docs/documentation.yaml) v1.22.0,
[developers.lava.top](https://developers.lava.top/ru), faq.lava.top,
[оферта](https://lava.top/docs/terms) (ред. 18.07.2026). Скачанная спека:
проверялась воркфлоу-агентами, все пункты ниже подтверждены адверсариальной
проверкой.

| Аспект | Факт |
|---|---|
| Оператор | LAVALANE LTD, Кипр (HE 387079); продавец-физлицо ок, ИП/самозанятость не нужны; KYC по паспорту для вывода |
| Комиссия | 8% с продажи |
| Валюты/методы | RUB: карты МИР/Visa/MC банков РФ + СБП (провайдеры SMART_GLOCAL, PAY2ME); USD/EUR: зарубежные карты/PayPal. **2026-09: карта у PAY2ME закрыта — см. «Инцидент 2026-09-19»** |
| Динамическая сумма | POST `/api/v3/invoice` с полем `amount` — только для продукта с включённым в кабинете режимом **«Цена по запросу через API»** (`offerId` обязателен всегда). Лимиты: 50–1 000 000 ₽ |
| Auth API | Заголовок `X-Api-Key` (кабинет → Интеграции → Public API); rate limit 50 rps |
| Вебхук | **HMAC-подписи НЕТ.** Basic или свой статический секрет в заголовке `X-Api-Key` (настраивается в кабинете). Payload: `eventType, contractId, amount, currency, status, buyer.email, clientUtm`. Успех: `payment.success` + `status=completed`. До 20 попыток доставки (1s/5s/15s, ~11×1мин, 5×1ч). Исходящий IP `158.160.60.174` |
| Привязка платежа | **Нет поля metadata/orderId.** Сквозной канал произвольных данных — только `clientUtm` (возвращается в вебхуке) + `contractId` из ответа на create |
| Email покупателя | **Обязателен** при создании инвойса (идентификатор клиента у lava.top) |
| VPN в запрещённых категориях | **Не упомянут** ни в оферте (п. 8.2.2), ни в FAQ «Что нельзя продавать» (grep полного текста — 0 вхождений vpn/proxy/обход). Но п. 8.2.5: применение правил «исключительно по усмотрению Компании» |
| Модерация/холды | Продукт модерируется ≤24–48 ч; **первый вывод: верификация + минимум 3 реальные продажи + проверка до 7 раб. дней** (в отзывах — растягивалось до месяца с блокировкой и возвратом денег покупателям) |
| Вывод | Карты РФ ≤250 000 ₽/мес на карту; СБП ≤200 000 ₽/мес; USDT (TRC-20 и др.) от $20. Чеков ФЗ-54 нет, налоги декларирует продавец |
| Антифрод | Официально требует от **покупателя** отключать VPN при оплате/верификации — для нашей аудитории источник фейлов конверсии |

## Решения

| # | Решение | Why |
|---|---|---|
| provider | Отдельный драйвер `lava_top.py`, не generic_sbp | Схема create/webhook не совпадает с generic-контрактом |
| currency | Только RUB в v1 | `_convert_for_provider` пропускает RUB как есть — нулевые правки конвертации |
| round-trip | `invoice_id` → `clientUtm.utm_content` при create; вебхук достаёт его оттуда → `WebhookEvent.external_id` | Единственный сквозной канал у lava.top; паттерн идентичен `payload` у CryptoBot |
| contractId | Из ответа create → `ProviderInvoice.external_id` (→ `Payment.external_id`); в вебхуке из `raw` → матчинг Payment-строки | Как provider invoice id у CryptoBot |
| webhook auth | Наш длинный случайный секрет (≤80 симв.) как ApiKeyWebhookAuth в кабинете; в драйвере `hmac.compare_digest` с заголовком `X-Api-Key` | HMAC у платформы нет; сверка суммы в `payment_webhook` (#аудит) уже страхует |
| email | Синтетический `inv{invoice_id}@{LAVA_TOP_EMAIL_DOMAIN}` | Email юзеров у нас нет; per-invoice адрес не сцепляет покупателей в одного «клиента» антифрода. **Open:** какой домен (см. вопросы) |
| recurring | Не используем (v1 — разовые) | Динамическая цена документирована только для разовых покупок |
| rollout | В ротацию `PAYMENT_PROVIDERS` НЕ добавляем сразу; канарейка через явный `body.provider="lava_top"` в checkout | Ротация — random.choice, сразу отдала бы 1/N трафика непроверенному каналу |

## Дизайн драйвера `backend/app/services/payments/lava_top.py`

```
class LavaTopProvider:
    name = "lava_top"

    create_invoice(*, invoice_id, amount, currency, description=None, return_url=None):
        POST {LAVA_TOP_API_BASE:-https://gate.lava.top}/api/v3/invoice
        headers: X-Api-Key: <LAVA_TOP_API_KEY>
        body: {
          email: f"inv{invoice_id}@{LAVA_TOP_EMAIL_DOMAIN}",
          offerId: <LAVA_TOP_OFFER_ID>,
          currency: "RUB",            # assert currency == "RUB", иначе ProviderError
          amount: round(amount, 2),   # 50 ≤ amount ≤ 1_000_000 — иначе 4xx от платформы
          clientUtm: {"utm_content": str(invoice_id)},
        }
        ← 201 {id (contractId), status, amountTotal, paymentUrl}
        return ProviderInvoice(external_id=contractId, pay_url=paymentUrl, ...)

    verify_webhook(body, headers):
        1. header X-Api-Key (case-insensitive) vs LAVA_TOP_WEBHOOK_SECRET через
           hmac.compare_digest; нет/не совпал → ProviderError (→ 401)
        2. json body → eventType/status:
           payment.success + status=completed → "paid"
           payment.failed                     → "failed"
           subscription.* / прочее            → "other"
        3. external_id = clientUtm.utm_content (наш invoice_id);
           отсутствует → ProviderError (лучше 401, чем слепой матч)
        4. amount/currency из payload → сверка суммы в payment_webhook работает
           из коробки (RUB без конвертации)
        return WebhookEvent(external_id, status, amount, currency, raw=payload)
```

Регистрация: ветка `if name == "lava_top":` в `get_provider`
(`base.py:87-119`), ленивый импорт, отсутствие `LAVA_TOP_API_KEY` /
`LAVA_TOP_OFFER_ID` / `LAVA_TOP_WEBHOOK_SECRET` → `ProviderError`.

Проверить: `_provider_invoice_id_from_event` (`api/payments.py:29`) — научить
доставать `contractId` из `event.raw`, чтобы матчинг Payment-строки по
provider invoice id работал (как у CryptoBot).

Ключевой инвариант webhook-роута: `int(event.external_id)` == наш
`invoice_id` (`payments.py:255-257`) — дизайн выше его соблюдает.

## Env-цепочка (по правилу: compose + env.j2 + defaults + group_vars + vault)

Новые переменные: `LAVA_TOP_API_KEY` (secret), `LAVA_TOP_OFFER_ID`,
`LAVA_TOP_WEBHOOK_SECRET` (secret, генерим сами), `LAVA_TOP_EMAIL_DOMAIN`,
опц. `LAVA_TOP_API_BASE` (для тестов/стейджинга).

1. `docker-compose.yml` — оба сервиса (backend ~48-54, второй ~318-323).
2. `infra/ansible/roles/deploy_app_stack/templates/env.j2` (~31-36).
   ⚠️ Не копировать существующий пробел: `CRYPTOBOT_TOKEN`/`CRYPTOBOT_RUB_PER_USDT`
   в env.j2 отсутствуют — для lava_top прокинуть цепочку полностью.
3. `infra/ansible/roles/deploy_app_stack/defaults/main.yml` (~46-51).
4. `infra/ansible/group_vars/web/main.yml` — маппинг `vault_lava_top_* → deploy_app_stack_lava_top_*`.
5. `infra/ansible/group_vars/web/vault.yml.example` + прод-vault.

## Тесты

`backend/tests/test_payments_lava_top.py` по образцу
`test_payment_providers.py` (cryptobot: 64-129) + `test_payments_rotation_sbp.py`:
- create: happy-path (мок HTTP, проверка body: email/offerId/amount/clientUtm),
  не-RUB → ProviderError, HTTP 4xx → ProviderError, отсутствие paymentUrl.
- webhook: happy-path paid, wrong/missing X-Api-Key → ProviderError,
  payment.failed → "failed", subscription.recurring.* → "other",
  отсутствие clientUtm → ProviderError, малформленный JSON.
- get_provider("lava_top") диспатч + missing-env.
- Прогон: docker-harness (audit-pg:55432), один прогон за раз.

Бот и webapp: правок НЕ требуется (pay_url — обычная inline-URL-кнопка,
`handlers.py:523-524`). Webapp-топап хардкодит `"telegram_stars"`
(`Home.tsx:738`, `Plans.tsx:258`) — добавление выбора «оплата картой» в UI
— отдельная задача, только с явного одобрения (правило проекта).

## Rollout

0. **Владелец** (см. следующий раздел): аккаунт, продукт, ключи, вебхук.
1. Код в `dev` + тесты + ruff/ansible-lint/CI.
2. `git tag pre-lava-top` на прод-коммите + бэкап БД:
   `ansible-playbook playbooks/db_backup.yml --vault-password-file ~/.vpn_vault_pass`.
3. Vault-правки на проде, деплой `--tags app`.
4. Smoke (канарейка, ротация не тронута): checkout с явным
   `provider="lava_top"` на 50–100 ₽ со своего аккаунта → оплата картой →
   вебхук → `Invoice.paid` + зачисление; повтор вебхука → идемпотентный 200.
5. Наблюдение за холдом первых 3 продаж; вывод средств часто, остаток
   минимальный.
6. Решение о добавлении в ротацию `PAYMENT_PROVIDERS` (читается на лету,
   рестарт не нужен) и/или кнопка в боте/webapp — после успешной канарейки.

## Риски и митигации

| Риск | Митигация |
|---|---|
| Модерация «по усмотрению», прецеденты блокировок с возвратом денег | Нейтральное имя/описание продукта (без слов «VPN», «обход блокировок»); блок «После оплаты» реально выдаёт доступ; частый вывод, минимальный остаток; CryptoBot/Stars остаются в ротации как несгораемый канал |
| Антифрод lava.top требует у покупателя выключенный VPN — а наш покупатель почти всегда под нашим же VPN | Подсказка в боте у кнопки оплаты («если платёж не проходит — временно отключите VPN»); мониторить конверсию канарейки до включения в ротацию |
| Холд первых ≥3 продаж до 7 раб. дней (бывает дольше) | Смоук-платежи со своего аккаунта; не рассчитывать на этот канал как основной до прохождения проверки |
| Вебхук без HMAC (статический секрет) | Длинный случайный секрет, constant-time сравнение; сверка суммы уже в `payment_webhook`; опц. allowlist исходящего IP `158.160.60.174` на nginx (задокументировать, не хардкодить — IP может смениться) |
| Реклама/популяризация VPN — ст. 14.3 ч.18 КоАП (с 01.09.2025, юрлица 200–500 тыс. ₽; УФАС уже штрафует за ссылки в TG) | Публичная витрина продукта на lava.top — нейтральная формулировка услуги; продажа как таковая не запрещена |
| Лимит вывода 250 тыс. ₽/мес на карту РФ | Вывод в USDT как альтернативный канал |
| Нет чеков ФЗ-54, ФНС-отчётности | Осознанный выбор владельца; налоги декларируются самостоятельно |

## Что нужно от владельца (блокеры старта деплоя)

1. Аккаунт lava.top + KYC-верификация (паспорт, для будущего вывода).
2. Продукт типа «Цифровой продукт» с включённым **«Цена по запросу через
   API»**, нейтральное название/описание; заполнить «После оплаты».
   → получить `offerId` (UUID цены).
3. Кабинет → Интеграции → Public API → **API key**.
4. Кабинет → Интеграции → Добавить Webhook:
   URL `https://<prod-домен>/api/payments/webhook/lava_top`,
   тип ApiKeyWebhookAuth, секрет — сгенерированный нами (ляжет в vault).
5. Решение по `LAVA_TOP_EMAIL_DOMAIN` (домен для синтетических email;
   catch-all не обязателен — письма платформы туда просто не дойдут).
6. RUB должен быть включён в «Платёжных настройках» аккаунта.

## Альтернативы (если lava.top отпадёт)

| | Lava.top | Tribute (tribute.tg) | WATA / PayPalych (ниша) |
|---|---|---|---|
| Комиссия | 8% | 10% | индивидуально / выше |
| VPN в запретах | нет (усмотрение) | нет (catch-all «запрещено законом») | де-факто терпимы, стандарт VPN-ботов (Bedolaga/RWP) |
| Произвольная сумма | да (продукт с динамической ценой) | да (Shop API `POST /shop/orders`, без пре-продукта) | да |
| Вебхук | статический секрет, HMAC нет | **HMAC-SHA256** (`trbt-signature`) | HMAC (у большинства — ляжет в generic_sbp) |
| Идентификация покупателя | email обязателен (синтетика) | `telegram_user_id` в payload — идеально для бота | order_id round-trip |
| Оплата в TG | внешний paymentUrl | `webappPaymentUrl` + СБП/Stars внутри TG | внешний URL |
| Вывод | ₽ (лимиты) / USDT | ₽ / € / USDT (мин 3000 ₽, 10-го и 25-го) | часто фиат→USDT |
| Особые риски | холд 3 продаж, антифрод против VPN-покупателей | серые зоны Stars-only политики Telegram | RKN-блок сайтов, внезапные верификации с заморозкой |

Технически Tribute Shop API удобнее для TG-бота (telegram_user_id вместо
email, HMAC-вебхуки, оплата внутри TG), комиссия на 2 п.п. выше. Драйвер
`tribute.py` при желании добавляется тем же паттерном (~1 день).
