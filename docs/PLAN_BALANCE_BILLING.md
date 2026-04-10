# Plan: Balance Billing (Stage 4) — ✅ IMPLEMENTED 2026-04-08

> **Status:** all sections below shipped. Migration `0009_balance_billing` applied,
> `services/balance.py` + worker tick + WebApp + bot live, tests in
> `backend/tests/test_balance.py` (12 cases incl. threaded topup atomicity).
> Document kept for design rationale; deltas vs spec:
> - `Invoice.plan_id` was made nullable (topup invoices have no plan).
> - `reprovision_subscription` helper was added on `ProvisioningOrchestrator`
>   to preserve `sub_token` across unfreeze (not just call `provision_subscription`).
> - Seed daily rates landed in migration `0009`, not via separate UPDATE.
>
> **⚠️ Architectural update 2026-04-10 — prepaid bucket rework.**
> Чистая модель «wallet → daily spend», описанная ниже, была изменена: юзеры могли
> тривиально обходить месячный план, кладя 5 ₽ и катаясь день-в-день. Решение —
> per-subscription prepaid bucket, авторитативный reference теперь
> [BALANCE_REFERENCE.md](BALANCE_REFERENCE.md). Краткая суть изменений:
> - Новая колонка `subscriptions.prepaid_kopecks` (миграция `0013_sub_prepaid` +
>   backfill `0014_backfill_prepaid` для существующих подписок).
> - При активации подписки вся цена `plan.price` списывается с кошелька как
>   одна `kind=spend` транзакция и оседает в `sub.prepaid_kopecks` — это
>   делает новый `balance_svc.activate_prepaid()`. Webapp `/activate` требует
>   полную цену плана, не один день.
> - `charge_subscription` сначала дренит `sub.prepaid_kopecks` **без записи
>   в ledger** (spend уже учтён), при исчерпании — fallback в кошелёк с
>   `kind=spend, reference=daily:{sub}:{date}`.
> - `sub_days_remaining(sub, devices, wallet_kopecks=0)` — новая сигнатура,
>   UI показывает `(prepaid + wallet) // daily_cost`.
> - `MAX_FREEZE_DAYS_PER_PERIOD` снижен с `14` до `7`.
> - Freeze остаётся pause-only: bucket замораживается as-is, при unfreeze
>   просто продолжает дрениться.
> - `refund_prepaid()` добавлен в balance.py под будущий явный `/cancel`,
>   текущими кодом-паттернами не используется.
>
> Design-документ ниже сохранён как исторический контекст оригинальной
> stage-4 модели. Для поведения текущего прода — смотри BALANCE_REFERENCE.md.



Cycle goal: switch from "invoice = 1 month subscription" to "invoice =
balance topup; subscription burns balance per day". Same UX shape as
hitvpn — пользователь видит «Баланс: 320 ₽ · хватит на 21 день»,
кнопка «Пополнить», тарифы — это per-day rate, а не billing period.

DB scaffold landed in [`0008_warmpool_and_balance.py`](../backend/app/alembic/versions/0008_warmpool_and_balance.py)
(`users.balance_kopecks`, `plans.daily_rate_kopecks`, `balance_transactions`).
This document covers the application layer on top of that scaffold.

## Decisions (locked)

| # | Decision | Why |
|---|---|---|
| c | Anchor-based списание: `next_charge_at` пер-подписка, advance `+24h` каждый успешный charge | Юзер платит ровно за свои сутки от активации, никакой UTC-дискриминации |
| f | Реферал = 50 ₽ обоим, `kind=bonus` | ~неделя на Solo, 5 дней на Family. Универсальный механизм через transaction journal |
| freeze | Pause-only model: при заморозке списания не идут, девайсы физически revoke'аются с ноды (слот освобождается). Никаких refund/cancel-кнопок. | Меньше абуза, проще учёт. Юзер «уходит» = перестаёт топапить, balance→0, sub→expired |
| max_freeze | 14 дней за раз, **30 дней в год** суммарно | Защита от «вечной паузы» |
| tick | Hourly charge tick (а не daily) | Anchor-based: у разных юзеров разные часы, нужна гранулярность ≤1ч |
| min_topup | 100 ₽ | Минимальный invoice Stars + защита от микротранзакций |

## Migration `0009_balance_billing`

Идемпотентная (как `0008`).

