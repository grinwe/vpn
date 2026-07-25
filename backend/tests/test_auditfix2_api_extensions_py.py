"""Аудит-фикс #247: сэмплирование горячей записи ``subscription_fetch``.

``GET /api/sub/{token}`` — самый горячий read-путь (клиенты опрашивают каждые
6 ч + ручные рефреши), и раньше он писал ``AuditLog`` + ``COMMIT`` на КАЖДЫЙ
фетч, раздувая ``audit_logs`` без ретеншена. Теперь запись проходит через
``SUB_FETCH_AUDIT_SAMPLE`` (env, int; 1 = писать всегда — дефолт-noop;
N>1 → примерно 1 из N). Здесь проверяем оба края:

1. дефолт (env не задан) → строка пишется, поведение прежнее;
2. сэмплирование (N>1 и «жребий не выпал») → выдача 200 с конфигом, но
   ``subscription_fetch`` НЕ пишется (INSERT+COMMIT срезаны с горячего пути).
"""
from __future__ import annotations

import base64

from app import models

from .factories import make_config, make_node, make_plan, make_subscription, make_user


def _mk_device(db, sub, cfg, *, token, uri):
    device = models.Device(
        user_id=sub.user_id,
        subscription_id=sub.id,
        config_id=cfg.id,
        name="primary",
        status=models.DeviceStatus.active,
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
            node_id=cfg.node_id,
            proto="vless-reality",
            config_text=uri,
            is_active=True,
        )
    )
    db.commit()
    db.refresh(device)
    return device


def _setup(db, token, uri):
    node = make_node(db)
    cfg = make_config(db, node)
    user = make_user(db)
    plan = make_plan(db)
    sub = make_subscription(db, user, plan, node)
    return _mk_device(db, sub, cfg, token=token, uri=uri)


def _fetch_count(db, device_id):
    return (
        db.query(models.AuditLog)
        .filter_by(action="subscription_fetch", target_type="device", target_id=device_id)
        .count()
    )


def test_default_env_writes_subscription_fetch(client, db_session, monkeypatch):
    """Дефолт (SUB_FETCH_AUDIT_SAMPLE не задан) → пишем каждый фетч."""
    monkeypatch.delenv("SUB_FETCH_AUDIT_SAMPLE", raising=False)
    dev = _setup(db_session, token="tok-default", uri="vless://node-default")

    resp = client.get(f"/api/sub/{dev.sub_token}", headers={})
    assert resp.status_code == 200
    # URI несёт нейтральное имя эндпоинта (#«{эмодзи} V8 сервер N»,
    # 75769ca) — здесь важен сам кред, имя проверяется в test_sub_headers.
    assert base64.b64decode(resp.text).decode().strip().split("#")[0] == "vless://node-default"
    assert _fetch_count(db_session, dev.id) == 1


def test_sampling_skips_subscription_fetch(client, db_session, monkeypatch):
    """N>1 и «жребий не выпал» → 200 с конфигом, но audit-строку не пишем."""
    dev = _setup(db_session, token="tok-sampled", uri="vless://node-sampled")

    monkeypatch.setenv("SUB_FETCH_AUDIT_SAMPLE", "10")
    # randbelow != 0 → пропуск записи (детерминированно, без флаки-рандома).
    monkeypatch.setattr("app.api_extensions.secrets.randbelow", lambda n: 1)

    resp = client.get(f"/api/sub/{dev.sub_token}", headers={})
    assert resp.status_code == 200
    # Клиент всё равно получает рабочий конфиг — сэмплируется только телеметрия.
    # URI несёт нейтральное имя эндпоинта (#«{эмодзи} V8 сервер N»,
    # 75769ca) — здесь важен сам кред, имя проверяется в test_sub_headers.
    assert base64.b64decode(resp.text).decode().strip().split("#")[0] == "vless://node-sampled"
    assert _fetch_count(db_session, dev.id) == 0
