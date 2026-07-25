"""Регрессы на последствия миграции легаси-секретов (223dd71).

Миграция зашифровала осевший в БД plaintext ПРЯМО в тех же полях, поэтому
в проде теперь сосуществуют зашифрованные и плейнтекстовые значения, а
`decrypt()` получил достижимую ветку «вернуть None» (ротация APP_SECRET_KEY,
режим ALLOW_PLAINTEXT_SECRETS против зашифрованной БД, битый блоб). Тесты
ниже фиксируют, что код это переживает.
"""
from __future__ import annotations

from app import models
from app.api.admin_claim import ORPHAN_OWNER_ID
from app.security import encrypt
from app.services.provisioning import (
    _extract_hy2_auth,
    _extract_vless_uuid,
)

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)

UUID = "3f2b8c1e-0000-4000-8000-abcdefabcdef"

# Валидный enc:v1:-префикс с мусорным телом: decrypt() ловит InvalidToken и
# ВОЗВРАЩАЕТ None (а не бросает) — именно этот вход валил re.match TypeError'ом.
BROKEN_BLOB = "enc:v1:gAAAAABmZZZZzzzzNOT-A-REAL-FERNET-TOKEN"


def test_extract_vless_uuid_survives_undecryptable_blob():
    assert _extract_vless_uuid(BROKEN_BLOB, cred_id=1) is None


def test_extract_hy2_auth_survives_undecryptable_blob():
    assert _extract_hy2_auth(BROKEN_BLOB, cred_id=1) is None


def test_extract_vless_uuid_still_reads_good_blob():
    blob = encrypt(f"vless://{UUID}@1.2.3.4:443?security=reality")
    assert _extract_vless_uuid(blob, cred_id=1) == UUID


def test_extract_hy2_auth_still_reads_good_blob():
    blob = encrypt("hy2://user-1-2:s3cr3t@1.2.3.4:443?obfs=salamander")
    assert _extract_hy2_auth(blob, cred_id=1) == "user-1-2:s3cr3t"


def test_extract_reads_legacy_plaintext_row():
    """DR-восстановление и легаси-строки кладут URI без enc:v1: — decrypt на
    них no-op, парсеры обязаны продолжать работать."""
    assert _extract_vless_uuid(f"vless://{UUID}@1.2.3.4:443", cred_id=1) == UUID
    assert _extract_hy2_auth("hy2://u:p@1.2.3.4:443", cred_id=1) == "u:p"


# ── claim-orphan: поиск кредa по UUID ───────────────────────────────────────
# Эндпоинт искал `config_text ILIKE '%uuid%'`, а миграция превратила колонку в
# шифртекст → 404 ровно той популяции сирот, ради которой он написан.


def _orphan_owner(db):
    """Placeholder-юзер DR 2026-05-19 (id зафиксирован в коде эндпоинта)."""
    user = db.get(models.User, ORPHAN_OWNER_ID)
    if user is None:
        user = models.User(id=ORPHAN_OWNER_ID, telegram_id="__recovery_orphans__")
        db.add(user)
        db.commit()
    return user


def _orphan_setup(db, *, config_text: str, bind_subscription: bool = True):
    """Сирота: подписка на placeholder-юзере + один кред с нужным config_text."""
    node = make_node(db, name="claim-node", host="203.0.113.7")
    cfg = make_config(db, node)
    plan = make_plan(db)
    owner = _orphan_owner(db)
    sub = make_subscription_with_device(db, owner, plan, node)
    device = sub.devices[0]
    cred = models.Credential(
        subscription_id=sub.id if bind_subscription else None,
        device_id=device.id if bind_subscription else None,
        config_id=cfg.id,
        node_id=node.id,
        proto="vless-reality",
        config_text=config_text,
        access_username=device.access_username,
        pool_state=(
            models.CredentialPoolState.assigned
            if bind_subscription
            else models.CredentialPoolState.warm
        ),
    )
    db.add(cred)
    db.commit()
    return sub, cred


