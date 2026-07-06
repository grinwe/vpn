"""Аудит-фиксы node_spawner (волна 2, находка #78).

#78 — после reinstall на ноду авто-resync (resync_node_clients) возвращает
только vless-семейство; пер-юзерные hysteria2-учётки (auth=userpass) молча
теряются. ShadowTLS НЕ затронут — там общий node-wide пароль, который site.yml
восстанавливает сам. Полный фикс (расширение resync'а на hysteria2) живёт в
provisioning.py — вне владения этого файла. Здесь проверяем оперативную
страховку, добавленную в node_spawner: ``_warn_lost_hysteria2_users`` собирает
именно те hysteria2-учётки, которые resync НЕ восстановит, чтобы оператор
переспровижинил их вручную.

Проверяем, что helper:
  * ловит активную (через подписку) hysteria2-учётку;
  * ловит warm-бандл hysteria2 (без подписки, по node_id);
  * НЕ включает vless-семейство (его resync и так вернёт);
  * НЕ включает shadowtls_ss (его вернёт site.yml — общий node-wide пароль);
  * не подхватывает hysteria2 другой ноды и отозванные (is_active=False).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services.node_spawner import _warn_lost_hysteria2_users
from tests.factories import make_node, make_plan, make_subscription, make_user


def _cred(
    db: Session,
    *,
    proto: models.VPNConfigProtocol,
    access_username: str,
    node_id: int | None = None,
    subscription_id: int | None = None,
    is_active: bool = True,
    pool_state: models.CredentialPoolState = models.CredentialPoolState.assigned,
) -> models.Credential:
    cred = models.Credential(
        subscription_id=subscription_id,
        node_id=node_id,
        proto=proto.value,
        config_text=encrypt("dummy://cred"),
        access_username=access_username,
        is_active=is_active,
        pool_state=pool_state,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


def test_warn_lost_hysteria2_users_collects_only_hy2(db_session: Session):
    db = db_session
    node = make_node(db, name="reinst-node-1", host="203.0.113.5")
    other = make_node(db, name="reinst-node-2", host="203.0.113.6")
    plan = make_plan(db)
    user = make_user(db, telegram_id="tg-78")
    sub = make_subscription(db, user, plan, node)

    # Активная hysteria2-учётка на ноде (через подписку) — должна попасть.
    _cred(
        db,
        proto=models.VPNConfigProtocol.hysteria2,
        access_username="hy2-active",
        subscription_id=sub.id,
        node_id=node.id,
    )
    # Warm hysteria2 на ноде (без подписки) — должна попасть.
    _cred(
        db,
        proto=models.VPNConfigProtocol.hysteria2,
        access_username="hy2-warm",
        node_id=node.id,
        subscription_id=None,
        is_active=False,
        pool_state=models.CredentialPoolState.warm,
    )
    # shadowtls_ss — восстанавливается site.yml (node-wide), в список НЕ должно.
    _cred(
        db,
        proto=models.VPNConfigProtocol.shadowtls_ss,
        access_username="stls-active",
        subscription_id=sub.id,
        node_id=node.id,
    )
    # vless-семейство — resync его вернёт, в список НЕ должно.
    _cred(
        db,
        proto=models.VPNConfigProtocol.vless_reality,
        access_username="vless-active",
        subscription_id=sub.id,
        node_id=node.id,
    )
    # Отозванная активная hysteria2 (is_active=False, есть подписка) — не в счёт.
    _cred(
        db,
        proto=models.VPNConfigProtocol.hysteria2,
        access_username="hy2-revoked",
        subscription_id=sub.id,
        node_id=node.id,
        is_active=False,
    )
    # hysteria2 на ДРУГОЙ ноде — не должна утекать в результат для node.
    other_user = make_user(db, telegram_id="tg-78b")
    other_sub = make_subscription(db, other_user, plan, other)
    _cred(
        db,
        proto=models.VPNConfigProtocol.hysteria2,
        access_username="hy2-othernode",
        subscription_id=other_sub.id,
        node_id=other.id,
    )

    lost = _warn_lost_hysteria2_users(db, node)

    assert lost == ["hy2-active", "hy2-warm"]


def test_warn_lost_hysteria2_users_empty_when_no_hy2(db_session: Session):
    db = db_session
    node = make_node(db, name="reinst-vless-only", host="203.0.113.7")
    plan = make_plan(db)
    user = make_user(db, telegram_id="tg-78c")
    sub = make_subscription(db, user, plan, node)
    # Только vless и shadowtls — ни один helper вернуть не должен.
    _cred(
        db,
        proto=models.VPNConfigProtocol.vless_reality,
        access_username="vless-only",
        subscription_id=sub.id,
        node_id=node.id,
    )
    _cred(
        db,
        proto=models.VPNConfigProtocol.shadowtls_ss,
        access_username="stls-only",
        subscription_id=sub.id,
        node_id=node.id,
    )

    assert _warn_lost_hysteria2_users(db, node) == []
