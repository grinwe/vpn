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
        "---8<---\n"
        '{"version": "1.2.3", "applied_at": "2026-07-26T00:00:00Z"}\n'
    )
    assert _parse_probe_output(raw) == ("26.3.27", "1.2.3", None)


def test_parse_probe_output_survives_missing_marker():
    """Нода прошита до появления маркера — версия xray всё равно нужна."""
    raw = "Xray 25.6.8 (Xray, Penetrates Everything.)\n---8<---\n"
    assert _parse_probe_output(raw) == ("25.6.8", None, None)


def test_parse_probe_output_survives_broken_marker():
    """Битый JSON не должен ронять сбор и терять версию ядра."""
    raw = "Xray 26.3.27 (Xray)\n---8<---\n{не json"
    assert _parse_probe_output(raw) == ("26.3.27", None, None)


def test_parse_probe_output_handles_missing_xray():
    """xray не установлен (нода только под hysteria2) — не падаем."""
    assert _parse_probe_output("\n---8<---\n") == (None, None, None)


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
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: "v26.3.27")
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
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: "v26.3.27")
    _seed_release(db_session, "v27.0.0")
    overview = xray_releases.version_overview(db_session)
    assert overview["xray"]["pin_behind_upstream"] is True


def test_check_upstream_notifies_admin(db_session, monkeypatch):
    """Новый релиз → ровно один пуш админу, и он в allowlist доставки."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "4242")
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: "v26.3.27")
    monkeypatch.setattr(
        xray_releases,
        "fetch_latest_release",
        lambda spec=None: {
            "version": "v27.0.0", "published_at": None, "html_url": "https://x",
        },
    )

    result = xray_releases.check_upstream_and_notify(db_session)
    assert result["notified"] is True

    alerts = [
        a
        for a in db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_xray_version_drift")
        .all()
        if (a.extra or {}).get("product") == "xray-core"
    ]
    assert len(alerts) == 1

    from app.api_extensions import ADMIN_NOTIFICATION_ACTIONS

    assert "admin_alert_xray_version_drift" in ADMIN_NOTIFICATION_ACTIONS


def test_check_upstream_deduplicates_across_ticks(db_session, monkeypatch):
    """Второй тик с тем же релизом не будит админа повторно."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "4242")
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: "v26.3.27")
    monkeypatch.setattr(
        xray_releases,
        "fetch_latest_release",
        lambda spec=None: {
            "version": "v27.0.0", "published_at": None, "html_url": "https://x",
        },
    )

    xray_releases.check_upstream_and_notify(db_session)
    second = xray_releases.check_upstream_and_notify(db_session)
    assert second["notified"] is True  # событие есть, но строк не прибавилось

    alerts = [
        a
        for a in db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_xray_version_drift")
        .all()
        if (a.extra or {}).get("product") == "xray-core"
    ]
    assert len(alerts) == 1


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
        xray_releases,
        "fetch_latest_release",
        lambda spec=None: {"error": "URLError: timeout"},
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


def test_overview_falls_back_to_pin_from_db(db_session, monkeypatch):
    """API-контейнер ansible-дерева не видит (оно только в образе воркера) —
    пин обязан подхватываться из того, что воркер записал в БД."""
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: None)
    row = _seed_release(db_session, "v27.0.0")
    row.pinned_version = "v26.3.27"
    db_session.commit()

    overview = xray_releases.version_overview(db_session)
    assert overview["xray"]["pinned"] == "v26.3.27"
    assert overview["xray"]["pin_behind_upstream"] is True


def test_upstream_error_still_records_pin(db_session, monkeypatch):
    """Пин читается локально: неудачный поход к GitHub не должен оставлять
    сводку с прошлым (или пустым) пином после бампа роли."""
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: "v26.3.27")
    monkeypatch.setattr(
        xray_releases,
        "fetch_latest_release",
        lambda spec=None: {"error": "URLError: timeout"},
    )
    xray_releases.check_upstream_and_notify(db_session)

    row = (
        db_session.query(models.SoftwareRelease)
        .filter(models.SoftwareRelease.name == xray_releases.RELEASE_NAME)
        .one()
    )
    assert row.pinned_version == "v26.3.27"


# ── hysteria2: тот же цикл, отдельный продукт ───────────────────────────────
# Три VLESS-протокола держит один бинарь xray, hysteria2 — отдельный демон, и до
# 2026-07-26 его версия не пинилась и не собиралась вовсе.


def test_parse_probe_output_reads_hysteria_version():
    raw = (
        "Xray 26.3.27 (Xray, Penetrates Everything.)\n"
        "---8<---\n"
        '{"version": "1.0.0"}\n'
        "---8<---\n"
        "Version:\tv2.10.0\n"
    )
    assert _parse_probe_output(raw) == ("26.3.27", "1.0.0", "v2.10.0")


def test_parse_probe_output_hysteria_real_format():
    """Регресс: `hysteria version` печатает "Version:\tv2.10.0" — с двоеточием.
    Первый вариант regex его не матчил, и на проде все 10 нод выглядели как
    «hy2 не установлен», хотя демон работал."""
    raw = "x\n---8<---\n---8<---\nVersion:\tv2.10.0\nBuildDate:\t2026-07-13\n"
    assert _parse_probe_output(raw)[2] == "v2.10.0"


def test_parse_probe_output_node_without_hysteria():
    """Нода без hy2 бинаря не имеет — это норма, а не ошибка сбора."""
    raw = "Xray 26.3.27 (Xray)\n---8<---\n---8<---\n"
    assert _parse_probe_output(raw) == ("26.3.27", None, None)