def _claim(client, db, uuid_value: str, *, telegram_id: str = "claimer"):
    target = make_user(db, telegram_id=telegram_id)
    return target, client.post(
        "/api/admin/claim-orphan",
        json={"telegram_id": telegram_id, "uuid": uuid_value},
    )


def test_claim_orphan_finds_encrypted_credential(client, db_session):
    sub, _ = _orphan_setup(
        db_session, config_text=encrypt(f"vless://{UUID}@203.0.113.7:443?x=1")
    )
    target, resp = _claim(client, db_session, UUID)
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_id"] == sub.id
    assert resp.json()["new_user_id"] == target.id


def test_claim_orphan_finds_legacy_plaintext_credential(client, db_session):
    """DR-восстановление насыпает config_text открытым текстом — обе формы
    обязаны находиться одной веткой."""
    sub, _ = _orphan_setup(
        db_session, config_text=f"vless://{UUID}@203.0.113.7:443?x=1"
    )
    _, resp = _claim(client, db_session, UUID)
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_id"] == sub.id


def test_claim_orphan_matches_uuid_case_insensitively(client, db_session):
    """Прежний SQL был ILIKE — регистро-независимость сохраняем явно."""
    _orphan_setup(db_session, config_text=encrypt(f"vless://{UUID}@1.2.3.4:443"))
    _, resp = _claim(client, db_session, f"vless://{UUID.upper()}@1.2.3.4:443")
    assert resp.status_code == 200, resp.text


def test_claim_orphan_matches_dr_placeholder_credential(client, db_session):
    """У не-VLESS сирот DR пишет `placeholder:warm-recovery:<uuid>` — фильтр по
    proto вернул бы тот же молчаливый 404."""
    _orphan_setup(
        db_session, config_text=encrypt(f"placeholder:warm-recovery:{UUID}")
    )
    _, resp = _claim(client, db_session, UUID)
    assert resp.status_code == 200, resp.text


def test_claim_orphan_ignores_warm_pool_credential(client, db_session):
    """Warm-кред (subscription_id IS NULL) не является claim'абельным — и не
    должен давать ложный 409."""
    _orphan_setup(
        db_session,
        config_text=encrypt(f"vless://{UUID}@1.2.3.4:443"),
        bind_subscription=False,
    )
    _, resp = _claim(client, db_session, UUID)
    assert resp.status_code == 404, resp.text


def test_claim_orphan_reports_non_orphan_owner_instead_of_404(client, db_session):
    """Скан идёт по всем привязанным кредам, чтобы не-сирота давал внятный 409
    с текущим владельцем, а не 404."""
    node = make_node(db_session, name="live-node", host="203.0.113.8")
    cfg = make_config(db_session, node)
    plan = make_plan(db_session)
    owner = make_user(db_session, telegram_id="real-owner")
    sub = make_subscription_with_device(db_session, owner, plan, node)
    db_session.add(
        models.Credential(
            subscription_id=sub.id,
            device_id=sub.devices[0].id,
            config_id=cfg.id,
            node_id=node.id,
            proto="vless-reality",
            config_text=encrypt(f"vless://{UUID}@203.0.113.8:443"),
            access_username=sub.devices[0].access_username,
        )
    )
    db_session.commit()
    _, resp = _claim(client, db_session, UUID)
    assert resp.status_code == 409, resp.text
    assert "not an orphan" in resp.json()["detail"]


def test_claim_orphan_404_hints_at_undecryptable_credentials(client, db_session):
    """Рассинхрон APP_SECRET_KEY не должен выглядеть как «такого UUID нет»."""
    _orphan_setup(db_session, config_text=BROKEN_BLOB)
    _, resp = _claim(client, db_session, UUID)
    assert resp.status_code == 404, resp.text
    assert "APP_SECRET_KEY" in resp.json()["detail"]


