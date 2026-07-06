"""audit-fix wave 3 — кластер жизненного цикла спавна/провижина нод.

Покрывает находки:
  * #48  — RQ job_timeout с запасом над ansible-таймаутом site.yml (900с).
  * #57  — debounce reconcile'а с верхней границей (LEAST): burst правок не
           отодвигает дедлайн бесконечно.
  * #72  — choose_node не выдаёт юзеров на ноду в статусе registering.
  * #78  — reinstall восстанавливает пер-юзерные hysteria2-учётки.
  * #81  — REALITY_DEST env-override реально применяется в ensure_reality_config.
  * #245 — вынесенный _maybe_inject_ssh_key (best-effort, no-op без пароля).
  * #70  — sweep_stuck_spawns готов к вызову из worker-тика (guard).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services import provisioning as prov_mod
from app.services.provisioning import ProvisioningOrchestrator, choose_node
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


# ── #48 — job_timeout > самого долгого ansible-таймаута ──────────────────
def test_job_timeout_exceeds_site_playbook_timeout() -> None:
    """RQ_JOB_TIMEOUT обязан превышать site.yml timeout (900с) с запасом,
    иначе RQ убивает джобу раньше конца плейбука → ansible-сирота + Retry
    запускает второй параллельный прогон на ту же ноду."""
    from app import queue

    # site.yml гоняется с timeout=900 (provisioning._execute_task).
    assert queue.DEFAULT_JOB_TIMEOUT > 900
    # запас должен быть заметным (очередь/семафор/пост-обработка), не «на грани».
    assert queue.DEFAULT_JOB_TIMEOUT >= 1200


# ── #57 — debounce с верхней границей ────────────────────────────────────
def test_mark_node_dirty_debounce_has_upper_bound(db_session: Session) -> None:
    """Повторный mark_node_dirty НЕ отодвигает уже вооружённый reconcile_due_at
    дальше в будущее — сохраняется ПЕРВЫЙ (ранний) дедлайн."""
    node = make_node(db_session, name="dirty-1", host="198.51.100.140")
    orch = ProvisioningOrchestrator(db_session)

    # Первая правка с маленькой задержкой — дедлайн близко.
    orch.mark_node_dirty(node, delay_s=5)
    db_session.commit()
    first = (
        db_session.query(models.VPNNode.reconcile_due_at)
        .filter(models.VPNNode.id == node.id)
        .scalar()
    )
    assert first is not None

    # Вторая правка с БОЛЬШОЙ задержкой — без cap'а перезаписала бы дедлайн
    # далеко вперёд. С LEAST — остаётся ранний первый.
    orch.mark_node_dirty(node, delay_s=100_000)
    db_session.commit()
    second = (
        db_session.query(models.VPNNode.reconcile_due_at)
        .filter(models.VPNNode.id == node.id)
        .scalar()
    )
    assert second == first  # дедлайн не убежал вперёд
    assert second < utcnow() + timedelta(seconds=3600)

    # desired_generation при этом бампается на КАЖДУЮ правку.
    gen = (
        db_session.query(models.VPNNode.desired_generation)
        .filter(models.VPNNode.id == node.id)
        .scalar()
    )
    assert gen >= 2


# ── #72 — registering-нода вне choose_node ───────────────────────────────
def test_choose_node_skips_registering_node(db_session: Session) -> None:
    """Свежеспавненная нода (status=registering, is_active=True) НЕ должна
    получать новых юзеров до успешного bootstrap'а — иначе холодный
    provision_device.yml падает на неготовой ноде."""
    plan = make_plan(db_session)
    make_node(
        db_session, name="registering-node", host="198.51.100.141",
        status=models.VPNNodeStatus.registering,
    )
    active = make_node(
        db_session, name="active-node", host="198.51.100.142",
        status=models.VPNNodeStatus.active,
    )
    picked = choose_node(db_session, plan)
    assert picked.id == active.id


def test_choose_node_raises_when_only_registering(db_session: Session) -> None:
    """Если единственная нода — registering, выборки нет (а не выдача на
    неготовую ноду)."""
    plan = make_plan(db_session)
    make_node(
        db_session, name="only-registering", host="198.51.100.143",
        status=models.VPNNodeStatus.registering,
    )
    with pytest.raises(RuntimeError):
        choose_node(db_session, plan)


# ── #78 — reinstall восстанавливает hysteria2-учётки ─────────────────────
def _make_hy2_credential(
    db: Session, node: models.VPNNode, sub: models.Subscription,
    device: models.Device, password: str, port: int = 8443,
) -> models.Credential:
    hy2_cfg = models.VPNConfig(
        node_id=node.id,
        name=f"{node.name}-hy2",
        protocol=models.VPNConfigProtocol.hysteria2,
        port=port,
        sni="example.com",
        settings={},
        is_enabled=True,
    )
    db.add(hy2_cfg)
    db.commit()
    db.refresh(hy2_cfg)
    uri = f"hy2://{password}@{node.host}:{port}?sni=example.com#hy2-{node.region}"
    cred = models.Credential(
        subscription_id=sub.id,
        device_id=device.id,
        config_id=hy2_cfg.id,
        node_id=node.id,
        proto=models.VPNConfigProtocol.hysteria2.value,
        config_text=encrypt(uri),
        access_username=device.access_username,
        is_active=True,
    )
    db.add(cred)
    db.commit()
    return cred


def test_resync_hysteria2_restores_assigned_users(db_session: Session) -> None:
    """resync_node_hysteria2_clients создаёт device/apply-таску с hy2-протоколом
    и тем же паролем, что в существующей ссылке."""
    from tests.factories import make_device

    node = make_node(db_session, name="hy2-node", host="198.51.100.144")
    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = make_subscription(db_session, user, plan, node)
    cfg = make_config(db_session, node)
    device = make_device(db_session, sub, cfg, access_username="hy2-user")
    password = "s3cretHY2pw"
    _make_hy2_credential(db_session, node, sub, device, password)

    orch = ProvisioningOrchestrator(db_session)
    tasks = orch.resync_node_hysteria2_clients(node)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.target_type == "device"
    assert task.target_id == device.id
    assert task.action == "apply"
    assert task.payload["state"] == "present"
    assert task.payload["password"] == password
    protos = task.payload["protocols"]
    assert len(protos) == 1
    assert protos[0]["proto"] == models.VPNConfigProtocol.hysteria2.value
    assert protos[0]["port"] == 8443


def test_resync_hysteria2_noop_without_hy2_users(db_session: Session) -> None:
    """Нода без hy2-учёток → пустой список задач (лишних ansible-ранов нет)."""
    node = make_node(db_session, name="no-hy2-node", host="198.51.100.145")
    orch = ProvisioningOrchestrator(db_session)
    assert orch.resync_node_hysteria2_clients(node) == []


def test_extract_hy2_password_parses_uri() -> None:
    uri = "hy2://myPassw0rd@203.0.113.5:8443?sni=x#tag"
    assert prov_mod._extract_hy2_password(encrypt(uri)) == "myPassw0rd"


# ── #81 — REALITY_DEST env-override применяется ──────────────────────────
def test_reality_dest_override_applied(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При активном REALITY_SNI-override ensure_reality_config берёт dest из
    DEFAULT_REALITY_DEST (ручка REALITY_DEST перестала быть мёртвой)."""
    from app.services import node_spawner

    monkeypatch.setattr(node_spawner, "_REALITY_SNI_ENV_OVERRIDE", "forced.example")
    monkeypatch.setattr(node_spawner, "DEFAULT_REALITY_DEST", "forced.example:8443")

    node = make_node(db_session, name="reality-node", host="198.51.100.146")
    cfg = node_spawner.ensure_reality_config(db_session, node)
    assert cfg.settings["dest"] == "forced.example:8443"
    assert cfg.fallback == "forced.example:8443"


