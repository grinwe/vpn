"""Регресс: миграция легаси-секретов обязана покрывать ВСЕ шифруемые поля.

Первая версия `scripts/encrypt_legacy_secrets.py` знала два поля из двенадцати:
всё, что создавалось в окне без `APP_SECRET_KEY` (токены провайдеров, рут-пароли
нод, WG-ключи exit'ов и relay-линков, reality/shadowtls-секреты в JSONB), молча
оставалось в базе открытым текстом, а отчёт при этом рапортовал «чисто».
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.security import decrypt, encrypt
from app.services.provisioning import _collect_site_extra_vars
from scripts.encrypt_legacy_secrets import (
    SPECS,
    assert_cipher_is_real,
    run_migration,
)

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_provider,
    make_subscription,
    make_user,
)

_PLAIN = "enc:v1:"


def _seed_plaintext(db) -> dict[str, tuple[type, int, str]]:
    """По одной строке на каждое поле из SPECS, значение — открытым текстом.

    Возвращает {ключ_поля: (модель, id строки, исходное значение)}.
    """
    node = make_node(db, name="secmig-node", host="10.9.0.1")
    node.provider_root_password_enc = "plain-node-root"
    node.relay_config = {
        "wg_private_key_enc": "plain-relay-wg",
        "wg_address_v4": "10.77.0.5/32",
    }
    flag_modified(node, "relay_config")

    reality = make_config(db, node, protocol=models.VPNConfigProtocol.vless_reality)
    reality.settings = {
        **(reality.settings or {}),
        "private_key_enc": "plain-reality-new",   # схема node_spawner
        "private_key": "plain-reality-legacy",    # легаси-схема
    }
    flag_modified(reality, "settings")

    stls = make_config(
        db, node, name="secmig-stls", protocol=models.VPNConfigProtocol.shadowtls_ss
    )
    stls.settings = {
        **(stls.settings or {}),
        "ss_password_enc": "plain-ss",
        "shadowtls_password_enc": "plain-stls",
    }
    flag_modified(stls, "settings")

    provider = make_provider(db, name="secmig-prov")
    provider.api_token_enc = "plain-api-token"

    exit_node = models.WGExitNode(
        name="secmig-exit",
        region="nl",
        host="203.0.113.7",
        status=models.WGExitNodeStatus.active,
        is_active=True,
        wg_public_key="exit-pub",
        wg_private_key_enc="plain-exit-priv",
        provider_root_password_enc="plain-exit-root",
    )
    db.add(exit_node)
    db.flush()

    link = models.RelayExitLink(
        relay_node_id=node.id,
        exit_id=exit_node.id,
        wg_interface_name="wg0",
        wg_client_private_key_enc="plain-link-priv",
        wg_client_public_key="link-pub",
        wg_client_address_v4="10.77.0.5/32",
    )
    db.add(link)

    cred = models.Credential(
        node_id=node.id,
        config_id=reality.id,
        proto="vless",
        config_text="vless://plain-uri",
    )
    db.add(cred)

    user = make_user(db, telegram_id="secmig-1")
    plan = make_plan(db, name="secmig-plan")
    sub = make_subscription(db, user, plan, node)
    device = models.Device(
        user_id=user.id,
        subscription_id=sub.id,
        config_id=reality.id,
        name="secmig-dev",
        status=models.DeviceStatus.active,
        connection_uri="https://sub.example/plain-token",
    )
    db.add(device)
    db.commit()

    return {
        "Credential.config_text": (models.Credential, cred.id, "vless://plain-uri"),
        "Device.connection_uri": (
            models.Device, device.id, "https://sub.example/plain-token",
        ),
        "CloudProvider.api_token_enc": (
            models.CloudProvider, provider.id, "plain-api-token",
        ),
        "VPNNode.provider_root_password_enc": (
            models.VPNNode, node.id, "plain-node-root",
        ),
        "WGExitNode.wg_private_key_enc": (
            models.WGExitNode, exit_node.id, "plain-exit-priv",
        ),
        "WGExitNode.provider_root_password_enc": (
            models.WGExitNode, exit_node.id, "plain-exit-root",
        ),
        "RelayExitLink.wg_client_private_key_enc": (
            models.RelayExitLink, link.id, "plain-link-priv",
        ),
        "VPNConfig.settings[private_key_enc]": (
            models.VPNConfig, reality.id, "plain-reality-new",
        ),
        "VPNConfig.settings[private_key]": (
            models.VPNConfig, reality.id, "plain-reality-legacy",
        ),
        "VPNConfig.settings[ss_password_enc]": (
            models.VPNConfig, stls.id, "plain-ss",
        ),
        "VPNConfig.settings[shadowtls_password_enc]": (
            models.VPNConfig, stls.id, "plain-stls",
        ),
        "VPNNode.relay_config[wg_private_key_enc]": (
            models.VPNNode, node.id, "plain-relay-wg",
        ),
    }


def _stored(db, spec, row_id: int):
    row = db.get(spec.model, row_id)
    raw = getattr(row, spec.column)
    if spec.json_key is None:
        return raw
    return (raw or {}).get(spec.json_key)


def _spec_by_key(key: str):
    return next(s for s in SPECS if s.key == key)


def test_spec_table_covers_all_enc_columns():
    """Новая `*_enc`-колонка обязана попасть в SPECS, иначе секреты в ней
    останутся плейнтекстом навсегда — и отчёт скрипта об этом не скажет."""
    from app.db import Base

    covered = {(s.model.__name__, s.column) for s in SPECS if s.json_key is None}
    missing = [
        f"{mapper.class_.__name__}.{name}"
        for mapper in Base.registry.mappers
        for name in mapper.columns.keys()
        if name.endswith("_enc") and (mapper.class_.__name__, name) not in covered
    ]
    assert not missing, f"поля не покрыты миграцией секретов: {missing}"


def test_migrates_every_field_in_spec_table(db_session):
    """Каждое поле из таблицы реально шифруется и читается назад."""
    seeded = _seed_plaintext(db_session)
    assert set(seeded) == {s.key for s in SPECS}, "тест засеял не все поля"

    reports = run_migration(db_session, apply=True)
    assert all(r.checked for r in reports)
    assert sum(r.remaining_plaintext for r in reports) == 0

    db_session.expire_all()
    for key, (_model, row_id, original) in seeded.items():
        spec = _spec_by_key(key)
        stored = _stored(db_session, spec, row_id)
        assert stored.startswith(_PLAIN), f"{key} остался открытым текстом: {stored!r}"
        assert decrypt(stored) == original, key


def test_dry_run_writes_nothing(db_session):
    """dry-run обязан только считать: оператор гоняет его на живом проде."""
    seeded = _seed_plaintext(db_session)
    reports = run_migration(db_session, apply=False)

    assert sum(r.plaintext for r in reports) == len(seeded)
    assert sum(r.written for r in reports) == 0
    db_session.expire_all()
    for key, (_model, row_id, original) in seeded.items():
        assert _stored(db_session, _spec_by_key(key), row_id) == original


def test_second_pass_is_idempotent(db_session):
    """Повторный прогон не должен шифровать уже зашифрованное (enc:v1:enc:v1:)."""
    seeded = _seed_plaintext(db_session)
    run_migration(db_session, apply=True)
    db_session.expire_all()
    after_first = {
        key: _stored(db_session, _spec_by_key(key), row_id)
        for key, (_m, row_id, _v) in seeded.items()
    }

    reports = run_migration(db_session, apply=True)
    assert sum(r.plaintext for r in reports) == 0
    assert sum(r.encrypted for r in reports) == len(seeded)
    db_session.expire_all()
    for key, (_m, row_id, _v) in seeded.items():
        assert _stored(db_session, _spec_by_key(key), row_id) == after_first[key]


def test_json_fields_survive_new_session(db_session):
    """JSONB-поля пишутся через flag_modified: без него UPDATE не уезжает и
    секрет остаётся плейнтекстом, хотя отчёт рапортует «зашифровано»."""
    seeded = _seed_plaintext(db_session)
    run_migration(db_session, apply=True)
    db_session.close()

    from app.db import SessionLocal

    fresh = SessionLocal()
    try:
        for key in (
            "VPNConfig.settings[private_key_enc]",
            "VPNConfig.settings[private_key]",
            "VPNConfig.settings[ss_password_enc]",
            "VPNConfig.settings[shadowtls_password_enc]",
            "VPNNode.relay_config[wg_private_key_enc]",
        ):
            spec = _spec_by_key(key)
            _model, row_id, original = seeded[key]
            stored = _stored(fresh, spec, row_id)
            assert stored.startswith(_PLAIN), key
            assert decrypt(stored) == original, key
    finally:
        fresh.close()


def test_refuses_to_run_when_cipher_is_noop(db_session, monkeypatch):
    """Под ALLOW_PLAINTEXT_SECRETS=1 encrypt() — no-op: round-trip сходится,
    скрипт отрапортовал бы успех, не зашифровав ничего. Должен отказываться."""
    from app import security

    seeded = _seed_plaintext(db_session)
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALLOW_PLAINTEXT_SECRETS", "1")
    security._cipher.cache_clear()
    try:
        with pytest.raises(SystemExit):
            assert_cipher_is_real()
        with pytest.raises(SystemExit):
            run_migration(db_session, apply=True)
    finally:
        security._cipher.cache_clear()

    db_session.expire_all()
    for key, (_m, row_id, original) in seeded.items():
        assert _stored(db_session, _spec_by_key(key), row_id) == original


def test_report_distinguishes_unchecked_from_clean(db_session):
    """«Поле не проверялось» и «плейнтекста нет» — разные вещи; отчёт обязан
    их различать, иначе частичный прогон читается как полная зачистка."""
    _seed_plaintext(db_session)
    only = {"Credential.config_text"}
    reports = run_migration(db_session, apply=True, only=only)

    by_key = {r.spec.key: r for r in reports}
    assert by_key["Credential.config_text"].checked
    assert by_key["Credential.config_text"].written == 1
    others = [r for k, r in by_key.items() if k not in only]
    assert others and all(not r.checked and r.reason for r in others)
    assert all(r.total == 0 and r.plaintext == 0 for r in others)


def test_non_string_json_value_is_left_alone(db_session):
    """Мусор не-строкой в JSON не должен ронять прогон и не должен писаться."""
    node = make_node(db_session, name="secmig-junk", host="10.9.0.2")
    cfg = make_config(db_session, node, protocol=models.VPNConfigProtocol.vless_reality)
    cfg.settings = {**(cfg.settings or {}), "private_key_enc": 12345}
    flag_modified(cfg, "settings")
    db_session.commit()

    reports = run_migration(db_session, apply=True)
    rep = next(r for r in reports if r.spec.key == "VPNConfig.settings[private_key_enc]")
    assert rep.checked and rep.non_string == 1 and rep.plaintext == 0
    db_session.expire_all()
    assert db_session.get(models.VPNConfig, cfg.id).settings["private_key_enc"] == 12345


def test_production_readers_see_secrets_after_migration(db_session):
    """Смысл миграции — что боевые читатели по-прежнему получают plaintext:
    reality-ключ и shadowtls-пароли уезжают в ansible extra_vars, креды — в сабу."""
    seeded = _seed_plaintext(db_session)
    run_migration(db_session, apply=True)
    db_session.expire_all()

    node = db_session.get(
        models.VPNNode, seeded["VPNNode.provider_root_password_enc"][1]
    )
    extra = _collect_site_extra_vars(db_session, node)
    assert extra["vless_reality_private_key"] == "plain-reality-new"
    assert extra["shadowtls_ss_password"] == "plain-ss"
    assert extra["shadowtls_password"] == "plain-stls"

    cred = db_session.get(models.Credential, seeded["Credential.config_text"][1])
    assert decrypt(cred.config_text) == "vless://plain-uri"
    assert encrypt("x").startswith(_PLAIN)  # шифр в тесте действительно рабочий