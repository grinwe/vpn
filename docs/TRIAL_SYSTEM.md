# Trial + Referral system

Единственный источник правды по механике бесплатных дней (триала) и реферальным выплатам. Если этот документ расходится с кодом — прав код, документ подлежит апдейту.

> **С 2026-09-30 (v1.4.0, план [trial_3_days_plan_2026_09_30.md](operations/trial_3_days_plan_2026_09_30.md)) триал = 3 дня вместо месяца.** Бонус 15 ₽ (3 дня Solo по 5 ₽/день) сразу тратится на подписку на 3 дня, по приглашению 3 + 3 = 6 дней за 30 ₽. Подписка живёт на `TRIAL_HIDDEN_HOURS` (24 ч) дольше обещанного, за эти часы не списываем. Бот и кабинет выдают триал одним серверным сервисом `activate_trial_full`. Заморозка только после первой оплаты, награда рефереру за первую оплату любым способом. Уже выданные 30-дневные триалы не трогали. **Дата переключения (дата деплоя) = граница когорт «до/после»: вписать сюда при выкатке: ____.** Когорта «после» — `spend` с `reference LIKE 'trial-full:%'` на 1500/3000 и `metadata.trial_days` в аудите `trial_activated_full` / `trial_activated` (`full=true`); по сумме `trial:{uid}=1500` отбирать нельзя, туда попадают бонус-онли платящие.

## Why it exists

Старая реферальная модель сжигала ссылку на первом `/start` юзера и обещала «+3 дня» в бот-копии — фактически бонус никому не капал, если юзер хоть раз до этого нажал на бота (гейт `created=True` отсеивал всех). Welcome-копия обещала «первый день в подарок», которого в коде не было вообще.

Новая модель:

1. **Free trial** — любой юзер, включая ретроактивно существующих, один раз активирует бесплатные дни. Бонус = `TRIAL_DURATION_DAYS` (3) дней по цене дня самого дешёвого видимого 30-дневного плана (Solo 150 ₽ → 15 ₽, читаем из БД, не хардкод), и он **сразу тратится на подписку ровно на эти дни** тем же провижинингом, что и обычная покупка. Отдельного флага «триальная подписка» нет: это обычный Solo с `auto_renew=True`, после бесплатных дней `renew_subscription` списывает 150 ₽ и продлевает от старого срока (T+4 → T+34). Признак триала — строка журнала `spend trial-full:{sub.id}` (`balance.is_unpaid_trial`).

   > **Оба пути серверные (с 2026-09-30).** И бот (кнопка `trial:activate` → `POST /api/trial/activate_full`), и кабинет (`POST /api/webapp/trial/activate`) зовут один сервис `services.trial.activate_trial_full`, поэтому сроки, суммы и отказы не разъезжаются. До этого кабинет делал два вызова из браузера (бонус, затем `activateSubscription(cheapestMonthly)`, авто-активация 2026-07-22 против застревания 62% на бонусе), а бот с 2026-08-21 делал то же одним вызовом.
   >
   > **Что приходит в бот после 200.** Бот шлёт саб-ссылку тем же `cmd_config`, что и `/config`, с заголовком `intro` из ответа `activate_full`: «🎉 Готово! VPN бесплатно на 3 дня.», по приглашению «🎉 Готово! VPN бесплатно на 6 дней: 3 дня дарим всем и ещё 3, потому что тебя пригласил друг.», на историческом пути (`trial_days` = null) «🎉 Готово! Подписка активирована.». **Даты в заголовке нет** намеренно: подписка идёт до конца скрытых суток, и дата 4-го дня противоречила бы «3 дня». **Warm-путь (2026-08-28): ровно два сообщения** — (1) заголовок + «🔗 Твоя ссылка для подключения. Тапни по ней ниже, чтобы скопировать. Не знаешь, как настроить? Выбери платформу 👇» с клавиатурой онбординга (ЛК + 4 платформы), (2) голый URL. Пуш `config_ready` на warm-хите не шлётся (`provision_subscription(..., notify_config_ready=False)`: ссылка уже отдана), приглашение «приведи друга» приходит через `REFERRAL_INVITE_DELAY_H` (24 ч). На cold-пути (девайс ещё pending) первое сообщение — заголовок + «⏳ Твой конфиг ещё создаётся», затем подсказка про ЛК, а ссылка приезжает пушем `config_ready` по завершении Ansible. Если прислать не удалось — фолбэк «забери в личном кабинете или /config» с тем же заголовком и кнопкой ЛК (инцидент 2026-08-25: выдача падала молча).