def test_hysteria_pin_read_from_role():
    spec = xray_releases.PRODUCTS_BY_KEY["hysteria"]
    pin = xray_releases.pinned_version(spec)
    if pin is None:
        pytest.skip("ansible-дерево недоступно в этом окружении")
    assert pin.startswith("v")


def test_normalize_strips_hysteria_tag_prefix():
    """Upstream-тег hysteria — `app/vX.Y.Z`; без срезания префикса сравнение с
    пином давало бы вечный ложный дрейф."""
    norm = xray_releases._normalize
    assert norm("app/v2.10.0", tag_prefix="app/") == norm("v2.10.0", tag_prefix="app/")


def test_overview_flags_outdated_hysteria_nodes(db_session, monkeypatch):
    def fake_pin(spec=None):
        key = getattr(spec, "key", "xray-core")
        return "v26.3.27" if key == "xray-core" else "v2.10.0"

    monkeypatch.setattr(xray_releases, "pinned_version", fake_pin)

    fresh = make_node(db_session, name="hy2-fresh", host="203.0.113.30")
    stale = make_node(db_session, name="hy2-stale", host="203.0.113.31")
    nohy2 = make_node(db_session, name="hy2-absent", host="203.0.113.32")
    fresh.xray_version = "26.3.27"
    fresh.hysteria_version = "v2.10.0"
    stale.xray_version = "26.3.27"
    stale.hysteria_version = "v2.6.0"
    nohy2.xray_version = "26.3.27"
    db_session.commit()

    overview = xray_releases.version_overview(db_session)
    assert overview["nodes_outdated_xray"] == []
    assert overview["nodes_outdated_hysteria"] == ["hy2-stale"]
    assert overview["nodes_hysteria_unknown"] == ["hy2-absent"]
    assert overview["hysteria"]["pinned"] == "v2.10.0"


def test_check_upstream_notifies_per_product(db_session, monkeypatch):
    """Два продукта — два независимых пуша (дедуп различает их по ключу)."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "4242")

    def fake_pin(spec=None):
        key = getattr(spec, "key", "xray-core")
        return "v26.3.27" if key == "xray-core" else "v2.10.0"

    def fake_fetch(spec=None):
        key = getattr(spec, "key", "xray-core")
        return {
            "version": "v27.0.0" if key == "xray-core" else "app/v2.11.0",
            "published_at": None,
            "html_url": "https://x",
        }

    monkeypatch.setattr(xray_releases, "pinned_version", fake_pin)
    monkeypatch.setattr(xray_releases, "fetch_latest_release", fake_fetch)

    result = xray_releases.check_upstream_and_notify(db_session)
    assert result["notified"] is True
    assert {r["product"] for r in result["products"]} == {"xray-core", "hysteria"}

    alerts = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_xray_version_drift")
        .all()
    )
    # По одному на продукт: ключ дедупа включает product.
    assert len(alerts) == 2
    assert {(a.extra or {}).get("product") for a in alerts} == {"xray-core", "hysteria"}


def test_upgrade_hysteria_endpoint_creates_task(client, db_session):
    node = make_node(db_session, name="hy2-upgrade", host="203.0.113.33")
    resp = client.post(f"/api/nodes/{node.id}/upgrade-hysteria")
    assert resp.status_code == 200, resp.text
    task = db_session.get(models.ProvisioningTask, resp.json()["task_id"])
    assert task.action == "upgrade_hysteria"


def test_upgrade_hysteria_does_not_touch_node_status(db_session):
    """Провал доставки бинаря hy2 не должен обнулять health живой ноды."""
    from app.services.provisioning import ProvisioningOrchestrator

    node = make_node(db_session, name="hy2-status", host="203.0.113.34")
    node.health_score = 100
    node.status = models.VPNNodeStatus.active
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task("node", node.id, "upgrade_hysteria", {})
    db_session.commit()
    orch._handle_task_outcome(task, success=False)
    db_session.commit()
    db_session.refresh(node)
    assert node.health_score == 100
    assert node.status == models.VPNNodeStatus.active


def test_release_rows_created_once_per_product(db_session, monkeypatch):
    """Регресс: SessionLocal с autoflush=False — pending INSERT не виден
    следующему SELECT, и один прогон тика создавал строку продукта дважды →
    UniqueViolation на commit откатывал всю проверку (hysteria не появлялась)."""
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: "v1.0.0")
    monkeypatch.setattr(
        xray_releases,
        "fetch_latest_release",
        lambda spec=None: {
            "version": "v1.0.0", "published_at": None, "html_url": "https://x",
        },
    )

    xray_releases.check_upstream_and_notify(db_session)

    names = [r.name for r in db_session.query(models.SoftwareRelease).all()]
    assert sorted(names) == sorted(p.key for p in xray_releases.PRODUCTS)
    assert len(names) == len(set(names))


def test_unknown_pin_does_not_mark_all_nodes_outdated(db_session, monkeypatch):
    """Пустой пин — не повод объявлять дрейф: раньше сводка звала обновлять
    весь флот, который в порядке."""
    monkeypatch.setattr(xray_releases, "pinned_version", lambda spec=None: None)
    node = make_node(db_session, name="pinless", host="203.0.113.40")
    node.xray_version = "26.3.27"
    node.hysteria_version = "v2.10.0"
    db_session.commit()

    overview = xray_releases.version_overview(db_session)
    assert overview["nodes_outdated_xray"] == []
    assert overview["nodes_outdated_hysteria"] == []
