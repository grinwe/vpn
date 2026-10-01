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
    from sqlalchemy import text as sql_text

    from app.db import engine

    # Тот же экзорцизм advisory-локов, что в _failover_fixture (см. её
    # докстринг): без него в ПОЛНОМ прогоне сьюта тест стабильно ловил
    # «already in progress» от лока (4001, id), залипшего на idle-коннекте
    # чужого теста, — а изолированно был вечно зелёным.
    engine.dispose()
    db_session.execute(sql_text("SELECT pg_advisory_unlock_all()"))
    db_session.commit()
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


def _failover_fixture(db_session, monkeypatch, suffix: str):
    """Общий сетап: битый девайс с токеном + свежая нода + оркестратор.

    Advisory-локи failover'а — session-level и живут на коннекте; коннект
    после close() возвращается в пул живым, а TRUNCATE RESTART IDENTITY
    делает device.id одинаковыми между тестами — залипший лок соседнего
    теста ловил бы ложный «already in progress». Рвём пул целиком.
    """
    from sqlalchemy import text as sql_text

    from app.db import engine

    engine.dispose()  # idle-коннекты пула (чужие залипшие локи)
    db_session.execute(sql_text("SELECT pg_advisory_unlock_all()"))
    db_session.commit()  # свой checked-out коннект
    plan = make_plan(db_session, name=f"fd-{suffix}-plan")
    user = make_user(db_session, telegram_id=f"fd-{suffix}")
    node = make_node(db_session, name=f"fd-{suffix}-a", region="ru", host="10.0.2.1")
    fresh = make_node(db_session, name=f"fd-{suffix}-f", region="ru", host="10.0.2.9")
    cfg = make_config(db_session, node)
    cfg_fresh = make_config(db_session, fresh)
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg, access_username=f"O-{suffix}")
    dev.sub_token = f"tok-old-{suffix}"
    _cred(db_session, node, dev, f"O-{suffix}-a")
    db_session.commit()

    monkeypatch.setattr(prov_mod, "choose_node", lambda db, plan_, **kw: fresh)
    orch = ProvisioningOrchestrator(db_session)
    return orch, sub, dev, cfg_fresh, fresh