2. **Трёхстадийный реферал** — атрибуция отвязана от начисления, приглашённый получает дни сверху к триалу (3 + 3), а награда рефереру отложена до первой **реальной** оплаты приглашённого (любым способом). Это убивает фарм.

## Data model

[models.py](../backend/app/models.py) — два nullable поля на `User`:

```python
trial_activated_at = Column(DateTime, nullable=True)
trial_expires_at   = Column(DateTime, nullable=True)  # конец бесплатного доступа
```

`trial_expires_at` = `activated_at + TRIAL_DURATION_DAYS (+ дни по приглашению) + TRIAL_HIDDEN_HOURS`, у триальной подписки совпадает с её `expires_at` (реальный конец доступа, поэтому clawback не сработает внутри скрытых суток). У бонус-онли (платящий забрал в кабинете только деньги, `set_expiry=False`) и на историческом пути остаётся NULL или прежним: возвращать нечего, предупреждение не нужно. У старых 30-дневных триалов — прежнее значение.

Миграция: [alembic/versions/0012_trial_bonus.py](../backend/app/alembic/versions/0012_trial_bonus.py) — `ALTER TABLE users ADD COLUMN IF NOT EXISTS ...`, идемпотентно.

Никаких новых таблиц. Все денежные движения — через существующий `BalanceTransaction` ledger.

## Environment

| Var | Default | Effect |
|-----|---------|--------|
| `TRIAL_DURATION_DAYS` | `3` | Видимые бесплатные дни: бонус = столько дней по цене дня Solo, сразу тратится на подписку на эти дни. До 2026-09-30 было `30` (месяц) |
| `TRIAL_HIDDEN_HOURS` | `24` | Скрытый запас сверх видимых дней: подписка до `now + дни + часы`, списываем только за дни. Счётчики (кабинет, Happ) округляют вниз и показывают ровно «3 дня», «🚨 истекает завтра» приходит в конце обещанного срока, пополнение в этот день продлевает ту же подписку с той же ссылкой. `0` выключает |
| `TRIAL_EXPIRY_WARN_DAYS` | `3` | За сколько дней до `trial_expires_at` воркер шлёт `trial_expiry_warning`. При 3 днях пуш приходит в T+1 вместе с `renewal_reminder` 3d; если пушей перебор, ставить `1` без правок кода |
| `REFERRAL_INVITEE_DAYS` | `3` | Подарок приглашённому в днях, прибавляется к бесплатным дням (3 + 3). Перебивается `bonus_days` кода, в env не проброшен |
| `REFERRAL_REWARD_DAYS` | `10` | Награда рефереру в днях при первой оплате приглашённого. Перебивается `reward_days` кода |
| `REFERRAL_BONUS_KOPECKS` | `5000` | Легаси-сумма: фолбэк, если в БД нет видимого 30-дневного плана и дни не во что перевести |

Ставятся в [balance.py](../backend/app/services/balance.py) (блок констант в начале модуля). `TRIAL_DURATION_DAYS` и `TRIAL_HIDDEN_HOURS` проброшены в `backend` в `docker-compose.yml`, `TRIAL_EXPIRY_WARN_DAYS` в `worker` (и `worker-scheduler` через якорь), все три рендерит `env.j2` с дефолтами из кода. Цифры для примеров ниже: Solo 150 ₽ / 30 дней = 5 ₽/день.

## Activation flow

Сервис: [services/trial.py](../backend/app/services/trial.py). Две функции, обе оставляют транзакцию открытой (commit и аудит у HTTP-слоя, чтобы любая ошибка откатила бонус целиком).

### `activate_trial(db, user_id, *, set_expiry=True)` — только бонус

