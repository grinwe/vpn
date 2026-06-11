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
