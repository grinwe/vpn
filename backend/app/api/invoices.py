"""Invoice endpoints: ``/api/invoices/*``.

Includes both the admin CRUD (list, create, mark_paid, mark_unpaid,
cancel, batch) and the ``_mark_invoice_paid_core`` helper that is
reused by the payment webhook (``api/payments.py``). Keeping the
helper next to the admin handler rather than in ``_common`` makes the
ownership obvious — only the invoice and payment routes need it.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas
from ..auth import optional_admin as optional_admin_token
from ..auth import require_admin
from ..services.balance import total_renewal_cost_kopecks
from ..time_utils import utcnow
from ._common import (
    ADMIN_ACTOR_HEADER,
    _audit,
    _create_subscription_for_user,
    _get_user_from_payload,
    _resolve_admin_actor,
    get_db,
    logger,
)

router = APIRouter()


def _invoice_with_credentials(
    invoice: models.Invoice,
    credentials: list[models.Credential] | None = None,
    subscription: models.Subscription | None = None,
    task: models.ProvisioningTask | None = None,
) -> schemas.InvoicePaidOut:
    creds = credentials or []
    device_id = None
    if subscription and subscription.devices:
        device_id = subscription.devices[0].id
    return schemas.InvoicePaidOut(
        id=invoice.id,
        user_id=invoice.user_id,
        user_telegram_id=invoice.user.telegram_id if invoice.user else None,
        plan_id=invoice.plan_id,
        plan_name=invoice.plan.name if invoice.plan else "",
        subscription_id=invoice.subscription_id,
        amount=float(invoice.amount),
        currency=invoice.currency,
        status=invoice.status.value,
        action=invoice.action.value,
        kind=invoice.kind or "subscription",
        created_at=invoice.created_at,
        credentials=[schemas.CredentialOut.from_orm(c) for c in creds],
        provisioning_task_id=task.id if task else None,
        device_id=device_id,
    )


def _mark_invoice_paid_core(
    db: Session,
    invoice_id: int,
    *,
    actor: str,
    actor_type: models.AuditActor,
    payment_id: int | None = None,
) -> schemas.InvoicePaidOut:
    """Internal helper shared by the admin endpoint and payment webhooks.

    Kept outside of the route function so that the payment webhook code path
    (which does its own auth via HMAC signature, not admin token) can reuse
    the exact same "flip invoice to paid → provision subscription" logic.
    """
    # populate_existing ОБЯЗАТЕЛЕН: вызывающие (lava-reconcile-тик, вебхук,
    # telegram_webhook) уже подгрузили этот же Invoice в ту же сессию, и без
    # него SQLAlchemy вернёт объект из identity-map — с атрибутами, прочитанными
    # ДО взятия row-lock. Тогда проверки `status == paid` / `subscription_id`
    # внутри критической секции смотрят на устаревшее состояние, и два
    # параллельных зачисления (вебхук + сверка) могут оба увидеть pending
    # (аудит 2026-07-25). Тот же приём уже применён в api/traffic.py.
    invoice = (
        db.query(models.Invoice)
        .filter(models.Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status == models.InvoiceStatus.failed:
        raise HTTPException(status_code=400, detail="Invoice is marked as failed")

    if payment_id:
        payment = db.get(models.Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.subscription and payment.subscription.user_id != invoice.user_id:
            raise HTTPException(status_code=400, detail="Payment does not belong to invoice user")
        payment.status = models.PaymentStatus.paid
        db.add(payment)

    latest_subscription = invoice.subscription
    if invoice.status == models.InvoiceStatus.paid:
        # Инвойс уже оплачен (ретрай вебхука после ручного mark_paid или
        # повторная доставка вебхука провайдера). Payment мог быть только
        # что помечён paid выше — зафиксируем его, иначе get_db закроет
        # сессию с неявным откатом и Payment навсегда останется pending.
        if payment_id:
            db.commit()
        if not latest_subscription:
            latest_subscription = (
                db.query(models.Subscription)
                .filter(
                    models.Subscription.user_id == invoice.user_id,
                    models.Subscription.plan_id == invoice.plan_id,
                )
                .order_by(models.Subscription.created_at.desc())
                .first()
            )
        device = latest_subscription.devices[0] if latest_subscription and latest_subscription.devices else None
        task = None
        if device:
            task = (
                db.query(models.ProvisioningTask)
                .filter(
                    models.ProvisioningTask.target_type == "device",
                    models.ProvisioningTask.target_id == device.id,
                )
                .order_by(models.ProvisioningTask.created_at.desc())
                .first()
            )
        credentials = latest_subscription.credentials if latest_subscription else []
        return _invoice_with_credentials(invoice, credentials, subscription=latest_subscription, task=task)

    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(status_code=400, detail="Invoice cannot be paid in current status")

    # ── Stage 4: balance topup branch ───────────────────────────────
    # ``kind=topup`` means this invoice is just a wallet load — no
    # subscription provisioning, no plan to honor. Credit the user's
    # balance, mark the invoice paid, and return early. The legacy
    # path below still services ``kind=subscription`` invoices for
    # any in-flight purchases or admin-created plan invoices.
    if invoice.kind == "topup":
        from ..services import balance as balance_svc

        amount_kopecks = int(round(float(invoice.amount) * 100))
        if amount_kopecks <= 0:
            raise HTTPException(status_code=400, detail="Topup invoice has non-positive amount")

        # Идемпотентность топапа: инвариант «оплаченный topup-инвойс = ровно
        # одна topup-транзакция». Если строка reference=invoice:<id> уже есть
        # (ретрай вебхука CryptoBot при не-2xx, либо mark_paid после ошибочного
        # mark_unpaid), баланс уже зачислен — не кредитуем повторно, только
        # возвращаем инвойс в статус paid. balance.py дедупа по reference не
        # делает, поэтому защита живёт здесь.
        existing_tx = (
            db.query(models.BalanceTransaction)
            .filter_by(reference=f"invoice:{invoice.id}")
            .first()
        )
        if existing_tx is not None:
            invoice.status = models.InvoiceStatus.paid
            db.add(invoice)
            db.commit()
            db.refresh(invoice)
            return _invoice_with_credentials(invoice, [])

        # Referrer payout: runs strictly BEFORE we write the user's own
        # topup row so "first kind=topup" detection is unambiguous. If
        # the user was attributed to a referrer (via /users/register)
        # and has never completed a real topup before, credit
        # REFERRAL_BONUS_KOPECKS to the referrer. Idempotent by
        # reference — a retried webhook can't double-pay.
        #
        # Блокируем строку пополняемого пользователя (SELECT ... FOR UPDATE)
        # ДО проверок prior/already: два одновременных вебхука по двум
        # разным topup-инвойсам одного юзера лочат разные Invoice-строки и
        # без этой блокировки оба прошли бы дедуп до коммита друг друга →
        # двойная выплата бонуса. Блокировка сериализует их по одному
        # пользователю: второй увидит уже записанную topup-строку первого.
        topup_user = (
            db.query(models.User)
            .filter(models.User.id == invoice.user_id)
            .with_for_update()
            .first()
        )
        if topup_user and topup_user.referred_by_id is not None:
            prior = (
                db.query(models.BalanceTransaction)
                .filter_by(
                    user_id=topup_user.id,
                    kind=models.BalanceTxKind.topup,
                )
                .first()
            )
            if prior is None:
                ref_key = f"referral_payout:{topup_user.id}"
                already = (
                    db.query(models.BalanceTransaction)
                    .filter_by(reference=ref_key)
                    .first()
                )
                if already is None:
                    try:
                        # Награда в ДНЯХ: сколько именно — берём из кода
                        # реферера (там поля лежали с самой первой миграции и
                        # до сих пор не использовались), с падением на
                        # общий дефолт REFERRAL_REWARD_DAYS.
                        ref_code = (
                            db.query(models.ReferralCode)
                            .filter_by(owner_id=topup_user.referred_by_id)
                            .order_by(models.ReferralCode.id.desc())
                            .first()
                        )
                        reward_days = (
                            ref_code.reward_days
                            if ref_code and ref_code.reward_days
                            else balance_svc.REFERRAL_REWARD_DAYS
                        )
                        balance_svc.referral_bonus(
                            db,
                            topup_user.referred_by_id,
                            reference=ref_key,
                            days=reward_days,
                            note=f"referral reward: {reward_days}d",
                        )
                    except Exception:
                        # Don't fail the whole topup over a referral
                        # bonus write — log and move on. Payout will
                        # be retried by a nightly reconciliation if we
                        # ever add one; for now it's fire-and-forget.
                        logger.exception(
                            "referral payout failed for user=%s",
                            topup_user.id,
                        )

        try:
            balance_svc.topup(
                db,
                invoice.user_id,
                amount_kopecks,
                reference=f"invoice:{invoice.id}",
                kind=models.BalanceTxKind.topup,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to topup balance for invoice %s", invoice_id)
            raise HTTPException(status_code=500, detail="Failed to credit balance") from exc

        invoice.status = models.InvoiceStatus.paid
        db.add(invoice)
        db.commit()
        db.refresh(invoice)
        _audit(
            db,
            actor,
            "invoice_paid",
            "invoice",
            invoice.id,
            actor_type=actor_type,
            metadata={"kind": "topup", "amount_kopecks": amount_kopecks},
        )
        return _invoice_with_credentials(invoice, [])

    plan = db.get(models.Plan, invoice.plan_id)
    user = db.get(models.User, invoice.user_id)
    if not plan or not user:
        raise HTTPException(status_code=400, detail="Invoice is inconsistent: missing user or plan")

    credentials: list[models.Credential] = []
    subscription: models.Subscription | None = None
    task: models.ProvisioningTask | None = None
    try:
        # Страховка от гонки «оплатил new_subscription, пока активировался
        # триал»: к моменту зачисления у юзера уже есть живая подписка этого
        # плана, и провижининг второй гарантированно упадёт в «Device limit
        # reached» — так завис счёт #65 (инцидент 2026-08-21): 500 ловили и
        # вебхук, и reconcile-тик, и ручной mark paid. Проводим как продление
        # существующей — деньги получены ровно за период этого плана.
        if (
            invoice.action == models.InvoiceAction.new_subscription
            and not invoice.subscription_id
        ):
            existing = _active_subscription_for(db, invoice.user_id, invoice.plan_id)
            if existing is not None:
                logger.warning(
                    "invoice %s: new_subscription при живой подписке %s "
                    "(user %s, plan %s) — проводим как renewal",
                    invoice.id, existing.id, invoice.user_id, invoice.plan_id,
                )
                invoice.action = models.InvoiceAction.renewal
                invoice.subscription_id = existing.id

        if invoice.action == models.InvoiceAction.renewal:
            if not invoice.subscription_id:
                raise HTTPException(status_code=400, detail="Invoice missing subscription for renewal")
            subscription = db.get(models.Subscription, invoice.subscription_id)
            if not subscription:
                raise HTTPException(status_code=404, detail="Subscription not found for renewal")
            if subscription.user_id != invoice.user_id or subscription.plan_id != invoice.plan_id:
                raise HTTPException(status_code=400, detail="Invoice does not match subscription")
            now = utcnow()
            base_time = subscription.expires_at if subscription.expires_at > now else now
            subscription.expires_at = base_time + timedelta(days=plan.duration_days)
            # Новый оплаченный период — шкала трафика в клиенте начинает с нуля
            # (счётчик информационный, семантика «за период»).
            subscription.traffic_used_bytes = 0
            subscription.status = models.SubscriptionStatus.active
            # Ревью 2026-08-21: между выставлением счёта и оплатой подписка
            # могла замёрзнуть/заблокироваться/истечь — прежний код молча
            # ставил active, оставляя frozen_* поля (auto-unfreeze тик слеп
            # к active, ручной unfreeze падает на «not frozen») и ноль живых
            # девайсов (freeze/блокировка/grace-истечение их ревокают).
            # Деньги приняты — чистим freeze-поля (годовой лимит
            # has_frozen_this_year не возвращаем) и, если живых девайсов не
            # осталось, реповижним — зеркально unfreeze_subscription.
            subscription.frozen_at = None
            subscription.frozen_until = None
            db.add(subscription)
            db.flush()
            has_live_device = any(
                d.status
                not in (models.DeviceStatus.revoked, models.DeviceStatus.disabled)
                for d in subscription.devices
            )
            if not has_live_device:
                from ..services.provisioning import ProvisioningOrchestrator

                try:
                    ProvisioningOrchestrator(db).reprovision_subscription(subscription)
                except Exception:  # noqa: BLE001 — оплата важнее провижна
                    logger.exception(
                        "renewal invoice %s: reprovision после оплаты не удался — "
                        "подписка %s активна без девайсов",
                        invoice.id,
                        subscription.id,
                    )
                    from ..services.admin_notify import notify_admins

                    notify_admins(
                        db,
                        kind="renewal_reprovision_failed",
                        text=(
                            f"⚠️ Счёт #{invoice.id} оплачен и продлил подписку "
                            f"{subscription.id}, но выдать девайс не удалось — "
                            f"подписка активна без единого устройства. Нужен "
                            f"ручной репровижн."
                        ),
                        dedup_key={"subscription_id": subscription.id},
                        extra={
                            "invoice_id": invoice.id,
                            "subscription_id": subscription.id,
                        },
                        autocommit=False,
                    )
            invoice.subscription_id = subscription.id
            credentials = subscription.credentials
        else:
            if invoice.subscription_id:
                raise HTTPException(status_code=400, detail="Invoice already bound to subscription")
            subscription, task = _create_subscription_for_user(db, user, plan)
            credentials = subscription.credentials
            invoice.subscription_id = subscription.id
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process invoice %s", invoice_id)
        raise HTTPException(status_code=500, detail="Failed to create or update subscription") from exc

    invoice.status = models.InvoiceStatus.paid
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    _audit(
        db,
        actor,
        "invoice_paid",
        "invoice",
        invoice.id,
        actor_type=actor_type,
        metadata={"subscription_id": invoice.subscription_id},
    )
    return _invoice_with_credentials(invoice, credentials, subscription=subscription, task=task)


@router.post("/invoices/topup", response_model=schemas.InvoiceOut)
def create_topup_invoice(
    body: schemas.TopupInvoiceCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Счёт на пополнение баланса для бота (аудит 2026-08-21, паритет A).

    Зеркало webapp_topup без initData-аутентификации: бот ходит с
    admin-токеном и передаёт telegram_id юзера. Сумма всегда в рублях —
    конвертацию в валюту провайдера делает checkout, как у план-счетов
    бота. Проведение — штатная topup-ветка ``_mark_invoice_paid_core``
    (зачисление на баланс + реферальный бонус за первый топап).
    """
    from ..services import balance as balance_svc

    user = (
        db.query(models.User)
        .filter_by(telegram_id=str(body.telegram_id))
        .first()
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.amount_kopecks < balance_svc.MIN_TOPUP_KOPECKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Minimum topup is {balance_svc.MIN_TOPUP_KOPECKS // 100} ₽"
            ),
        )

    invoice = models.Invoice(
        user_id=user.id,
        plan_id=None,
        amount=body.amount_kopecks / 100,
        currency="RUB",
        action=models.InvoiceAction.new_subscription,
        kind="topup",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "invoice_created", "invoice", invoice.id,
        actor_type=actor_type,
        metadata={"kind": "topup", "amount_kopecks": body.amount_kopecks},
    )
    return invoice


