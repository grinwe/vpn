# WebApp API reference

Single source of truth по `/api/webapp/*` — что фронт ([webapp/src/pages/](../webapp/src/pages/)) реально дёргает, какие pydantic-модели возвращаются, какие 4xx ждать. Если расходится с кодом — прав [api_webapp.py](../backend/app/api_webapp.py).

Самопочинка «VPN не работает» живёт на странице «Помощь» (`webapp/src/pages/Help.tsx`, не Home) и с 2026-09-12 ходит в **то же ядро**, что бот и страница по саб-токену (`backend/app/services/self_repair.py`): `GET /api/webapp/repair-state` → `POST /report-broken-device` либо `POST /report-broken` → `POST /report-operator` → `POST /report-ok` / `POST /report-still-broken`. Полный контракт — раздел «Самопочинка «VPN не работает»» ниже. Телеметрия при этом не потерялась: на каждый шаг починки (`/report-broken-device`, `/report-broken`) бэкенд по-прежнему пишет `AuditLog` с `action=health_ping_response`, `extra.answer="bad"`, `extra.source="self_reported"`, `extra.scope` (`device` / `subscription`) и `extra.node_id` (заполнен только для whole-sub — у per-device ноду выбирает лестница) — эти события едут в тот же дашборд, что и плановые пинги бота, а админка (`/health-pings`) отдельной карточкой выделяет self-reported как более сильный сигнал.

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
    "trial_amount_kopecks": int,
    "trial_autoactivate_allowed": bool
  },
  "subscription_extras": [SubscriptionWebAppExtra...],
  "sub_link_base_url": string
}
```

Полезные детали:

- **`min_days_remaining`** — *минимум* по всем active balance-subs, не среднее. Это то, что отображается в header card — юзер должен видеть когда кончится его **самая ранняя** подписка, не усреднённую оптимистичную оценку.
- **`trial_available`** — `user.trial_activated_at IS NULL`. `trial_amount_kopecks` читается каждый запрос из БД (cheapest visible 30-day plan price × 100), поэтому изменение цены через `/admin/plans` автоматически подхватывается без деплоя. Если в БД нет ни одного visible 30-day плана — `trial_amount_kopecks=0` и баннер не рендерится (см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md)).
- **`trial_autoactivate_allowed`** — `trial_available AND` у юзера нет живой (`active`/`frozen`) подписки. Только при `true` webapp имеет право после claim'а бонуса сразу вызвать `POST /subscriptions/activate`: этот эндпоинт в single-sub модели **меняет тариф** (отзывает все живые подписки, ревокает девайсы, делает проратный возврат), поэтому тихая авто-активация у юзера с действующей подпиской снесла бы её без подтверждения (аудит 2026-07-25). Бонус на баланс зачисляется в обоих случаях.
- **`subscription_extras[].plan_price_kopecks`** — цена плана на **один период** (месяц/год) как в `Plan.price`. Это то, что списывается при каждом renewal из кошелька (плюс опционально extra-device surcharge, см. ниже).
- **`subscription_extras[].bundled_devices`** — `plan.max_devices` (сколько девайсов «бесплатно» идёт с тарифом).
- **`subscription_extras[].extra_device_slots`** — платные слоты сверх бандла, **хранятся на `Subscription.extra_device_slots`** (миграция `0018_extra_device_slots`). Bumps +1 на каждом успешном add-device с платой, обнуляется только при смене тарифа или отмене. Явно **не** декрементится при remove-device — именно это чинит баг «удалил → следующий renewal дешевле», и пользовательский UI в `Home.tsx` об этом предупреждает в confirm'е.
- **`subscription_extras[].extra_device_monthly_kopecks`** — абонплата за один платный слот, `EXTRA_DEVICE_MONTHLY_KOPECKS` из env, default **10000 kopecks = ₽100/мес**. Экспонируется в ответе специально чтобы UI не дублировал константу.
- **`subscription_extras[].next_extra_fee_kopecks`** — prorated плата, которая спишется *прямо сейчас* если юзер нажмёт «+ Добавить устройство». 0 если `live_devices + 1 <= bundled_devices + extra_device_slots` (т.е. новый девайс ещё помещается в уже оплаченный envelope — например, юзер купил слот, потом удалил девайс, и теперь возвращает его бесплатно). Иначе — `prorated_extra_device_fee(sub)` по остатку до `sub.expires_at`. `Home.tsx` показывает это значение в confirm-диалоге add-device, чтобы юзер видел реальную сумму до тапа.
- **`subscription_extras[].can_freeze`** — `sub.status == active AND auto_renew AND NOT has_frozen_this_year`. Кнопка «заморозить» на карточке скрывается по этому флагу; 400 от `/freeze` всё равно ловится тостом.
- **`sub_link_base_url`** — из env `SUB_LINK_BASE_URL`. Пустая строка = используй relative `/api/sub/<token>` на том же origin'е. Нужно, потому что WebApp и sub-link могут жить на разных доменах (boring-domain proxy для обхода DPI).
- **Какая ссылка на карточке** (`subUrl`, она же QR) — с 2026-09-28 та же, что показывает бот: `subscriptions[].link_token` (токен устройства, запомненный при создании подписки; `services/sub_links.link_token_for`). Раньше бралось «первое живое устройство по id» — после failover primary токен переезжает на строку с бОльшим id, и кабинет показывал ссылку device-2, а бот — primary. Если `link_token == sub_token` (подписки до alembic `0070`: бэк отдаёт в `link_token` legacy-ссылку на ВСЕ устройства) — остаётся прежнее поведение, первое живое устройство. Ответ `POST /subscriptions/activate` (экран «Готово») отдаёт тот же `link_token`. URL целиком тоже как у бота: у подписок с `link_token` бэк отдаёт готовые `subscriptions[].link_url`, `subscription_extras[].devices[].sub_url` и `ActivateResponse.sub_url` (`sub_links.sub_url_for` — домен 50/50 `SUB_LINK_BASE_URL`/`SUB_LINK_BASE_URL_ALT` по sha256 токена), фронт берёт их через `subLinkUrl` (`webapp/src/api.ts`). Без этого у половины новых юзеров бот давал `grwr.ink/<tok>`, а кабинет `grn-ssync.pro/<tok>` — один токен, два URL, дубль профиля при двойном импорте. У старых подписок поля пустые, URL строится по-старому из `sub_link_base_url` (смена домена у уже импортированной ссылки дала бы тот же дубль).
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

**Выбор способа оплаты (Stage 9b).** `provider` — произвольный (бэкенд диспатчит через `get_provider`), фронт передаёт выбранный юзером. UI-флоу: сначала сумма, затем «Выберите способ оплаты» — `⭐ Telegram Stars`, `🏦 СБП` (`lava_top_sbp`) или `💳 Карта РФ` (`lava_top`). Кнопок стало три 2026-09-19: до этого lava-кнопка была одна, «💳 Карта РФ / СБП», и способ человек выбирал уже на странице агрегатора PAY2ME — lava закрыл там карту, счета начали падать (400 «Restricted payment method type» → в кабинете «Не удалось создать счёт: 502»), поэтому способ выбирается ДО создания счёта (`docs/PLAN_LAVA_TOP.md`, «Инцидент 2026-09-19»). Обе lava-кнопки — одна интеграция, механика оплаты у них одинаковая. После успешной карточной оплаты (poll поймал зачисление) показывается явный экран «✅ Баланс пополнен» с суммой — lava не редиректит юзера обратно в Mini App, поэтому молча закрывать модалку нельзя. Две механики оплаты различаются:
- **Stars** → `pay_url` это `t.me/$slug`, открывается нативно `tg.openInvoice(url, callback)` с мгновенным колбэком `paid/failed/cancelled`.
- **Карта (`lava_top`) и СБП (`lava_top_sbp`)** → `pay_url` это внешняя платёжная страница, открывается `tg.openLink` (хелпер `openExternalUrl` в [telegram.ts](../webapp/src/telegram.ts), фолбэк `window.open`). Колбэка нет → зачисление ловится поллингом [`pollBalanceIncrease`](../webapp/src/api.ts) (~90с: 30×3с, сравнивает `/me` баланс с baseline). Baseline берётся свежим `fetchMe` с фолбэком на текущее значение (не сентинел — иначе поллинг никогда не подтвердит; не устаревшее — иначе быстрый повторный топап даст ложное «зачислено»).

Живёт в `TopupModal` ([Home.tsx](../webapp/src/pages/Home.tsx)) и `TopupHintSheet` ([Plans.tsx](../webapp/src/pages/Plans.tsx), всплывает на 402) — обе рисуют один и тот же набор кнопок (Stars + «🏦 СБП» + «💳 Карта РФ»). Обе модалки закрываемы во время карточного ожидания (иначе залипали бы на 90с); закрытие **отменяет** фоновый поллинг/колбэк текущего платежа через токен поколения (`pollGenRef`/`payGenRef` — инкрементится на закрытии и на новом платеже, `pollBalanceIncrease` проверяет `shouldStop`, продолжения после `await` гвардятся `gen !== myGen`). Деньги при этом всё равно зачислит вебхук на бэке — баланс появится при следующем `/me`. Бэкенд для карты править не пришлось — `webapp_topup`/`webapp_checkout` уже принимали любой `provider` и отдавали `pay_url`.

### `POST /api/webapp/trial/activate`

Без тела — user_id из JWT. Thin wrapper над [services.trial.activate_trial()](../backend/app/services/trial.py) (admin-token версия в `api_extensions.py` шарит ту же функцию, чтобы логика не дрейфовала).

Возвращает `{ trial_amount_kopecks, referral_bonus_kopecks, balance_kopecks, trial_expires_at }`.

Ошибки:
- `409 Trial already activated` — фронт в ответ скрывает баннер (всё равно рефрешит `/me`, чтобы actual state отразился).
- `503 No trial plan configured` — нет visible 30-day плана; баннер не должен был вообще рендериться, но если словил — это как missing config.

### `POST /api/webapp/subscriptions/activate`

Req: `{ plan_id }`. **Single-subscription invariant:** у юзера может быть максимум одна live-подписка. Если уже есть — старая отменяется (devices revoked), остаток её цены возвращается на кошелёк, затем обычный activate-флоу на новый план.

Механика (обновлена 2026-04-11 — добавлен single-sub invariant с revoke+refund старой подписки; базовая логика из 2026-04-10 `activate_prepaid`/`prorated_sub_refund_kopecks`, см. [BALANCE_REFERENCE.md § activate_prepaid](BALANCE_REFERENCE.md#activation-activate_prepaid--refund_prepaid); 2026-07 аудит-фикс — прорейтед-хелперы реализованы локально в `api_webapp.py` как `_prorated_sub_refund_kopecks`/`_refund_subscription_remainder` (раньше вызывались несуществующие `balance_svc.*` и любая смена плана при живой подписке падала 500), плюс FOR UPDATE-блокировка строки users в начале `activate`/`change_plan` против гонки двойной активации):

1. Plan должен быть `is_visible` **и** иметь `daily_rate_kopecks > 0`. Legacy (invoice-only) плана — 400 «use /checkout for one-shot billing».
2. Загрузка всех live-подписок юзера (status ∈ `active`, `frozen` — т.е. всё что пропускает `/me`-фильтр). Если среди них есть **та же** `plan_id` — 400 «Already on this plan». UX: фронт прячет кнопку «Активировать» на карточке текущего плана (через `isCurrent` флаг), так что в норме юзер сюда не попадёт, но backend enforces на всякий случай.
3. Pre-flight: `refund_estimate = sum(_prorated_sub_refund_kopecks(old) for old in live_subs)` (хелпер в `api_webapp.py`; `remaining_days` капится `duration_days` плана, чтобы удлинённый freeze'ом срок не дал рефанд больше цены) — сколько вернётся с плюс-прошлых подписок, `required = plan_price_kopecks(plan)`. Если `balance_kopecks + refund_estimate < required` → **402** с телом:
   ```json
   { "code": "insufficient_balance", "balance_kopecks", "required_kopecks", "suggested_topup_kopecks" }
   ```
   `suggested_topup_kopecks = max(MIN_TOPUP_KOPECKS, required - balance_kopecks - refund_estimate)`. Фронт использует это, чтобы pre-fill топап-модал на недостающую сумму. Обрати внимание: при смене плана юзеру иногда **не нужно ничего топапить** даже если кошелёк пустой — прорейтед возврат со старого плана покрывает новый.
4. `ProvisioningOrchestrator.provision_subscription(user, plan)` — warm pool fast path → fallback cold ansible.
5. **Атомарный refund+charge блок** (всё через `db.flush()`, без промежуточных коммитов — чтобы на сбое можно было откатить одним `db.rollback()`):
   - для каждой старой подписки — `_refund_subscription_remainder(db, old, reference=f"switch:{new_sub.id}:{old.id}", note=f"switch {old_plan} -> {new_plan}")` — под user-lock перечитывает подписку (параллельный флоу мог уже терминировать её — тогда 0), кладёт `kind=refund` ledger row на реальный прорейтед, возвращает сумму. `refunded_total += …`.
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

**Провижининг → charge → bump.** После `reprovision_subscription` (если `fee > 0`) идёт `balance_svc.charge_extra_device(...)` с `kind=spend` ledger row `reference=f"extra_device:{sub.id}:{device.id}"`. Инкремент `sub.extra_device_slots += 1` делает **сам** `charge_extra_device` (внутри, под user-локом) — хэндлер его **не** дублирует. 2026-07 аудит-фикс (#116): раньше хэндлер поверх этого перезаписывал `sub.extra_device_slots = current_slots + 1` значением, прочитанным до списания, из-за чего два параллельных add-device (даблклик) оба видели `slots=0`, оба списывали fee, но итог был `slots=1` — одна оплата пропадала. Теперь строка подписки читается через `SELECT … FOR UPDATE` в начале `webapp_add_device`, так что конкурентные запросы сериализуются и second-запрос видит уже инкрементированные слоты. На `ValueError` от charge (race — кто-то другой дренил wallet между pre-flight и commit'ом) — откатываем provision через `revoke_device(background=True)`, возвращаем 402.

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

## Самопочинка «VPN не работает» (Help)

Унифицирована 2026-09-12: кабинет, бот и страница по саб-токену используют одно
ядро и один набор исходов. Разбор расхождений до унификации —
[operations/vpn_broken_channels_parity_2026_09_12.md](operations/vpn_broken_channels_parity_2026_09_12.md).

### `GET /api/webapp/repair-state`

Пре-чек **до** кнопки, зеркало бот-эндпоинта `devices-by-telegram`. Ничего не
чинит; если человек внутри окна повторов — фиксирует жалобу (`complaint_received`),
чтобы «жму ещё раз, не помогло» не терялся.

Res (`RepairStateResponse`):

```
{
  devices: [{ id, name, status }],   // active + failed первой active-подписки
  retry_after_sec: number | null,
  wait_reason: "throttled" | "daily_limit" | null,
  subscription_id: number | null
}
```

`devices` — `self_repair.live_devices(sub)`: `pending` исключён (устройство ещё
собирается), `disabled`/`revoked` — уже не живые. Один и тот же набор у бота и
кабинета, поэтому «сколько у меня устройств» больше не зависит от канала.
`subscription_id != null` при пустом `devices` = все устройства pending.

### `POST /api/webapp/report-broken-device` `{device_id}`

Один шаг лестницы для ОДНОГО устройства (перетасовка протоколов → перенос ноды →
дубль лега). Соседние устройства не трогаются, нода user-wide не банится.
Чужое/несуществующее устройство → **не 404**, а `action="no_subscription"`:
фронт показывает тот же честный текст, что и при отсутствии подписки.

### `POST /api/webapp/report-broken` (без тела)

«🔁 Все мои устройства» — перенос ВСЕЙ подписки на другую ноду + user-wide бан
старой (`self_repair.handle_broken_subscription`). Зеркало одноимённой бот-кнопки.
Лестница ротации здесь не применяется намеренно: человек сказал, что легла вся
подписка, а не транспорт одного устройства.

### Ответ обоих — `WebappRepairResponse`

```
{
  ok: true,
  action: "migrated" | "reshuffled" | "duplicated" | "throttled" |
          "daily_limit" | "no_target" | "no_subscription" | "not_ready",
  report_id, new_node_name, new_node_region, task_id,
  retry_after_sec,          // throttled / daily_limit
  device_name,              // для per-device migrated
  scope: "device" | "subscription",
  // совместимость со старым бандлом:
  migrated: boolean,        // true для migrated/reshuffled/duplicated
  subscription_id, node_id, target_node_name
}
```

Поля `action`…`scope` — ровно те же, что в бот-контракте
`client_control.ReportBrokenResponse` (один хелпер `outcome_response` на оба).
`migrated`/`target_node_name` оставлены потому, что бандл кабинета кэшируется
вебвью Telegram и старый фронт ещё какое-то время читает именно их.

### `POST /api/webapp/health-ping-report` — только совместимость

Старый бандл при одном устройстве ходил сюда и получал whole-sub миграцию мимо
лестницы. Эндпоинт жив, но теперь ведёт себя как новая кнопка: ровно одно живое
устройство → per-device ядро, иначе → перенос подписки. **Новый фронт его не
вызывает** (`reportVpnBroken()` из `api.ts` удалён).

### Обратная связь

| Эндпоинт | Тело | Что делает |
|---|---|---|
| `POST /api/webapp/report-operator` | `{report_id, operator}` | оператор связи в `OperatorNodeReport.operator`; значение вне таксономии → `unknown` |
| `POST /api/webapp/report-still-broken` | `{report_id}` | `outcome="fail"` + `resolved_at` — самый весомый негативный сигнал для матрицы «нода × оператор». До унификации из кабинета `fail` не приходил никогда |
| `POST /api/webapp/report-ok` | `{report_id}` | `outcome="ok"`, идемпотентно: уже закрытый `ok`/`fail` не перетирается |

Все три проверяют владельца (`report.user_id == user.id`) и отвечают **404** на
чужой `report_id`.

### Машина состояний Help.tsx

```
idle ──тап──> pending ──GET /repair-state──┬─ wait_reason ───────────> outcome (throttled | daily_limit)
                                           ├─ devices = 0 ──────────> outcome (not_ready, если есть subscription_id; иначе no_subscription + «💎 Выбрать тариф»)
                                           ├─ devices = 1 ──POST /report-broken-device──> outcome
                                           └─ devices > 1 ─────────> pick_device ──┬── устройство → POST /report-broken-device ──> outcome
                                                                                   ├── «🔁 Все мои устройства» → POST /report-broken ──> outcome
                                                                                   └── «Отмена» → idle
