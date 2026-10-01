"""Netfix client_control_py — сетевой аудит failover-контура.

Покрывает четыре находки по ``_do_failover`` / report-failure:

* current_node_id из репорта валидируется по кредам девайса и используется как
  нода-виновник (crowd-health + OperatorNodeReport.failed_node) — diverse-саба;
* fail_count-гейт: единичный транзиент (fail_count<порога) не мигрирует;
* непредвиденный сбой миграции отдаёт структурированный action="error", а не
  сырой HTTP 500;
* провизия помечается pending в ответе migrated.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.api.client_control import _do_failover, _resolve_reported_node_id
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


def _add_credential(db: Session, device: models.Device, node: models.VPNNode,
                    *, is_active: bool = True) -> models.Credential:
    cred = models.Credential(
        subscription_id=device.subscription_id,
        device_id=device.id,
        node_id=node.id,
        proto="vless-reality",
        config_text="dummy",
        access_username=f"user-{device.user_id}-{node.id}",
        is_active=is_active,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


# ── current_node_id: валидация по кредам девайса ────────────────────────────


def test_resolve_reported_node_accepts_node_in_device_creds(db_session: Session) -> None:
    """current_node_id, входящий в активные креды девайса, принимается."""
    plan = make_plan(db_session)
    primary = make_node(db_session, name="netfix-a", region="ru", host="10.20.0.1")
    diverse = make_node(db_session, name="netfix-b", region="ru", host="10.20.0.2")
    u = make_user(db_session, telegram_id="tg-netfix-1")
    sub = make_subscription_with_device(db_session, u, plan, primary)
    device = sub.devices[0]
    _add_credential(db_session, device, primary)
    _add_credential(db_session, device, diverse)

    # Клиент упал на diverse-соседе → приписываем именно его, не sub.node_id.
    assert _resolve_reported_node_id(db_session, device.id, diverse.id) == diverse.id


def test_resolve_reported_node_rejects_foreign_node(db_session: Session) -> None:
    """Нода вне набора кредов девайса (анти-forge) → None (fallback на sub)."""
    plan = make_plan(db_session)
    primary = make_node(db_session, name="netfix-c", region="ru", host="10.20.0.3")
    u = make_user(db_session, telegram_id="tg-netfix-2")
    sub = make_subscription_with_device(db_session, u, plan, primary)
    device = sub.devices[0]
    _add_credential(db_session, device, primary)

    assert _resolve_reported_node_id(db_session, device.id, 999_999) is None


def test_resolve_reported_node_ignores_inactive_cred(db_session: Session) -> None:
    """Отозванный (is_active=False) кред не даёт приписать ноду."""
    plan = make_plan(db_session)
    primary = make_node(db_session, name="netfix-d", region="ru", host="10.20.0.4")
    stale = make_node(db_session, name="netfix-e", region="ru", host="10.20.0.5")
    u = make_user(db_session, telegram_id="tg-netfix-3")
    sub = make_subscription_with_device(db_session, u, plan, primary)
    device = sub.devices[0]
    _add_credential(db_session, device, primary)
    _add_credential(db_session, device, stale, is_active=False)

    assert _resolve_reported_node_id(db_session, device.id, stale.id) is None


def test_resolve_reported_node_none_inputs(db_session: Session) -> None:
    """Нет client_node_id / device_id → None (whole-sub fallback)."""
    assert _resolve_reported_node_id(db_session, None, 5) is None
    assert _resolve_reported_node_id(db_session, 5, None) is None


def test_failover_attributes_reported_node_not_primary(
    db_session: Session, monkeypatch,
) -> None:
    """OperatorNodeReport.failed_node = присланная клиентом нода (diverse),
    а не sub.node_id."""
    plan = make_plan(db_session)
    primary = make_node(db_session, name="netfix-f", region="ru", host="10.20.0.6")
    diverse = make_node(db_session, name="netfix-g", region="ru", host="10.20.0.7")
    target = make_node(db_session, name="netfix-h", region="ru", host="10.20.0.8")
    u = make_user(db_session, telegram_id="tg-netfix-4")
    sub = make_subscription_with_device(db_session, u, plan, primary)
    device = sub.devices[0]
    _add_credential(db_session, device, primary)
    _add_credential(db_session, device, diverse)
    tgt_cfg = make_config(db_session, target)
    new_dev = models.Device(
        user_id=u.id, subscription_id=sub.id, config_id=tgt_cfg.id,
        name="migrated", status=models.DeviceStatus.active,
        access_username="user-mig",
    )
    db_session.add(new_dev)
    db_session.commit()
    db_session.refresh(new_dev)

    def _fake_migrate(self, s, **kwargs):  # noqa: ANN001, ARG001
        return target, new_dev, None, True

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _fake_migrate
    )

    resp = _do_failover(
        db_session, sub, kind="connect_failed", actor="test",
        device_id=device.id, client_node_id=diverse.id,
    )
    assert resp.action == "migrated"
    assert resp.provisioning_pending is True
    report = (
        db_session.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.subscription_id == sub.id)
        .one()
    )
    assert report.failed_node_id == diverse.id  # не primary.id


# ── fail_count-гейт ─────────────────────────────────────────────────────────


def test_failover_gate_defers_below_threshold(
    db_session: Session, monkeypatch,
) -> None:
    """fail_count ниже FAILOVER_MIN_FAIL_COUNT → action="deferred", миграции нет."""
    monkeypatch.setenv("FAILOVER_MIN_FAIL_COUNT", "2")
    plan = make_plan(db_session)
    node = make_node(db_session, name="netfix-gate", region="ru", host="10.20.1.1")
    u = make_user(db_session, telegram_id="tg-netfix-gate")
    sub = make_subscription_with_device(db_session, u, plan, node)

    def _must_not_call(self, s, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("migrate must not run below fail_count threshold")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _must_not_call
    )

    resp = _do_failover(
        db_session, sub, kind="connect_failed", actor="client_control",
        fail_count=1, apply_fail_count_gate=True,
    )
    assert resp.action == "deferred"
    assert resp.ok is True


def test_failover_gate_passes_at_threshold(
    db_session: Session, monkeypatch,
) -> None:
    """fail_count >= порога → гейт пропускает, доходим до миграции."""
    monkeypatch.setenv("FAILOVER_MIN_FAIL_COUNT", "2")
    plan = make_plan(db_session)
    node = make_node(db_session, name="netfix-gate2", region="ru", host="10.20.1.2")
    u = make_user(db_session, telegram_id="tg-netfix-gate2")
    sub = make_subscription_with_device(db_session, u, plan, node)

    def _no_free(self, s, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("no free node")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _no_free
    )

    resp = _do_failover(
        db_session, sub, kind="connect_failed", actor="client_control",
        fail_count=2, apply_fail_count_gate=True,
    )
    assert resp.action == "no_target_available"


def test_admin_path_not_gated(db_session: Session, monkeypatch) -> None:
    """Без apply_fail_count_gate (админ-триггер) гейт не применяется даже при
    высоком пороге и fail_count=1."""
    monkeypatch.setenv("FAILOVER_MIN_FAIL_COUNT", "5")
    plan = make_plan(db_session)
    node = make_node(db_session, name="netfix-admin", region="ru", host="10.20.1.3")
    u = make_user(db_session, telegram_id="tg-netfix-admin")
    sub = make_subscription_with_device(db_session, u, plan, node)

    def _no_free(self, s, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("no free node")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _no_free
    )

    resp = _do_failover(db_session, sub, kind="user_reported", actor="admin_panel")
    assert resp.action == "no_target_available"  # дошли до миграции, не deferred


# ── непредвиденный сбой миграции → структурированный ответ, не 500 ──────────


def test_failover_unexpected_error_returns_structured_response(
    db_session: Session, monkeypatch,
) -> None:
    """Generic Exception в миграторе → action="error"+retry_after, не raise 500."""
    plan = make_plan(db_session)
    node = make_node(db_session, name="netfix-err", region="ru", host="10.20.2.1")
    u = make_user(db_session, telegram_id="tg-netfix-err")
    sub = make_subscription_with_device(db_session, u, plan, node)

    def _boom(self, s, **kwargs):  # noqa: ANN001, ARG001
        raise ValueError("db exploded")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _boom
    )

    resp = _do_failover(db_session, sub, kind="connect_failed", actor="client_control")
    assert resp.action == "error"
    assert resp.ok is False
    assert resp.retry_after_sec > 0
