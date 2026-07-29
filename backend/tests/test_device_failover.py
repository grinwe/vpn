"""failover_device — per-device «это устройство не работает» (operator-routing P1).

Перетряхивает ноды ТОЛЬКО выбранного устройства, не трогая соседние и не
баня ноду user-wide. Diverse-aware: НЕ падает на диверс-гарде (в отличие от
migrate_device_to_node), исключает весь битый набор из выбора свежей.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import provisioning as prov_mod
from app.services.provisioning import ProvisioningOrchestrator
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
            node_id=node.id, device_id=device.id, access_username=username,
            is_active=True, proto="vless-reality", config_text="enc",
            pool_state=models.CredentialPoolState.assigned,
        )
    )


def test_failover_device_diverse_excludes_blocked_and_spares_siblings(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    n_a = make_node(db_session, name="fd-a", region="ru")
    n_b = make_node(db_session, name="fd-b", region="ru", host="10.0.0.2")
    n_c = make_node(db_session, name="fd-c", region="ru", host="10.0.0.3")
    fresh = make_node(db_session, name="fd-fresh", region="ru", host="10.0.0.9")
    cfg_a = make_config(db_session, n_a)
    cfg_c = make_config(db_session, n_c)
    cfg_fresh = make_config(db_session, fresh)
    sub = make_subscription(db_session, user, plan, n_a)

    # битое устройство: creds на a + b (диверсное, 2 ноды)
    dev_a = make_device(db_session, sub, cfg_a, access_username="A")
    _cred(db_session, n_a, dev_a, "A-a")
    _cred(db_session, n_b, dev_a, "A-b")
    # соседнее устройство той же подписки: cred на c — НЕ должно пострадать
    dev_b = make_device(db_session, sub, cfg_c, access_username="B")
    _cred(db_session, n_c, dev_b, "B-c")
    db_session.commit()

    captured: dict = {}

    def fake_choose_node(db, plan_, **kw):
        captured["exclude"] = set(kw.get("exclude_node_ids") or [])
        return fresh

    monkeypatch.setattr(prov_mod, "choose_node", fake_choose_node)

    orch = ProvisioningOrchestrator(db_session)
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="A-new")
    monkeypatch.setattr(orch, "revoke_device", lambda *a, **k: None)
    monkeypatch.setattr(
        orch, "reprovision_subscription", lambda *a, **k: (new_dev, None)
    )
    monkeypatch.setattr(orch, "_maybe_attach_diverse", lambda *a, **k: None)

    target, _nd, _task, old_primary = orch.failover_device(dev_a)

    # diverse-гард НЕ сработал (failover_device проходит, где migrate_device_to_node
    # бросил бы RuntimeError "диверсная"); выбрана свежая нода.
    assert target.id == fresh.id
    assert old_primary == n_a.id
    # из выбора исключён ВЕСЬ битый набор устройства (a и b)
    assert n_a.id in captured["exclude"]
    assert n_b.id in captured["exclude"]
    # соседнее устройство (cred на c) не тронуто
    db_session.refresh(dev_b)
    assert any(c.is_active and c.node_id == n_c.id for c in dev_b.credentials)


def test_failover_applies_leg_scheme_to_the_new_device(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После миграции схема публикации применяется к НОВОМУ устройству.

    Без этого у него опубликованы ВСЕ леги (колонка leg_published дефолтится
    в true), и человек, нажавший «VPN не работает», получает в клиенте 16
    строк вместо четырёх — чинилка на его глазах ломает список серверов.
    Поймано на живом проде 2026-07-29.
    """
    plan = make_plan(db_session, name="fd-legs-plan")
    user = make_user(db_session, telegram_id="fd-legs")
    node = make_node(db_session, name="fd-legs-a", region="ru", host="10.0.1.1")
    fresh = make_node(db_session, name="fd-legs-fresh", region="ru", host="10.0.1.9")
    cfg = make_config(db_session, node)
    cfg_fresh = make_config(db_session, fresh)
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg, access_username="L")
    _cred(db_session, node, dev, "L-a")
    db_session.commit()

    monkeypatch.setattr(prov_mod, "choose_node", lambda db, plan_, **kw: fresh)
    orch = ProvisioningOrchestrator(db_session)
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="L-new")
    monkeypatch.setattr(orch, "revoke_device", lambda *a, **k: None)
    monkeypatch.setattr(
        orch, "reprovision_subscription", lambda *a, **k: (new_dev, None)
    )
    monkeypatch.setattr(orch, "_maybe_attach_diverse", lambda *a, **k: None)

    applied: list[int] = []
    monkeypatch.setattr(
        orch, "_apply_leg_scheme", lambda device: applied.append(device.id)
    )

    orch.failover_device(dev)
    assert applied == [new_dev.id], "схема обязана примениться к новому устройству"
