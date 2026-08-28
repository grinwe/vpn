"""Пуш «конфиг готов» (services/config_ready.py) — продюсер строки config_ready.

Инцидент 2026-08-25: бот пообещал «сейчас пришлю ссылку», а канал config_ready
на бэкенде никогда не писался (``_notify`` уходил в мёртвый
``ProvisioningTask.result``). Здесь фиксируем: cold-apply с флагом → ровно одна
строка очереди; всё, что не «первая выдача свежей подписки», — молчит.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.db import SessionLocal
from app.services import config_ready, provisioning, provisioning_throttle, warm_pool
from app.services.config_ready import notify_config_ready
from app.services.provisioning import ProvisioningOrchestrator
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)

TG = "633354882"
PRIMARY = "https://grn-ssync.pro"


@pytest.fixture(autouse=True)
def _no_sub_link_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Чистый env: тесты, которым нужна абсолютная ссылка, выставляют базу сами.
    for name in ("SUB_LINK_BASE_URL", "SUB_LINK_BASE_URL_ALT", "SUB_LINK_ALT_SHARE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_cold_throttle():
    """Сквозные тесты продюсера ходят через provision_subscription (cold-путь
    троттлится глобальным in-memory bucket'ом) — не съедать бюджет соседей."""
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


def _cred(db: Session, node, device, *, is_active: bool) -> models.Credential:
    cred = models.Credential(
        node_id=node.id, device_id=device.id, subscription_id=device.subscription_id,
        access_username=device.access_username, is_active=is_active,
        proto="vless-reality", config_text="enc",
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(cred)
    return cred


def _cold_fixture(db: Session, *, tg: str = TG, notify_flag: bool | None = True):
    """Свежая подписка + pending-девайс + холодный кред + apply-таска."""
    plan = make_plan(db, name="cr-plan")
    user = make_user(db, telegram_id=tg)
    node = make_node(db, name="cr-node", host="10.9.0.1")
    cfg = make_config(db, node)
    sub = make_subscription(db, user, plan, node)
    sub.sub_token = "sub-tok-cr"
    device = make_device(db, sub, cfg, access_username=f"user-{user.id}-{sub.id}")
    device.status = models.DeviceStatus.pending
    _cred(db, node, device, is_active=False)
    db.commit()

    orch = ProvisioningOrchestrator(db)
    payload: dict = {
        "node_id": node.id,
        "protocols": [{"proto": "vless-reality"}],
        "state": "present",
    }
    if notify_flag is not None:
        payload["notify_config_ready"] = notify_flag
    task = orch.create_task("device", device.id, "apply", payload)
    db.commit()
    return orch, sub, device, task, node, cfg


def _pushes(db: Session, sub_id: int) -> list[models.AuditLog]:
    db.expire_all()
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.target_type == "subscription",
            models.AuditLog.target_id == sub_id,
            models.AuditLog.action.in_(["config_ready", "config_ready:delivered"]),
        )
        .all()
    )


# ── cold-путь через _handle_task_outcome ─────────────────────────────────


