"""Э0 эпика «набор эндпоинтов»: фиксы, нужные независимо от самого эпика.

Три дефекта кусались уже сегодня, а схему публикации легов ломали бы молча:
apply-таска активировала ВСЕ креды устройства; миграция не исключала ноды, на
которых человек уже сидит; жалоба «VPN не работает» внутри троттла не доезжала
до сервера вовсе — то есть главный сигнал эскалации терялся в самом частом
сценарии.
"""
from __future__ import annotations

from app import models

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


def _cred(db, *, sub, device, node, proto, active=False):
    cred = models.Credential(
        subscription_id=sub.id,
        device_id=device.id,
        node_id=node.id,
        proto=proto,
        config_text="enc-stub",
        access_username=device.access_username,
        is_active=active,
    )
    db.add(cred)
    db.commit()
    return cred


# ── apply-таска не должна воскрешать чужие креды ────────────────────────────


def test_apply_activates_only_its_own_node_and_protocols(db_session):
    """Раньше `for cred in device.credentials: is_active = True` поднимал и
    отозванные креды других нод — человек получал в подписке эндпоинты, которых
    на нодах уже нет, и они выглядели как «сервер не работает»."""
    from app.services.provisioning import ProvisioningOrchestrator

    node_a = make_node(db_session, name="apply-a", host="203.0.113.50")
    node_b = make_node(db_session, name="apply-b", host="203.0.113.51")
    cfg = make_config(db_session, node_a)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="apply-user")
    sub = make_subscription_with_device(db_session, user, plan, node_a)
    device = sub.devices[0]
    device.config_id = cfg.id
    db_session.commit()

    mine = _cred(db_session, sub=sub, device=device, node=node_a, proto="vless-reality")
    other_node = _cred(
        db_session, sub=sub, device=device, node=node_b, proto="vless-reality"
    )
    other_proto = _cred(
        db_session, sub=sub, device=device, node=node_a, proto="hysteria2"
    )

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task(
        "device",
        device.id,
        "apply",
        {"node_id": node_a.id, "protocols": [{"proto": "vless-reality", "port": 443}]},
    )
    db_session.commit()
    orch._handle_task_outcome(task, success=True)
    db_session.commit()

    for cred in (mine, other_node, other_proto):
        db_session.refresh(cred)
    assert mine.is_active is True
    assert other_node.is_active is False, "кред ЧУЖОЙ ноды не должен воскресать"
    assert other_proto.is_active is False, "кред другого протокола — тоже"


def test_apply_without_payload_keeps_old_behaviour(db_session):
    """Легаси-таски без node_id/protocols в payload (их в проде хватает) должны
    продолжать работать как раньше, иначе фикс сломает старые ретраи."""
    from app.services.provisioning import ProvisioningOrchestrator

    node = make_node(db_session, name="apply-legacy", host="203.0.113.52")
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="legacy-user")
    sub = make_subscription_with_device(db_session, user, plan, node)
    device = sub.devices[0]
    cred = _cred(db_session, sub=sub, device=device, node=node, proto="vless-reality")

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task("device", device.id, "apply", {})
    db_session.commit()
    orch._handle_task_outcome(task, success=True)
    db_session.commit()

    db_session.refresh(cred)
    assert cred.is_active is True


# ── миграция не должна возвращать на свою же ноду ───────────────────────────


