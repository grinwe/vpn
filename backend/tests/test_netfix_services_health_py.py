"""Net-fix #7: probe-блок региона / probe-смерть ноды → админ-алерт.

Авто-миграция по healthcheck отключена с 2026-04-15, и до этого фикса сигнал
probe-риг (единственный детектор DPI-блока, который SSH-тик не видит) молча
копился в ``node.blocked_regions`` без единого операторского алерта.

Фикс (backend/app/services/health.py): ``recompute_node_health`` на
автоматическом (probe-driven) пути при blocked-регионе или probe-смерти шлёт
админ-пуш через ``_alert_probe_degradation`` (без авто-миграции), с анти-спам
гейтами: mute оператора + dedup-окно ``notify_admins``.

JSONB containment ``@>`` в дедупе не эмулируется SQLite, поэтому тест гоняет
против боевого Postgres через фикстуру ``db_session`` из conftest.
"""
from __future__ import annotations

from app import models
from app.services import diagnostics_state
from app.services.health import MIN_SAMPLES, record_probe, recompute_node_health

from .factories import make_node


def _count_alerts(db, kind: str) -> int:
    return (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action == f"admin_alert_{kind}")
        .count()
    )


def _seed_region_fails(db, node, region: str, n: int) -> None:
    for _ in range(n):
        record_probe(
            db,
            node=node,
            source_region=region,
            result=models.ProbeResult.timeout,
        )
    db.flush()


def _seed_region_ok(db, node, region: str, n: int) -> None:
    """Успешные пробы из живого региона.

    Нужны, чтобы блок ЕДИНСТВЕННОГО региона не обвалил общий success-rate ниже
    DEAD_THRESHOLD: при overall == 0 срабатывает global_death и алерт уходит как
    ``node_probe_death``, а не ``node_region_blocked`` (в ``_alert_probe_
    degradation`` probe-death имеет приоритет). Держим overall >= DEAD_THRESHOLD,
    тогда это чистый region-block.
    """
    for _ in range(n):
        record_probe(
            db,
            node=node,
            source_region=region,
            result=models.ProbeResult.ok,
        )
    db.flush()


def test_blocked_region_fires_admin_alert(db_session, monkeypatch):
    """MIN_SAMPLES провальных проб из региона → blocked_regions + один пуш."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    node = make_node(db_session, name="netfix7-blocked", region="ru")

    # Живой регион держит overall выше DEAD_THRESHOLD → блок ru = region-block,
    # а не probe-death (см. _seed_region_ok).
    _seed_region_ok(db_session, node, "eu", MIN_SAMPLES)
    _seed_region_fails(db_session, node, "ru", MIN_SAMPLES)
    summary = recompute_node_health(db_session, node)

    assert summary["blocked_regions"] == ["ru"]
    assert _count_alerts(db_session, "node_region_blocked") == 1


def test_probe_death_fires_probe_death_alert(db_session, monkeypatch):
    """Общий success-rate ниже порога при достаточной выборке → probe-death пуш."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    node = make_node(db_session, name="netfix7-death", region="eu")

    # Провалы сразу в двух регионах: overall < DEAD_THRESHOLD, global_death.
    _seed_region_fails(db_session, node, "eu", MIN_SAMPLES)
    _seed_region_fails(db_session, node, "kz", MIN_SAMPLES)
    recompute_node_health(db_session, node)

    assert _count_alerts(db_session, "node_probe_death") == 1


def test_alert_deduped_within_window(db_session, monkeypatch):
    """Флаппинг: повторный пересчёт в окне не плодит второй пуш."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    node = make_node(db_session, name="netfix7-dedup", region="ru")

    # Живой регион → чистый region-block (не probe-death), см. _seed_region_ok.
    _seed_region_ok(db_session, node, "eu", MIN_SAMPLES)
    _seed_region_fails(db_session, node, "ru", MIN_SAMPLES)
    recompute_node_health(db_session, node)
    # Ещё провалы + повторный пересчёт — то же состояние blocked=[ru].
    _seed_region_fails(db_session, node, "ru", MIN_SAMPLES)
    recompute_node_health(db_session, node)

    assert _count_alerts(db_session, "node_region_blocked") == 1


def test_manual_recompute_does_not_alert(db_session, monkeypatch):
    """Ручной пересчёт из админки (auto_migrate=False) не шлёт пуш."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    node = make_node(db_session, name="netfix7-manual", region="ru")

    _seed_region_fails(db_session, node, "ru", MIN_SAMPLES)
    summary = recompute_node_health(db_session, node, auto_migrate=False)

    assert summary["blocked_regions"] == ["ru"]
    assert _count_alerts(db_session, "node_region_blocked") == 0


def test_muted_node_suppresses_alert(db_session, monkeypatch):
    """Оператор заглушил алерты по ноде → пуш подавлен, blocked всё равно считается."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    node = make_node(db_session, name="netfix7-muted", region="ru")
    diagnostics_state.mute_alerts(node, hours=6)
    db_session.commit()

    _seed_region_fails(db_session, node, "ru", MIN_SAMPLES)
    summary = recompute_node_health(db_session, node)

    assert summary["blocked_regions"] == ["ru"]
    assert _count_alerts(db_session, "node_region_blocked") == 0


def test_below_min_samples_no_alert(db_session, monkeypatch):
    """Мало проб (< MIN_SAMPLES) → ни blocked, ни пуша (защита от единичного сбоя)."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    node = make_node(db_session, name="netfix7-fewsamples", region="ru")

    _seed_region_fails(db_session, node, "ru", MIN_SAMPLES - 1)
    summary = recompute_node_health(db_session, node)

    assert summary["blocked_regions"] == []
    assert _count_alerts(db_session, "node_region_blocked") == 0
    assert _count_alerts(db_session, "node_probe_death") == 0