def _active_subscription_for(
    db: Session, user_id: int, plan_id: int | None
) -> models.Subscription | None:
    """Живая подписка юзера на этот план — цель авто-renewal.

    Только ``active``: продление frozen размораживало бы её силой, а
    blocked — оживляло бы то, что заблокировали намеренно.
    """
    if plan_id is None:
        return None
    return (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user_id,
            models.Subscription.plan_id == plan_id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .order_by(models.Subscription.id.desc())
        .first()
    )


@router.post("/invoices", response_model=schemas.InvoiceOut)
def create_invoice(
    request: Request,
    body: schemas.InvoiceCreate,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin_token),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    plan = db.get(models.Plan, body.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    try:
        action = models.InvoiceAction(body.action)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid invoice action") from exc

    user = _get_user_from_payload(db, body.user_id, body.telegram_id)
    subscription = None
    if body.subscription_id:
        subscription = db.get(models.Subscription, body.subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if subscription.user_id != user.id or subscription.plan_id != plan.id:
            raise HTTPException(status_code=400, detail="Subscription does not match invoice data")

    # Бот шлёт new_subscription всегда (дефолт схемы), но «купить» при уже
    # живой подписке этого плана — всегда продление: вторая подписка упрётся
    # в per-user лимит девайсов плана при провижининге (инцидент 2026-08-21,
    # счёт #65). Переключаем прямо на создании, чтобы админка и вебхук видели
    # правду, а сумма ниже посчиталась ценой продления (со слотами).
    if action == models.InvoiceAction.new_subscription and subscription is None:
        existing = _active_subscription_for(db, user.id, plan.id)
        if existing is not None:
            logger.info(
                "create_invoice: new_subscription при живой подписке %s "
                "(user %s, plan %s) — счёт создаётся как renewal",
                existing.id, user.id, plan.id,
            )
            action = models.InvoiceAction.renewal
            subscription = existing
        elif not admin_token:
            # Покупка ДРУГОГО плана при живой подписке из бота создавала
            # вторую параллельную подписку с двойным списанием (аудит
            # 2026-08-21, C1). Смена тарифа с перерасчётом живёт в ЛК;
            # клиентский путь режем, админский (с токеном) — оставляем.
            other = (
                db.query(models.Subscription)
                .filter(
                    models.Subscription.user_id == user.id,
                    models.Subscription.status
                    == models.SubscriptionStatus.active,
                    models.Subscription.plan_id != plan.id,
                )
                .order_by(models.Subscription.id.desc())
                .first()
            )
            if other is not None:
                raise HTTPException(
                    status_code=409,
                    detail="user already has an active subscription on another plan",
                )
    # Сумму диктует СЕРВЕР. body.amount — только с админ-токеном: эндпоинт
    # доступен без него, а _mark_invoice_paid_core сумму с планом не сверяет —
    # клиентский renewal-инвойс на 1 ₽ продлевал бы подписку целиком (дыра из
    # ревью 2026-07-29). Вебхук провайдера сверяет платёж с Invoice.amount,
    # то есть с той же подконтрольной клиенту цифрой — защита обязана стоять
    # на создании счёта.
    if body.amount is not None and not admin_token:
        raise HTTPException(status_code=403, detail="amount override requires admin token")
    if body.amount is not None:
        amount = body.amount
    elif action == models.InvoiceAction.renewal and subscription is not None:
        # Цена продления со слотами: баланс-путь берёт доплату за
        # extra_device_slots, а invoice-путь её терял — два пути продления
        # брали разные деньги за один и тот же период.
        amount = total_renewal_cost_kopecks(subscription) / 100
    else:
        amount = float(plan.price)
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        # Не body.subscription_id: авто-renewal выше мог подставить живую
        # подписку, которой в body не было.
        subscription_id=subscription.id if subscription else None,
        amount=amount,
        currency=body.currency,
        action=action,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    if admin_token:
        actor, actor_type = _resolve_admin_actor(admin_actor)
    else:
        actor, actor_type = (body.telegram_id or str(user.id), models.AuditActor.user)
    _audit(db, actor, "invoice_created", "invoice", invoice.id, actor_type=actor_type)
    return schemas.InvoiceOut.from_orm(invoice)


@router.get("/invoices", response_model=list[schemas.InvoiceListItem])
def list_invoices(
    status: str | None = None,
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    # eager-load user/plan: карточка списка читает user.telegram_id и
    # plan.name на каждую строку — без joinedload это N+1 lazy-load.
    query = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.user), joinedload(models.Invoice.plan))
        .order_by(models.Invoice.created_at.desc())
    )
    if status:
        try:
            invoice_status = models.InvoiceStatus(status)
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
        query = query.filter(models.Invoice.status == invoice_status)

    invoices = query.offset(offset).limit(limit).all()
    result: list[schemas.InvoiceListItem] = []
    for inv in invoices:
        result.append(
            schemas.InvoiceListItem(
                id=inv.id,
                user_id=inv.user_id,
                user_telegram_id=inv.user.telegram_id if inv.user else None,
                plan_id=inv.plan_id,
                plan_name=inv.plan.name if inv.plan else "",
                subscription_id=inv.subscription_id,
                amount=float(inv.amount),
                currency=inv.currency,
                status=inv.status.value,
                action=inv.action.value,
                kind=inv.kind or "subscription",
                created_at=inv.created_at,
            )
        )
    return result


