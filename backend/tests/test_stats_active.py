"""GET /api/stats — метрика «активны за 24ч» по реальному трафику.

Считаем distinct юзеров/устройств из NodeTrafficSample.details["users"] за
24ч (резолв username→Credential→Device→User), сирот (user_id=999999)
отдельно; сэмплы старше 24ч не учитываются.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app import models
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _cred(db: Session, node, device, username: str) -> None:
    db.add(
        models.Credential(
            node_id=node.id,
            device_id=device.id,
            access_username=username,
            is_active=True,
            proto="vless-reality",
            config_text="enc",
            pool_state=models.CredentialPoolState.assigned,
        )
    )


def test_stats_active_24h(client, db_session: Session) -> None:
    node = make_node(db_session, name="act-node", region="ru")
    cfg = make_config(db_session, node)
    plan = make_plan(db_session)

    # реальный юзер, активен в окне
    user = make_user(db_session, telegram_id="real")
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg, access_username="u-real")
    _cred(db_session, node, dev, "u-real")

    # сирота (recovery-плейсхолдер 999999), активен в окне
    orphan = models.User(id=999999, telegram_id="__recovery_orphans__")
    db_session.add(orphan)
    db_session.commit()
    osub = make_subscription(db_session, orphan, plan, node)
    odev = make_device(db_session, osub, cfg, access_username="u-orphan")
    _cred(db_session, node, odev, "u-orphan")

    # ещё один юзер, но его трафик-сэмпл СТАРШЕ 24ч → не должен считаться
    stale = make_user(db_session, telegram_id="stale")
    ssub = make_subscription(db_session, stale, plan, node)
    sdev = make_device(db_session, ssub, cfg, access_username="u-stale")
    _cred(db_session, node, sdev, "u-stale")

    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id,
            observed_at=utcnow(),
            details={"vless-reality": {"users": ["u-real", "u-orphan"]}},
        )
    )
    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id,
            observed_at=utcnow() - timedelta(hours=30),
            details={"vless-reality": {"users": ["u-stale"]}},
        )
    )
    db_session.commit()

    body = client.get("/api/stats").json()
    # u-real (сирота исключён из users), u-stale вне окна
    assert body["users_active_24h"] == 1
    # u-real + u-orphan устройства (u-stale вне окна)
    assert body["devices_active_24h"] == 2
    # только u-orphan
    assert body["orphans_active_24h"] == 1