# ── extra_vars: нерасшифрованный секрет не должен уезжать в ansible ─────────


def test_extra_vars_raises_named_error_on_undecryptable_reality_key(db_session):
    """Раньше None уезжал в ansible как null, и роль падала на
    `NoneType has no len()` — по такой ошибке причину не найти."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.provisioning import (
        SecretDecryptError,
        _collect_site_extra_vars,
    )

    node = make_node(db_session, name="broken-key-node", host="203.0.113.9")
    cfg = make_config(db_session, node)
    settings = dict(cfg.settings or {})
    settings["private_key_enc"] = BROKEN_BLOB
    cfg.settings = settings
    cfg.public_key = "pub"
    flag_modified(cfg, "settings")
    db_session.commit()

    try:
        _collect_site_extra_vars(db_session, node)
    except SecretDecryptError as exc:
        assert "broken-key-node" in str(exc)
        assert "APP_SECRET_KEY" in str(exc)
    else:
        raise AssertionError("ожидали SecretDecryptError")


def test_validate_extra_vars_rejects_none_value():
    """Defence-in-depth: любая будущая ветка с None ловится до ansible."""
    import pytest

    from app.services.provisioning import _validate_extra_vars

    with pytest.raises(ValueError, match="is None"):
        _validate_extra_vars({"some_secret": None}, node_hint="node-x")


# ── DR-восстановление: restore.sql не должен возвращать plaintext в БД ──────
# generate_restore_sql.py писал config_text и reality private_key открытым
# текстом в поля, которые весь живой код пишет только через encrypt().


def _load_restore_generator():
    """Скрипт лежит ВНЕ backend/, а тест-образ монтирует только backend/ —
    поэтому импорт по пути со скипом, а не обычный import."""
    import importlib.util
    from pathlib import Path

    import pytest

    path = (
        Path(__file__).resolve().parents[2]
        / "infra" / "ansible" / "scripts" / "generate_restore_sql.py"
    )
    if not path.is_file():
        pytest.skip("generate_restore_sql.py недоступен (backend-only тест-образ)")
    spec = importlib.util.spec_from_file_location("dr_generate_restore_sql", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:  # нет PyYAML/cryptography в образе
        pytest.skip("generate_restore_sql.py не импортируется в этом окружении")
    return module


# Валидный X25519-приватник в base64url без padding — иначе derive_reality_public_key
# не выведет public и нода не соберётся.
_REALITY_PRIV = "aB3dEfGhIjKlMnOpQrStUvWxYz0123456789_-ABC"
_DEV_UUID = "11111111-2222-4333-8444-555555555555"


def _write_restore_inputs(tmp_path):
    import json

    (tmp_path / "users.json").write_text(json.dumps([
        {
            "user_id": 1,
            "subscription_id": 1,
            "node": "n1",
            "devices": [{"uuid": _DEV_UUID, "email": "user-1-1"}],
        }
    ]))
    (tmp_path / "nodes.json").write_text(json.dumps([
        {
            "name": "n1",
            "configs": [
                {
                    "tag": "vless-reality",
                    "protocol": "vless-reality",
                    "port": 9443,
                    "sni": "www.microsoft.com",
                    "short_ids": ["deadbeefdeadbeef"],
                    "private_key": _REALITY_PRIV,
                }
            ],
        }
    ]))
    (tmp_path / "hosts.yml").write_text(
        "all:\n  children:\n    vpn_nodes:\n      hosts:\n"
        "        n1:\n          ansible_host: 203.0.113.10\n          location: ru\n"
    )
    return tmp_path


def _run_generator(module, tmp_path, *extra_argv):
    inputs = _write_restore_inputs(tmp_path)
    out = tmp_path / "restore.sql"
    rc = module.main([
        "--users-json", str(inputs / "users.json"),
        "--nodes-json", str(inputs / "nodes.json"),
        "--inventory", str(inputs / "hosts.yml"),
        "--output", str(out),
        "--default-plan-id", "1",
        *extra_argv,
    ])
    return rc, out


def test_restore_sql_encrypts_credential_config_text(tmp_path):
    module = _load_restore_generator()
    rc, out = _run_generator(module, tmp_path, "--app-secret-key", "k")
    assert rc == 0
    sql = out.read_text()
    assert "vless://" not in sql, "клиентский URI уехал в restore.sql открытым текстом"
    assert _DEV_UUID not in sql, "UUID клиента виден в restore.sql"
    assert "enc:v1:" in sql


def test_restore_sql_ciphertext_is_readable_by_backend_decrypt(tmp_path, monkeypatch):
    """Схема шифрования в скрипте продублирована руками — сторожим паритет с
    backend'ом, иначе restore.sql молча положит нечитаемые креды."""
    import os
    import re

    from app.security import decrypt

    module = _load_restore_generator()
    rc, out = _run_generator(
        module, tmp_path, "--app-secret-key", os.environ["APP_SECRET_KEY"]
    )
    assert rc == 0
    blobs = re.findall(r"'(enc:v1:[^']+)'", out.read_text())
    assert blobs, "в restore.sql нет ни одного зашифрованного значения"
    assert any((decrypt(b) or "").startswith("vless://") for b in blobs)


