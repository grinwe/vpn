"""Версионирование: код, версии на нодах, upstream-релиз xray.

До этой фичи ответить «какой код на проде» и «на каких нодах какое ядро xray»
было нечем: .git вырезается при деплое, а фактическая версия xray жила только в
stdout ansible-таски. Тесты фиксируют три звена цепочки — версия кода, сбор
версий с нод, сравнение с upstream и уведомление админу.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import models
from app.services import xray_releases
from app.services.node_versions import _parse_probe_output

from .factories import make_node

# ── версия кода ─────────────────────────────────────────────────────────────


def test_app_version_prefers_env(monkeypatch):
    """Ansible кладёт версию в APP_VERSION — она главнее файла в образе."""
    from app import version as version_mod

    version_mod.app_version.cache_clear()
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    try:
        assert version_mod.app_version() == "9.9.9"
    finally:
        version_mod.app_version.cache_clear()


def test_version_info_has_no_none_version(monkeypatch):
    """Версия — телеметрия: её отсутствие не должно ронять ни API, ни маркер."""
    from app import version as version_mod

    version_mod.app_version.cache_clear()
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(version_mod, "_read_version_file", lambda: None)
    try:
        info = version_mod.version_info()
        assert info["version"] == version_mod.FALLBACK_VERSION
        assert info["started_at"]
    finally:
        version_mod.app_version.cache_clear()


def test_version_endpoint(client):
    resp = client.get("/api/version")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"]
    assert body["roles_version"] == body["version"]


def test_site_extra_vars_carry_release_version(db_session):
    """Ноду прошиваем версией кода — иначе маркер на ноде писать нечем."""
    from app.services.provisioning import _collect_site_extra_vars

    node = make_node(db_session, name="ver-node", host="203.0.113.20")
    extra = _collect_site_extra_vars(db_session, node)
    assert extra["vpn_release_version"]


# ── разбор ответа ноды ──────────────────────────────────────────────────────


def test_parse_probe_output_reads_both_versions():
    raw = (
        "Xray 26.3.27 (Xray, Penetrates Everything.) d2758a0 (go1.26.1 linux/amd64)\n"
        "---\n"
        '{"version": "1.2.3", "applied_at": "2026-07-26T00:00:00Z"}\n'
    )
    assert _parse_probe_output(raw) == ("26.3.27", "1.2.3")


def test_parse_probe_output_survives_missing_marker():
    """Нода прошита до появления маркера — версия xray всё равно нужна."""
    raw = "Xray 25.6.8 (Xray, Penetrates Everything.)\n---\n"
    assert _parse_probe_output(raw) == ("25.6.8", None)


def test_parse_probe_output_survives_broken_marker():
    """Битый JSON не должен ронять сбор и терять версию ядра."""
    raw = "Xray 26.3.27 (Xray)\n---\n{не json"
    assert _parse_probe_output(raw) == ("26.3.27", None)


def test_parse_probe_output_handles_missing_xray():
    """xray не установлен (нода только под hysteria2) — не падаем."""
    assert _parse_probe_output("\n---\n") == (None, None)


# ── upstream-релиз и сравнение ──────────────────────────────────────────────


def test_normalize_strips_leading_v():
    """Роль пинит `v26.3.27`, нода печатает `26.3.27` — без нормализации был бы
    вечный ложный дрейф."""
    assert xray_releases._normalize("v26.3.27") == xray_releases._normalize("26.3.27")


def test_pinned_version_read_from_role():
    pin = xray_releases.pinned_version()
    if pin is None:
        pytest.skip("ansible-дерево недоступно в этом окружении")
    assert pin.startswith("v")


def _seed_release(db_session, latest: str):
    row = models.SoftwareRelease(
        name=xray_releases.RELEASE_NAME,
        latest_version=latest,
        checked_at=datetime.utcnow(),
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_overview_flags_outdated_nodes(db_session, monkeypatch):
    monkeypatch.setattr(xray_releases, "pinned_version", lambda: "v26.3.27")
    _seed_release(db_session, "v26.3.27")

    fresh = make_node(db_session, name="fresh-node", host="203.0.113.21")
    stale = make_node(db_session, name="stale-node", host="203.0.113.22")
    unknown = make_node(db_session, name="unknown-node", host="203.0.113.23")
    fresh.xray_version = "26.3.27"
    stale.xray_version = "25.6.8"
    unknown.xray_version = None
    db_session.commit()

    overview = xray_releases.version_overview(db_session)
    assert overview["xray"]["pin_behind_upstream"] is False
    assert overview["nodes_outdated_xray"] == ["stale-node"]
    assert overview["nodes_version_unknown"] == ["unknown-node"]


def test_overview_flags_pin_behind_upstream(db_session, monkeypatch):
    monkeypatch.setattr(xray_releases, "pinned_version", lambda: "v26.3.27")
    _seed_release(db_session, "v27.0.0")
    overview = xray_releases.version_overview(db_session)
    assert overview["xray"]["pin_behind_upstream"] is True


def test_check_upstream_notifies_admin(db_session, monkeypatch):
    """Новый релиз → ровно один пуш админу, и он в allowlist доставки."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "4242")
    monkeypatch.setattr(xray_releases, "pinned_version", lambda: "v26.3.27")
    monkeypatch.setattr(
        xray_releases,
        "fetch_latest_release",
        lambda: {"version": "v27.0.0", "published_at": None, "html_url": "https://x"},
    )

    result = xray_releases.check_upstream_and_notify(db_session)
    assert result["notified"] is True

    alerts = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_xray_version_drift")
        .all()
    )
    assert len(alerts) == 1

    from app.api_extensions import ADMIN_NOTIFICATION_ACTIONS

    assert "admin_alert_xray_version_drift" in ADMIN_NOTIFICATION_ACTIONS


