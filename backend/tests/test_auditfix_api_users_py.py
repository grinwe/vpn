"""Аудит-фикс #37: `_sub_sharing_blocked` без 2N сканов audit_logs.

Проверяем, что после переписывания на один агрегирующий запрос
семантика не поменялась: подписка «заблокирована», если у любого
живого email последний `sharing_block` свежее последнего
`sharing_unblock`, упомянувшего этот email.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import models
from app.api.users import _sub_sharing_blocked

from .factories import make_node, make_plan, make_subscription_with_device, make_user


def _audit_row(
    db,
    action: str,
    extra: dict,
    created_at: datetime,
) -> None:
    db.add(
        models.AuditLog(
            actor="test",
            action=action,
            target_type="subscription",
            target_id=None,
            created_at=created_at,
            extra=extra,
        )
    )
    db.commit()


def test_no_audit_rows_means_not_blocked(db_session):
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription_with_device(db_session, user, plan, node, access_username="u-1-1")
    assert _sub_sharing_blocked(db_session, sub) is False


def test_block_without_unblock_is_blocked(db_session):
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription_with_device(db_session, user, plan, node, access_username="u-1-1")
    _audit_row(
        db_session, "sharing_block", {"email": "u-1-1"}, datetime.utcnow() - timedelta(hours=1)
    )
    assert _sub_sharing_blocked(db_session, sub) is True


def test_unblock_after_block_clears(db_session):
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription_with_device(db_session, user, plan, node, access_username="u-1-1")
    now = datetime.utcnow()
    _audit_row(db_session, "sharing_block", {"email": "u-1-1"}, now - timedelta(hours=2))
    # unblock батчится по подписке — email лежит в массиве extra->'emails'
    _audit_row(db_session, "sharing_unblock", {"emails": ["u-1-1"]}, now - timedelta(hours=1))
    assert _sub_sharing_blocked(db_session, sub) is False


def test_reblock_after_unblock_is_blocked_again(db_session):
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription_with_device(db_session, user, plan, node, access_username="u-1-1")
    now = datetime.utcnow()
    _audit_row(db_session, "sharing_block", {"email": "u-1-1"}, now - timedelta(hours=3))
    _audit_row(db_session, "sharing_unblock", {"emails": ["u-1-1"]}, now - timedelta(hours=2))
    _audit_row(db_session, "sharing_block", {"email": "u-1-1"}, now - timedelta(hours=1))
    assert _sub_sharing_blocked(db_session, sub) is True


def test_foreign_email_block_does_not_affect_sub(db_session):
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription_with_device(db_session, user, plan, node, access_username="u-1-1")
    _audit_row(
        db_session, "sharing_block", {"email": "someone-else"}, datetime.utcnow()
    )
    assert _sub_sharing_blocked(db_session, sub) is False


def test_revoked_device_email_is_ignored(db_session):
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription_with_device(db_session, user, plan, node, access_username="u-1-1")
    device = sub.devices[0]
    device.status = models.DeviceStatus.revoked
    db_session.commit()
    _audit_row(db_session, "sharing_block", {"email": "u-1-1"}, datetime.utcnow())
    db_session.refresh(sub)
    assert _sub_sharing_blocked(db_session, sub) is False
