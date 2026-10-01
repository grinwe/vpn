"""Netfix (сетевой аудит 2026-07): доставка конфига через `GET /api/sub/{token}`
и очередь пуш-уведомлений `/api/notifications/pending`.

Покрывает находки:
  * profile-update-interval вынесен в env + дефолт снижен до 2ч;
  * `cache-control: no-store, private` на саб-ответах;
  * `Retry-After` на транзиентных 503;
  * frozen-подписка → 503+Retry-After (а не 403, который стирает профиль);
    blocked/expired остаются 403;
  * креды нод в cooldown/декоммишене исключаются из выдачи (+ fallback,
    когда все ноды нездоровы — лучше живой-но-неоптимальный набор, чем 503);
  * пустой decrypt в per-device ветке логируется (диагностика деградации);
  * очередь пушей FIFO с приоритетом: массовая рассылка не топит срочные пуши.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta

from app import models
from app.time_utils import utcnow

from .factories import make_config, make_node, make_plan, make_subscription, make_user


def _mk_device(
    db,
    sub: models.Subscription,
    cfg: models.VPNConfig,
    *,
    token: str,
    uri: str,
    status: models.DeviceStatus = models.DeviceStatus.active,
    cred_active: bool = True,
    name: str = "primary",
    node_id: int | None = None,
) -> models.Device:
    device = models.Device(
        user_id=sub.user_id,
        subscription_id=sub.id,
        config_id=cfg.id,
        name=name,
        status=status,
        access_username=f"u-{sub.id}-{token}",
        sub_token=token,
    )
    db.add(device)
    db.flush()
    db.add(
        models.Credential(
            subscription_id=sub.id,
            device_id=device.id,
            config_id=cfg.id,
            node_id=node_id if node_id is not None else cfg.node_id,
            proto="vless-reality",
            config_text=uri,
            is_active=cred_active,
        )
    )
    db.commit()
    db.refresh(device)
    return device


def _setup(db):
    node = make_node(db)
    cfg = make_config(db, node)
    user = make_user(db)
    plan = make_plan(db)
    sub = make_subscription(db, user, plan, node)
    return node, cfg, user, plan, sub


def _body(resp) -> str:
    """Тело саб-ответа с ОТРЕЗАННЫМИ #fragment'ами.

    С 2026-07 каждый URI несёт нейтральное имя эндпоинта («{эмодзи} V8 сервер N»,
    коммит 75769ca) — тесты этого файла проверяют, КАКИЕ креды отданы, а не как
    они подписаны, поэтому сравнивать удобнее без хвоста. Сам формат имени
    покрыт в test_sub_headers.py.
    """
    text = base64.b64decode(resp.text).decode()
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


# ── profile-update-interval + Cache-Control ──────────────────────────────


def test_profile_update_interval_default_is_two(client, db_session, monkeypatch):
    monkeypatch.delenv("SUB_PROFILE_UPDATE_INTERVAL_H", raising=False)
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(db_session, sub, cfg, token="tok-i", uri="vless://a")

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 200
    # Дефолт снижен с захардкоженных 6ч до 2ч.
    assert resp.headers["profile-update-interval"] == "2"


def test_profile_update_interval_env_override(client, db_session, monkeypatch):
    monkeypatch.setenv("SUB_PROFILE_UPDATE_INTERVAL_H", "1")
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(db_session, sub, cfg, token="tok-i2", uri="vless://a")

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 200
    assert resp.headers["profile-update-interval"] == "1"


def test_sub_response_has_no_store_cache_control(client, db_session):
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(db_session, sub, cfg, token="tok-cc", uri="vless://a")

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 200
    # Персональный динамический конфиг не должен кэшироваться CF-Worker'ом/прокси.
    assert resp.headers["cache-control"] == "no-store, private"


# ── Retry-After на 503 ───────────────────────────────────────────────────


def test_503_no_configs_has_retry_after(client, db_session):
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(
        db_session, sub, cfg,
        token="tok-dead", uri="vless://dead",
        status=models.DeviceStatus.revoked, cred_active=False,
    )
    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "60"


def test_retry_after_env_override(client, db_session, monkeypatch):
    monkeypatch.setenv("SUB_RETRY_AFTER_SEC", "30")
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(
        db_session, sub, cfg,
        token="tok-dead2", uri="vless://dead",
        status=models.DeviceStatus.revoked, cred_active=False,
    )
    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"


# ── frozen → 503, blocked/expired → 403 ──────────────────────────────────


def test_frozen_subscription_returns_503_not_403(client, db_session):
    """frozen — временная пауза с сохранённым sub_token; 503+Retry-After
    (не 403), чтобы клиент держал last-known-good и сам восстановился."""
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(db_session, sub, cfg, token="tok-frozen", uri="vless://a")
    sub.status = models.SubscriptionStatus.frozen
    db_session.commit()

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "60"


def test_blocked_subscription_still_403(client, db_session):
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(db_session, sub, cfg, token="tok-blk", uri="vless://a")
    sub.status = models.SubscriptionStatus.blocked
    db_session.commit()

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 403


def test_expired_subscription_still_403(client, db_session):
    _node, cfg, _u, _p, sub = _setup(db_session)
    dev = _mk_device(db_session, sub, cfg, token="tok-exp", uri="vless://a")
    sub.expires_at = utcnow() - timedelta(days=1)
    db_session.commit()

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 403


# ── фильтрация кредов нездоровых нод ──────────────────────────────────────


def test_cooldown_node_credential_excluded(client, db_session):
    """Кред на ноде в cooldown выпадает из выдачи — клиент не держит мёртвый
    эндпоинт в ротации; кред на здоровой ноде остаётся."""
    healthy_node = make_node(db_session, name="healthy-node", host="198.51.100.10")
    healthy_cfg = make_config(db_session, healthy_node)
    bad_node = make_node(db_session, name="cooldown-node", host="198.51.100.11")
    bad_node.cooldown_until = utcnow() + timedelta(hours=1)
    db_session.commit()

    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = make_subscription(db_session, user, plan, healthy_node)

    # Один девайс с двумя кредами: на здоровой и на cooldown-ноде.
    dev = _mk_device(
        db_session, sub, healthy_cfg,
        token="tok-multi", uri="vless://healthy", node_id=healthy_node.id,
    )
    db_session.add(
        models.Credential(
            subscription_id=sub.id,
            device_id=dev.id,
            config_id=healthy_cfg.id,
            node_id=bad_node.id,
            proto="vless-xhttp",
            config_text="vless://cooldown",
            is_active=True,
        )
    )
    db_session.commit()

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 200
    lines = [line for line in _body(resp).splitlines() if line]
    assert lines == ["vless://healthy"]


def test_all_nodes_unhealthy_falls_back_to_full_set(client, db_session):
    """Если ЕДИНСТВЕННАЯ нода девайса в cooldown — не 503, а деградация к
    нефильтрованному набору (живой-но-неоптимальный конфиг лучше пустого)."""
    node = make_node(db_session)
    node.cooldown_until = utcnow() + timedelta(hours=1)
    cfg = make_config(db_session, node)
    db_session.commit()

    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = make_subscription(db_session, user, plan, node)
    dev = _mk_device(
        db_session, sub, cfg, token="tok-solo-bad",
        uri="vless://only", node_id=node.id,
    )

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 200
    assert _body(resp).strip() == "vless://only"


def test_filter_kill_switch_disables_node_health_check(client, db_session, monkeypatch):
    monkeypatch.setenv("SUB_FILTER_UNHEALTHY_NODES", "0")
    healthy_node = make_node(db_session, name="ks-healthy", host="198.51.100.10")
    healthy_cfg = make_config(db_session, healthy_node)
    bad_node = make_node(db_session, name="ks-cooldown", host="198.51.100.11")
    bad_node.cooldown_until = utcnow() + timedelta(hours=1)
    db_session.commit()

    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = make_subscription(db_session, user, plan, healthy_node)
    dev = _mk_device(
        db_session, sub, healthy_cfg,
        token="tok-ks", uri="vless://healthy", node_id=healthy_node.id,
    )
    db_session.add(
        models.Credential(
            subscription_id=sub.id, device_id=dev.id, config_id=healthy_cfg.id,
            node_id=bad_node.id, proto="vless-xhttp",
            config_text="vless://cooldown", is_active=True,
        )
    )
    db_session.commit()

    resp = client.get(f"/api/sub/{dev.sub_token}")
    assert resp.status_code == 200
    lines = sorted(line for line in _body(resp).splitlines() if line)
    # Kill-switch выключил фильтр — оба конфига выданы.
    assert lines == ["vless://cooldown", "vless://healthy"]


# ── логирование пустого decrypt (per-device) ─────────────────────────────


def test_per_device_empty_decrypt_is_logged(client, db_session, monkeypatch, caplog):
    """Недешифруемый кред в per-device ветке больше не выпадает молча —
    пишется warning с контекстом (симметрично legacy-ветке)."""
    import app.api_extensions as ext

    _node, cfg, _u, _p, sub = _setup(db_session)
    good = _mk_device(db_session, sub, cfg, token="tok-good", uri="vless://good")
    bad_cred = models.Credential(
        subscription_id=sub.id, device_id=good.id, config_id=cfg.id,
        node_id=cfg.node_id, proto="vless-xhttp",
        config_text="__BROKEN__", is_active=True,
    )
    db_session.add(bad_cred)
    db_session.commit()

    # Заставляем decrypt вернуть пустоту для битого шифротекста.
    real_decrypt = ext._decrypt

    def _fake(txt):
        return "" if txt == "__BROKEN__" else real_decrypt(txt)

    monkeypatch.setattr(ext, "_decrypt", _fake)

    # alembic fileConfig(disable_existing_loggers) на старте харнесса глушит
    # уже созданный логгер модуля — ре-активируем, иначе warning про пустой
    # decrypt не доходит до caplog и проверка залогированности ложно падает.
    logging.getLogger(ext.__name__).disabled = False
    with caplog.at_level(logging.WARNING, logger="app.api_extensions"):
        resp = client.get(f"/api/sub/{good.sub_token}")

    assert resp.status_code == 200
    # Рабочий протокол выдан, битый — отброшен, но с логом.
    assert _body(resp).strip() == "vless://good"
    assert any(
        "decrypt returned empty" in r.getMessage() and "vless-xhttp" in r.getMessage()
        for r in caplog.records
    )


# ── очередь пушей: приоритет + FIFO ──────────────────────────────────────


def _mk_notif(db, action: str, *, tg: str, created_at: datetime, text: str | None = None):
    extra = {"telegram_id": tg}
    if text is not None:
        extra["text"] = text
    db.add(
        models.AuditLog(
            actor="system",
            actor_type=models.AuditActor.system,
            action=action,
            target_type="user",
            target_id=None,
            extra=extra,
            created_at=created_at,
        )
    )


def test_broadcast_does_not_starve_transactional_push(client, db_session):
    """Массовая рассылка (admin_broadcast), даже более свежая, не вытесняет
    срочный config_ready из окна доставки: приоритетный класс + FIFO."""
    now = utcnow()
    # 5 свежих broadcast-строк — под старым DESC+limit они бы заняли всё окно.
    for i in range(5):
        _mk_notif(
            db_session, "admin_broadcast",
            tg=f"200{i}", created_at=now, text="рассылка",
        )
    # Один config_ready, СТАРЕЕ рассылки (создан до её тика).
    _mk_notif(
        db_session, "config_ready",
        tg="9999", created_at=now - timedelta(hours=1),
    )
    db_session.commit()

    resp = client.get("/api/notifications/pending?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    types = {n["type"] for n in data}
    # config_ready доставлен, несмотря на 5 более свежих broadcast-строк.
    assert "config_ready" in types
    # Приоритетный пуш идёт первым.
    assert data[0]["type"] == "config_ready"


def test_pending_delivers_fifo_within_priority(client, db_session):
    """Внутри приоритетного класса — FIFO: старейший срочный первым."""
    now = utcnow()
    _mk_notif(db_session, "config_ready", tg="111", created_at=now - timedelta(minutes=5))
    _mk_notif(db_session, "migration_notice", tg="222", created_at=now - timedelta(minutes=10))
    db_session.commit()

    resp = client.get("/api/notifications/pending?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    order = [n["type"] for n in data]
    # migration_notice создан раньше → доставляется первым (asc/FIFO).
    assert order.index("migration_notice") < order.index("config_ready")
