# WebApp API reference

Single source of truth по `/api/webapp/*` — что фронт ([webapp/src/pages/](../webapp/src/pages/)) реально дёргает, какие pydantic-модели возвращаются, какие 4xx ждать. Если расходится с кодом — прав [api_webapp.py](../backend/app/api_webapp.py).

Self-report: `POST /api/webapp/health-ping-report` (без тела) под webapp-JWT — юзер жмёт красную кнопку «🆘 VPN не работает» на Home; бэкенд находит первую `active` подписку юзера и пишет `AuditLog` с `action=health_ping_response`, `extra.answer="bad"`, `extra.source="self_reported"`, `extra.node_id=<denorm>`. Если активной подписки нет, row всё равно пишется, но с `node_id=null`. Rate-limit серверный пока не делаем; клиент (webapp/src/pages/Home.tsx) сам блокирует кнопку на 5 минут после успешной отправки. Функция `reportVpnBroken()` в `webapp/src/api.ts`. Ответ: `{ ok, subscription_id, node_id }`. Эти события едут в тот же дашборд, что и плановые пинги бота, — админка (`/health-pings`) отдельной карточкой выделяет self-reported как более сильный сигнал.

Отдельная от admin API дорожка: здесь нет `X-Admin-Token`, вместо него — JWT, выданный по Telegram initData. Авторизован только юзер на себя самого, admin-скоупы не применимы.

## Auth handshake

Telegram Mini App открывается с `window.Telegram.WebApp.initData` в URL-фрагменте. Фронт обменивает его на короткоживущий JWT:

### `POST /api/webapp/auth`

Req: `{ init_data: string }` — сырой initData как его отдал Telegram.

Res: `{ token, expires_in, user_id }`.

