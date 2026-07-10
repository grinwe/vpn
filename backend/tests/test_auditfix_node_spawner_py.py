"""Аудит-фиксы node_spawner (находки #69/#70).

#69 — оплаченный сервер-сирота при сбое между покупкой VM и записью VPNNode:
  * spawn_node теперь коммитит строку-намерение (placeholder host,
    is_active=False) ДО create_server;
  * external_id привязывается отдельным коротким коммитом сразу после
    ответа драйвера, ДО валидации host;
  * провал валидации / сбой драйвера → нода в error со следом в notes,
    а не исключение без следа;
  * занятость имени проверяется до оплаты (в т.ч. в spawn_node_async).

#70 — sweep_stuck_spawns подбирает ноды, застрявшие в registering с
placeholder-host (daemon-поток финализации умер при рестарте backend):
возобновляемые заказы (external_id + wait_for_ipv4) перезапускаются,
невозобновляемые помечаются error.

Драйверы фейковые (SimpleNamespace), threading.Thread перехватывается —
сеть/ansible не трогаем (run_task_async уже noop в conftest).
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services import node_spawner
from app.services.cloud import DriverError
from app.services.node_spawner import (
    SPAWN_PLACEHOLDER_HOST,
    NodeSpawnError,
    spawn_node,
    spawn_node_async,
    sweep_stuck_spawns,
)
from app.time_utils import utcnow


def _make_provider(db: Session) -> models.CloudProvider:
    provider = models.CloudProvider(
        name="prov-auditfix",
        kind=models.CloudProviderKind.fourvps,
        api_token_enc=encrypt("1:apikey"),
        is_active=True,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _server(**overrides):  # type: ignore[no-untyped-def]
    base = dict(
        external_id="srv-ok",
        ipv4="203.0.113.10",
        region="dc1",
        plan="t2",
        monthly_cost=5.0,
        root_password="pw-root",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _DummyThread:
    """Перехват daemon-потоков: старт записывается, но не выполняется."""

    started: list[tuple] = []

    def __init__(self, *, target, args=(), kwargs=None, daemon=False):  # type: ignore[no-untyped-def]
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self) -> None:
        _DummyThread.started.append((self.target, self.args, self.kwargs))


@pytest.fixture(autouse=True)
def _capture_threads(monkeypatch: pytest.MonkeyPatch):
    _DummyThread.started = []
    monkeypatch.setattr(node_spawner.threading, "Thread", _DummyThread)
    yield


# ── #69: spawn_node — строка-намерение до оплаты ─────────────────────


def test_spawn_node_success_tracks_before_payment(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    seen_before_create: dict[str, object] = {}

    def fake_create_server(**kwargs):  # type: ignore[no-untyped-def]
        # В момент оплаты строка-намерение уже должна быть в БД.
        row = (
            db_session.query(models.VPNNode)
            .filter(models.VPNNode.name == "audit-node-1")
            .first()
        )
        seen_before_create["exists"] = row is not None
        seen_before_create["host"] = row.host if row else None
        seen_before_create["is_active"] = row.is_active if row else None
        return _server()

    fake_driver = SimpleNamespace(
        create_server=fake_create_server,
        set_autoprolong=lambda sid, enabled=True: enabled,
    )
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    node, task = spawn_node(
        db_session,
        provider_id=provider.id,
        name="audit-node-1",
        region="dc1",
        plan="t2",
        image="os3",
    )

    assert seen_before_create == {
        "exists": True,
        "host": SPAWN_PLACEHOLDER_HOST,
        "is_active": False,
    }
    assert node.provider_external_id == "srv-ok"
    assert node.host == "203.0.113.10"
    assert node.is_active is True
    assert node.status == models.VPNNodeStatus.registering
    assert node.monthly_cost == 5.0
    assert node.provider_root_password_enc is not None
    assert task is not None


def test_spawn_node_driver_failure_leaves_error_row(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)

    def boom(**kwargs):  # type: ignore[no-untyped-def]
        raise DriverError("payment went through, polling died")

    monkeypatch.setattr(
        node_spawner, "get_driver",
        lambda _p: SimpleNamespace(create_server=boom),
    )

    with pytest.raises(NodeSpawnError, match="polling died"):
        spawn_node(
            db_session,
            provider_id=provider.id,
            name="audit-node-fail",
            region="dc1",
            plan="t2",
        )

    # След остался: нода error+inactive, причина в notes.
    row = (
        db_session.query(models.VPNNode)
        .filter(models.VPNNode.name == "audit-node-fail")
        .one()
    )
    assert row.status == models.VPNNodeStatus.error
    assert row.is_active is False
    assert "create_server failed" in (row.notes or "")


def test_spawn_node_invalid_host_keeps_external_id(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    fake_driver = SimpleNamespace(
        # Значение с запрещёнными для inventory символами —
        # validate_node_identity_fields обязан его отклонить.
        create_server=lambda **kw: _server(ipv4="203.0.113.10 evil: {}"),
    )
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    with pytest.raises(NodeSpawnError, match="invalid host"):
        spawn_node(
            db_session,
            provider_id=provider.id,
            name="audit-node-badip",
            region="dc1",
            plan="t2",
        )

    # external_id закоммичен ДО валидации → оплаченный сервер отслеживается.
    row = (
        db_session.query(models.VPNNode)
        .filter(models.VPNNode.name == "audit-node-badip")
        .one()
    )
    assert row.provider_external_id == "srv-ok"
    assert row.status == models.VPNNodeStatus.error
    assert row.is_active is False


def test_spawn_node_duplicate_name_rejected_before_payment(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    existing = models.VPNNode(
        name="audit-node-dup", region="eu", host="198.51.100.77",
        status=models.VPNNodeStatus.active, is_active=True,
    )
    db_session.add(existing)
    db_session.commit()

    calls: list[dict] = []
    fake_driver = SimpleNamespace(
        create_server=lambda **kw: calls.append(kw) or _server(),
    )
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    with pytest.raises(NodeSpawnError, match="already taken"):
        spawn_node(
            db_session,
            provider_id=provider.id,
            name="audit-node-dup",
            region="dc1",
            plan="t2",
        )
    # Денег не потратили: до драйвера не дошло.
    assert calls == []


def test_spawn_node_async_duplicate_name_rejected_before_order(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    existing = models.VPNNode(
        name="audit-async-dup", region="eu", host="198.51.100.78",
        status=models.VPNNodeStatus.active, is_active=True,
    )
    db_session.add(existing)
    db_session.commit()

    orders: list[dict] = []
    fake_driver = SimpleNamespace(
        order_server=lambda **kw: orders.append(kw) or ("srv-x", "pw"),
    )
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    with pytest.raises(NodeSpawnError, match="already taken"):
        spawn_node_async(
            db_session,
            provider_id=provider.id,
            name="audit-async-dup",
            region="dc1",
            plan="t2",
        )
    assert orders == []


def test_spawn_node_async_order_failure_leaves_error_row(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)

    def boom(**kwargs):  # type: ignore[no-untyped-def]
        raise DriverError("buyServer rejected")

    monkeypatch.setattr(
        node_spawner, "get_driver",
        lambda _p: SimpleNamespace(order_server=boom),
    )

    with pytest.raises(NodeSpawnError, match="buyServer rejected"):
        spawn_node_async(
            db_session,
            provider_id=provider.id,
            name="audit-async-fail",
            region="dc1",
            plan="t2",
        )

    row = (
        db_session.query(models.VPNNode)
        .filter(models.VPNNode.name == "audit-async-fail")
        .one()
    )
    assert row.status == models.VPNNodeStatus.error
    assert row.is_active is False
    assert "order_server failed" in (row.notes or "")
    # Фоновая финализация не стартует для упавшего заказа.
    assert _DummyThread.started == []


# ── #70: sweep_stuck_spawns ──────────────────────────────────────────


def _stuck_node(
    db: Session, provider: models.CloudProvider, *,
    name: str, external_id: str | None, age_min: int = 120,
) -> models.VPNNode:
    node = models.VPNNode(
        name=name,
        region="dc1",
        host=SPAWN_PLACEHOLDER_HOST,
        status=models.VPNNodeStatus.registering,
        is_active=False,
        provider_id=provider.id,
        provider_external_id=external_id,
        provider_region="dc1",
        provider_plan="t2",
        updated_at=utcnow() - timedelta(minutes=age_min),
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def test_sweep_resumes_stuck_spawn_with_external_id(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    node = _stuck_node(
        db_session, provider, name="stuck-resumable", external_id="srv-42"
    )
    fake_driver = SimpleNamespace(wait_for_ipv4=lambda sid: ("1.2.3.4", 5.0, {}))
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    out = sweep_stuck_spawns(db_session)

    assert out["relay_resumed"] == 1
    assert out["relay_errored"] == 0
    assert len(_DummyThread.started) == 1
    target, args, _kwargs = _DummyThread.started[0]
    assert target is node_spawner._finalize_spawn
    assert args == (node.id,)
    # updated_at отодвинут — второй тик не запустит дубль-финализацию.
    db_session.refresh(node)
    assert node.updated_at > utcnow() - timedelta(minutes=5)
    assert node.status == models.VPNNodeStatus.registering


def test_sweep_marks_error_without_external_id(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    node = _stuck_node(
        db_session, provider, name="stuck-orphan", external_id=None
    )
    fake_driver = SimpleNamespace(wait_for_ipv4=lambda sid: ("1.2.3.4", 5.0, {}))
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    out = sweep_stuck_spawns(db_session)

    assert out["relay_errored"] == 1
    assert out["relay_resumed"] == 0
    assert _DummyThread.started == []
    db_session.refresh(node)
    assert node.status == models.VPNNodeStatus.error
    assert node.is_active is False
    assert "проверьте панель хостера" in (node.notes or "")


def test_sweep_skips_fresh_registering_nodes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    node = _stuck_node(
        db_session, provider, name="fresh-spawn", external_id="srv-99",
        age_min=1,  # моложе порога — финализация ещё может быть в полёте
    )
    fake_driver = SimpleNamespace(wait_for_ipv4=lambda sid: ("1.2.3.4", 5.0, {}))
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    out = sweep_stuck_spawns(db_session)

    assert out == {
        "relay_resumed": 0, "relay_errored": 0,
        "exit_resumed": 0, "exit_errored": 0,
        "enqueue_failed": 0,
    }
    assert _DummyThread.started == []
    db_session.refresh(node)
    assert node.status == models.VPNNodeStatus.registering


def test_sweep_handles_stuck_exit_nodes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_provider(db_session)
    resumable = models.WGExitNode(
        name="stuck-exit-resumable",
        region="fi",
        host=SPAWN_PLACEHOLDER_HOST,
        status=models.WGExitNodeStatus.registering,
        is_active=False,
        provider_id=provider.id,
        provider_external_id="srv-e7",
        provider_region="10",
        updated_at=utcnow() - timedelta(minutes=120),
    )
    orphan = models.WGExitNode(
        name="stuck-exit-orphan",
        region="fi",
        host=SPAWN_PLACEHOLDER_HOST,
        status=models.WGExitNodeStatus.registering,
        is_active=False,
        provider_id=provider.id,
        provider_external_id=None,
        provider_region="10",
        updated_at=utcnow() - timedelta(minutes=120),
    )
    db_session.add_all([resumable, orphan])
    db_session.commit()
    db_session.refresh(resumable)
    db_session.refresh(orphan)

    fake_driver = SimpleNamespace(wait_for_ipv4=lambda sid: ("5.6.7.8", 6.0, {}))
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    out = sweep_stuck_spawns(db_session)

    assert out["exit_resumed"] == 1
    assert out["exit_errored"] == 1
    assert len(_DummyThread.started) == 1
    target, args, _kwargs = _DummyThread.started[0]
    assert target is node_spawner._finalize_exit_spawn
    assert args == (resumable.id,)
    db_session.refresh(orphan)
    assert orphan.status == models.WGExitNodeStatus.error