def test_restore_sql_encrypts_reality_private_key(tmp_path):
    module = _load_restore_generator()
    rc, out = _run_generator(module, tmp_path, "--app-secret-key", "k")
    assert rc == 0
    sql = out.read_text()
    assert _REALITY_PRIV not in sql, "reality private_key лежит в restore.sql plaintext"
    assert "private_key_enc" in sql


def test_restore_sql_refuses_to_run_without_app_secret_key(tmp_path, monkeypatch, capsys):
    """Отказ обязан случиться ДО чтения входных файлов — поэтому users.json
    здесь намеренно не существует."""
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    module = _load_restore_generator()
    out = tmp_path / "restore.sql"
    rc = module.main([
        "--users-json", str(tmp_path / "does-not-exist.json"),
        "--nodes-json", str(tmp_path / "does-not-exist.json"),
        "--inventory", str(tmp_path / "does-not-exist.yml"),
        "--output", str(out),
        "--default-plan-id", "1",
    ])
    assert rc == 3
    assert not out.exists()
    assert "APP_SECRET_KEY" in capsys.readouterr().err


def test_restore_sql_reads_app_secret_key_from_env(tmp_path, monkeypatch):
    """В DR ключ берут из .env web-хоста; argv светится в ps и history."""
    monkeypatch.setenv("APP_SECRET_KEY", "env-key")
    module = _load_restore_generator()
    rc, out = _run_generator(module, tmp_path)
    assert rc == 0
    assert "enc:v1:" in out.read_text()


def test_restore_sql_plaintext_escape_hatch_is_explicit(tmp_path, monkeypatch):
    """Ключ утерян безвозвратно — путь есть, но только явный и с меткой в файле."""
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    module = _load_restore_generator()
    rc, out = _run_generator(module, tmp_path, "--allow-plaintext-secrets")
    assert rc == 0
    sql = out.read_text()
    assert "vless://" in sql
    assert "--allow-plaintext-secrets" in sql, "в файле нет метки о плейнтексте"


def test_restore_sql_copies_are_byte_identical():
    """Скрипт лежит в двух местах — расхождение копий уже кусало (DR-путь
    берут по первому попавшемуся пути)."""
    from pathlib import Path

    import pytest

    root = Path(__file__).resolve().parents[2]
    a = root / "infra" / "ansible" / "scripts" / "generate_restore_sql.py"
    b = root / "scripts" / "generate_restore_sql.py"
    if not (a.is_file() and b.is_file()):
        pytest.skip("скрипты недоступны (backend-only тест-образ)")
    assert a.read_bytes() == b.read_bytes()
