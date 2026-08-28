# Trial + Referral system

Единственный источник правды по механике бесплатного месяца и реферальным выплатам. Если этот документ расходится с кодом — прав код, документ подлежит апдейту.

## Why it exists

Старая реферальная модель сжигала ссылку на первом `/start` юзера и обещала «+3 дня» в бот-копии — фактически бонус никому не капал, если юзер хоть раз до этого нажал на бота (гейт `created=True` отсеивал всех). Welcome-копия обещала «первый день в подарок», которого в коде не было вообще.

Новая модель:

1. **Free trial** — любой юзер, включая ретроактивно существующих, один раз активирует триал через WebApp. Баланс получает сумму, равную стоимости Basic 1m (читаем из БД — не хардкод). Дальше юзер тратит эти «виртуальные рубли» штатной покупкой подписки, без отдельной trial-ветки в провижининге.

   > **Авто-активация (2026-07-22, фикс воронки).** Раньше `activate_trial` только зачислял бонус на баланс, а активацию подписки юзер должен был сделать ВТОРЫМ, неочевидным шагом — 62% застревали с деньгами на балансе без работающего VPN. Теперь фронт (`Home.tsx::handleActivateTrial`) сразу после `activateTrial()` вызывает `activateSubscription(cheapestMonthly.id)` — тратит бонус на самый дешёвый месячный план (ровно тот, под который размерен бонус в `_trial_plan`) и провижнит девайсы. «Забрать месяц» = рабочий VPN сразу. Авто-активация в try/catch: при сбое бонус остаётся на балансе (не хуже прежнего), юзер активирует вручную. Бэкенд `activate_trial` не менялся.
   >
   > **Бот-путь (2026-08-21, кнопка `trial:activate`).** ЛК у части юзеров не открывается, поэтому бот делает то же самое ОДНИМ вызовом `POST /api/trial/activate_full` (бонус + подписка самого дешёвого месячного плана, см. «HTTP-обёртки» ниже). После 200 бот шлёт в чат саб-ссылку тем же `cmd_config`, что и `/config`, передавая заголовок `intro="🎉 Бесплатный месяц активирован до DD.MM.YYYY."`. **Что приходит юзеру (warm-путь, 2026-08-28): ровно два сообщения** — (1) заголовок + «🔗 Твоя ссылка для подключения. Тапни по ней ниже, чтобы скопировать. Не знаешь, как настроить? Выбери платформу 👇» с клавиатурой онбординга (ЛК + 4 платформы), (2) голый URL. Пуш `config_ready` на warm-хите не шлётся (`activate_trial_full` зовёт `provision_subscription(..., notify_config_ready=False)`: ссылка уже отдана), приглашение «приведи друга» приходит через `REFERRAL_INVITE_DELAY_H` (24 ч). На cold-пути (девайс ещё pending) первое сообщение — заголовок + «⏳ Твой конфиг ещё создаётся», затем подсказка про ЛК, а ссылка приезжает пушем `config_ready` по завершении Ansible. Если прислать не удалось — фолбэк «забери в личном кабинете или /config» с тем же заголовком и кнопкой ЛК (инцидент 2026-08-25: выдача падала молча).
2. **Трёхстадийный реферал** — атрибуция отвязана от начисления, а начисление рефереру отложено до первой **реальной** оплаты рефералом. Это убивает фарм.

## Data model