Что делает бэкенд ([api_webapp.py:173](../backend/app/api_webapp.py#L173)):

1. `_verify_init_data()` — парсит querystring, проверяет HMAC-SHA256 по секрету, производному от `BOT_TOKEN`. Если подпись не сходится — 401 `initData hash mismatch`. Плюс жёсткая валидация `auth_date`: обязан быть, положительный integer, не в будущем (60с clock-skew), не старше `WEBAPP_INIT_DATA_TTL_SECONDS` (дефолт 300с) — закрыли дыру «auth_date=0 / missing пропускали TTL», было 24h hardcoded (fix #53, 2026-04).
2. `json.loads(parsed["user"])` — извлекает Telegram user object.
3. `User.get_or_create(telegram_id=…)` — здесь может родиться новая строка в `users` без `/start` в боте. Это ожидаемо: юзер, открывший WebApp через deeplink до бот-приветствия, сразу валидный.
4. `issue_token(user_id, WEBAPP_JWT_SECRET, WEBAPP_JWT_TTL_SECONDS)` — HS256, payload `{sub: user_id, exp, iat}`. Default TTL — `WEBAPP_JWT_TTL_SECONDS=1800` (30 мин).

Фронт кладёт токен в `Authorization: Bearer <token>` на все последующие запросы. [require_webapp_user()](../backend/app/api_webapp.py#L142) — FastAPI dependency: валидирует JWT, загружает `User` по `sub`, 401 если истёк или подпись не сходится.

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
- **`subscription_extras[].plan_price_kopecks`** — цена плана на **один период** (месяц/год) как в `Plan.price`. Это то, что списывается при каждом renewal из кошелька (плюс опционально extra-device surcharge, см. ниже).
- **`subscription_extras[].bundled_devices`** — `plan.max_devices` (сколько девайсов «бесплатно» идёт с тарифом).
- **`subscription_extras[].extra_device_slots`** — платные слоты сверх бандла, **хранятся на `Subscription.extra_device_slots`** (миграция `0018_extra_device_slots`). Bumps +1 на каждом успешном add-device с платой, обнуляется только при смене тарифа или отмене. Явно **не** декрементится при remove-device — именно это чинит баг «удалил → следующий renewal дешевле», и пользовательский UI в `Home.tsx` об этом предупреждает в confirm'е.
- **`subscription_extras[].extra_device_monthly_kopecks`** — абонплата за один платный слот, `EXTRA_DEVICE_MONTHLY_KOPECKS` из env, default **10000 kopecks = ₽100/мес**. Экспонируется в ответе специально чтобы UI не дублировал константу.
- **`subscription_extras[].next_extra_fee_kopecks`** — prorated плата, которая спишется *прямо сейчас* если юзер нажмёт «+ Добавить устройство». 0 если `live_devices + 1 <= bundled_devices + extra_device_slots` (т.е. новый девайс ещё помещается в уже оплаченный envelope — например, юзер купил слот, потом удалил девайс, и теперь возвращает его бесплатно). Иначе — `prorated_extra_device_fee(sub)` по остатку до `sub.expires_at`. `Home.tsx` показывает это значение в confirm-диалоге add-device, чтобы юзер видел реальную сумму до тапа.
- **`subscription_extras[].can_freeze`** — `sub.status == active AND auto_renew AND NOT has_frozen_this_year`. Кнопка «заморозить» на карточке скрывается по этому флагу; 400 от `/freeze` всё равно ловится тостом.
- **`sub_link_base_url`** — из env `SUB_LINK_BASE_URL`. Пустая строка = используй relative `/api/sub/<token>` на том же origin'е. Нужно, потому что WebApp и sub-link могут жить на разных доменах (boring-domain proxy для обхода DPI).
- **Copy-config UI** — кнопка на карточке подписки зовёт `navigator.clipboard.writeText(subUrl)`, с fallback на `document.execCommand('copy')` для старых WebView. После успеха — тост `Скопировано ✓` на 1.5с + Telegram haptic `notificationOccurred('success')`. Это заменило старую «Показать конфиг» кнопку (которая разворачивала текст прямо на странице) — юзеры почти всегда хотят скопировать, а не разглядывать base64.
- **QR-код рядом с copy-кнопкой** — вторая кнопка «Показать QR» в той же flex-row; тап переключает локальный `showQR` и рендерит `<canvas>` через `QRCode.toCanvas(ref.current, subUrl, { width: 260, margin: 2, errorCorrectionLevel: "M" })` в `useEffect` на `showQR`. Пакет [`qrcode`](https://www.npmjs.com/package/qrcode) (~15 KB, canvas-based, без runtime-depов). Use-case: юзер открывает webapp на ноуте и сканирует QR с телефона, не перекидывая sub-link через мессенджер.
- **`/me` фильтрует терминальные подписки** — `status in ("blocked", "expired")` вырезаются на webapp-сайде ([api_webapp.py](../backend/app/api_webapp.py) около L382), чтобы в кабинете не болталось легаси от миграций/expiration'ов. Это тот же фильтр, которым единственная подписка юзера исчезает из UI после `activate` нового плана (старая получает `status=blocked` в single-sub-swap'е) — поведение совпадает с админским «revoke now». Админский `/api/me` отдаёт всё, не трогаем — там истории полный набор нужен.

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

Создаётся `Invoice(kind="topup", plan_id=NULL)` — эта pair «без плана + kind=topup» — та самая, которую хук в [api/invoices.py:132](../backend/app/api/invoices.py#L132) распознаёт и кладёт в баланс вместо провижининга, плюс тригерит referrer payout при первом топапе (см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md#stage-3)).

### `POST /api/webapp/trial/activate`

Без тела — user_id из JWT. Thin wrapper над [services.trial.activate_trial()](../backend/app/services/trial.py) (admin-token версия в `api_extensions.py` шарит ту же функцию, чтобы логика не дрейфовала).

Возвращает `{ trial_amount_kopecks, referral_bonus_kopecks, balance_kopecks, trial_expires_at }`.

Ошибки:
- `409 Trial already activated` — фронт в ответ скрывает баннер (всё равно рефрешит `/me`, чтобы actual state отразился).
- `503 No trial plan configured` — нет visible 30-day плана; баннер не должен был вообще рендериться, но если словил — это как missing config.

### `POST /api/webapp/subscriptions/activate`

Req: `{ plan_id }`. **Single-subscription invariant:** у юзера может быть максимум одна live-подписка. Если уже есть — старая отменяется (devices revoked), остаток её цены возвращается на кошелёк, затем обычный activate-флоу на новый план.

Механика (обновлена 2026-04-11 — добавлен single-sub invariant с revoke+refund старой подписки; базовая логика из 2026-04-10 `activate_prepaid`/`prorated_sub_refund_kopecks`, см. [BALANCE_REFERENCE.md § activate_prepaid](BALANCE_REFERENCE.md#activation-activate_prepaid--refund_prepaid)):

1. Plan должен быть `is_visible` **и** иметь `daily_rate_kopecks > 0`. Legacy (invoice-only) плана — 400 «use /checkout for one-shot billing».
2. Загрузка всех live-подписок юзера (status ∈ `active`, `frozen` — т.е. всё что пропускает `/me`-фильтр). Если среди них есть **та же** `plan_id` — 400 «Already on this plan». UX: фронт прячет кнопку «Активировать» на карточке текущего плана (через `isCurrent` флаг), так что в норме юзер сюда не попадёт, но backend enforces на всякий случай.
3. Pre-flight: `refund_estimate = sum(prorated_sub_refund_kopecks(old) for old in live_subs)` — сколько вернётся с плюс-прошлых подписок, `required = plan_price_kopecks(plan)`. Если `balance_kopecks + refund_estimate < required` → **402** с телом:
   ```json
   { "code": "insufficient_balance", "balance_kopecks", "required_kopecks", "suggested_topup_kopecks" }
   ```
   `suggested_topup_kopecks = max(MIN_TOPUP_KOPECKS, required - balance_kopecks - refund_estimate)`. Фронт использует это, чтобы pre-fill топап-модал на недостающую сумму. Обрати внимание: при смене плана юзеру иногда **не нужно ничего топапить** даже если кошелёк пустой — прорейтед возврат со старого плана покрывает новый.
4. `ProvisioningOrchestrator.provision_subscription(user, plan)` — warm pool fast path → fallback cold ansible.
5. **Атомарный refund+charge блок** (всё через `db.flush()`, без промежуточных коммитов — чтобы на сбое можно было откатить одним `db.rollback()`):
   - для каждой старой подписки — `balance_svc.refund_subscription_remainder(db, old, reference=f"sub_switch_refund:{old.id}", note="refund on plan switch")` — кладёт `kind=refund` ledger row на реальный прорейтед, возвращает сумму. `refunded_total += …`.
   - `balance_svc.activate_prepaid(db, user_id, new_sub, reference=f"activate:{new_sub.id}")` — списывает полную цену нового плана с кошелька в `new_sub.prepaid_kopecks`. На `ValueError` — `db.rollback()` (откатывает и refund и charge), `new_sub.status = expired`, 402 `Balance was drained mid-activation`.
6. `new_sub.next_charge_at = utcnow()` + `balance_svc.charge_subscription(db, new_sub)` — day-1 tick из bucket'а (как раньше).
7. **Revoke старых подписок.** Для каждой `old_sub`: `orchestrator.revoke_subscription_devices(old_sub)` (помечает credentials `revoked`, запускает ansible background-job на снос юзера с ноды), затем `old_sub.status = blocked`, `old_sub.auto_renew = False`. Это делается **после** refund+charge коммита намеренно: `revoke_device` внутри orchestrator коммитит свои таблицы, и если бы мы уронились тут после рефанда — юзер бы получил два плана на балансе. Каждый revoke обёрнут в `try/except logger.warning` — если ansible-job не стартанул, это не должно вернуть юзеру error status (провижининг-воркер добьёт позже).
8. `db.commit()` финального состояния, return.

**Эффект для юзера:** одна кнопка «Активировать другой тариф» честно выполняет «старое нуль → новое полное», без скрытых parallel-subs. Res несёт `refunded_from_previous_kopecks`, фронт показывает это на success-экране зелёным баннером «С предыдущей подписки на баланс вернулось N ₽», чтобы юзер своими глазами увидел возврат.

Res: `{ subscription_id, sub_token, expires_at, balance_kopecks, plan_price_kopecks, plan_duration_days, refunded_from_previous_kopecks }`. `refunded_from_previous_kopecks = 0` на чистой первой активации или когда остаток старой sub был нулём (expired/только что куплена).

**UI warning.** Перед `activateSubscription(...)` фронт показывает `confirm()` диалог, если `currentSub && currentSub.plan_id !== plan.id`: «У тебя уже активна подписка «X». Если продолжишь, старая будет отключена (устройства отозваны), а остаток её стоимости вернётся на баланс. Затем спишется полная стоимость нового тарифа «Y». Продолжить?». Backend всё равно enforces — диалог это UX, не security.

### `POST /api/webapp/subscriptions/{id}/freeze` / `unfreeze`

Обёртки над [balance_svc.freeze_subscription / unfreeze_subscription](BALANCE_REFERENCE.md#freezeunfreeze). Оба — `auto=False` (то есть ручная заморозка, списывает из year budget). 404 если sub не твоя, 400 если balance_svc возражает.

`freeze` Res: `{ subscription_id, status, frozen_until, freeze_days_left_in_year }`.
`unfreeze` Res: `{ subscription_id, status, next_charge_at }`.

### `POST /api/webapp/subscriptions/{id}/migrate_node` (UI отключён)

Кнопка «🌍 Сменить ноду» из [SubscriptionCard](../webapp/src/pages/Home.tsx) **убрана** — пользовательский self-migrate больше не экспонируется в webapp. Endpoint остаётся в бэкенде как вестижиал (та же механика, что drain-tick), но UI его не вызывает. Админский эквивалент — `POST /api/subscriptions/{id}/migrate` (см. [backend-api.md](components/backend-api.md)) с возможностью указать конкретную target-ноду в обход фильтров пула.

**Seamless URL refresh.** Исторически этот endpoint гарантировал, что `sub_token` не меняется при переезде, и клиент (Hiddify/v2rayN) по кнопке «обновить подписку» автоматически подхватывал новые сервера. После alembic `0022_device_sub_token` токен стал **per-device**, и миграция создаёт новый Device с новым токеном — сохранённый в клиенте URL указывал бы на revoked device. Фикс в `api_extensions.dynamic_sub_link`: если device revoked/disabled и нет активных credential'ов — эндпойнт alias'ит запрос на live-device той же Subscription. Для юзера URL продолжает работать «навсегда» (пока подписка жива), без повторного копирования из ЛК. Admin-миграция через новый эндпойнт использует тот же механизм, так что переезд бесшовен для клиента.

### `POST /api/webapp/subscriptions/{id}/devices` (add-device)

Добавить устройство на активную подписку. В V2 billing тариф покрывает **до `plan.max_devices`** устройств за ежемесячный `plan.price`. Каждое устройство сверх бандла требует купить **платный слот** за `EXTRA_DEVICE_MONTHLY_KOPECKS` (env, **по умолчанию 10000 kopecks = ₽100/мес**). Нет жёсткого cap'а — сколько угодно слотов за дополнительные деньги. Старый V1-гейт по `sub.prepaid_kopecks` удалён вместе с переходом на monthly-renewal (миграция `0015_billing_v2`).

**Модель хранения слотов.** Каждый купленный extra-слот персистится на `Subscription.extra_device_slots` (колонка добавлена миграцией `0018_extra_device_slots`, `INT NOT NULL DEFAULT 0`). Это критично: при удалении девайса слот **не** освобождается и продолжает оплачиваться на каждом renewal'е. Иначе мы попадали в баг «удалил → следующий renewal дешевле → юзер экономит, пересоздавая девайсы», а также давали юзеру «пожить» на чужие деньги, покупая слоты непосредственно перед удалением.

Обнуление слотов — только через `/change_plan` (смена тарифа сбрасывает состояние) или отмену подписки. Пока подписка жива в текущем тарифе — она платит за все когда-либо купленные слоты до конца.

**Формула платы.**
```
capacity = plan.max_devices + sub.extra_device_slots
needs_new_slot = (live_devices + 1) > capacity
fee = needs_new_slot ? prorated_extra_device_fee(sub) : 0
```
`prorated_extra_device_fee` — пропорция `EXTRA_DEVICE_MONTHLY_KOPECKS` от остатка текущего периода до `sub.expires_at`, через `math.ceil` (минимум 1 копейка за начатый день). Если `live_devices < capacity` (юзер раньше удалил девайс, но заплаченный слот остался) — `fee = 0`, юзер возвращает девайс в уже оплаченный envelope бесплатно.

Pre-flight: `user.balance_kopecks >= fee`. На нехватке — **402** с `detail = { code: "insufficient_balance", balance_kopecks, required_kopecks: fee, suggested_topup_kopecks: max(min_topup_kopecks(db), fee - balance), hint }`. Фронтенд в [Home.tsx](../webapp/src/pages/Home.tsx) парсит ошибку через `parseInsufficientBalance` из [errors.ts](../webapp/src/errors.ts) (общий хелпер — тот же паттерн у `/activate` и `/change_plan`), показывает конкретную сумму в алерте вместо тостa с кодом.

**Провижининг → charge → bump.** После `reprovision_subscription` (если `fee > 0`) идёт `balance_svc.charge_extra_device(...)` с `kind=spend` ledger row `reference=f"extra_device:{sub.id}:{device.id}"`, затем `sub.extra_device_slots += 1`. На `ValueError` от charge (race — кто-то другой дренил wallet между pre-flight и commit'ом) — откатываем provision через `revoke_device(background=True)`, возвращаем 402.

Res: `{ subscription_id, device_id, device_count, extra_device_slots, charged_kopecks, balance_kopecks }`. `charged_kopecks=0` и `extra_device_slots` не меняется, когда девайс помещается в уже оплаченный envelope.

**Renewal.** В `balance.renew_subscription`: `total = plan.price + sub.extra_device_slots * EXTRA_DEVICE_MONTHLY_KOPECKS`. Считается **из стореджа**, не из `live_devices` — это делает учёт устойчивым к remove/re-add между рenewal'ами. `note` у ledger row: `renew {plan} (sub N) + K extra slot(s)`.

**UI.** Confirm-диалог кнопки «+ добавить устройство» в [Home.tsx](../webapp/src/pages/Home.tsx) читает `extra.next_extra_fee_kopecks` + `extra.extra_device_monthly_kopecks` и строит текст вида «Сейчас спишется N ₽ за остаток периода. На каждом следующем продлении будет добавляться M ₽/мес за этот слот — пока не поменяешь тариф или не отменишь подписку.» Если fee=0 — короткий вариант «У тебя ещё есть оплаченный слот — ничего не спишется.»

**Admin bypass.** Админский `POST /api/subscriptions/{id}/devices` (см. [ADMIN_UI.md](ADMIN_UI.md)) провижинит девайс **без** charge *и* **без** bump `extra_device_slots`. Это сознательно: админ раздаёт постоянные freebies. Следующий renewal будет платить только за `sub.extra_device_slots` (которое админ не тронул), т.е. админский slot не зафиксируется в будущих списаниях. Минус: при смене тарифа или handoff'е на self-serve админский freebie тихо исчезнет в пределах одного renewal'а — об этом знать, если подарил Pro-тариф с «+2 девайса на подарок».

### `DELETE /api/webapp/devices/{id}` (remove-device)

Отзыв одного устройства с подписки. Последний девайс удалить нельзя — это отмена подписки, обрабатывается через `/cancel`.

**Важно: никакого refund'а, и `sub.extra_device_slots` не декрементится.** Юзер заплатил за слот, слот остаётся и продолжает тикать на каждом renewal'е до смены тарифа / отмены. Это by design — смотри объяснение в секции add-device выше. UI в [Home.tsx](../webapp/src/pages/Home.tsx) явно предупреждает об этом в confirm-диалоге удаления («Деньги за уже оплаченный период не возвращаются, а платный слот сохраняется на подписке…»), чтобы юзер не подумал «удалил = перестал платить».

Что физически делает endpoint: `revoke_device(device, reason="user_removed", background=True)` — credential помечается `revoked`, в фоне запускается ansible playbook, который сносит юзера с ноды. `sub.extra_device_slots` и баланс не меняются.

Res: `{ device_id, device_count, extra_device_slots, balance_kopecks }`. `extra_device_slots` всегда возвращается как есть на `sub`, чтобы фронт мог сразу обновить локальный state без второго похода в `/me`.

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
| 402 | Нет баланса — `/subscriptions/activate`, `/subscriptions/{id}/devices`, `/change_plan`. Тело несёт `code`, `balance_kopecks`, `required_kopecks`, `suggested_topup_kopecks`. |
| 404 | Plan / subscription / invoice не найден **или** не принадлежит юзеру (не различаем, чтобы не утекало existence) |
| 409 | Trial уже активирован / слишком много активных subs на плане |
| 400 | Legacy плана через `/activate` / freeze нельзя / plan без daily_rate / `Already on this plan` на `/subscriptions/activate` |
| 503 | Нет trial-плана / провайдер не настроен / `/migrate_node` не нашёл альтернативную ноду в пуле тарифа |
| 502 | Провайдер вернул ошибку при `create_invoice` |

**Важно: юзер не должен видеть сырые `401: …`, `402: {...}`, `500: …` тексты.** Всё клиентское сообщение об ошибке проходит через [`friendlyError`](../webapp/src/errors.ts) — общий хелпер, который распознаёт HTTP-код в префиксе сообщения fetch'а и возвращает русскую human-readable строку («Сервис временно недоступен…», «Сессия истекла…» и т.п.). Страницы `App.tsx`, `Home.tsx`, `Plans.tsx`, `History.tsx`, `CheckoutPending.tsx` вызывают его через `alert(friendlyError(msg, { fallback: "операция" }))` или `setError(friendlyError(...))`. 402 с `suggested_topup_kopecks` обрабатывается отдельным хелпером `parseInsufficientBalance(msg)`, который достаёт сумму и открывает топап-модал / показывает конкретное «Пополни на N ₽».

## What's on each webapp page

| Page | File | Endpoints |
|------|------|-----------|
| Home | [Home.tsx](../webapp/src/pages/Home.tsx) | `/me`, `/trial/activate`, `/subscriptions/{id}/freeze`, `/subscriptions/{id}/unfreeze`, `/subscriptions/{id}/devices`, `/referral` |
| Plans | [Plans.tsx](../webapp/src/pages/Plans.tsx) | `/plans`, `/checkout`, `/subscriptions/activate`, `/topup` |
| CheckoutPending | [CheckoutPending.tsx](../webapp/src/pages/CheckoutPending.tsx) | `/invoices/{id}` (poll) |
| History | [History.tsx](../webapp/src/pages/History.tsx) | `/transactions` |
