# Balance billing reference

Как устроен кошелёк, ledger и daily-billing цикл. Если тебе нужно «почему юзер заплатил N, а списалось M», «откуда взялась эта `kind=adjust` строка», «как вообще считается daily_rate с extra devices» — отвечает этот документ, а за ним — [balance.py](../backend/app/services/balance.py).

Связанная документация:
- [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md) — trial/referral механика, тоже сидит поверх этого ledger'а
- [WEBAPP_REFERENCE.md](WEBAPP_REFERENCE.md) — как webapp экспонирует balance данные
- [DEPLOY.md § 8](DEPLOY.md#8-auto-renewal-и-balance-billing) — воркер-тики

## Model overview

Две «ямы» с деньгами: общий кошелёк юзера и per-subscription prepaid bucket. Ledger — единый signed.

```
users.balance_kopecks           ← общий кошелёк (cached cumulative по ledger'у)
balance_transactions            ← signed ledger (+topup/bonus/refund, −spend/adjust)
subscriptions.prepaid_kopecks   ← per-sub prepaid bucket (НЕ в ledger'е)
subscriptions.next_charge_at    ← per-sub anchor for the daily tick
subscriptions.frozen_*          ← freeze state + year-budget accounting
plans.daily_rate_kopecks        ← per-day bundle rate (NULL for legacy invoice plans)
plans.max_devices               ← bundled device cap (extras are surcharged)
```

**Две ямы, зачем?** Чтобы юзер не мог купить месячный план и ездить его «по 1₽ в день» из пустого кошелька. При активации вся цена плана (`plan.price`) списывается с кошелька как одна `kind=spend` транзакция и моментально оседает в `sub.prepaid_kopecks`. Дальше daily tick сначала дёргает этот бакет (без записей в ledger — spend уже учтён), и только когда бакет пуст — fallback в кошелёк (уже с `kind=spend` записью).

**Invariant**: `user.balance_kopecks == SUM(balance_transactions.amount_kopecks WHERE user_id=user.id)`. Prepaid bucket **вне** этого равенства — он представляет деньги, уже покинувшие кошелёк (spend уже записан), но ещё не «сгоревшие» как услуга. Кешированная колонка — source of truth для reads; ledger — source of truth для аудита и ночной reconciliation. Обе двигаются вместе через [`_record_tx()`](../backend/app/services/balance.py#L160), никогда одна без другой.

## BalanceTxKind enum

| Kind | Sign | Что значит | Где пишется |
|------|------|-----------|-------------|
| `topup` | + | Реальные деньги через платёжного провайдера | `_mark_invoice_paid_core` для `invoice.kind='topup'` |
| `bonus` | + | Halya: trial bonus, referral signup bonus, referrer payout | `trial_bonus()`, `referral_bonus()` |
| `spend` | − | (a) Активация подписки — вся цена плана сразу, ref=`activate:{sub}`. (b) Fallback-чардж из кошелька, когда prepaid bucket пуст, ref=`daily:{sub}:{date}` | `activate_prepaid()`, `charge_subscription()` |
| `adjust` | ± | Ручные корректировки + clawback trial'а не-платящих + prepaid refund | `adjustment()`, `refund_prepaid()`, trial expiry в worker'е |
| `refund` | + | Возврат через провайдера (зарезервировано) | — |

**Важно**: ежедневный draw из `sub.prepaid_kopecks` **не** пишет `kind=spend` — одна активация = одна транзакция. Ledger-запись на daily tick появляется только когда prepaid исчерпан и мы лезем в кошелёк (fallback).

**Fungibility**: `bonus` деньги тратятся точно так же, как `topup` — `charge_subscription` не различает источник. Разница только в tag'е для отчётов («сколько мы раздали бонусов vs заработали реально»).

## Tunables (env)

Все через `os.getenv`, читаются один раз при импорте модуля (процесс надо перезагрузить для применения):

| Var | Default | Эффект |
|-----|---------|--------|
| `MAX_FREEZE_DAYS_PER_PERIOD` | `7` | Сколько дней блокирует один вызов `freeze_subscription` |
| `FREEZE_YEAR_BUDGET_DAYS` | `30` | Жёсткий годовой cap на замороженные дни per subscription |
| `REFERRAL_BONUS_KOPECKS` | `5000` | Сумма обеих референс-выплат (рефералу и рефереру) |
| `TRIAL_DURATION_DAYS` | `30` | Длина trial-окна (см. TRIAL_SYSTEM.md) |
| `TRIAL_EXPIRY_WARN_DAYS` | `3` | За сколько дней до истечения worker шлёт warning |
| `MIN_TOPUP_KOPECKS` | `10000` | Минимальный топап-инвойс (₽100) |
| `EXTRA_DEVICE_KOPECKS_PER_MONTH` | `10000` | Доплата за устройство сверх `plan.max_devices` |
| → `EXTRA_DEVICE_DAILY_KOPECKS` | computed | `EXTRA_DEVICE_KOPECKS_PER_MONTH // 30`, floor 1 |

## Pricing formula

[`_daily_cost_kopecks(plan, device_count)`](../backend/app/services/balance.py#L116):

```
cost = plan.daily_rate_kopecks + max(0, device_count − plan.max_devices) × EXTRA_DEVICE_DAILY_KOPECKS
```

Ключевое:

- **Bundle pricing**, не per-device. Family @ 10₽/день буквально значит «весь 3-девайсный бандл за 10₽», а не «10₽ за каждый девайс». Юзер с 3 девайсами платит те же 10₽, что и юзер с 1.
- **Extras over cap** — считается только превышение, без скидок в обратную сторону. 4 девайса на 3-девайсном плане = `base + 1 × extra_daily`.
- **Zero-device floor**: подписка без единого live-девайса всё равно стоит `base` (не 0). Иначе юзер мог бы заморозить/revoke'нуть всё и ездить подписку бесплатно.
- **Legacy invoice plans** (где `daily_rate_kopecks IS NULL`) — `_daily_cost_kopecks` кидает `RuntimeError`. Эти плана биллятся старым renewal-cron'ом, не сюда. Caller guard'ит.

`_live_device_count` считает девайсы с `status NOT IN (revoked, disabled)` — frozen sub имеет 0 по construction (freeze revoke'ает всё физически).

## Locking discipline

Единственная точка сериализации — `SELECT * FROM users WHERE id=? FOR UPDATE` через [`_lock_user()`](../backend/app/services/balance.py#L141). Через него проходят **все** мутации баланса:

- `topup()` / `trial_bonus()` / `referral_bonus()`
- `charge_subscription()` (блокирует юзера *до* чтения device_count, чтобы топап, прилетевший между read и debit, не ушёл мимо)
- `adjustment()`

Это значит: параллельные webapp-топап + webhook-топап + charge-tick + admin adjust на одного и того же юзера — все сериализуются через одну строку. Остальная БД свободна.

**Caller owns commit**: balance-функции делают `flush()`, но не `commit()`. HTTP-слой / worker-tick коммитят сами. Это чтобы можно было запаковать «зачисли + создай подписку + enqueue задачу» в одну транзакцию.

## Daily charge tick

Живёт в [worker.py run_balance_charge_tick()](../backend/app/worker.py). Тикает раз в `BALANCE_CHARGE_INTERVAL` (default 3600 секунд = час), но выбирает подписки по anchor'у, а не по часам, так что опоздавший тик не дрейфует окна.

Упрощённый цикл:

```python
due = session.query(Subscription).filter(
    Subscription.status == SubscriptionStatus.active,
    Subscription.next_charge_at <= now,
).all()
for sub in due:
    ok = balance_svc.charge_subscription(session, sub)
    if not ok:
        sub.status = SubscriptionStatus.frozen  # insufficient → pause
        # (обработка revoke девайсов отдельной фазой)
session.commit()
```

[`charge_subscription`](../backend/app/services/balance.py#L237):

1. Row-lock юзера (да, лок нужен даже для prepaid-draw — fallback-ветка пишет в кошелёк, а ещё чтобы топап между read и debit был виден).
2. Пересчитать `cost = _daily_cost_kopecks(plan, _live_device_count())` **под локом**.
3. Если `prepaid_kopecks + balance_kopecks < cost` → `False`. Caller решает, что делать (воркер фризит или переводит в expired).
4. **Prepaid-first draw**: `from_prepaid = min(prepaid, cost); from_wallet = cost − from_prepaid`. Декрементит `sub.prepaid_kopecks -= from_prepaid` **без ledger-записи** — этот spend уже был зафиксирован в `activate_prepaid()`.
5. Если `from_wallet > 0` — пишет `kind=spend` на `-from_wallet` с `reference=daily:{sub_id}:{today}`, note `"daily charge (N devices[, +K over plan]) (wallet fallback)"`.
6. **Anchor advance**: `next_charge_at += 24h` от **старого значения**, не от `now`. Если тик опоздал на 5 минут, окно юзера не сдвигается. Пропустили сутки целиком — следующий тик перехватит два окна подряд без дрейфа.
7. Инкрементит prometheus `vpn_balance_charges_total{result="success"}` + `vpn_balance_charge_amount_kopecks_total`.

Insufficient (ни prepaid, ни wallet) → `CHARGES_TOTAL{result="insufficient"}` + лог. Anchor не двигается — на следующем тике попробуем снова.

### Runway hint для UI

Пока юзер держит одну активную подписку (типичный случай), его «когда кончится» = `(prepaid + wallet) // daily_cost`. Хелпер — [`sub_days_remaining(sub, devices, wallet_kopecks)`](../backend/app/services/balance.py#L642). Для мульти-саб юзеров caller ответственен за то, чтобы не считать кошелёк дважды (либо передать `0`, либо руками поделить). В webapp/worker сейчас просто передаём весь кошелёк — для 1-sub случая точно, для мульти — слегка оптимистично.

## Freeze / unfreeze

Freeze — это **pause**, не refund. Пока заморожено:
- `sub.status = frozen`
- `sub.next_charge_at = NULL` (тик пропускает)
- все live девайсы физически revoke'аются через orchestrator (освобождает слот на ноде)
- `SUBS_FROZEN` gauge инкрементируется

**Prepaid bucket во время freeze**: `sub.prepaid_kopecks` не трогается. При freeze девайсы уходят в revoke, tick пропускает sub, бакет висит как есть. При unfreeze всё возвращается — прошедшие дни simply не оплачивались (pause-семантика, не refund). Это и есть «заморозка денег», буквально.

Year budget accounting:

- `frozen_days_used` и `frozen_year` трекают потраченное в текущем календарном году.
- При смене года — автоматический reset через `if sub.frozen_year != current_year: used = 0`.
- Проверка перед freeze: `if used + MAX_FREEZE_DAYS_PER_PERIOD > FREEZE_YEAR_BUDGET_DAYS: reject`. То есть конкретно — **не** «осталось X дней», а «поместится ли полный `MAX_FREEZE_DAYS_PER_PERIOD` в бюджете». Consequence: если у юзера осталось 5 дней на год, а `MAX_FREEZE_DAYS_PER_PERIOD=7` — `freeze` упадёт. Сейчас это by design: юзер должен иметь full-period budget, либо ждать нового года.
- Budget — **per subscription**, не per user. Разные подписки → разные пулы freeze. Нельзя гейминг «пятое устройство на новой подписке пожру все фризы».

Unfreeze:

- `elapsed_days = ceil((now - frozen_at) / 86400)`, floor 1, прибавляется к `frozen_days_used`. Early unfreeze (через 3 часа) всё равно стоит 1 день бюджета — иначе rapid freeze/unfreeze ничего не стоили бы.
- Anchor восстанавливается: `next_charge_at = now` (свежие 24ч с момента размораживания).
- Re-provision одного девайса через warm pool. `sub_token` сохраняется, так что установленные у юзера клиенты auto-update'ятся через sub-link без ре-конфигурации.
- Если reprovision упал — unfreeze **не** откатывается. Sub остаётся active, девайс может добавить юзер руками из WebApp. Same failure mode что и cold provisioning miss.

`auto=True` в `unfreeze_subscription` — это просто audit-log маркер для worker'а, когда `frozen_until` истёк сам. Поведение идентично ручному.

## Topup → balance path

Это правда единственная точка, где реальные деньги становятся балансом. Инвойс с `kind='topup'` создаётся через `POST /api/webapp/topup` (см. [WEBAPP_REFERENCE.md](WEBAPP_REFERENCE.md#post-apiwebapptopup)), провайдер отдаёт `pay_url`, юзер платит, webhook дергает `_mark_invoice_paid_core` в [api.py:1195](../backend/app/api.py#L1195):

```python
if invoice.kind == "topup":
    # 1. Referrer payout check (first real topup ever?)
    if user.referred_by_id is not None:
        prior = db.query(BalanceTransaction).filter_by(
            user_id=user.id, kind=BalanceTxKind.topup).first()
        if prior is None:
            balance_svc.referral_bonus(db, user.referred_by_id,
                                       reference=f"referral_payout:{user.id}")
    # 2. Actual topup
    balance_svc.topup(db, user.id, amount_kopecks=int(invoice.amount * 100),
                      reference=f"invoice:{invoice.id}", kind=BalanceTxKind.topup)
```

Важно: **referrer payout проверяется ДО записи своего топапа**, чтобы условие «первый топап» было однозначным. Дубль-защита — идемпотентный reference `referral_payout:{user_id}` плюс блокировка строки пополняемого пользователя (`SELECT ... FOR UPDATE`) перед проверкой: два одновременных topup-вебхука одного юзера сериализуются, второй видит уже записанный первый топап и бонус не повторяет. Подробнее: [TRIAL_SYSTEM.md § Stage 3](TRIAL_SYSTEM.md#stage-3--referrer-payout-on-referees-first-real-topup).

## Activation: `activate_prepaid()` / `refund_prepaid()`

[`activate_prepaid(db, user_id, sub, reference)`](../backend/app/services/balance.py#L550) — это точка перехода «кошелёк → prepaid bucket». Вызывается из [webapp_activate](../backend/app/api_webapp.py) при покупке подписки:

1. Берёт `price = plan_price_kopecks(sub.plan)` (Decimal → kopecks).
2. Row-lock юзера, проверка `balance_kopecks >= price` (иначе 402 с `insufficient_balance`).
3. Пишет **одну** `kind=spend` транзакцию на `-price` с note `"prepay plan {name} (sub {id})"` — это и есть учёт в ledger'е. Кошелёк уменьшается.
4. `sub.prepaid_kopecks += price` — деньги «зарезервированы» под эту конкретную подписку.

После этого `charge_subscription` ежедневно дренит бакет без ledger-шума.

[`refund_prepaid(db, sub, reference, note)`](../backend/app/services/balance.py#L597) — обратная операция: возвращает остаток бакета в кошелёк как `kind=refund`, обнуляет `sub.prepaid_kopecks`. Сейчас **не используется** — ни freeze (там pause), ни disable (симметрия с enable). Висит в модуле под будущий явный `/cancel subscription` flow, где юзер добровольно сворачивает подписку и хочет деньги назад.

## `adjustment()` — единственный способ списать минусовую сумму мимо charge

`topup()` жёстко требует `amount > 0` и не принимает отрицательные. Единственная санкционированная точка для отрицательного движения мимо `charge_subscription` — [`adjustment()`](../backend/app/services/balance.py#L479):

- Принимает signed `amount_kopecks`.
- Для negative — **полит баланс**: `cap = -balance_kopecks; if amount < cap: amount = cap`. То есть никогда не загоняет юзера в минус — максимум до нуля.
- Если после cap'а amount == 0 → возвращает non-persisted «пустую» транзакцию, чтобы caller не получил exception (но в ledger ничего не попадает).
- Пишет `kind=adjust` с caller-provided `reference` и `note`.

**Единственный текущий caller** — trial expiry clawback в worker'е ([worker.py Phase 3](../backend/app/worker.py)). Админы пока ручные adjustments делают через psql (UI пока нет, см. [ADMIN_UI.md](ADMIN_UI.md) — отсутствует кнопка «adjust balance»).

## Prometheus metrics

Всё в [`/metrics`](../backend/app/main.py):

| Metric | Labels | Что считает |
|--------|--------|------------|
| `vpn_balance_charges_total` | `result={success,insufficient}` | Попытки daily charge |
| `vpn_balance_charge_amount_kopecks_total` | — | Сумма успешно списанных копеек |
| `vpn_balance_topups_total` | `kind` | Ledger-записи любого вида (включая spend/adjust) |
| `vpn_balance_subs_frozen` | — | Gauge: сколько подписок сейчас в `frozen` статусе |

Grafana dashboard: `docs/dashboards/business-metrics.json`. Критичные алерты (пока не настроены явно — в backlog):
- `rate(vpn_balance_charges_total{result="insufficient"}[1h])` — нормально ненулевое (истекающие подписки), но резкий всплеск = возможный баг в pricing.
- `vpn_balance_subs_frozen` спайк — либо массовый freeze (редко), либо массовый insufficient → auto-freeze.

## Reconciliation (plan, not yet implemented)

Идея: ночная задача считает `SUM(amount_kopecks) GROUP BY user_id` и сверяет с `users.balance_kopecks`. Рассинхрон = какой-то writer забыл пройти через `_record_tx`. Сейчас такой задачи нет, потому что все writer'ы централизованы в balance.py — но как safety net на случай ручных SQL-фиксов, план есть.

## Common operations cheat sheet

| Задача | Как |
|--------|-----|
| Посмотреть ledger юзера | `SELECT * FROM balance_transactions WHERE user_id=X ORDER BY id DESC LIMIT 50` |
| Сверить баланс с ledger | `SELECT balance_kopecks, (SELECT SUM(amount_kopecks) FROM balance_transactions WHERE user_id=u.id) FROM users u WHERE id=X` — должны совпадать |
| Найти, кто списал | `SELECT kind, reference, note, amount_kopecks, created_at FROM balance_transactions WHERE user_id=X AND amount_kopecks < 0` |
| Trial clawback у юзера | `WHERE reference = 'trial_expiry_clawback:X'` |
| Prepaid bucket подписки | `SELECT id, prepaid_kopecks FROM subscriptions WHERE id = X` |
| Сколько у юзера всего в двух ямах | `SELECT u.balance_kopecks + COALESCE(SUM(s.prepaid_kopecks),0) FROM users u LEFT JOIN subscriptions s ON s.user_id = u.id WHERE u.id = X GROUP BY u.id` |
| Поставить юзеру премию руками | Через UI пока нет. psql: напрямую `INSERT INTO balance_transactions (...) VALUES (..., 'adjust', 'manual:ticket-123')` + `UPDATE users SET balance_kopecks = balance_kopecks + X WHERE id = Y`. **Обе операции в одной транзакции**. |
| Фриз подписки руками | Сейчас нет admin-ручки. WebApp `/freeze` работает от имени самого юзера. Иначе — psql `UPDATE`, но тогда ты не триггеришь физический revoke девайсов, и слот на ноде останется занят. |