**Subscription:**
- `next_charge_at: DateTime nullable` — anchor для следующего списания. NULL для legacy invoice-подписок (cron их игнорирует, доживают до `expires_at`).
- `frozen_at: DateTime nullable`
- `frozen_until: DateTime nullable`
- `frozen_days_used: Integer NOT NULL DEFAULT 0` — суммарно за `frozen_year`
- `frozen_year: Integer nullable` — UTC год, к которому относится `frozen_days_used`. На переходе года сбрасывается.
- `SubscriptionStatus.frozen` — добавить значение в enum через `ALTER TYPE ... ADD VALUE IF NOT EXISTS`

**Plan seed:** UPDATE существующих планов с `daily_rate_kopecks`:
- Solo (30d, 150 ₽) → 500 коп/день (~ 150/30)
- Family (30d, 300 ₽) → 1000 коп/день
- Pro (30d, 500 ₽) → 1700 коп/день
- Solo-Year (1200 ₽) → 330 коп/день
- Family-Year (2400 ₽) → 660 коп/день
- Pro-Year (4000 ₽) → 1100 коп/день

**Invoice:** добавляем `kind: String NOT NULL DEFAULT 'subscription'` (`subscription` | `topup`). Старые инвойсы остаются с `subscription`. Новые — `topup`.

## Service `services/balance.py`

```
topup(db, user_id, amount_kopecks, *, reference, note=None, kind=topup)
    → BalanceTransaction
    Atomic: SELECT user FOR UPDATE, increment balance, write tx.

charge_subscription(db, sub) → bool
    Atomic: SELECT user FOR UPDATE, compute cost = daily_rate * device_count.
    If balance < cost: return False (caller flips status to expired).
    Else decrement, write spend tx, advance next_charge_at += 24h.

freeze_subscription(db, sub) → None
    Validates year budget (frozen_days_used + 14 ≤ 30, with frozen_year reset).
    Sets status=frozen, frozen_at=now, frozen_until=now+14d.
    Calls revoke_device(...) for each device — releases node capacity.

unfreeze_subscription(db, sub, *, auto=False) → None
    Adds (now - frozen_at).days to frozen_days_used.
    Re-provisions devices via warm pool (or cold fallback). If original
    node is full, choose_node picks another in the same pool.
    status=active, next_charge_at = now (immediately starts new day).

referral_bonus(db, user_id, *, reference) → BalanceTransaction
    50 ₽ topup with kind=bonus, reference=referrer_user_id.

days_remaining(user, plan, devices) → int
    balance_kopecks // (daily_rate * devices). Used by UI.
```

Все мутации — через `with_for_update()` на user-row. Один воркер-процесс
+ row lock = нет дублей и race'ов.

## Worker job `run_balance_charge_tick`

Hourly self-rescheduling (mirror `run_warm_pool_check`).

```python
def run_balance_charge_tick():
    # 1. Charge due subscriptions
    due = SELECT * FROM subscriptions
          WHERE status='active' AND next_charge_at <= now
          FOR UPDATE SKIP LOCKED LIMIT 500
    for sub in due:
        ok = balance.charge_subscription(db, sub)
        if not ok:
            sub.status = expired
            # existing renewal_check grace path handles physical revoke

    # 2. Auto-unfreeze
    expired_freezes = SELECT * FROM subscriptions
                      WHERE status='frozen' AND frozen_until <= now
                      FOR UPDATE SKIP LOCKED LIMIT 100
    for sub in expired_freezes:
        balance.unfreeze_subscription(db, sub, auto=True)

    # 3. Re-enqueue self
    enqueue_in(BALANCE_CHARGE_INTERVAL, run_balance_charge_tick)
```

Env vars:
- `BALANCE_CHARGE_INTERVAL` (default 3600)
- `MAX_FREEZE_DAYS_PER_PERIOD` (default 14)
- `FREEZE_YEAR_BUDGET_DAYS` (default 30)
- `REFERRAL_BONUS_KOPECKS` (default 5000 = 50 ₽)
- `MIN_TOPUP_KOPECKS` (default 10000 = 100 ₽)

Metrics:
- `vpn_balance_charges_total{result}` (`success`/`insufficient`)
- `vpn_balance_topups_total{kind}` (`topup`/`bonus`/`refund`/`adjust`)
- `vpn_balance_charge_amount_kopecks_total` (counter, всего списано)
- `vpn_balance_subs_frozen` (gauge — текущее число замороженных)