[models.py:272-277](../backend/app/models.py#L272) — два nullable поля на `User`:

```python
trial_activated_at = Column(DateTime, nullable=True)
trial_expires_at   = Column(DateTime, nullable=True)  # activated_at + TRIAL_DURATION_DAYS
```

Миграция: [alembic/versions/0012_trial_bonus.py](../backend/app/alembic/versions/0012_trial_bonus.py) — `ALTER TABLE users ADD COLUMN IF NOT EXISTS ...`, идемпотентно.

Никаких новых таблиц. Все денежные движения — через существующий `BalanceTransaction` ledger.

## Environment

| Var | Default | Effect |
|-----|---------|--------|
| `TRIAL_DURATION_DAYS` | `30` | Длина триал-окна от момента активации |
| `TRIAL_EXPIRY_WARN_DAYS` | `3` | За сколько дней до истечения воркер шлёт нотификацию |
| `REFERRAL_BONUS_KOPECKS` | `5000` | Сумма обоих рефёрл-выплат (рефералу при активации триала, рефереру при первой оплате) |

Ставятся в [balance.py:57-60](../backend/app/services/balance.py#L57).

## Activation flow

Сервис: [services/trial.py](../backend/app/services/trial.py) — функция `activate_trial(db, user_id)` живёт в одном месте и вызывается из обоих HTTP-энтрипоинтов (admin-scoped из бота, WebApp-scoped по initData).

Последовательность:

1. `SELECT ... FOR UPDATE` по `user_id`. Два параллельных тапа в ту же секунду пройдут стейл-чек `trial_activated_at IS NULL`, но только один выиграет лок.
2. Повторная проверка под локом → `TrialAlreadyActivated` (→ HTTP 409).
3. Чтение цены триала: `_trial_plan()` — cheapest visible 30-day plan. Если нет — `NoTrialPlan` (→ 503), баннер в UI в этом случае тоже не рендерится.
4. `user.trial_activated_at = now()`, `user.trial_expires_at = now() + TRIAL_DURATION_DAYS`.
5. `balance_svc.trial_bonus(db, user.id, amount, reference=f"trial:{user.id}")` — пишет `kind=bonus` транзакцию. Уникальный reference делает повторные попытки безопасными.
6. Если `user.referred_by_id is not None` — дополнительно `balance_svc.referral_bonus(db, user.id, reference=f"referral_signup:{user.id}")`. Это **референс-бонус** — деньги идут рефералу (тому, кто сейчас активирует триал), а не рефереру.
7. `commit()` — делает его HTTP-слой.

HTTP-обёртки:
- Admin: [api_extensions.py:278](../backend/app/api_extensions.py#L278) — `POST /api/trial/activate`, принимает `telegram_id` в теле.
- WebApp: [api_webapp.py:828](../backend/app/api_webapp.py#L828) — `POST /api/webapp/trial/activate`, `user_id` берётся из проверенного initData (JWT).

Оба возвращают `TrialActivateResponse { trial_amount_kopecks, referral_bonus_kopecks, balance_kopecks, trial_expires_at }`.

- Бот, целиком: `POST /api/trial/activate_full` (`api_extensions.py::activate_trial_full`, admin-scoped) — `activate_trial` + провижининг подписки на самый дешёвый видимый месячный план + `balance.activate_subscription`. Идемпотентно по бонусу: «уже активирован» не ошибка, застрявшего на бонусе доделывает. Ответ `{subscription_id, plan_name, expires_at}`. Коды: **409** `User already has a live subscription` — подписка (active/frozen) уже есть → бот отдаёт ссылку вместо прайса; **409** с другим `detail` — `str(RuntimeError)` провижининга (нет нод / лимит устройств): бонус зачислен, подписки нет → бот честно шлёт в `/help`, прайс не показывает; **402** — бонуса не хватило (подписка гасится); **503** — нет плана или наплыв (`Retry-After`). Что бот делает после 200 — см. блок «Бот-путь» выше.

## Referral: three stages

### Stage 1 — attribution (on /start ref_XXX)

Происходит в [api_extensions.py `/users/register`](../backend/app/api_extensions.py). Без гейта `created=True` — работает для любого юзера, пока его `referred_by_id IS NULL`. Проверки:

- `ref.owner_id != user.id` (никаких self-referrals)
- `ref.is_active`
- `user.referred_by_id IS NULL` (атрибуция одноразовая)

На успехе — выставляется `user.referred_by_id = ref.owner_id`, инкрементится `ref.uses`. **Никаких денежных начислений на этой стадии.**

Респонс `/users/register` отдаёт `trial_available: bool` — бот использует его для условной строчки «🎁 Первый месяц — в подарок» в welcome, без второго запроса.

### Stage 2 — referee bonus (on trial activation)

Начисляется внутри `activate_trial()` сразу после собственного trial-бонуса, если `user.referred_by_id` стоит. Сумма: `REFERRAL_BONUS_KOPECKS`. Reference: `referral_signup:{user.id}`.

Итого рефёрл, активировавший триал по ссылке: **trial_amount + 50₽** на балансе, две транзакции в ledger.

### Stage 3 — referrer payout (on referee's first real topup)

Живёт в [api/invoices.py:_mark_invoice_paid_core](../backend/app/api/invoices.py#L61), в ветке `if invoice.kind == "topup":` (строки 132-204), **до** записи самого топапа:

```python
if topup_user.referred_by_id is not None:
    prior = db.query(BalanceTransaction).filter_by(
        user_id=topup_user.id, kind=BalanceTxKind.topup).first()
    if prior is None:
        ref_key = f"referral_payout:{topup_user.id}"
        if not db.query(BalanceTransaction).filter_by(reference=ref_key).first():
            balance_svc.referral_bonus(db, topup_user.referred_by_id, reference=ref_key)
```

Защиты:
- **«Первый kind=topup»** — проверяется ДО записи своего, чтобы условие было однозначным.
- **Идемпотентность** — дубль-проверка по уникальному `reference=referral_payout:{user_id}`. Повторный webhook от провайдера не задвоит выплату.
- **Fire-and-forget** — исключение в `referral_bonus` логируется, но не роллбэчит топап. Потеря 50₽ лучше, чем упавший платёж.

**Анти-фарм:** 10 фейк-аккаунтов = 10 × 50₽ халявы рефералам (они пришли по ссылке), но **0₽ рефереру**, пока фейки не занесут реальные деньги. Реальная экономика — реферер получает выплату только при появлении платящего юзера.

## Expiry worker

Третий pass в `run_balance_charge_tick()` — [worker.py:507-616](../backend/app/worker.py#L507). Бежит на том же интервале, что и балансовые списания (`BALANCE_CHARGE_INTERVAL`, default 3600s).

### 3a. Warning (за N дней до истечения)

```sql
WHERE trial_expires_at IS NOT NULL
  AND trial_expires_at <= now() + TRIAL_EXPIRY_WARN_DAYS days
  AND trial_expires_at  > now()
```

Дедупится через `AuditLog`: перед вставкой проверяется, что для этого `target_id` нет действия `trial_expiry_warning` или `trial_expiry_warning:delivered`. Повторные тики не задвоят нотификацию.

Бот забирает её через `GET /api/notifications/pending`, матчится на `action == "trial_expiry_warning"`, доставляет копию и при успехе апдейтит action до `:delivered`.

### 3b. Clawback (на истечении)

```sql
WHERE trial_expires_at IS NOT NULL
  AND trial_expires_at <= now()
  AND trial_activated_at IS NOT NULL
```

Для каждого:

1. **Проверка «стал ли платящим»** — есть ли хоть одна `kind=topup` транзакция у юзера. Если да → триал «отработан», просто `trial_expires_at = None`, clawback пропускается.
2. **Дедуп** по `reference=trial_expiry_clawback:{user.id}`. На всякий случай — сейчас `trial_expires_at=None` гарантирует, что мы сюда не попадаем повторно, но это подстраховка.
3. **Списание**: `amount = min(15000, user.balance_kopecks)` — не уходим в минус даже если юзер уже частично потратил триал. Пишется как `kind=adjust` с `note="trial_expiry_clawback"`.
4. `trial_expires_at = None` — флаг, что триал закрыт.

**Что НЕ трогаем:** реферальные 50₽, которые пришли на стадии 2. Они остаются навсегда. Это цена привлечения юзера, и отбирать её нечестно.

## WebApp integration

- `GET /api/webapp/me` возвращает `balance.trial_available: bool` и `balance.trial_amount_kopecks: int` ([api_webapp.py:229](../backend/app/api_webapp.py#L229)).
- [webapp/src/pages/Home.tsx](../webapp/src/pages/Home.tsx) показывает карточку «🎁 Забери пробный месяц» iff `trial_available`. Тап → `POST /api/webapp/trial/activate` → refresh `/me` → баннер скрывается.
- После claim'а бонус **автоматически тратится** на самый дешёвый месячный план (фикс воронки, `18c1d26`) — но только при `balance.trial_autoactivate_allowed=true`, т.е. когда живой подписки нет. У юзера с действующей подпиской авто-активация пропускается: `/subscriptions/activate` — это смена тарифа, он бы отозвал текущую подписку и ревокнул девайсы без подтверждения (аудит 2026-07-25, находка #1).
- На 409 — тост «Триал уже активирован», `/me` всё равно рефрешится.

## Bot integration

- [handlers.py](../bot/handlers.py) `format_welcome(name, is_new, trial_available)` — склеивает welcome-копию из кусочков. `_TRIAL_LINE` (`"🎁 Первый месяц — бесплатно, карта не нужна.\nОдин тап по кнопке ниже: получишь ссылку и инструкцию, как подключиться."`) добавляется только если `trial_available=True`; кнопка ниже — «🎁 Забрать бесплатный месяц» (`trial:activate`, нативная активация в боте, не web_app). Используется для `/start`, в т.ч. после атрибуции реферала.
- Для новых юзеров `is_new=True` даёт полную версию с bullet points; для returning — короткую шапку, но обе могут нести trial-строчку.
- Notification poller матчит `action == "trial_expiry_warning"` и шлёт копию о близком истечении.

## Verification checklist

Смок-тест (локально через docker-compose):

```bash
# 1. Миграция применяется
docker compose run --rm backend alembic upgrade head
# psql: \d users должен показать trial_activated_at, trial_expires_at

# 2. Trial new user (без реферала)
# → /start в боте, welcome с trial-строчкой
# → WebApp Home → баннер виден → tap Activate
# → баланс = trial_amount, ledger: одна kind=bonus reference=trial:<uid>
# → повторный tap → 409

# 3. Trial referee (по ref-ссылке)
# → новый tg_id, /start ref_<code>
# → в БД: referred_by_id выставлен, ref.uses инкрементнут, транзакций нет
# → активировать триал → баланс = trial_amount + 50, ledger: trial:... + referral_signup:...
# → реферер пока без бонуса

# 4. Referrer payout
# → реферал делает первый реальный topup (cryptobot sandbox)
# → у реферера новая kind=bonus reference=referral_payout:<referee_uid>
# → повторный топап того же юзера → никаких новых записей

# 5. Warning
# → UPDATE users SET trial_expires_at = NOW() + INTERVAL '2 days' WHERE id = X
# → подождать/прогнать balance_charge tick
# → AuditLog: action=trial_expiry_warning для X
# → бот доставил копию в чат
# → повторный tick → ноль новых записей

# 6. Clawback (не платящий)
# → UPDATE users SET trial_expires_at = NOW() - INTERVAL '1s' WHERE id = X
# → баланс = trial_amount, 0 топапов
# → tick → kind=adjust reference=trial_expiry_clawback:X на -min(15000, balance)
# → users.trial_expires_at = NULL

# 7. Clawback skip (платящий)
# → то же, но у юзера есть kind=topup → clawback не пишется, trial_expires_at обнуляется
```

## Files involved

- [backend/app/models.py](../backend/app/models.py) — поля на User
- [backend/app/alembic/versions/0012_trial_bonus.py](../backend/app/alembic/versions/0012_trial_bonus.py) — миграция
- [backend/app/services/balance.py](../backend/app/services/balance.py) — константы, `trial_bonus()`, `referral_bonus()`
- [backend/app/services/trial.py](../backend/app/services/trial.py) — `activate_trial()`, `trial_amount_kopecks()`
- [backend/app/api_extensions.py](../backend/app/api_extensions.py) — admin-scoped `/trial/activate`, refactored `/users/register`
- [backend/app/api_webapp.py](../backend/app/api_webapp.py) — WebApp wrapper + `trial_available` на `/me`
- [backend/app/api/invoices.py](../backend/app/api/invoices.py) — referrer payout hook в `_mark_invoice_paid_core`
- [backend/app/worker.py](../backend/app/worker.py) — phase 3 trial expiry pass
- [bot/handlers.py](../bot/handlers.py) — `format_welcome()`, trial notification mapping
- [webapp/src/pages/Home.tsx](../webapp/src/pages/Home.tsx) — баннер + активация