1. `SELECT ... FOR UPDATE` по `user_id`. Два параллельных тапа в ту же секунду пройдут стейл-чек `trial_activated_at IS NULL`, но только один выиграет лок.
2. Повторная проверка под локом → `TrialAlreadyActivated`.
3. `_trial_plan()` — cheapest visible 30-day plan. Если нет — `NoTrialPlan` (→ 503), баннер в UI в этом случае тоже не рендерится. Сумма `trial_amount_kopecks()` = `days_to_kopecks(TRIAL_DURATION_DAYS)` = 1500 коп.
4. `user.trial_activated_at = now()`.
5. `balance_svc.trial_bonus(db, user.id, amount, reference=f"trial:{user.id}")` — пишет `kind=bonus` транзакцию. Уникальный reference делает повторные попытки безопасными.
6. Если `user.referred_by_id is not None` — дополнительно `balance_svc.referral_bonus(db, user.id, reference=f"referral_signup:{user.id}", days=invitee_bonus_days)`: подарок приглашённому (тому, кто сейчас активирует триал), а не рефереру. Дни = `bonus_days` кода реферера, иначе `REFERRAL_INVITEE_DAYS` (3) = 15 ₽.
7. `user.trial_expires_at = now + (дни + дни по приглашению) + TRIAL_HIDDEN_HOURS`, либо NULL при `set_expiry=False` (бонус-онли: платящий с живой подпиской, деньги пойдут на продление, ни предупреждения, ни clawback).
8. Результат несёт `trial_days` и `referral_days` (0, если подарок не зачислен).

### `activate_trial_full(db, user, *, source)` — бонус и сразу подписка

Общий для бота (`source="bot"`) и кабинета без живой подписки (`source="webapp"`):

1. `activate_trial`. `TrialAlreadyActivated` здесь не ошибка (`fresh=False`): бонус мог быть выдан раньше без подписки.
2. `db.refresh(user, with_for_update=True)`. Есть active/frozen подписка → `TrialLiveSubscription` (409 «User already has a live subscription»).
3. **Защита от повторного триала.** `not fresh` и у юзера есть хоть одна `Subscription` в любом статусе → `TrialAlreadyUsed` (409 «Trial already used»). То же, если `trial_activated_at` стоит, а записи `trial:{uid}` нет (восстановленные через `generate_restore_sql.py`, скрипт так блокирует им триал). Без этой защиты старая кнопка `trial:activate` в чате давала истёкшему триальщику, пополнившему баланс, новую подписку мимо продления.
4. `not fresh`, подписок не было: путь по сумме записи `trial:{uid}`. **Не меньше цены плана** — исторический «застрял на бонусе» со старыми 150 ₽: `activate_subscription` без параметров, полная цена и 30 дней, `trial_expires_at` не трогаем, `trial_days`/`referral_days` = null, 402 при нехватке. **Меньше** — новый бонус без подписки (после legacy `/api/trial/activate`): тратится шагами 5–9 на N дней по зачисленной сумме.
5. Тариф — `_trial_plan(db)`, тот же, по которому считалась цена дня.
6. Баланс проверяется **до** провижининга (иначе на 402 оставалась бы уже выданная cold-путём подписка).
7. `expires = now + (дни + дни по приглашению) + TRIAL_HIDDEN_HOURS`; `provision_subscription(user, plan, expires_at_override=expires, notify_config_ready=False)`. `ColdPathThrottled` и `RuntimeError` летят до commit: вызывающий откатывает бонус и `trial_activated_at`, юзер может повторить.
8. `activate_subscription(db, user.id, sub, reference=f"trial-full:{sub.id}", price_kopecks=бонус + подарок, expires_at=expires)` — списывается ровно зачисленное, остаток 0, своих денег юзера это не касается.
9. `user.trial_expires_at = sub.expires_at`.
10. Возвращает `TrialFullResult {sub, plan, trial_days, referral_days, hidden_hours, charged_kopecks, fresh, trial_amount_kopecks, referral_bonus_kopecks}`.

Цифры: 3 дня = 1500 коп. (списание 15 ₽, подписка на 4 суток). 6 дней = 1500 + 1500 = 3000 коп. (30 ₽, подписка на 7 суток). В журнале у приглашённого: +15 ₽ `trial:`, +15 ₽ `referral_signup:`, −30 ₽ `trial-full:`. Нестандартный `bonus_days` (X) работает сам: 3 + X дней за 15 + 5X ₽.

### HTTP-обёртки