outcome (migrated | reshuffled | duplicated, есть report_id)
   → pick_operator (6 операторов + «Пропустить») → feedback («✅ Всё работает» / «❌ Всё равно не работает»)
   → done_ok | done_fail (в done_fail — ссылка в поддержку)
```

Правила:

* **Клиентских кулдаунов нет.** После любого неуспешного исхода кнопка активна
  сразу — повторы ограничивает сервер единой политикой (`SELF_REPAIR_THROTTLE_SEC`
  120 с и `SELF_REPAIR_DAILY_MAX` 5/сутки, обе по подписке). Единственный таймер —
  5 с на тексте транспортной ошибки. Прежний React-state на 5 минут после
  `no_target`/`throttled` убран.
* **Тексты исходов совпадают с ботом** (`outcomeText()` в Help.tsx против
  `_send_repair_outcome` в `bot/handlers.py`): `migrated` различает «для «{имя}»»
  и «для всех устройств» по `scope`, `throttled` показывает
  `M = ceil(retry_after_sec / 60)` минут. Технические имена устройств
  (`primary`, `device-6`) прячутся регуляркой `TECH_NAME_RE` — зеркало
  `_TECH_NAME_RE` на странице по токену.
* **«Пропустить» у вопроса об операторе** — вопрос не шлагбаум; ответ уходит
  best-effort, сбой запроса человека не блокирует.
* Кабинет пуш не умеет, поэтому спрашивает «получилось?» сразу после шага
  оператора (у бота это отложенный нудж через 15 минут).
* Хинт под кнопкой честный: «Кнопка переключит тебя на другой способ связи или
  другой сервер. Это не замена поддержке — для диалога используй кнопку выше»
  (раньше было «пошлёт маячок админам»). В FAQ «VPN не подключается / медленный»
  первым пунктом стоит отсылка к этой же кнопке.
* `App.tsx` устройства в Help больше не передаёт — страница берёт их сама из
  `repair-state` в момент тапа (раньше список приходил пропом из `/me` и мог быть
  протухшим).

## Status codes cheat sheet

| Код | Когда |
|-----|-------|
| 401 | initData не валидируется / JWT протух / не передан Bearer |
| 402 | Нет баланса — `/subscriptions/activate`, `/subscriptions/{id}/devices`, `/change_plan`. Тело несёт `code`, `balance_kopecks`, `required_kopecks`, `suggested_topup_kopecks`. |
| 404 | Plan / subscription / invoice / `report_id` не найден **или** не принадлежит юзеру (не различаем, чтобы не утекало existence). Исключение — `report-broken-device`: чужое устройство отдаёт 200 с `action="no_subscription"`, чтобы человек видел человеческий текст, а не ошибку |
| 409 | Trial уже активирован / слишком много активных subs на плане |
| 400 | Legacy плана через `/activate` / freeze нельзя / plan без daily_rate / `Already on this plan` на `/subscriptions/activate` |
| 503 | Нет trial-плана / провайдер не настроен / `/migrate_node` не нашёл альтернативную ноду в пуле тарифа |
| 502 | Провайдер вернул ошибку при `create_invoice` |

**Важно: юзер не должен видеть сырые `401: …`, `402: {...}`, `500: …` тексты.** Всё клиентское сообщение об ошибке проходит через [`friendlyError`](../webapp/src/errors.ts) — общий хелпер, который распознаёт HTTP-код в префиксе сообщения fetch'а и возвращает русскую human-readable строку («Сервис временно недоступен…», «Сессия истекла…» и т.п.). Страницы `App.tsx`, `Home.tsx`, `Plans.tsx`, `History.tsx`, `CheckoutPending.tsx` вызывают его через `alert(friendlyError(msg, { fallback: "операция" }))` или `setError(friendlyError(...))`. 402 с `suggested_topup_kopecks` обрабатывается отдельным хелпером `parseInsufficientBalance(msg)`, который достаёт сумму и открывает топап-модал / показывает конкретное «Пополни на N ₽».

**Живучесть токена и рефреша (`App.tsx`).** JWT короткоживущий (`WEBAPP_JWT_TTL_SECONDS`, дефолт 30 мин), а Mini App висит в фоне часами. Поэтому `/me`-запросы, которыми управляет `App.tsx` (стартовый bootstrap, фоновый рефреш при возврате на home, `refreshMe` после мутаций), идут через `fetchMeResilient()`: на `401/403` он один раз переавторизуется по `getTg().initData` (`authWithInitData` → `setToken`) и повторяет запрос, а транзиентные сетевые сбои ретраит (2 попытки, пауза 2с). Стартовый bootstrap на неустранимой ошибке показывает текст через `friendlyError`. **Централизованный re-auth (`api.ts`).** Обёртка `request()` теперь сама переавторизуется на `401/403`: один раз дёргает `authWithInitData(getTg().initData)` → `setToken` и повторяет исходный запрос (рекурсию режет исключение эндпоинта `/api/webapp/auth`). Поэтому ВСЕ мутации (`freezeSubscription`, `addDevice`, `removeDevice`, `renameDevice`, `cancelSubscription`, `activateSubscription`, `changePlan`, `createTopup`, `toggleAutoRenew`, `activateTrial` из `Home.tsx`/`Plans.tsx`) переживают протухший токен прозрачно, а не падают «Сессия истекла» на первый тап. `reauth()` в `App.tsx` остаётся страховкой для стартового bootstrap и не конфликтует. Таймаут запроса (`FETCH_TIMEOUT_MS`, 15с) строится через `AbortController` с feature-detection — на старых WebView (iOS 15 / Android <2022) без `AbortSignal.timeout` приложение больше не падает целиком, а деградирует до fetch без таймаута. Сетевые сбои (http-error/timeout/network-error/reauth) пишутся лёгкими breadcrumbs `console.warn("[webapp-net] …")` для поддержки — юзеру не показываются.

## What's on each webapp page

| Page | File | Endpoints |
|------|------|-----------|
| Home | [Home.tsx](../webapp/src/pages/Home.tsx) | `/me`, `/trial/activate`, `/subscriptions/{id}/freeze`, `/subscriptions/{id}/unfreeze`, `/subscriptions/{id}/devices`, `/referral` |
| Plans | [Plans.tsx](../webapp/src/pages/Plans.tsx) | `/plans`, `/checkout`, `/subscriptions/activate`, `/topup` |
| CheckoutPending | [CheckoutPending.tsx](../webapp/src/pages/CheckoutPending.tsx) | `/invoices/{id}` (poll) |
| History | [History.tsx](../webapp/src/pages/History.tsx) | `/transactions` |
| Help | [Help.tsx](../webapp/src/pages/Help.tsx) | `/repair-state`, `/report-broken-device`, `/report-broken`, `/report-operator`, `/report-ok`, `/report-still-broken` |