def test_migration_excludes_nodes_user_already_has(db_session):
    """При диверсификации человек сидит на нескольких нодах; «перенос» мог
    отправить его ровно туда, где он уже был, — снаружи это выглядело как
    «нажал не работает, ничего не изменилось»."""
    from app.services import provisioning as prov

    primary = make_node(db_session, name="mig-primary", host="203.0.113.60")
    secondary = make_node(db_session, name="mig-secondary", host="203.0.113.61")
    free = make_node(db_session, name="mig-free", host="203.0.113.62")
    for node in (primary, secondary, free):
        make_config(db_session, node)

    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="mig-user")
    sub = make_subscription_with_device(db_session, user, plan, primary)
    device = sub.devices[0]
    _cred(
        db_session, sub=sub, device=device, node=secondary,
        proto="vless-reality", active=True,
    )

    seen: dict[str, list[int]] = {}

    def fake_choose(db, plan_, *, node_id=None, exclude_node_ids=None, **kw):
        seen["excluded"] = sorted(exclude_node_ids or [])
        return free

    monkey = getattr(prov, "choose_node")
    prov.choose_node = fake_choose
    try:
        orch = prov.ProvisioningOrchestrator(db_session)
        try:
            orch.migrate_subscription_to_new_node(sub)
        except Exception:
            # Нас интересует только состав exclude — дальше миграция уходит в
            # провижининг, который в тестах отключён фикстурой.
            pass
    finally:
        prov.choose_node = monkey

    assert primary.id in seen["excluded"]
    assert secondary.id in seen["excluded"], "вторичная нода тоже должна исключаться"


# ── жалоба доезжает до сервера даже внутри троттла ──────────────────────────


def test_complaint_recorded_when_throttled(client, db_session):
    """Внутри троттла бот показывает «уже перенесли» и дальше не идёт. Жалоба
    обязана осесть на сервере — на ней строится эскалация."""
    from app.time_utils import utcnow

    node = make_node(db_session, name="compl-node", host="203.0.113.70")
    make_config(db_session, node)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="complainer")
    sub = make_subscription_with_device(db_session, user, plan, node)
    db_session.add(
        models.OperatorNodeReport(
            user_id=user.id,
            subscription_id=sub.id,
            failed_node_id=node.id,
            reported_at=utcnow(),
        )
    )
    db_session.commit()

    resp = client.get(
        "/api/admin/client-control/devices-by-telegram?telegram_id=complainer"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retry_after_sec"], "ожидали троттл"

    complaints = (
        db_session.query(models.AuditLog)
        .filter_by(action="complaint_received", target_id=user.id)
        .count()
    )
    assert complaints == 1


def test_double_tap_is_one_complaint(client, db_session, monkeypatch):
    """Дребезг ≠ повторная жалоба: человек нетерпелив и жмёт дважды подряд.
    Иначе порог эскалации срабатывал бы от одного двойного клика."""
    from app.time_utils import utcnow

    node = make_node(db_session, name="dbl-node", host="203.0.113.71")
    make_config(db_session, node)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="double-tapper")
    sub = make_subscription_with_device(db_session, user, plan, node)
    db_session.add(
        models.OperatorNodeReport(
            user_id=user.id,
            subscription_id=sub.id,
            failed_node_id=node.id,
            reported_at=utcnow(),
        )
    )
    db_session.commit()

    url = "/api/admin/client-control/devices-by-telegram?telegram_id=double-tapper"
    client.get(url)
    client.get(url)
    client.get(url)

    complaints = (
        db_session.query(models.AuditLog)
        .filter_by(action="complaint_received", target_id=user.id)
        .count()
    )
    assert complaints == 1, "три тапа подряд — одна жалоба"


def test_complaint_recorded_again_after_dedup_window(client, db_session, monkeypatch):
    """А вот жалоба через время — это уже вторая, и именно она триггерит
    эскалацию."""
    from app.api import client_control
    from app.time_utils import utcnow

    monkeypatch.setattr(client_control, "COMPLAINT_DEDUP_SEC", 0)

    node = make_node(db_session, name="again-node", host="203.0.113.72")
    make_config(db_session, node)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="again-user")
    sub = make_subscription_with_device(db_session, user, plan, node)
    db_session.add(
        models.OperatorNodeReport(
            user_id=user.id,
            subscription_id=sub.id,
            failed_node_id=node.id,
            reported_at=utcnow(),
        )
    )
    db_session.commit()

    url = "/api/admin/client-control/devices-by-telegram?telegram_id=again-user"
    client.get(url)
    client.get(url)

    complaints = (
        db_session.query(models.AuditLog)
        .filter_by(action="complaint_received", target_id=user.id)
        .count()
    )
    assert complaints == 2