- **Бот:** `POST /api/trial/activate_full` (`api_extensions.py`, admin-scoped, `telegram_id` в теле) — тонкая обёртка над `activate_trial_full(..., source="bot")`. Ответ `{subscription_id, plan_name, expires_at, trial_days, referral_days}` (`expires_at` бот больше не печатает). Коды: `TrialLiveSubscription` → **409** `User already has a live subscription` (бот отдаёт ссылку вместо прайса); `TrialAlreadyUsed` → **409** `Trial already used` (бот: «Бесплатные дни уже использованы. Продлить прежнюю подписку: /renew, все тарифы: /plans.»); `RuntimeError` → **409** `str(exc)` (нет нод / лимит устройств; бонус откатан, бот: «Не получилось собрать подписку 😔 Подарок остаётся за тобой: попробуй ещё раз через пару минут или напиши /help.»); `ValueError` → **402** (только исторический путь); `NoTrialPlan` → **503**; `ColdPathThrottled` → **503** с `Retry-After`. Аудит успеха `trial_activated_full`: `metadata = {plan_id, trial_days, referral_days, hidden_hours, charged_kopecks, source}`.
- **Кабинет:** `POST /api/webapp/trial/activate` (`api_webapp.py`, `user_id` из JWT). Нет живой подписки → `activate_trial_full(..., source="webapp")`, ссылка (`sub_token`, `sub_url`) прямо в ответе. Живая подписка есть → только `activate_trial(..., set_expiry=False)`. Аудит `trial_activated` с `metadata += {trial_days, referral_days, full}`. Подробно — [WEBAPP_REFERENCE.md](WEBAPP_REFERENCE.md#post-apiwebapptrialactivate).
- **Legacy:** `POST /api/trial/activate` (admin-scoped, только бонус). Вызывающих в коде нет, бонус здесь автоматически 15 ₽. Застрять после него нельзя: следующий тап в боте или кабинете потратит бонус по шагу 4.

**Отказы в обеих обёртках: сначала `db.rollback()`, потом аудит `trial_activate_rejected`** с `reason` из `live`, `already_used`, `already_activated`, `no_trial_plan`, `throttled`, `provision_failed`, `insufficient` и `source`. `_audit` по умолчанию коммитит, а `live`, `throttled` и `provision_failed` случаются уже после flush бонуса и таймера: без rollback платящий на `live` получил бы 15 ₽ и таймер триала. `onboarding_funnel.trial_failures` считает только `no_trial_plan`, `throttled`, `provision_failed`, `insufficient`: `live`/`already_used`/`already_activated` — это платящие, нажавшие старые кнопки, а не провал активации.

## Referral: three stages

### Stage 1 — attribution (on /start ref_XXX)

Происходит в [api_extensions.py `/users/register`](../backend/app/api_extensions.py). Без гейта `created=True` — работает для любого юзера, пока его `referred_by_id IS NULL`. Проверки:

- `ref.owner_id != user.id` (никаких self-referrals)
- `ref.is_active`
- `user.referred_by_id IS NULL` (атрибуция одноразовая)

На успехе — выставляется `user.referred_by_id = ref.owner_id`, инкрементится `ref.uses`. **Никаких денежных начислений на этой стадии.**

Респонс `/users/register` отдаёт `trial_available: bool`, `trial_days` (3) и `trial_referral_days` (0 или 3), посчитанные **после** привязки реферала в том же запросе (`trial_svc.trial_days_for`). Бот по ним пишет строку про подарок в welcome, кнопку и `/plans` («3 дня» или «6 дней»), без второго запроса.

### Stage 2 — referee bonus (on trial activation)

Начисляется внутри `activate_trial()` сразу после собственного trial-бонуса, если `user.referred_by_id` стоит. Сумма в днях: `bonus_days` кода реферера, иначе `REFERRAL_INVITEE_DAYS` (3 дня = 15 ₽). Reference: `referral_signup:{user.id}`.

Итого приглашённый, активировавший триал по ссылке: +15 ₽ бонус и +15 ₽ подарок, которые `activate_trial_full` тут же тратит на подписку на **6 дней** (3 + 3). Бот и кабинет говорят прямо: «Тебя пригласил друг, поэтому у тебя 6 дней VPN бесплатно вместо 3».

### Stage 3 — referrer payout (on referee's first payment of any kind)

Хелпер `_maybe_pay_referrer(db, invoice)` в [api/invoices.py](../backend/app/api/invoices.py). С 2026-09-30 его зовут **обе ветки** `_mark_invoice_paid_core`: пополнение (до записи собственной topup-строки) и счёт за тариф или продление картой/СБП (`/plans`, `/renew`, `?fix=1`), до того как счёт помечен paid. При трёх бесплатных днях первой оплатой приглашённого чаще всего бывает как раз renewal-счёт (`/plans` → Solo при живой подписке превращается в renewal). Раньше награду давал только первый `kind=topup`.

Условия (все):
- сумма счёта > 0 и у плательщика стоит `referred_by_id`;
- **первая оплата**: у приглашённого нет ни других оплаченных счетов с суммой > 0, ни строк `topup`, ни ручных зачислений `adjust admin_topup:%` > 0 (так проводили оплаты, пока lava лежал 24.08–19.09). Одного дедупа мало: `/users/register` привязывает `referred_by_id` любому юзеру с пустым полем, даже давно платящему, и без этого условия его следующая оплата принесла бы владельцу чужой ссылки награду, а исторические приглашённые дали бы её задним числом;
- дедуп по уникальному `reference=referral_payout:{user_id}` поверх: повторный webhook не задвоит выплату, последующие оплаты тоже.

Защиты:
- **Лок строки плательщика** (`SELECT ... FOR UPDATE`) ДО проверок: два одновременных вебхука по разным счетам одного юзера сериализуются, второй видит оплату первого.
- **Fire-and-forget** — начисление в SAVEPOINT (`db.begin_nested()`), исключение логируется и откатывает только награду, но не платёж.

Размер: `reward_days` кода реферера, иначе `REFERRAL_REWARD_DAYS` (10 дней = 50 ₽). Бот (`/referral`), кабинет и пуш `referral_invite` пишут «…когда он впервые оплатит».

**Анти-фарм:** 10 фейк-аккаунтов = 10 × 15 ₽ подарков приглашённым (по 3 дня), но **0 ₽ рефереру**, пока фейки не занесут реальные деньги. Фарм 6 дней через второй аккаунт по своей же ссылке стоит 15 ₽ на аккаунт, принимаем.

## Expiry worker

Третий pass в `run_balance_charge_tick()` — `_run_trial_expiry_pass` в [worker.py](../backend/app/worker.py). Бежит на том же интервале, что и балансовые списания (`BALANCE_CHARGE_INTERVAL`, default 3600s).

Расписание нового 3-дневного триала без приглашения (с приглашением всё сдвигается на +3 дня; напоминания о продлении по решению владельца не трогали, «пусть подстёгивает купить раньше»):

| Когда | Что приходит |
|---|---|
| T+0 | «🎉 Готово! VPN бесплатно на 3 дня.» + ссылка |
| T+1 сут. (≤5 мин) | «⏰ Подписка истекает через 3 дня!» с датой T+4 (UTC) + pending renewal-счёт. В течение часа «⏳ Бесплатные дни скоро закончатся». Через 24 ч после первого фетча ещё `referral_invite`, итого до трёх пушей |
| T+3 сут. | «🚨 Подписка истекает завтра!» |
| T+4 сут. | Хватает 150 ₽ → продление до T+34. Нет → `expired`, ссылка 403 |
| T+5 сут. | Устройства сняты с нод (`RENEWAL_GRACE_HOURS`) |

Скрытые сутки видны в датах: Happ, карточка кабинета, `/status` и строка «Дата: …» в напоминаниях показывают 4-й день. Это принято (вариант А плана).

### 3a. Warning (за N дней до истечения)

```sql
WHERE trial_expires_at IS NOT NULL
  AND trial_expires_at <= now() + TRIAL_EXPIRY_WARN_DAYS days
  AND trial_expires_at  > now()
```

Дедупится через `AuditLog`: перед вставкой проверяется, что для этого `target_id` нет действия `trial_expiry_warning` или `trial_expiry_warning:delivered`. Повторные тики не задвоят нотификацию.

**Гейт (с 30.09.2026, `_trial_warning_not_needed`).** Юзера пропускаем, если у него есть живая (active/frozen) подписка и выполнено одно из двух: баланса хватает на её продление целиком (`total_renewal_cost_kopecks`, тариф + слоты) или она уже не неоплаченный триал (`balance.is_unpaid_trial` = false, человек платит). Пропуск дедупом не помечается, следующий тик в окне перепроверит. Юзера без живой подписки предупреждаем, как раньше. `user_has_paid` в гейт намеренно не берём: пополнение на 100 ₽ не покрывает продление за 150 ₽, и «пополни баланс» для такого триальщика правдив. Старые кабинетные триалы (`activate:{sub.id}`, без `trial-full`) под гейт попадают и этот пуш не получают, им в тот же момент приходит `renewal_reminder` 3d.

`balance.is_unpaid_trial(db, sub)`: есть `spend trial-full:{sub.id}` и нет ни `spend renew:{sub.id}`, ни `spend change_plan:{sub.id}`, ни оплаченного счёта с `subscription_id = sub.id` и суммой > 0 (продление картой или СБП строк в журнал не пишет).

Текст пуша (`api_extensions.py`, без числа дней и сумм, верен и для старых 30-дневных триалов):

> ⏳ Бесплатные дни скоро закончатся.
> Чтобы VPN не отключился, пополни баланс: /balance

Бот забирает её через `GET /api/notifications/pending`, матчится на `action == "trial_expiry_warning"`, доставляет копию и при успехе апдейтит action до `:delivered`.

### 3b. Clawback (на истечении)

```sql
WHERE trial_expires_at IS NOT NULL
  AND trial_expires_at <= now()
  AND trial_activated_at IS NOT NULL
```

Для каждого:

1. **Проверка «стал ли платящим»**: `balance.user_has_paid(db, uid)`. Правда, если есть оплаченный счёт с суммой > 0, или `kind=topup`, или `adjust admin_topup:%` > 0, или `spend renew:%`, или у юзера нет записи `trial:{uid}` (восстановленные через `generate_restore_sql.py` журнала не имеют). Если да → триал «отработан», просто `trial_expires_at = None`, clawback пропускается. До 30.09.2026 проверялся только `kind=topup`: оплату тарифа картой и ручное зачисление эта проверка не видела.
2. **Дедуп** по `reference=trial_expiry_clawback:{user.id}`. На всякий случай — сейчас `trial_expires_at=None` гарантирует, что мы сюда не попадаем повторно, но это подстраховка.
3. **Списание только непотраченной части бонуса** (`_unspent_trial_bonus_kopecks`): `min(баланс, max(0, trial:{uid} + referral_signup:{uid} − |Σ всех spend юзера|))`. Вычитаются все траты, а не только `trial-full:`: старые кабинетные триалы тратили бонус через `activate:{sub.id}`. Для триалов, потративших бонус на подписку, сумма 0, таймер просто обнуляется. Пишется как `kind=adjust` с `note="trial_expiry_clawback"`, только если сумма > 0.
4. `trial_expires_at = None` — флаг, что триал закрыт.

**Что НЕ трогаем:** деньги сверх бонуса (например, `referral_payout` за приглашённых) остаются при юзере всегда: формула снимает не больше непотраченного бонуса.

## WebApp integration

- `GET /api/webapp/me` возвращает в `balance`: `trial_available`, `trial_amount_kopecks`, `trial_days`, `trial_referral_days`, `trial_bonus_only` и `trial_autoactivate_allowed` (с 2026-09-30 всегда `false`, держим ради старых закэшированных бандлов). Подробно — [WEBAPP_REFERENCE.md](WEBAPP_REFERENCE.md).
- [webapp/src/pages/Home.tsx](../webapp/src/pages/Home.tsx) показывает баннер iff `trial_available`:
  - без живой подписки: «🎁 Забери 3 дня бесплатно» (по приглашению «6 дней» и строка «Тебя пригласил друг, поэтому дней не 3, а 6.»), кнопка «Активировать бесплатно»;
  - при `trial_bonus_only` (живая подписка, тап даст только деньги): «🎁 Подарок: 15 ₽ на баланс, это 3 дня подписки» (или «30 ₽, 6 дней»), «Зачтётся при следующем продлении.», кнопка «Забрать подарок».
- `handleActivateTrial` делает **один** вызов `POST /api/webapp/trial/activate`. В ответе есть `sub_token`/`sub_url` → экран «Готово» со строкой «Бесплатно на N дней», затем refresh `/me`. Нет → только refresh `/me` (бонус лёг на баланс). Второго шага «купить план на бонус» из браузера больше нет.
- Ошибки: 409 «уже активирован / уже использован / живая подписка» — молча refresh `/me`; 503 — «Сейчас много желающих, попробуй через пару минут.»; 402 — «Не получилось включить бесплатные дни. Напиши в поддержку из раздела «Помощь».»; остальное (в т.ч. 409 от сбоя провижининга) — «Не получилось забрать подарок. Проверь связь и попробуй ещё раз.»
- Пустое состояние без подписок: «Подписки пока нет. Забери бесплатные дни выше.» Первый пресет пополнения 150 ₽ (продление Solo), а не 100 ₽.
- Сразу после «Готово» красный баннер «⚠️ Подписка скоро кончится» (`minDays ≤ 3`) ожидаем, это не баг.

## Bot integration

- [handlers.py](../bot/handlers.py) `format_welcome(name, is_new, trial_available, trial_days, ref_days)` — склеивает welcome-копию из кусочков. `_trial_line(days, ref_days)` добавляется только если `trial_available=True`: «🎁 3 дня VPN бесплатно, карта не нужна.» или по приглашению «🎁 Тебя пригласил друг, поэтому у тебя 6 дней VPN бесплатно вместо 3. Карта не нужна.», затем «Один тап по кнопке ниже: получишь ссылку и инструкцию, как подключиться.». Кнопка ниже — «🎁 Забрать 3 дня бесплатно» / «🎁 Забрать 6 дней бесплатно» (`trial:activate`, нативная активация в боте, не web_app; `welcome_action_keyboard(..., trial_days=…)`). Дни берутся из `/users/register` (`trial_days`, `trial_referral_days`), в т.ч. после атрибуции реферала в том же `/start`. Если бэкенд недоступен — дефолт 3 + 0. Склонение — `bot/plural.py::plural_days`.
- `/plans` при доступном триале: «🎁 <b>Сначала 3 дня бесплатно.</b>» (или «…6 дней бесплатно: тебя пригласил друг.»), «Один тап по кнопке ниже, карта не нужна.», «Ниже тарифы, если захочешь больше устройств или сразу на год.»
- Для новых юзеров `is_new=True` даёт полную версию с bullet points; для returning — короткую шапку, но обе могут нести trial-строчку.
- Ветки ответа `activate_full` в `trial_activate_cb`: заголовок после 200 и тексты на 409/402 — см. «HTTP-обёртки» выше; 402: «Бонус уже потрачен, включить бесплатные дни с него не получилось. Выбери тариф: /plans, или загляни в баланс: /balance.»
- Покупка другого тарифа во время неоплаченного триала: `POST /api/invoices` отвечает 409 `on trial` (`balance.is_unpaid_trial`), бот: «Сейчас у тебя бесплатные дни на Solo. Другой тариф можно взять в личном кабинете, неиспользованные дни зачтутся, или напиши /help, поменяем вручную.» (запасной /help нужен: `confirm()` смены тарифа в кабинете в TG подавлен). Полноценная смена тарифа из бота — отдельная задача.
- `/referral`: «Друг получит 6 дней VPN бесплатно вместо 3, а тебе начислим 10 дней подписки, когда он впервые оплатит.» Числа из `/api/referral/code` (`invitee_total_days`, `bonus_days`, `reward_days`); если поля нет или числа не сходятся, бот опускает число или «вместо N», но не пишет ложное.
- Заморозки в боте нет (только «Разморозить» для уже замороженных), прятать нечего.
- Notification poller матчит `action == "trial_expiry_warning"` и шлёт копию о близком истечении.

## Verification checklist

Смок-тест (локально через docker-compose):

```bash
# 1. Миграция применяется
docker compose run --rm backend alembic upgrade head
# psql: \d users должен показать trial_activated_at, trial_expires_at

# 2. Trial new user (без реферала)
# → /start в боте: «🎁 3 дня VPN бесплатно», кнопка «Забрать 3 дня бесплатно»
# → тап → «🎉 Готово! VPN бесплатно на 3 дня.» + ссылка (или WebApp Home → баннер → Activate → экран «Готово»)
# → подписка до ≈ now + 4 сут., ledger: trial:<uid> +1500 / trial-full:<sub> −1500, баланс 0
# → аудит trial_activated_full (или trial_activated с full=true) с trial_days=3
# → повторный тап после истечения → 409 «Trial already used», бот: «Бесплатные дни уже использованы…»

# 3. Trial referee (по ref-ссылке)
# → новый tg_id, /start ref_<code>
# → в БД: referred_by_id выставлен, ref.uses инкрементнут, транзакций нет
# → «6 дней» в /start, /plans, на кнопке и в заголовке
# → подписка ≈ now + 7 сут., ledger: trial:+1500, referral_signup:+1500, trial-full:−3000
# → реферер пока без награды

# 4. Referrer payout
# → приглашённый впервые платит: пополнение ИЛИ счёт за тариф/продление (lava sandbox)
# → у реферера новая kind=bonus reference=referral_payout:<referee_uid>
# → повторная оплата того же юзера → никаких новых записей
# → юзер, который платил до привязки реферала, выплату не порождает

# 4a. Заморозка
# → у триальщика без оплат в кабинете нет «Заморозить», POST /freeze → 400 «Заморозка станет доступна после первой оплаты»
# → после оплаты renewal-счёта картой / admin_topup / renew: кнопка есть

# 5. Warning
# → UPDATE users SET trial_expires_at = NOW() + INTERVAL '2 days' WHERE id = X
# → подождать/прогнать balance_charge tick
# → AuditLog: action=trial_expiry_warning для X
# → бот доставил копию в чат
# → повторный tick → ноль новых записей

# 6. Clawback (не платящий)
# → UPDATE users SET trial_expires_at = NOW() - INTERVAL '1s' WHERE id = X
# → баланс = trial_amount, 0 топапов
# → tick → kind=adjust reference=trial_expiry_clawback:X на -min(непотраченный бонус, balance)
# → users.trial_expires_at = NULL

# 7. Clawback skip (платящий)
# → то же, но у юзера есть оплата (topup, оплаченный счёт, admin_topup, renew:) → clawback не пишется, trial_expires_at обнуляется
```

## Files involved

- [backend/app/models.py](../backend/app/models.py) — поля на User
- [backend/app/alembic/versions/0012_trial_bonus.py](../backend/app/alembic/versions/0012_trial_bonus.py) — миграция
- [backend/app/services/balance.py](../backend/app/services/balance.py) — константы, `trial_bonus()`, `referral_bonus()`, `activate_subscription(price_kopecks=, expires_at=)`, `user_has_paid()`, `is_unpaid_trial()`, `freeze_allowed_by_payment()`
- [backend/app/services/trial.py](../backend/app/services/trial.py) — `activate_trial()`, `activate_trial_full()`, `trial_amount_kopecks()`, `trial_days_for()`, `invitee_total_days()`
- [backend/app/api_extensions.py](../backend/app/api_extensions.py) — `/trial/activate_full` (бот), legacy `/trial/activate`, `/users/register` и `/referral/code` с днями, текст `trial_expiry_warning`
- [backend/app/api_webapp.py](../backend/app/api_webapp.py) — `/webapp/trial/activate`, поля триала и `can_freeze` на `/me`, 400 на `/freeze`, `earned` в `/webapp/referral`
- [backend/app/api/invoices.py](../backend/app/api/invoices.py) — `_maybe_pay_referrer` в обеих ветках `_mark_invoice_paid_core`, 409 `on trial` в `create_invoice`
- [backend/app/services/onboarding_funnel.py](../backend/app/services/onboarding_funnel.py) — `trial_failures` только по реальным провалам
- [backend/app/worker.py](../backend/app/worker.py) — phase 3 trial expiry pass (гейт 3a, clawback 3b)
- [bot/handlers.py](../bot/handlers.py), [bot/keyboards.py](../bot/keyboards.py), [bot/plural.py](../bot/plural.py) — `format_welcome()`, `_trial_line()`, `_trial_done_intro()`, ветки 409, `/referral`, trial notification mapping
- [webapp/src/pages/Home.tsx](../webapp/src/pages/Home.tsx) — баннер (в т.ч. бонус-онли) + активация одним вызовом