def test_check_upstream_deduplicates_across_ticks(db_session, monkeypatch):
    """Второй тик с тем же релизом не будит админа повторно."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "4242")
    monkeypatch.setattr(xray_releases, "pinned_version", lambda: "v26.3.27")
    monkeypatch.setattr(
        xray_releases,
        "fetch_latest_release",
        lambda: {"version": "v27.0.0", "published_at": None, "html_url": "https://x"},
    )

    xray_releases.check_upstream_and_notify(db_session)
    second = xray_releases.check_upstream_and_notify(db_session)
    assert second["notified"] is True  # событие есть, но строк не прибавилось

    alerts = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_xray_version_drift")
        .count()
    )
    assert alerts == 1


def test_dedup_survives_delivery_rename(db_session, monkeypatch):
    """Бот переименовывает action в ':delivered' — дедуп обязан это учитывать,
    иначе окно молчания схлопывается до ~10 секунд."""
    from app.services.admin_notify import notify_admins

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "4242")
    ids = notify_admins(
        db_session, kind="xray_version_drift", text="раз",
        dedup_key={"latest": "v27.0.0"}, autocommit=True,
    )
    assert ids
    row = db_session.get(models.AuditLog, ids[0])
    row.action = f"{row.action}:delivered"
    db_session.commit()

    again = notify_admins(
        db_session, kind="xray_version_drift", text="два",
        dedup_key={"latest": "v27.0.0"}, autocommit=True,
    )
    assert again == []


def test_upstream_error_does_not_wipe_cached_version(db_session, monkeypatch):
    """GitHub недоступен — показываем последнюю известную версию и причину."""
    _seed_release(db_session, "v26.3.27")
    monkeypatch.setattr(
        xray_releases, "fetch_latest_release", lambda: {"error": "URLError: timeout"}
    )
    result = xray_releases.check_upstream_and_notify(db_session)
    assert result["latest"] == "v26.3.27"
    assert result["notified"] is False

    row = (
        db_session.query(models.SoftwareRelease)
        .filter(models.SoftwareRelease.name == xray_releases.RELEASE_NAME)
        .one()
    )
    assert row.latest_version == "v26.3.27"
    assert "timeout" in (row.last_error or "")


# ── действие «обновить xray» ────────────────────────────────────────────────


def test_upgrade_xray_endpoint_creates_task(client, db_session):
    node = make_node(db_session, name="upgrade-node", host="203.0.113.24")
    resp = client.post(f"/api/nodes/{node.id}/upgrade-xray")
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    task = db_session.get(models.ProvisioningTask, task_id)
    assert task.action == "upgrade_xray"
    assert task.target_id == node.id


def test_upgrade_xray_batch_reports_unknown_nodes(client, db_session):
    node = make_node(db_session, name="batch-node", host="203.0.113.25")
    resp = client.post(
        "/api/nodes/upgrade-xray", json={"node_ids": [node.id, 999999]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["node_id"] for item in body["started"]] == [node.id]
    assert body["skipped"] == [999999]


def test_upgrade_xray_does_not_touch_node_status(db_session):
    """Провал точечного апгрейда не должен обнулять health живой ноды."""
    from app.services.provisioning import ProvisioningOrchestrator

    node = make_node(db_session, name="status-node", host="203.0.113.26")
    node.health_score = 100
    node.status = models.VPNNodeStatus.active
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task("node", node.id, "upgrade_xray", {})
    db_session.commit()
    orch._handle_task_outcome(task, success=False)
    db_session.commit()
    db_session.refresh(node)

    assert node.health_score == 100
    assert node.status == models.VPNNodeStatus.active