def test_cold_apply_with_flag_queues_exactly_one_push(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    orch, sub, device, task, *_ = _cold_fixture(db_session)

    orch._handle_task_outcome(task, success=True)

    rows = _pushes(db_session, sub.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "config_ready"
    assert row.actor_type == models.AuditActor.system
    assert row.extra["telegram_id"] == TG
    assert row.extra["subscription_id"] == sub.id
    assert row.extra["device_id"] == device.id
    assert row.extra["source"] == "cold"
    assert row.extra["sub_uri"] == f"{PRIMARY}/{sub.sub_token}"
    # Девайс при этом активирован как и раньше.
    db_session.refresh(device)
    assert device.status == models.DeviceStatus.active


def test_retry_of_apply_does_not_duplicate(db_session: Session) -> None:
    orch, sub, device, task, *_ = _cold_fixture(db_session)
    orch._handle_task_outcome(task, success=True)
    # Ретрай той же таски (воркер перезапустил после падения) — дубля нет.
    orch._handle_task_outcome(task, success=True)
    assert len(_pushes(db_session, sub.id)) == 1


def test_delivered_row_also_blocks_duplicate(db_session: Session) -> None:
    """ACK поллера переименовывает строку в ':delivered' — это тоже дедуп."""
    orch, sub, device, task, *_ = _cold_fixture(db_session)
    orch._handle_task_outcome(task, success=True)
    row = _pushes(db_session, sub.id)[0]
    row.action = "config_ready:delivered"
    db_session.commit()

    orch._handle_task_outcome(task, success=True)
    rows = _pushes(db_session, sub.id)
    assert [r.action for r in rows] == ["config_ready:delivered"]


def test_apply_without_flag_is_silent(db_session: Session) -> None:
    """Эмуляция reprovision_subscription (failover/migrate/unfreeze): флага нет."""
    orch, sub, device, task, *_ = _cold_fixture(db_session, notify_flag=None)
    orch._handle_task_outcome(task, success=True)
    db_session.refresh(device)
    assert device.status == models.DeviceStatus.active, "активация не пострадала"
    assert _pushes(db_session, sub.id) == []


def test_second_device_of_subscription_is_silent(db_session: Session) -> None:
    """Предшественник (в т.ч. revoked — строки не удаляются) блокирует пуш."""
    orch, sub, device, task, node, cfg = _cold_fixture(db_session)
    older = make_device(db_session, sub, cfg, access_username="older")
    older.status = models.DeviceStatus.revoked
    db_session.commit()

    orch._handle_task_outcome(task, success=True)
    assert _pushes(db_session, sub.id) == []


def test_stale_subscription_is_silent(db_session: Session) -> None:
    """Подписке больше суток — это бэклог/ретрай, а не свежая активация."""
    orch, sub, device, task, *_ = _cold_fixture(db_session)
    sub.created_at = utcnow() - timedelta(hours=25)
    db_session.commit()

    orch._handle_task_outcome(task, success=True)
    assert _pushes(db_session, sub.id) == []


def test_non_numeric_telegram_id_is_silent(db_session: Session) -> None:
    orch, sub, device, task, *_ = _cold_fixture(db_session, tg="legacy-user")
    orch._handle_task_outcome(task, success=True)
    assert _pushes(db_session, sub.id) == []


@pytest.mark.parametrize(
    "status", [models.SubscriptionStatus.expired, models.SubscriptionStatus.blocked]
)
def test_dead_subscription_is_silent(
    db_session: Session, status: models.SubscriptionStatus
) -> None:
    """activate_trial_full на 402 откатывает подписку в expired, а cold-таска
    уже в очереди и позже успешно отработает — «конфиг готов» по мёртвой
    подписке слать нельзя. blocked — то же самое."""
    orch, sub, device, task, *_ = _cold_fixture(db_session)
    sub.status = status
    db_session.commit()

    orch._handle_task_outcome(task, success=True)
    assert _pushes(db_session, sub.id) == []


def test_flag_does_not_leak_into_ansible_extra_vars(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notify_config_ready — маркер для _handle_task_outcome, не переменная
    плейбука: в extra_vars provision_device.yml его быть не должно."""
    orch, sub, device, task, node, cfg = _cold_fixture(db_session)
    seen: list[dict] = []

    def _fake_run_playbook(playbook, inventory, *, limit=None, extra_vars=None, **kw):
        seen.append({"playbook": playbook, "limit": limit, "extra_vars": extra_vars})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provisioning, "run_playbook", _fake_run_playbook)
    monkeypatch.setattr(provisioning, "build_inventory_for_node", lambda node: None)

    result = orch._execute_task(task, node)

    assert result["returncode"] == 0
    assert len(seen) == 1 and seen[0]["playbook"] == "playbooks/provision_device.yml"
    assert "notify_config_ready" not in seen[0]["extra_vars"]
    assert seen[0]["extra_vars"]["state"] == "present"
    # Сам task.payload флаг сохраняет — по нему _handle_task_outcome решает про пуш.
    db_session.refresh(task)
    assert task.payload["notify_config_ready"] is True


# ── сквозные: продюсер флага/строки в provision_subscription ─────────────


def _fresh_node(db: Session, *, name: str, host: str) -> models.VPNNode:
    node = make_node(db, name=name, host=host)
    make_config(db, node, name=f"{name}-vless")
    db.refresh(node)
    return node


def _apply_task_for(db: Session, device_id: int) -> models.ProvisioningTask:
    return (
        db.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "device",
            models.ProvisioningTask.target_id == device_id,
            models.ProvisioningTask.action == "apply",
        )
        .order_by(models.ProvisioningTask.id.desc())
        .one()
    )


def test_cold_provision_sets_flag_and_reprovision_does_not(db_session: Session) -> None:
    """Флаг ставит ТОЛЬКО первая выдача (provision_subscription, cold-путь);
    reprovision_subscription (failover/migrate/unfreeze/add-device) его не несёт —
    структурный гейт «конфиг готов» не приходит юзеру с уже живой ссылкой.
    Warm-пул пуст (ни одного warm-креда), поэтому провижн идёт cold."""
    node = _fresh_node(db_session, name="cr-e2e-cold", host="10.9.0.3")
    plan = make_plan(db_session, name="cr-e2e-plan")
    user = make_user(db_session, telegram_id=TG)

    orch = ProvisioningOrchestrator(db_session)
    sub, task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()

    assert warm_pool.pool_depth(db_session, node.id) == 0
    first_device = sub.devices[0]
    apply_task = _apply_task_for(db_session, first_device.id)
    assert apply_task.id == task.id
    assert apply_task.payload["notify_config_ready"] is True

    new_device, new_task = orch.reprovision_subscription(sub, device_name="second")
    db_session.commit()

    assert new_device.id != first_device.id
    assert new_task.action == "apply"
    assert "notify_config_ready" not in (new_task.payload or {})
    assert "notify_config_ready" not in (_apply_task_for(db_session, new_device.id).payload or {})


def test_warm_provision_stages_push_before_caller_commits(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-хит минует _handle_task_outcome: строка config_ready ставится прямо
    в provision_subscription, в транзакции вызывающего (тот коммитит сам)."""
    # warm_one_bundle гоняет ansible — глушим его в самом warm_pool (импорт
    # по имени, как в test_warm_pool.py).
    monkeypatch.setattr(
        warm_pool, "run_playbook",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(warm_pool, "build_inventory_for_node", lambda node: None)
    node = _fresh_node(db_session, name="cr-e2e-warm", host="10.9.0.4")
    assert warm_pool.warm_one_bundle(db_session, node) is not None
    assert warm_pool.pool_depth(db_session, node.id) == 1
    plan = make_plan(db_session, name="cr-e2e-warm-plan")
    user = make_user(db_session, telegram_id=TG)

    orch = ProvisioningOrchestrator(db_session)
    sub, task = orch.provision_subscription(user, plan, node_id=node.id)

    assert task.action == "assign_warm"
    staged = (
        db_session.query(models.AuditLog)
        .filter_by(action="config_ready", target_type="subscription", target_id=sub.id)
        .all()
    )
    assert len(staged) == 1 and staged[0].extra["source"] == "warm"
    assert staged[0].extra["device_id"] == sub.devices[0].id
    # До коммита вызывающего строки в БД нет — она уходит вместе с подпиской.
    other = SessionLocal()
    try:
        assert other.query(models.AuditLog).filter_by(action="config_ready").count() == 0
        db_session.commit()
        assert other.query(models.AuditLog).filter_by(action="config_ready").count() == 1
    finally:
        other.close()


def test_warm_provision_with_notify_off_stages_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notify_config_ready=False — вызывающий (бот после activate_trial_full,
    ЛК в webapp_activate) отдаёт ссылку сам и сразу; warm-пуш с той же ссылкой
    через 10 с был бы дублем (жалоба владельца 28.08)."""
    monkeypatch.setattr(
        warm_pool, "run_playbook",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(warm_pool, "build_inventory_for_node", lambda node: None)
    node = _fresh_node(db_session, name="cr-e2e-warm-off", host="10.9.0.5")
    assert warm_pool.warm_one_bundle(db_session, node) is not None
    plan = make_plan(db_session, name="cr-e2e-warm-off-plan")
    user = make_user(db_session, telegram_id=TG)

    orch = ProvisioningOrchestrator(db_session)
    sub, task = orch.provision_subscription(
        user, plan, node_id=node.id, notify_config_ready=False
    )
    db_session.commit()

    assert task.action == "assign_warm"
    staged = (
        db_session.query(models.AuditLog)
        .filter_by(action="config_ready", target_type="subscription", target_id=sub.id)
        .all()
    )
    assert staged == []


def test_cold_provision_with_notify_off_keeps_payload_flag(db_session: Session) -> None:
    """Флаг вызывающего гасит только warm-пуш. На cold-пути девайс pending,
    ссылка в момент ответа ещё не рабочая (бот честно пишет «ещё создаётся»),
    и пуш по завершении Ansible нужен всегда — маркер в payload остаётся."""
    node = _fresh_node(db_session, name="cr-e2e-cold-off", host="10.9.0.6")
    plan = make_plan(db_session, name="cr-e2e-cold-off-plan")
    user = make_user(db_session, telegram_id=TG)

    orch = ProvisioningOrchestrator(db_session)
    sub, task = orch.provision_subscription(
        user, plan, node_id=node.id, notify_config_ready=False
    )
    db_session.commit()

    assert warm_pool.pool_depth(db_session, node.id) == 0
    assert task.action == "apply"
    assert task.payload["notify_config_ready"] is True
    assert (
        db_session.query(models.AuditLog)
        .filter_by(action="config_ready", target_type="subscription", target_id=sub.id)
        .count()
        == 0
    )


# ── warm-путь: прямой вызов хелпера без commit ───────────────────────────


def _warm_fixture(db: Session):
    plan = make_plan(db, name="cr-warm-plan")
    user = make_user(db, telegram_id=TG)
    node = make_node(db, name="cr-warm-node", host="10.9.0.2")
    cfg = make_config(db, node)
    sub = make_subscription(db, user, plan, node)
    sub.sub_token = "sub-tok-warm"
    device = make_device(db, sub, cfg, access_username=f"user-{user.id}-{sub.id}")
    _cred(db, node, device, is_active=True)
    db.commit()
    return sub, device


def test_warm_path_stages_row_and_caller_commits(db_session: Session) -> None:
    sub, device = _warm_fixture(db_session)

    assert notify_config_ready(db_session, device, source="warm", commit=False) is True

    # В сессии строка есть (хелпер делает flush через SAVEPOINT: SessionLocal
    # autoflush=False), в БД — ещё нет: коммитит вызывающий.
    staged = db_session.query(models.AuditLog).filter_by(action="config_ready").all()
    assert len(staged) == 1 and staged[0].extra["source"] == "warm"
    other = SessionLocal()
    try:
        assert other.query(models.AuditLog).filter_by(action="config_ready").count() == 0
        db_session.commit()
        assert other.query(models.AuditLog).filter_by(action="config_ready").count() == 1
    finally:
        other.close()

    # Повтор после коммита — дедуп.
    assert notify_config_ready(db_session, device, source="warm", commit=False) is False
    assert len(_pushes(db_session, sub.id)) == 1


def test_row_write_failure_keeps_session_usable(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запись строки — под SAVEPOINT: если flush упал, откатывается только он,
    а транзакция вызывающего (warm-путь: подписка и девайс ещё не закоммичены)
    остаётся рабочей — без PendingRollbackError на следующем запросе."""
    sub, device = _warm_fixture(db_session)
    # Несериализуемый extra → flush внутри savepoint падает на JSON-биндинге.
    monkeypatch.setattr(config_ready, "_absolute_sub_uri", lambda token: object())

    assert notify_config_ready(db_session, device, source="warm", commit=False) is False

    # Сессия жива: запрос не бросает PendingRollbackError, строки нет.
    assert db_session.query(models.AuditLog).filter_by(action="config_ready").count() == 0
    # И внешняя транзакция продолжает работать: повтор со снятым сбоем пишет строку.
    monkeypatch.undo()
    assert notify_config_ready(db_session, device, source="warm", commit=False) is True
    db_session.commit()
    assert len(_pushes(db_session, sub.id)) == 1


def test_warm_path_requires_live_credential(db_session: Session) -> None:
    sub, device = _warm_fixture(db_session)
    for c in device.credentials:
        c.is_active = False
    db_session.commit()
    assert notify_config_ready(db_session, device, source="warm", commit=True) is False
    assert _pushes(db_session, sub.id) == []


# ── ссылка в пуше ────────────────────────────────────────────────────────


def test_sub_uri_absolute_when_base_configured(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    sub, device = _warm_fixture(db_session)
    assert notify_config_ready(db_session, device, source="warm", commit=True)
    row = _pushes(db_session, sub.id)[0]
    assert row.extra["sub_uri"] == f"{PRIMARY}/{sub.sub_token}"


def test_sub_uri_omitted_without_base(db_session: Session) -> None:
    """Без базы sub_url_for даёт относительный путь — в пуш такое не кладём."""
    sub, device = _warm_fixture(db_session)
    assert notify_config_ready(db_session, device, source="warm", commit=True)
    row = _pushes(db_session, sub.id)[0]
    assert "sub_uri" not in row.extra


# ── доставка через /api/notifications/pending ────────────────────────────


def test_pending_renders_config_ready_with_link_and_platform_prompt(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Со ссылкой абзаца про ЛК/config в тексте нет: кнопка ЛК первой строкой
    в onboarding_keyboard, которую бот вешает на этот тип, — текстом это
    повторялось до четырёх раз за один тап (жалоба владельца 28.08)."""
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    sub, device = _warm_fixture(db_session)
    assert notify_config_ready(db_session, device, source="warm", commit=True)

    resp = client.get("/api/notifications/pending?limit=10")
    assert resp.status_code == 200
    items = [n for n in resp.json() if n["type"] == "config_ready"]
    assert len(items) == 1
    item = items[0]
    assert item["telegram_id"] == TG
    assert item["text"].startswith("✅ Конфиг VPN готов")
    assert f"Ссылка: {PRIMARY}/{sub.sub_token}" in item["text"]
    assert "Выбери платформу" in item["text"]
    assert "личном кабинете" not in item["text"]
    assert "/config" not in item["text"]


def test_pending_without_link_still_points_to_cabinet(
    client, db_session: Session
) -> None:
    """Без ссылки единственный путь к ней — ЛК: одна строка про него остаётся."""
    sub, device = _warm_fixture(db_session)
    assert notify_config_ready(db_session, device, source="warm", commit=True)

    resp = client.get("/api/notifications/pending?limit=10")
    items = [n for n in resp.json() if n["type"] == "config_ready"]
    assert len(items) == 1
    assert "Ссылка: " not in items[0]["text"]
    assert "личном кабинете" in items[0]["text"]
    assert "Выбери платформу" in items[0]["text"]
