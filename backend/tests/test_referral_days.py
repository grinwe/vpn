"""Реферальная программа: награда днями и приглашение в момент «вау».

Было: обе стороны получали фиксированные 50 ₽ (REFERRAL_BONUS_KOPECKS), а поля
`bonus_days` / `reward_days`, лежащие в модели с самой первой миграции, не
читал никто. Фиксированная сумма обесценивается при каждом повышении прайса, а
«месяц подписки» читается одинаково всегда и стоит нам маржи, а не выручки.

Приглашение позвать друга шлём один раз — сразу после первого скачивания
конфига: до этого человеку нечего рекомендовать.
"""
from __future__ import annotations

from app import models
from app.services import balance as balance_svc

from .factories import make_plan, make_user


def _visible_30d_plan(db_session, price: float = 150.0):
    """Прайс, по которому оценивается подарок (самый дешёвый видимый 30-дневный)."""
    plan = make_plan(db_session)
    plan.price = price
    plan.duration_days = 30
    plan.is_visible = True
    db_session.commit()
    return plan


def test_days_to_kopecks_follows_price(db_session):
    _visible_30d_plan(db_session, price=150.0)
    # 150 ₽ за 30 дней → 5 ₽ в день.
    assert balance_svc.days_to_kopecks(db_session, 30) == 15000
    assert balance_svc.days_to_kopecks(db_session, 7) == 3500
    assert balance_svc.days_to_kopecks(db_session, 0) == 0


def test_days_to_kopecks_tracks_price_change(db_session):
    """Подарок не должен застывать: подняли прайс — подорожал и день."""
    plan = _visible_30d_plan(db_session, price=150.0)
    before = balance_svc.days_to_kopecks(db_session, 30)
    plan.price = 300.0
    db_session.commit()
    assert balance_svc.days_to_kopecks(db_session, 30) == before * 2


def test_referral_bonus_credits_days_not_flat_sum(db_session):
    _visible_30d_plan(db_session, price=150.0)
    user = make_user(db_session, telegram_id="ref-owner")
    tx = balance_svc.referral_bonus(
        db_session, user.id, reference="test:1", days=30
    )
    assert tx.amount_kopecks == 15000  # месяц подписки, а не 5000 копеек
    assert tx.kind == models.BalanceTxKind.bonus


def test_referral_bonus_without_price_refuses(db_session):
    """Нет видимого 30-дневного плана — оценить подарок нечем. Пишем не нулевую
    транзакцию (она заняла бы reference и заблокировала выплату навсегда), а
    падаем, чтобы вызывающий решил сам."""
    import pytest

    user = make_user(db_session, telegram_id="no-price")
    with pytest.raises(ValueError):
        balance_svc.referral_bonus(db_session, user.id, reference="test:2", days=30)


def test_referral_bonus_legacy_flat_sum_still_works(db_session):
    """Без days падаем на прод-константу — прод-env её задаёт явно."""
    user = make_user(db_session, telegram_id="legacy")
    tx = balance_svc.referral_bonus(db_session, user.id, reference="test:3")
    assert tx.amount_kopecks == balance_svc.REFERRAL_BONUS_KOPECKS


def test_invitee_gets_days_on_trial(db_session):
    """Приглашённому — подарок из bonus_days его кода; реферер за триал НЕ
    получает ничего (иначе рефералка вырождается в фарм триалов)."""
    from app.services.trial import activate_trial

    _visible_30d_plan(db_session, price=150.0)
    owner = make_user(db_session, telegram_id="owner-1")
    code = models.ReferralCode(owner_id=owner.id, code="abc123", bonus_days=7,
                               reward_days=30)
    db_session.add(code)
    invitee = make_user(db_session, telegram_id="invitee-1")
    invitee.referred_by_id = owner.id
    db_session.commit()

    activate_trial(db_session, invitee.id)
    db_session.commit()

    invite_tx = (
        db_session.query(models.BalanceTransaction)
        .filter_by(reference=f"referral_signup:{invitee.id}")
        .one()
    )
    assert invite_tx.amount_kopecks == balance_svc.days_to_kopecks(db_session, 7)

    owner_rows = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=owner.id)
        .all()
    )
    assert owner_rows == []


def test_first_config_fetch_marks_user_and_queues_invite(db_session):
    """Первое скачивание конфига → метка + ровно одно приглашение."""
    from app.api_extensions import _mark_first_config_fetch

    user = make_user(db_session, telegram_id="fetcher")
    sub = type("SubStub", (), {"user_id": user.id})()

    _mark_first_config_fetch(db_session, sub)
    db_session.refresh(user)
    assert user.first_config_fetch_at is not None

    # Повторные фетчи (клиент дёргает ссылку каждые пару часов) не должны
    # порождать новых приглашений.
    _mark_first_config_fetch(db_session, sub)
    invites = (
        db_session.query(models.AuditLog)
        .filter_by(action="referral_invite", target_id=user.id)
        .all()
    )
    assert len(invites) == 1
    extra = invites[0].extra or {}
    assert extra["telegram_id"] == "fetcher"
    assert extra["reward_days"]


def test_referral_invite_is_deliverable(db_session):
    """Kind должен быть в allowlist поллера, иначе приглашение осядет в БД."""
    from app.api_extensions import ADMIN_NOTIFICATION_ACTIONS

    assert "referral_invite" in ADMIN_NOTIFICATION_ACTIONS


def test_one_referral_code_per_user(db_session):
    """Бот и мини-апп минтили коды разного формата, и человек видел две разные
    ссылки. Хелпер должен отдавать один и тот же код."""
    from app.api_extensions import ensure_referral_code

    user = make_user(db_session, telegram_id="single-code")
    first = ensure_referral_code(db_session, user)
    db_session.commit()
    second = ensure_referral_code(db_session, user)
    assert first.code == second.code
    assert db_session.query(models.ReferralCode).filter_by(owner_id=user.id).count() == 1
