"""Phase A — diverse N×M subscription (flag DIVERSE_SUB_NODES).

Проверяем гард-инвариант (по умолчанию метод — no-op, поведение не меняется) и
привязку тёплых бандлов диверсных нод к тому же device. Боевой провижининг
(provision_subscription целиком) тут не гоняем — только аддитивный хелпер.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import provisioning, warm_pool
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _warm_cred(db: Session, node: models.VPNNode, username: str) -> models.Credential:
    c = models.Credential(
        node_id=node.id,
        proto="vless-reality",
        config_text="enc-uri",
        access_username=username,
        is_active=True,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_attach_diverse_off_by_default(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DIVERSE_SUB_NODES", raising=False)
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="ru-1", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg)

    called: list[int] = []
    monkeypatch.setattr(
        provisioning, "choose_node", lambda *a, **k: called.append(1) or node
    )
    orch = ProvisioningOrchestrator(db_session)
    # флаг выключен (default 1) → метод не должен дёргать choose_node вообще
    orch._maybe_attach_diverse(sub, dev, plan, node)
    assert called == []


def test_attach_diverse_binds_warm_bundles(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-1", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg)

    d1 = make_node(db_session, name="de-1", region="de", host="198.51.100.20")
    d2 = make_node(db_session, name="nl-1", region="nl", host="198.51.100.30")
    c1 = _warm_cred(db_session, d1, "warm-de")
    c2 = _warm_cred(db_session, d2, "warm-nl")
    creds_by_node = {d1.id: [c1], d2.id: [c2]}
    seq = [d1, d2]

    def fake_choose(db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None):
        for n in seq:
            if n.id not in (exclude_node_ids or []):
                return n
        raise RuntimeError("no more diverse nodes")

    monkeypatch.setattr(provisioning, "choose_node", fake_choose)
    monkeypatch.setattr(
        warm_pool, "try_assign_bundle",
        lambda db, node_id, sub_id: creds_by_node.get(node_id),
    )

    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)

    db_session.refresh(c1)
    db_session.refresh(c2)
    # тёплые бандлы 2 диверсных нод привязаны к тому же device
    assert c1.device_id == dev.id
    assert c2.device_id == dev.id


def _active_cred_on(
    db: Session, node: models.VPNNode, device: models.Device, username: str
) -> models.Credential:
    c = models.Credential(
        node_id=node.id, device_id=device.id, is_active=True,
        proto="vless-reality", config_text="enc-uri", access_username=username,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_attach_diverse_idempotent_when_full(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # device уже несёт активные creds на 3 разных нодах ⇒ повторный вызов
    # (reprovision/миграция) НЕ должен добирать ещё (иначе набор раздувается).
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-idem", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg, access_username="u-ru")
    d1 = make_node(db_session, name="de-idem", region="de", host="198.51.100.50")
    d2 = make_node(db_session, name="nl-idem", region="nl", host="198.51.100.60")
    _active_cred_on(db_session, primary, dev, "u-ru")
    _active_cred_on(db_session, d1, dev, "u-de")
    _active_cred_on(db_session, d2, dev, "u-nl")

    called: list[int] = []
    monkeypatch.setattr(provisioning, "choose_node", lambda *a, **k: called.append(1))
    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)
    assert called == []  # уже 3 ноды (need=0) → choose_node не зовётся


def test_attach_diverse_skips_nodes_without_warm(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-2", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg)
    d1 = make_node(db_session, name="de-2", region="de", host="198.51.100.40")

    monkeypatch.setattr(
        provisioning, "choose_node",
        lambda db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None: (
            d1 if d1.id not in (exclude_node_ids or []) else (_ for _ in ()).throw(RuntimeError())
        ),
    )
    # нет тёплого бандла на диверсной ноде → пропускаем, не падаем
    monkeypatch.setattr(warm_pool, "try_assign_bundle", lambda db, n, s: None)

    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)  # не должно бросить


def test_attach_diverse_fallback_when_geo_exhausted(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Гео-разнесение исчерпано: все диверсные ноды — тот же регион primary'я или
    # region IS NULL. Старый цикл застревал (пасс с exclude_regions=["ru"] не
    # находил ничего — SQL `~region.in_` роняет и same-region, и NULL-ноды) →
    # device оставался на 1 ноде. Новый — пасс 2 без фильтра региона добирает
    # РАЗНЫМИ нодами до N. Заодно проверяет, что слот не «сгорает».
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-fb", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg)
    s1 = make_node(db_session, name="ru-fb-1", region="ru", host="198.51.100.91")
    # region NOT NULL в схеме → NULL нельзя. Тот же регион primary'я ("ru") даёт
    # тот же эффект «гео исчерпано»: пасс 1 (exclude_regions=["ru"]) прячет ноду,
    # пасс 2 (без фильтра) её добирает.
    s2 = make_node(db_session, name="same-fb", region="ru", host="198.51.100.92")
    w1 = _warm_cred(db_session, s1, "warm-s1")
    w2 = _warm_cred(db_session, s2, "warm-s2")

    pool = [s1, s2]

    def fake_choose(db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None):
        ex_ids = set(exclude_node_ids or [])
        ex_regs = set(exclude_regions or [])
        for n in pool:
            if n.id in ex_ids:
                continue
            # Мимикрия SQL `~region.in_(ex_regs)`: при НЕПУСТОМ фильтре строки с
            # region ∈ ex_regs И region IS NULL отбрасываются (NULL IN → NULL).
            if ex_regs and (n.region is None or n.region in ex_regs):
                continue
            return n
        raise RuntimeError("no node")

    monkeypatch.setattr(provisioning, "choose_node", fake_choose)
    monkeypatch.setattr(
        warm_pool, "try_assign_bundle",
        lambda db, nid, sid: {s1.id: [w1], s2.id: [w2]}.get(nid),
    )

    orch = ProvisioningOrchestrator(db_session)
    orch._maybe_attach_diverse(sub, dev, plan, primary)

    db_session.refresh(w1)
    db_session.refresh(w2)
    # обе диверсные ноды добраны, хотя гео-фильтр их прятал (пасс 2 спас набор)
    assert w1.device_id == dev.id
    assert w2.device_id == dev.id


def test_swap_node_out_replaces_one_node(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    a = make_node(db_session, name="swp-a", region="ru")
    b = make_node(db_session, name="swp-b", region="de", host="198.51.100.71")
    c = make_node(db_session, name="swp-c", region="nl", host="198.51.100.72")
    cfg = make_config(db_session, a)
    sub = make_subscription(db_session, user, plan, a)
    dev = make_device(db_session, sub, cfg, access_username="u")
    _active_cred_on(db_session, a, dev, "u-a")
    cred_b = _active_cred_on(db_session, b, dev, "u-b")
    _active_cred_on(db_session, c, dev, "u-c")

    # свежая нода d + тёплый бандл для добора взамен выкинутой
    d = make_node(db_session, name="swp-d", region="fr", host="198.51.100.73")
    warm_d = _warm_cred(db_session, d, "warm-d")

    def fake_choose(db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None):
        if d.id not in (exclude_node_ids or []):
            return d
        raise RuntimeError("no more")

    monkeypatch.setattr(provisioning, "choose_node", fake_choose)
    monkeypatch.setattr(
        warm_pool, "try_assign_bundle",
        lambda db, nid, sid: [warm_d] if nid == d.id else None,
    )

    orch = ProvisioningOrchestrator(db_session)
    added = orch.swap_node_out(dev, b.id)

    db_session.refresh(cred_b)
    db_session.refresh(warm_d)
    assert cred_b.is_active is False              # выкинутая нода деактивирована
    assert cred_b.pool_state == models.CredentialPoolState.revoked
    assert warm_d.device_id == dev.id             # добрана свежая взамен
    assert added == 1


def test_backfill_diverse_targets_user(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Таргетированный backfill: user_id фильтрует — трогаем только подписки юзера.
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    ua = make_user(db_session, telegram_id="tg-a")
    ub = make_user(db_session, telegram_id="tg-b")
    na = make_node(db_session, name="tu-a", region="ru")
    nb = make_node(db_session, name="tu-b", region="ru", host="198.51.100.211")
    cfg_a = make_config(db_session, na)
    cfg_b = make_config(db_session, nb)
    sub_a = make_subscription(db_session, ua, plan, na)
    sub_b = make_subscription(db_session, ub, plan, nb)
    dev_a = make_device(db_session, sub_a, cfg_a, access_username="ua")
    dev_b = make_device(db_session, sub_b, cfg_b, access_username="ub")
    _active_cred_on(db_session, na, dev_a, "ua-1")
    _active_cred_on(db_session, nb, dev_b, "ub-1")

    seen: list = []
    orch = ProvisioningOrchestrator(db_session)
    monkeypatch.setattr(
        orch, "_maybe_attach_diverse",
        lambda s, d, p, primary, **k: seen.append(d.id),
    )
    res = orch.backfill_diverse_subscriptions(limit=10, dry_run=False, user_id=ua.id)
    assert res["user_id"] == ua.id
    assert seen == [dev_a.id]  # тронут ТОЛЬКО девайс юзера A


def test_migrate_subscription_attaches_diverse(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Мягкая миграция: после переезда подписки на новую ноду юзер должен получить
    # диверсный набор (как при add-device), а НЕ застрять на одной ноде. Проверяем
    # проводку: migrate зовёт _maybe_attach_diverse с новой нодой как primary.
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    old = make_node(db_session, name="mig-old", region="ru")
    new = make_node(db_session, name="mig-new", region="ru", host="198.51.100.201")
    cfg = make_config(db_session, old)
    sub = make_subscription(db_session, user, plan, old)
    dev = make_device(db_session, sub, cfg, access_username="u")

    monkeypatch.setattr(provisioning, "choose_node", lambda *a, **k: new)
    orch = ProvisioningOrchestrator(db_session)
    monkeypatch.setattr(orch, "revoke_device", lambda *a, **k: None)
    monkeypatch.setattr(orch, "reprovision_subscription", lambda *a, **k: (dev, object()))
    calls: list = []
    monkeypatch.setattr(
        orch, "_maybe_attach_diverse",
        lambda s, d, p, primary, **k: calls.append((d, primary)),
    )

    target, first_dev, _task = orch.migrate_subscription_to_new_node(sub)
    assert target.id == new.id
    # диверс-добор вызван для переехавшего девайса, primary = НОВАЯ нода
    assert calls and calls[0][1].id == new.id


def test_migrate_device_blocked_for_diverse(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    a = make_node(db_session, name="mg-a", region="ru")
    b = make_node(db_session, name="mg-b", region="de", host="198.51.100.81")
    target = make_node(db_session, name="mg-t", region="fr", host="198.51.100.82")
    cfg = make_config(db_session, a)
    sub = make_subscription(db_session, user, plan, a)
    dev = make_device(db_session, sub, cfg, access_username="u")
    _active_cred_on(db_session, a, dev, "u-a")
    _active_cred_on(db_session, b, dev, "u-b")  # 2 ноды → диверсный

    orch = ProvisioningOrchestrator(db_session)
    # Диверс-девайс без тёплого бандла на целевой ноде: холодный legacy-путь
    # схлопнул бы набор до одной ноды — отказ с подсказкой «повтори позже».
    # С тёплым бандлом переезд идёт (см. test_device_migration.py).
    with pytest.raises(RuntimeError, match="тёплого бандла"):
        orch.migrate_device_to_node(dev, target_node_id=target.id)


def test_backfill_diverse_dry_run_no_mutation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Phase A.2: dry_run только считает охват, НИЧЕГО не привязывает.
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="bf-ru", region="ru")
    cfg = make_config(db_session, primary)
    sub = make_subscription(db_session, user, plan, primary)
    dev = make_device(db_session, sub, cfg, access_username="u")
    _active_cred_on(db_session, primary, dev, "u-ru")  # 1 нода → eligible

    d1 = make_node(db_session, name="bf-de", region="de", host="198.51.100.111")
    w1 = _warm_cred(db_session, d1, "warm-bf")
    monkeypatch.setattr(provisioning, "choose_node", lambda *a, **k: d1)
    monkeypatch.setattr(warm_pool, "try_assign_bundle", lambda db, nid, sid: [w1])

    orch = ProvisioningOrchestrator(db_session)
    res = orch.backfill_diverse_subscriptions(limit=10, dry_run=True)

    db_session.refresh(w1)
    assert res["eligible_total"] >= 1
    assert res["nodes_added"] == 0
    assert w1.device_id is None  # dry-run ничего не привязал


def test_backfill_diverse_tops_up_and_skips_full(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Phase A.2: однонодовый девайс добирается до N; уже-диверсный — пропускается.
    monkeypatch.setenv("DIVERSE_SUB_NODES", "3")
    plan = make_plan(db_session)
    user = make_user(db_session)
    # девайс A — однонодовый (должен добраться)
    pa = make_node(db_session, name="bf-a", region="ru")
    cfg_a = make_config(db_session, pa)
    sub_a = make_subscription(db_session, user, plan, pa)
    dev_a = make_device(db_session, sub_a, cfg_a, access_username="ua")
    _active_cred_on(db_session, pa, dev_a, "ua-ru")
    # девайс B — уже 3 ноды (должен быть пропущен, не eligible)
    pb = make_node(db_session, name="bf-b", region="ru", host="198.51.100.121")
    nb2 = make_node(db_session, name="bf-b2", region="de", host="198.51.100.122")
    nb3 = make_node(db_session, name="bf-b3", region="nl", host="198.51.100.123")
    cfg_b = make_config(db_session, pb)
    sub_b = make_subscription(db_session, user, plan, pb)
    dev_b = make_device(db_session, sub_b, cfg_b, access_username="ub")
    _active_cred_on(db_session, pb, dev_b, "ub-1")
    _active_cred_on(db_session, nb2, dev_b, "ub-2")
    _active_cred_on(db_session, nb3, dev_b, "ub-3")

    g1 = make_node(db_session, name="bf-g1", region="de", host="198.51.100.131")
    g2 = make_node(db_session, name="bf-g2", region="nl", host="198.51.100.132")
    wg1 = _warm_cred(db_session, g1, "warm-g1")
    wg2 = _warm_cred(db_session, g2, "warm-g2")
    seq = [g1, g2]

    def fake_choose(db, p, *, node_id=None, exclude_node_ids=None, exclude_regions=None):
        for n in seq:
            if n.id not in (exclude_node_ids or []):
                return n
        raise RuntimeError("no more")

    monkeypatch.setattr(provisioning, "choose_node", fake_choose)
    monkeypatch.setattr(
        warm_pool, "try_assign_bundle",
        lambda db, nid, sid: {g1.id: [wg1], g2.id: [wg2]}.get(nid),
    )

    orch = ProvisioningOrchestrator(db_session)
    res = orch.backfill_diverse_subscriptions(limit=10, dry_run=False)

    db_session.refresh(wg1)
    db_session.refresh(wg2)
    assert res["processed"] == 1      # тронут только однонодовый девайс A
    assert res["topped_up"] == 1      # девайс A реально добрался
    assert res["no_op"] == 0
    assert res["nodes_added"] == 2    # добрано 2 ноды (до N=3)
    assert wg1.device_id == dev_a.id
    assert wg2.device_id == dev_a.id