@router.post("/invoices/{invoice_id}/mark_paid", response_model=schemas.InvoicePaidOut)
def mark_invoice_paid(
    invoice_id: int,
    body: schemas.InvoiceMarkPaidRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    actor, actor_type = _resolve_admin_actor(admin_actor)
    return _mark_invoice_paid_core(
        db,
        invoice_id,
        actor=actor,
        actor_type=actor_type,
        payment_id=body.payment_id if body else None,
    )


@router.post("/invoices/{invoice_id}/cancel", response_model=schemas.InvoiceOut)
def cancel_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mark a stale pending invoice as failed (cancelled).

    Only works on pending invoices — paid ones should be mark_unpaid'd
    first if you truly need to void them.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(
            status_code=400,
            detail=f"Can only cancel pending invoices, this one is {invoice.status.value}",
        )
    invoice.status = models.InvoiceStatus.failed
    db.commit()
    db.refresh(invoice)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "invoice_cancelled", "invoice", invoice.id, actor_type=actor_type)
    return schemas.InvoiceOut.from_orm(invoice)


@router.post("/invoices/batch")
def batch_invoices(
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Batch action on multiple invoices.

    Body: ``{ "ids": [1,2,3], "action": "cancel" | "mark_paid" | "mark_unpaid" }``
    """
    ids = body.get("ids", [])
    action = body.get("action", "")
    if not ids or action not in ("cancel", "mark_paid", "mark_unpaid"):
        raise HTTPException(status_code=400, detail="ids (list) and action (cancel|mark_paid|mark_unpaid) required")

    actor, actor_type = _resolve_admin_actor(admin_actor)
    results: dict[str, list[int]] = {"ok": [], "skipped": [], "not_found": []}

    for inv_id in ids:
        invoice = db.get(models.Invoice, inv_id)
        if not invoice:
            results["not_found"].append(inv_id)
            continue

        if action == "cancel":
            if invoice.status != models.InvoiceStatus.pending:
                results["skipped"].append(inv_id)
                continue
            invoice.status = models.InvoiceStatus.failed
            _audit(db, actor, "invoice_cancelled", "invoice", inv_id, actor_type=actor_type)

        elif action == "mark_paid":
            if invoice.status != models.InvoiceStatus.pending:
                results["skipped"].append(inv_id)
                continue
            try:
                _mark_invoice_paid_core(db, inv_id, actor=actor, actor_type=actor_type)
            except HTTPException:
                results["skipped"].append(inv_id)
                continue

        elif action == "mark_unpaid":
            if invoice.status == models.InvoiceStatus.pending:
                results["skipped"].append(inv_id)
                continue
            # topup-инвойсы не откатываем (см. mark_invoice_unpaid): баланс
            # уже зачислен, откат ведёт к двойному зачислению.
            if invoice.kind == "topup":
                results["skipped"].append(inv_id)
                continue
            invoice.status = models.InvoiceStatus.pending
            _audit(db, actor, "invoice_marked_unpaid", "invoice", inv_id, actor_type=actor_type)

        results["ok"].append(inv_id)

    db.commit()
    return results


@router.post("/invoices/{invoice_id}/mark_unpaid", response_model=schemas.InvoiceOut)
def mark_invoice_unpaid(
    invoice_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Revert a mistakenly marked invoice back to pending.

    Does NOT touch the provisioned subscription/devices — if you also need
    to revoke access, do that separately. This is a bookkeeping fix.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == models.InvoiceStatus.pending:
        return schemas.InvoiceOut.from_orm(invoice)
    # topup-инвойс откатывать нельзя: баланс уже зачислен, а возврат в
    # pending открыл бы путь к повторному зачислению (mark_paid снова или
    # запоздалый ретрай вебхука). Для коррекции — balance adjustment.
    if invoice.kind == "topup":
        raise HTTPException(
            status_code=400,
            detail="Cannot revert a topup invoice — balance was already credited; use a balance adjustment instead",
        )
    invoice.status = models.InvoiceStatus.pending
    db.commit()
    db.refresh(invoice)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "invoice_marked_unpaid", "invoice", invoice.id, actor_type=actor_type)
    return schemas.InvoiceOut.from_orm(invoice)