## Payment integration

Repurpose Stars + CryptoBot. Existing `_mark_invoice_paid_core` branches:

```python
if invoice.kind == "topup":
    balance.topup(db, invoice.user_id, invoice.amount,
                  reference=f"invoice:{invoice.id}", kind="topup")
    notify_user(...)
else:
    # legacy: provision subscription as before
    ...
```

Старый плано-биллинг **не выпиливаем** — legacy invoices доживают
естественным путём. Новые покупки идут через topup → balance → activate.

## WebApp endpoints

- `GET /api/webapp/me` → добавить `balance_kopecks`, `balance_human`,
  per-sub `days_remaining` и `frozen_until`
- `POST /api/webapp/topup {amount_kopecks}` → создаёт Stars-invoice с
  `kind=topup`, возвращает `slug` (как сейчас)
- `POST /api/webapp/subscriptions/activate {plan_id}` →
  - Если `balance >= daily_rate`: создаёт sub, `next_charge_at=now`,
    сразу charge'ит первый день, выдаёт warm-bundle. Возвращает sub.
  - Иначе 402 Payment Required + suggested topup amount.
- `POST /api/webapp/subscriptions/{id}/freeze`
- `POST /api/webapp/subscriptions/{id}/unfreeze`

`POST /api/webapp/checkout` (старый plan-purchase) — оставить, помечен
deprecated, можно удалить когда WebApp полностью переедет на activate.

## WebApp UI changes

- **Header**: «Баланс: 320 ₽» + «хватит на 21 день» (min по всем активным subs)
- **Plans page**: карточки показывают «5 ₽/день · 1 устройство», CTA = «Активировать»
- **Sub card**: бейджи `active|frozen`, кнопки «Заморозить» / «Разморозить (осталось N дней в году)»
- **Topup modal**: пресеты 100/300/600/1500 ₽ + custom, открывается через `tg.openInvoice`

## Bot changes

- `/balance` — показывает баланс + days_remaining + кнопку «Пополнить» (deep link в WebApp)
- Notification «баланс < 3 дней» — рядом с renewal-нотификациями
- Реферал: старая «+3 дня обоим» → «+50 ₽ обоим» через `balance.referral_bonus`

## Tests

`tests/test_balance.py`:
1. `topup` атомарен под 10 потоками — баланс == sum(amounts)
2. `charge_subscription` списывает корректно, продвигает `next_charge_at`
3. Insufficient balance → возвращает False, charge не делает партиал-spend
4. `freeze` физически revoke'ает девайсы (mock ansible) и блокирует charge
5. `freeze` отказывает при превышении year-budget (30 дней)
6. `unfreeze` re-provision'ит через warm pool, возвращает в active
7. `referral_bonus` создаёт `kind=bonus` tx, баланс растёт

## Files touched

**New:**
- `backend/app/alembic/versions/0009_balance_billing.py`
- `backend/app/services/balance.py`
- `backend/tests/test_balance.py`
- `docs/PLAN_BALANCE_BILLING.md` (этот файл)

**Modified:**
- `backend/app/models.py` — Subscription поля, SubscriptionStatus.frozen, Invoice.kind
- `backend/app/worker.py` — `run_balance_charge_tick` + bootstrap
- `backend/app/api_webapp.py` — новые endpoints, обновлённый `/me`
- `backend/app/services/payments/_invoice_core.py` (или где `_mark_invoice_paid_core`) — branch на topup
- `webapp/src/pages/Home.tsx`, `Plans.tsx` — баланс + activate-флоу
- `webapp/src/api.ts` — новые методы
- `bot/handlers.py` — `/balance` + новый реферал
- `docker-compose.yml` — env vars `BALANCE_CHARGE_INTERVAL` etc.
- `infra/ansible/roles/deploy_app_stack/templates/env.j2` — те же
- `README.md`, `docs/ANALYSIS.md`

## Out of scope (this cycle)

- Refund/cancel button (юзер уходит через перестать топапить).
- Промокоды на discount (только bonus-topup пока).
- Полное удаление legacy invoice→subscription пути (доживёт само).
- Заморозка с продолжением списания (отброшена при обсуждении).
- Daily-rate миграция между планами с пересчётом — пока plan определяется
  при `activate`, смена тарифа = freeze + activate с другим планом.
