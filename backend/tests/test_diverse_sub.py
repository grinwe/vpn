"""Phase A — diverse N×M subscription (flag DIVERSE_SUB_NODES).

Проверяем гард-инвариант (по умолчанию метод — no-op, поведение не меняется) и
привязку тёплых бандлов диверсных нод к тому же device. Боевой провижининг
(provision_subscription целиком) тут не гоняем — только аддитивный хелпер.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import provisioning, warm_pool
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _warm_cred(db: Session, node: models.VPNNode, username: str) -> models.Credential:
    c = models.Credential(
        node_id=node.id,
        proto="vless-reality",
        config_text="enc-uri",
        access_username=username,
        is_active=True,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_attach_diverse_off_by_default(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DIVERSE_SUB_NODES", raising=False)
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="ru-1", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg)

    called: list[int] = []
    monkeypatch.setattr(
        provisioning, "choose_node", lambda *a, **k: called.append(1) or node
    )
    orch = ProvisioningOrchestrator(db_session)
    # флаг выключен (default 1) → метод не должен дёргать choose_node вообще
    orch._maybe_attach_diverse(sub, dev, plan, node)
    assert called == []


def test_attach_diverse_binds_warm_bundles(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-1", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg)

    d1 = make_node(db_session, name="de-1", region="de", host="198.51.100.20")
    d2 = make_node(db_session, name="nl-1", region="nl", host="198.51.100.30")
    c1 = _warm_cred(db_session, d1, "warm-de")
    c2 = _warm_cred(db_session, d2, "warm-nl")
    creds_by_node = {d1.id: [c1], d2.id: [c2]}
    seq = [d1, d2]

    def fake_choose(db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None):
        for n in seq:
            if n.id not in (exclude_node_ids or []):
                return n
        raise RuntimeError("no more diverse nodes")

    monkeypatch.setattr(provisioning, "choose_node", fake_choose)
    monkeypatch.setattr(
        warm_pool, "try_assign_bundle",
        lambda db, node_id, sub_id: creds_by_node.get(node_id),
    )

    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)

    db_session.refresh(c1)
    db_session.refresh(c2)
    # тёплые бандлы 2 диверсных нод привязаны к тому же device
    assert c1.device_id == dev.id
    assert c2.device_id == dev.id


def _active_cred_on(
    db: Session, node: models.VPNNode, device: models.Device, username: str
) -> models.Credential:
    c = models.Credential(
        node_id=node.id, device_id=device.id, is_active=True,
        proto="vless-reality", config_text="enc-uri", access_username=username,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_attach_diverse_idempotent_when_full(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # device уже несёт активные creds на 3 разных нодах ⇒ повторный вызов
    # (reprovision/миграция) НЕ должен добирать ещё (иначе набор раздувается).
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-idem", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg, access_username="u-ru")
    d1 = make_node(db_session, name="de-idem", region="de", host="198.51.100.50")
    d2 = make_node(db_session, name="nl-idem", region="nl", host="198.51.100.60")
    _active_cred_on(db_session, primary, dev, "u-ru")
    _active_cred_on(db_session, d1, dev, "u-de")
    _active_cred_on(db_session, d2, dev, "u-nl")

    called: list[int] = []
    monkeypatch.setattr(provisioning, "choose_node", lambda *a, **k: called.append(1))
    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)
    assert called == []  # уже 3 ноды (need=0) → choose_node не зовётся


def test_attach_diverse_skips_nodes_without_warm(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-2", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg)
    d1 = make_node(db_session, name="de-2", region="de", host="198.51.100.40")

    monkeypatch.setattr(
        provisioning, "choose_node",
        lambda db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None: (
            d1 if d1.id not in (exclude_node_ids or []) else (_ for _ in ()).throw(RuntimeError())
        ),
    )
    # нет тёплого бандла на диверсной ноде → пропускаем, не падаем
    monkeypatch.setattr(warm_pool, "try_assign_bundle", lambda db, n, s: None)

    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)  # не должно бросить


def test_swap_node_out_replaces_one_node(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    a = make_node(db_session, name="swp-a", region="ru")
    b = make_node(db_session, name="swp-b", region="de", host="198.51.100.71")
    c = make_node(db_session, name="swp-c", region="nl", host="198.51.100.72")
    cfg = make_config(db_session, a)
    sub = make_subscription(db_session, user, plan, a)
    dev = make_device(db_session, sub, cfg, access_username="u")
    _active_cred_on(db_session, a, dev, "u-a")
    cred_b = _active_cred_on(db_session, b, dev, "u-b")
    _active_cred_on(db_session, c, dev, "u-c")

    # свежая нода d + тёплый бандл для добора взамен выкинутой
    d = make_node(db_session, name="swp-d", region="fr", host="198.51.100.73")
    warm_d = _warm_cred(db_session, d, "warm-d")

    def fake_choose(db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None):
        if d.id not in (exclude_node_ids or []):
            return d
        raise RuntimeError("no more")

    monkeypatch.setattr(provisioning, "choose_node", fake_choose)
    monkeypatch.setattr(
        warm_pool, "try_assign_bundle",
        lambda db, nid, sid: [warm_d] if nid == d.id else None,
    )

    orch = ProvisioningOrchestrator(db_session)
    added = orch.swap_node_out(dev, b.id)

    db_session.refresh(cred_b)
    db_session.refresh(warm_d)
    assert cred_b.is_active is False              # выкинутая нода деактивирована
    assert cred_b.pool_state == models.CredentialPoolState.revoked
    assert warm_d.device_id == dev.id             # добрана свежая взамен
    assert added == 1


def test_migrate_device_blocked_for_diverse(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    a = make_node(db_session, name="mg-a", region="ru")
    b = make_node(db_session, name="mg-b", region="de", host="198.51.100.81")
    target = make_node(db_session, name="mg-t", region="fr", host="198.51.100.82")
    cfg = make_config(db_session, a)
    sub = make_subscription(db_session, user, plan, a)
    dev = make_device(db_session, sub, cfg, access_username="u")
    _active_cred_on(db_session, a, dev, "u-a")
    _active_cred_on(db_session, b, dev, "u-b")  # 2 ноды → диверсный

    orch = ProvisioningOrchestrator(db_session)
    # legacy-миграция диверс-девайса должна быть запрещена (иначе схлопнет до 1)
    with pytest.raises(RuntimeError, match="диверсная"):
        orch.migrate_device_to_node(dev, target_node_id=target.id)