def test_reality_dest_default_is_sni_443(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без env-override поведение прежнее — dest = <sni>:443."""
    from app.services import node_spawner

    monkeypatch.setattr(node_spawner, "_REALITY_SNI_ENV_OVERRIDE", None)
    node = make_node(db_session, name="reality-node-2", host="198.51.100.147")
    cfg = node_spawner.ensure_reality_config(db_session, node, sni="www.example.org")
    assert cfg.settings["dest"] == "www.example.org:443"


# ── #245 — _maybe_inject_ssh_key вынесен, best-effort ────────────────────
def test_maybe_inject_ssh_key_noop_without_password() -> None:
    """Нет root-пароля → хелпер тихо выходит (не пытается коннектиться)."""
    # None пароль — ранний return, никаких сетевых попыток / исключений.
    assert prov_mod._maybe_inject_ssh_key(
        "198.51.100.200", None, 22, label="node test"
    ) is None


# ── #70 — sweep_stuck_spawns готов к вызову из worker-тика ────────────────
def test_sweep_stuck_spawns_is_ready() -> None:
    """Функция подбора застрявших registering-нод существует и вызываема —
    финальная привязка к worker-тику остаётся за кластером worker.py."""
    from app.services.node_spawner import sweep_stuck_spawns

    assert callable(sweep_stuck_spawns)