def test_failover_builds_replacement_before_revoking_old(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Инцидент 2026-08-21: ревок старого коммитился ДО создания нового —
    в это окно саб-токен не находил ни одного живого девайса, и клиент,
    нажавший «не работает», получал пустую выдачу («ошибка конфигурации»
    в Happ) ровно пока его чинили. Порядок обязан быть: собрать замену →
    разложить → ревокнуть старый → перенести токен."""
    orch, sub, dev, cfg_fresh, fresh = _failover_fixture(db_session, monkeypatch, "ord")
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="O-ord-new")
    new_dev.sub_token = "tok-new-tmp"
    # Живой published-лег — тёплый путь: своп происходит немедленно.
    _cred(db_session, fresh, new_dev, "O-ord-new-a")
    for c in new_dev.credentials:
        c.leg_published = True
        c.leg_role = "primary"
    db_session.commit()

    events: list[str] = []
    monkeypatch.setattr(
        orch, "reprovision_subscription",
        lambda *a, **k: (events.append("reprovision"), (new_dev, None))[1],
    )
    monkeypatch.setattr(
        orch, "_maybe_attach_diverse", lambda *a, **k: events.append("diverse")
    )
    monkeypatch.setattr(
        orch, "_apply_leg_scheme", lambda device: events.append("apply")
    )
    monkeypatch.setattr(
        orch, "revoke_device", lambda *a, **k: events.append("revoke")
    )

    orch.failover_device(dev)

    assert events == ["reprovision", "diverse", "apply", "revoke"], (
        "замена обязана быть собрана и разложена ДО ревока старого девайса"
    )
    # Токен клиента переехал на новый девайс (byte-identical ссылки),
    # журнал намерения снят той же транзакцией.
    db_session.expire_all()
    refreshed_new = db_session.get(models.Device, new_dev.id)
    assert refreshed_new.sub_token == "tok-old-ord"
    assert refreshed_new.pending_swap_from is None
    assert db_session.get(models.Device, dev.id).sub_token is None


def test_failover_dry_pool_defers_swap(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сухой warm-пул: замена целиком cold — старый девайс НЕ трогается,
    токен остаётся у него, а замена помечена pending_swap_from. Своп
    доделает _handle_task_outcome после активации (или жнец)."""
    orch, sub, dev, cfg_fresh, _fresh = _failover_fixture(
        db_session, monkeypatch, "dry"
    )
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="O-dry-new")
    new_dev.sub_token = "tok-new-dry"
    db_session.commit()  # ни одного published-крема: сухой путь

    revoked: list[int] = []
    monkeypatch.setattr(orch, "revoke_device", lambda d, **k: revoked.append(d.id))
    monkeypatch.setattr(
        orch, "reprovision_subscription", lambda *a, **k: (new_dev, None)
    )
    monkeypatch.setattr(orch, "_maybe_attach_diverse", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_apply_leg_scheme", lambda device: None)

    orch.failover_device(dev)

    assert not revoked, "при сухом пуле старый девайс не должен ревокаться"
    db_session.expire_all()
    old = db_session.get(models.Device, dev.id)
    new = db_session.get(models.Device, new_dev.id)
    assert old.status == models.DeviceStatus.active
    assert old.sub_token == "tok-old-dry"
    assert new.pending_swap_from == dev.id


def test_task_outcome_finishes_deferred_swap(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Успешная apply-таска замены с маркером обязана доделать своп:
    активировать креды, ревокнуть старый, перенести токен, снять маркер."""
    from app.services import provisioning as prov_mod2  # noqa: F401

    orch, sub, dev, cfg_fresh, fresh = _failover_fixture(
        db_session, monkeypatch, "fin"
    )
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="O-fin-new")
    new_dev.status = models.DeviceStatus.pending
    new_dev.sub_token = "tok-new-fin"
    new_dev.pending_swap_from = dev.id
    db_session.add(
        models.Credential(
            node_id=fresh.id, device_id=new_dev.id, access_username="O-fin-new-a",
            is_active=False, proto="vless-reality", config_text="enc",
            pool_state=models.CredentialPoolState.assigned,
        )
    )
    db_session.commit()

    task = orch.create_task(
        "device",
        new_dev.id,
        "apply",
        {"node_id": fresh.id, "protocols": [{"proto": "vless-reality"}]},
    )
    db_session.commit()

    monkeypatch.setattr(
        type(orch), "_notify_bot_config_ready", lambda self, d: None
    )
    orch._handle_task_outcome(task, success=True)

    db_session.expire_all()
    old = db_session.get(models.Device, dev.id)
    new = db_session.get(models.Device, new_dev.id)
    assert new.pending_swap_from is None, "маркер обязан сняться"
    assert new.sub_token == "tok-old-fin", "токен обязан переехать"
    assert old.status in (
        models.DeviceStatus.disabled,
        models.DeviceStatus.revoked,
    ), "старый девайс обязан погаснуть"


def test_swap_reaper_finishes_and_reaps(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Жнец: старый погашен → доделать своп; старый жив → ревокнуть сироту."""
    from datetime import datetime, timedelta

    from app.worker import run_device_swap_reaper_tick

    orch, sub, dev, cfg_fresh, fresh = _failover_fixture(
        db_session, monkeypatch, "reap"
    )
    # Кейс 1: старый погашен, свап не доделан.
    dead_old = make_device(db_session, sub, cfg_fresh, access_username="R-old")
    dead_old.status = models.DeviceStatus.disabled
    dead_old.sub_token = "tok-dead-old"
    repl = make_device(db_session, sub, cfg_fresh, access_username="R-repl")
    repl.pending_swap_from = dead_old.id
    repl.created_at = datetime.utcnow() - timedelta(hours=1)
    # Кейс 2: старый жив — замена-сирота.
    orphan = make_device(db_session, sub, cfg_fresh, access_username="R-orph")
    orphan.pending_swap_from = dev.id  # dev — active из фикстуры
    orphan.created_at = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    result = run_device_swap_reaper_tick()

    assert result["finished"] >= 1 and result["reaped"] >= 1, result
    db_session.expire_all()
    assert db_session.get(models.Device, repl.id).sub_token == "tok-dead-old"
    assert db_session.get(models.Device, repl.id).pending_swap_from is None
    orph = db_session.get(models.Device, orphan.id)
    assert orph.pending_swap_from is None
    assert orph.status in (
        models.DeviceStatus.disabled,
        models.DeviceStatus.revoked,
    )
    # Старый живой девайс не тронут.
    assert db_session.get(models.Device, dev.id).status == models.DeviceStatus.active


def test_failover_keeps_old_device_when_reprovision_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Бонус нового порядка: упавший reprovision больше не оставляет юзера
    вовсе без девайса — старый не тронут, токен на месте."""
    orch, _sub, dev, _cfg_fresh, _fresh = _failover_fixture(
        db_session, monkeypatch, "fail"
    )

    revoked: list[int] = []
    monkeypatch.setattr(orch, "revoke_device", lambda *a, **k: revoked.append(1))

    def _boom(*a, **k):
        raise RuntimeError("no warm node")

    monkeypatch.setattr(orch, "reprovision_subscription", _boom)

    with pytest.raises(RuntimeError):
        orch.failover_device(dev)

    assert not revoked, "старый девайс не должен ревокаться при упавшей замене"
    db_session.expire_all()
    refreshed = db_session.get(models.Device, dev.id)
    assert refreshed.status == models.DeviceStatus.active
    assert refreshed.sub_token == "tok-old-fail"
    assert any(c.is_active for c in refreshed.credentials)


def test_failover_double_tap_is_rejected_by_advisory_lock(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью 2026-08-21: FOR UPDATE self_repair умирает на первом commit'е
    внутри failover, а старый девайс теперь active до конца сборки — второй
    тап «не работает» запускал параллельный failover и оставлял
    девайса-сироту. Session-level advisory lock обязан отсечь второго."""
    from sqlalchemy import text

    from app.db import SessionLocal

    orch, _sub, dev, _cfg_fresh, _fresh = _failover_fixture(
        db_session, monkeypatch, "lock"
    )

    rival = SessionLocal()
    try:
        got = rival.execute(
            text("SELECT pg_try_advisory_lock(4001, :d)"), {"d": dev.id}
        ).scalar()
        assert got, "соперник обязан был взять лок первым"

        with pytest.raises(RuntimeError, match="already in progress"):
            orch.failover_device(dev)
    finally:
        rival.execute(text("SELECT pg_advisory_unlock(4001, :d)"), {"d": dev.id})
        rival.commit()
        rival.close()

    # Старый девайс не тронут вторым тапом.
    db_session.expire_all()
    assert db_session.get(models.Device, dev.id).status == models.DeviceStatus.active


def test_failover_compensates_orphan_when_build_fails_after_reprovision(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью 2026-08-21: сбой между коммитом reprovision и токен-свопом
    оставлял закоммиченного девайса-сироту — подписка навсегда упиралась в
    Device limit. Компенсация обязана ревокнуть именно ЗАМЕНУ, не тронув
    старый девайс."""
    orch, sub, dev, cfg_fresh, _fresh = _failover_fixture(
        db_session, monkeypatch, "orph"
    )
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="O-orph-new")
    db_session.commit()

    revoked: list[int] = []
    monkeypatch.setattr(
        orch, "revoke_device", lambda d, **k: revoked.append(d.id)
    )
    monkeypatch.setattr(
        orch, "reprovision_subscription", lambda *a, **k: (new_dev, None)
    )

    def _boom(*a, **k):
        raise RuntimeError("diverse attach exploded")

    monkeypatch.setattr(orch, "_maybe_attach_diverse", _boom)

    with pytest.raises(RuntimeError, match="diverse attach exploded"):
        orch.failover_device(dev)

    assert revoked == [new_dev.id], (
        "компенсация обязана ревокнуть замену-сироту и только её"
    )
    db_session.expire_all()
    assert db_session.get(models.Device, dev.id).status == models.DeviceStatus.active


def test_failover_activates_replacement_with_live_legs(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling-alias отдаёт только status=active девайсы: замена с живыми
    (тёплыми) published-легами обязана активироваться ДО ревока старого,
    иначе в окно до ansible старый токен отдавал бы пустоту."""
    orch, sub, dev, cfg_fresh, fresh = _failover_fixture(
        db_session, monkeypatch, "act"
    )
    new_dev = make_device(db_session, sub, cfg_fresh, access_username="O-act-new")
    new_dev.status = models.DeviceStatus.pending
    new_dev.sub_token = "tok-new-act"
    _cred(db_session, fresh, new_dev, "O-act-new-a")
    db_session.commit()
    # Тёплый published-лег — как после _maybe_attach_diverse + раскладки.
    for c in new_dev.credentials:
        c.leg_published = True
        c.leg_role = "primary"
    db_session.commit()

    monkeypatch.setattr(
        orch, "reprovision_subscription", lambda *a, **k: (new_dev, None)
    )
    monkeypatch.setattr(orch, "_maybe_attach_diverse", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_apply_leg_scheme", lambda device: None)
    monkeypatch.setattr(orch, "revoke_device", lambda *a, **k: None)

    orch.failover_device(dev)

    db_session.expire_all()
    assert (
        db_session.get(models.Device, new_dev.id).status
        == models.DeviceStatus.active
    ), "замена с живым published-легом обязана стать active до ревока старого"


def test_failover_rejects_second_while_replacement_in_flight(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью 2026-08-25: в отложенном окне (сухой пул) старый девайс жив,
    и второй тап запускал ВТОРОЙ failover — проигравшая замена оставалась
    вечным active-девайсом. Живая незавершённая замена = «уже чиним»."""
    orch, sub, dev, cfg_fresh, _fresh = _failover_fixture(
        db_session, monkeypatch, "gate"
    )
    repl = make_device(db_session, sub, cfg_fresh, access_username="G-repl")
    repl.pending_swap_from = dev.id
    db_session.commit()

    called: list[str] = []
    monkeypatch.setattr(
        orch, "reprovision_subscription",
        lambda *a, **k: called.append("reprovision"),
    )

    with pytest.raises(RuntimeError, match="already in flight"):
        orch.failover_device(dev)

    assert not called, "второй failover не должен дойти до reprovision"
