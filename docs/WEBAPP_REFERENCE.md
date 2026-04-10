# WebApp API reference

Single source of truth по `/api/webapp/*` — что фронт ([webapp/src/pages/](../webapp/src/pages/)) реально дёргает, какие pydantic-модели возвращаются, какие 4xx ждать. Если расходится с кодом — прав [api_webapp.py](../backend/app/api_webapp.py).

Отдельная от admin API дорожка: здесь нет `X-Admin-Token`, вместо него — JWT, выданный по Telegram initData. Авторизован только юзер на себя самого, admin-скоупы не применимы.

## Auth handshake

Telegram Mini App открывается с `window.Telegram.WebApp.initData` в URL-фрагменте. Фронт обменивает его на короткоживущий JWT:

### `POST /api/webapp/auth`

Req: `{ init_data: string }` — сырой initData как его отдал Telegram.

Res: `{ token, expires_in, user_id }`.

Что делает бэкенд ([api_webapp.py:148](../backend/app/api_webapp.py#L148)):

1. `_verify_init_data()` — парсит querystring, проверяет HMAC-SHA256 по секрету, производному от `BOT_TOKEN`. Если подпись не сходится — 401 `bad initData signature`.
2. `json.loads(parsed["user"])` — извлекает Telegram user object.
3. `User.get_or_create(telegram_id=…)` — здесь может родиться новая строка в `users` без `/start` в боте. Это ожидаемо: юзер, открывший WebApp через deeplink до бот-приветствия, сразу валидный.
4. `issue_token(user_id, WEBAPP_JWT_SECRET, WEBAPP_JWT_TTL_SECONDS)` — HS256, payload `{sub: user_id, exp, iat}`. Default TTL — `WEBAPP_JWT_TTL_SECONDS=1800` (30 мин).

Фронт кладёт токен в `Authorization: Bearer <token>` на все последующие запросы. [require_webapp_user()](../backend/app/api_webapp.py#L119) — FastAPI dependency: валидирует JWT, загружает `User` по `sub`, 401 если истёк или подпись не сходится.

**Rotation invariant:** если ты ротируешь `WEBAPP_JWT_SECRET`, все открытые WebApp-сессии умирают и юзер должен перезайти через bot-кнопку. Это by design — tradeoff за простоту single-secret HMAC.

## Dashboard

### `GET /api/webapp/me`

Центральный запрос Home page — то, с чем рендерится всё: баланс, подписки, триал-баннер, sub-link base.

Res ([MeResponse](../backend/app/api_webapp.py#L232)):
```json
{
  "user": { "id", "telegram_id", "email", "created_at", "subscription_count" },
  "subscriptions": [SubscriptionOut...],
  "balance": {
    "balance_kopecks": int,
    "balance_rub": float,
    "min_days_remaining": int | null,
    "has_active_balance_sub": bool,
    "trial_available": bool,
    "trial_amount_kopecks": int
  },
  "subscription_extras": [SubscriptionWebAppExtra...],
  "sub_link_base_url": string
}
```

Полезные детали:

- **`min_days_remaining`** — *минимум* по всем active balance-subs, не среднее. Это то, что отображается в header card — юзер должен видеть когда кончится его **самая ранняя** подписка, не усреднённую оптимистичную оценку.
- **`trial_available`** — `user.trial_activated_at IS NULL`. `trial_amount_kopecks` читается каждый запрос из БД (cheapest visible 30-day plan price × 100), поэтому изменение цены через `/admin/plans` автоматически подхватывается без деплоя. Если в БД нет ни одного visible 30-day плана — `trial_amount_kopecks=0` и баннер не рендерится (см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md)).
- **`subscription_extras[].daily_cost_kopecks`** — канонический `_daily_cost_kopecks(plan, billable_devices)` из balance_svc, чтобы preview в UI совпадал с тем, что реально спишется на следующем tick'е. `billable_devices = max(live_devices, 1)` — свежая sub без девайсов показывается как 1-device, иначе будет «∞ дней» до первого provisioning'а.
- **`subscription_extras[].days_remaining`** — `sub_days_remaining(sub, billable_devices, user.balance_kopecks)` = `(sub.prepaid_kopecks + wallet) // daily_cost`. Включает и prepaid bucket подписки, и общий кошелёк юзера, потому что `charge_subscription` fallback'ится в кошелёк, когда bucket иссякает. Для мульти-саб юзеров кошелёк не делится — каждая карточка оптимистично видит его целиком; точный pro-rata split когда кому-то помешает. Default freeze window — `MAX_FREEZE_DAYS_PER_PERIOD = 7` (было 14 до 2026-04-10).
- **`subscription_extras[].can_freeze`** — `sub.status == active AND days_left_in_year >= MAX_FREEZE_DAYS_PER_PERIOD`. Кнопка «заморозить» на карточке скрывается по этому флагу; 400 от `/freeze` всё равно ловится тостом.
- **`sub_link_base_url`** — из env `SUB_LINK_BASE_URL`. Пустая строка = используй relative `/api/sub/<token>` на том же origin'е. Нужно, потому что WebApp и sub-link могут жить на разных доменах (boring-domain proxy для обхода DPI).
- **Copy-config UI** — кнопка на карточке подписки зовёт `navigator.clipboard.writeText(subUrl)`, с fallback на `document.execCommand('copy')` для старых WebView. После успеха — тост `Скопировано ✓` на 1.5с + Telegram haptic `notificationOccurred('success')`. Это заменило старую «Показать конфиг» кнопку (которая разворачивала текст прямо на странице) — юзеры почти всегда хотят скопировать, а не разглядывать base64.
- **QR-код рядом с copy-кнопкой** — вторая кнопка «Показать QR» в той же flex-row; тап переключает локальный `showQR` и рендерит `<canvas>` через `QRCode.toCanvas(ref.current, subUrl, { width: 260, margin: 2, errorCorrectionLevel: "M" })` в `useEffect` на `showQR`. Пакет [`qrcode`](https://www.npmjs.com/package/qrcode) (~15 KB, canvas-based, без runtime-depов). Use-case: юзер открывает webapp на ноуте и сканирует QR с телефона, не перекидывая sub-link через мессенджер.
- **`/me` фильтрует терминальные подписки** — `status in ("blocked", "expired")` вырезаются на webapp-сайде ([api_webapp.py](../backend/app/api_webapp.py) около L382), чтобы в кабинете не болталось легаси от миграций/expiration'ов. Админский `/api/me` отдаёт всё, не трогаем — там истории полный набор нужен.

## Plans & checkout

### `GET /api/webapp/plans`

Визивые тарифы, отсортированные `(max_devices ASC, duration_days ASC)`. Возвращает [WebAppPlan](../backend/app/api_webapp.py#L461) со сгенерированными полями:

- `tier`, `period`, `badge` — результат `_classify_plan(plan)`; badge'и типа «популярный» / «выгодно» — чистая презентационная логика для карточек.
- `price_rub` — из `Plan.price`
- `price_stars` — `_rub_to_stars(price_rub)` через `WEBAPP_STARS_PER_RUB` env (default 0.67). Не реальный exchange rate — проверяй @PremiumBot перед правкой константы.

### `POST /api/webapp/checkout`

Req: `{ plan_id, provider="telegram_stars", subscription_id?=null }`.

Два режима:
- **Новая покупка** (`subscription_id=null`): pre-flight checks — юзер не превысил `plan.max_devices` по активным подпискам на этот же план. Иначе 409 «You already have N active subscriptions on this plan».
- **Продление** (`subscription_id` передан): target sub должна принадлежать юзеру и её `plan_id` должен совпадать с запросом. Иначе 404/400.

Pricing:
- `provider=telegram_stars` → `amount = _rub_to_stars(plan.price)`, `currency="XTR"`.
- Остальные → RUB.

Создаёт `Invoice(kind=subscription)` + `Payment(status=pending)`, зовёт провайдер `create_invoice()`, возвращает `pay_url`. При сбое провайдера — 502 + откат не делается (`invoice` остаётся `pending`, его подчистит кто-то позже или останется висеть).

Res: `{ invoice_id, provider, pay_url, amount, currency }`.

### `GET /api/webapp/invoices/{invoice_id}`

Поллинг post-checkout. Возвращает три флага, чтобы фронт мог нарисовать 3-шаг прогресса (paid → provisioned → credentials ready):

```json
{ "invoice_id", "status", "subscription_id", "subscription_active", "has_credentials" }
```

`has_credentials` гейтит CTA «Открыть мой конфиг» — пока Ansible не отработал, остаётся false. 404 если invoice не твой (или не существует — одинаковая форма, чтобы не утекало).

## Balance billing

### `POST /api/webapp/topup`

Req: `{ amount_kopecks, provider="telegram_stars" }`.

Нижняя граница — `MIN_TOPUP_KOPECKS` (default 10000 = ₽100), иначе 400.

Особенность: `Invoice.amount` **всегда в рублях**, даже если displayed currency — XTR. Это чтобы `_mark_invoice_paid_core` начислил чистое количество копеек независимо от провайдера. Displayed — отдельно в `currency`.

Создаётся `Invoice(kind="topup", plan_id=NULL)` — эта pair «без плана + kind=topup» — та самая, которую хук в [api.py:1195](../backend/app/api.py#L1195) распознаёт и кладёт в баланс вместо провижининга, плюс тригерит referrer payout при первом топапе (см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md#stage-3)).

### `POST /api/webapp/trial/activate`

Без тела — user_id из JWT. Thin wrapper над [services.trial.activate_trial()](../backend/app/services/trial.py) (admin-token версия в `api_extensions.py` шарит ту же функцию, чтобы логика не дрейфовала).

Возвращает `{ trial_amount_kopecks, referral_bonus_kopecks, balance_kopecks, trial_expires_at }`.

Ошибки:
- `409 Trial already activated` — фронт в ответ скрывает баннер (всё равно рефрешит `/me`, чтобы actual state отразился).
- `503 No trial plan configured` — нет visible 30-day плана; баннер не должен был вообще рендериться, но если словил — это как missing config.

### `POST /api/webapp/subscriptions/activate`

Req: `{ plan_id }`. **Списать всю цену плана с кошелька в prepaid bucket подписки → создать подписку → списать day 1 из bucket'а.**

Механика (обновлена 2026-04-10 — переход с daily-from-wallet на prepaid bucket, см. [BALANCE_REFERENCE.md § activate_prepaid](BALANCE_REFERENCE.md#activation-activate_prepaid--refund_prepaid)):

1. Plan должен быть `is_visible` **и** иметь `daily_rate_kopecks > 0`. Legacy (invoice-only) плана — 400 «use /checkout for one-shot billing».
2. Pre-flight: `required = plan_price_kopecks(plan)` (полная цена плана, не один день!). Если `balance_kopecks < required` → **402** с телом:
   ```json
   { "code": "insufficient_balance", "balance_kopecks", "required_kopecks", "suggested_topup_kopecks" }
   ```
   `suggested_topup_kopecks = max(MIN_TOPUP_KOPECKS, required - balance_kopecks)`. Фронт использует это, чтобы pre-fill топап-модал на недостающую сумму.
3. `ProvisioningOrchestrator.provision_subscription(user, plan)` — warm pool fast path → fallback cold ansible.
4. `balance_svc.activate_prepaid(db, user_id, sub, reference=f"activate:{sub.id}")` — пишет **одну** `kind=spend` транзакцию на `-full_price`, инкрементит `sub.prepaid_kopecks` на ту же сумму. На ValueError (race с конкурентным списанием) → `sub.prepaid_kopecks = 0`, 402 `Balance was drained mid-activation`.
5. `sub.next_charge_at = utcnow()` — anchor текущим моментом.
6. `balance_svc.charge_subscription(db, sub)` — списывает день 1 уже из свежего prepaid bucket'а (без записи в ledger).

**Эффект:** юзер не может «купить месячный Basic за 5 ₽» — нужна полная цена плана upfront. Зато после покупки подписка гарантированно живёт свой месяц (при условии живого провижининга) — daily tick просто дренит bucket. Если юзер дополнительно топапит кошелёк во время подписки, он становится fallback-запасом: когда bucket иссякает, `charge_subscription` начинает брать из кошелька.

Res: `{ subscription_id, sub_token, days_remaining, balance_kopecks, daily_cost_kopecks }`. `days_remaining` = `(prepaid + wallet) // daily`.

### `POST /api/webapp/subscriptions/{id}/freeze` / `unfreeze`

Обёртки над [balance_svc.freeze_subscription / unfreeze_subscription](BALANCE_REFERENCE.md#freezeunfreeze). Оба — `auto=False` (то есть ручная заморозка, списывает из year budget). 404 если sub не твоя, 400 если balance_svc возражает.

`freeze` Res: `{ subscription_id, status, frozen_until, freeze_days_left_in_year }`.
`unfreeze` Res: `{ subscription_id, status, next_charge_at }`.

### `POST /api/webapp/subscriptions/{id}/devices`

Добавить устройство на активную подписку. Стоимость за каждое устройство **сверх** `plan.max_devices` — `EXTRA_DEVICE_KOPECKS_PER_MONTH / 30` в день, встраивается в `_daily_cost_kopecks(plan, new_device_count)`.

Pre-flight: `sub.prepaid_kopecks` должно покрывать хотя бы один следующий tick с новым device count. На нехватке — 402 с `code: "insufficient_prepaid_for_device"` (примечание: гейт перешёл с кошелька на prepaid bucket вместе с общим prepaid-реворком 2026-04-10).

Провижининг — `orchestrator.reprovision_subscription(sub, device_name=f"device-{N}")`. По успеху — `{ subscription_id, device_id, device_count, new_daily_cost_kopecks }`.

UI кнопки «+ добавить устройство» есть в Home.tsx на карточке подписки. Админский вариант (обходящий prepaid-гейт) — `POST /api/subscriptions/{id}/devices` в [admin Users.tsx](../admin/src/pages/Users.tsx), см. [ADMIN_UI.md](ADMIN_UI.md).

## History & referral

### `GET /api/webapp/transactions?limit=50&offset=0`

Пагинированный ledger для History page. `limit` капается `[1, 100]`. Возвращает `{ items: [TransactionRow], has_more: bool }`. Реализация читает `limit+1` и отрезает хвост, чтобы дешёво определить `has_more` без отдельного count-запроса.

`TransactionRow`: `{ id, amount_kopecks, kind, reference, note, created_at }`. `kind` — `.value` енума (`topup`/`bonus`/`spend`/`adjust`).

### `GET /api/webapp/referral`

Создаёт реферальный код на первом вызове (lazy mint), иначе возвращает существующий активный. Код — `secrets.token_urlsafe(5)[:6].upper()`, до 5 попыток на коллизию с unique-индексом.

Res: `{ code, bonus_kopecks, invited_count, earned_kopecks, share_url }`.

- `bonus_kopecks = REFERRAL_BONUS_KOPECKS` — сумма которую получит реферер при первом топапе рефёрла.
- `invited_count` = `COUNT(users WHERE referred_by_id = me.id)` — **атрибутированные**, не обязательно активировавшие триал и не обязательно платящие. Для точного counter «платящих» — отдельный запрос по `kind=topup`, сейчас не выставляется.
- `earned_kopecks` = сумма всех positive `kind=bonus` транзакций юзера. Включает trial-бонус самого юзера — технически шумно, но для UI «сколько ты заработал реферальных» это overinclusive rather than misleading, и пока никто не жаловался.
- `share_url` = `https://t.me/<BOT_USERNAME>?start=ref_<code>` или `null`, если `BOT_USERNAME` env не выставлен. Если null — фронт показывает только код как текст, copy-button без share.

## Status codes cheat sheet

| Код | Когда |
|-----|-------|
| 401 | initData не валидируется / JWT протух / не передан Bearer |
| 402 | Нет баланса — `/subscriptions/activate`, `/subscriptions/{id}/devices`. Тело несёт `code`, `balance_kopecks`, `required_kopecks`, `suggested_topup_kopecks`. |
| 404 | Plan / subscription / invoice не найден **или** не принадлежит юзеру (не различаем, чтобы не утекало existence) |
| 409 | Trial уже активирован / слишком много активных subs на плане |
| 400 | Legacy плана через `/activate` / freeze нельзя / plan без daily_rate |
| 503 | Нет trial-плана / провайдер не настроен |
| 502 | Провайдер вернул ошибку при `create_invoice` |

## What's on each webapp page

| Page | File | Endpoints |
|------|------|-----------|
| Home | [Home.tsx](../webapp/src/pages/Home.tsx) | `/me`, `/trial/activate`, `/subscriptions/{id}/freeze`, `/subscriptions/{id}/unfreeze`, `/subscriptions/{id}/devices`, `/referral` |
| Plans | [Plans.tsx](../webapp/src/pages/Plans.tsx) | `/plans`, `/checkout`, `/subscriptions/activate`, `/topup` |
| CheckoutPending | [CheckoutPending.tsx](../webapp/src/pages/CheckoutPending.tsx) | `/invoices/{id}` (poll) |
| History | [History.tsx](../webapp/src/pages/History.tsx) | `/transactions` |
