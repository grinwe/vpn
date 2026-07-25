"""Разовая миграция: дошифровать секреты, осевшие в БД открытым текстом.

Аудит 2026-07-25. До фикса `security._cipher()` при отсутствии `APP_SECRET_KEY`
молча возвращал None, и всё писалось plaintext'ом. В проде так осело:
  * `Credential.config_text` — клиентские URI (UUID/пароли внутри);
  * `VPNConfig.settings["private_key"]` — приватные ключи Reality.

Шифрование идемпотентно по префиксу `enc:v1:` — уже зашифрованные строки
пропускаются, поэтому скрипт безопасно запускать повторно.

Запуск (на web-хосте):
    docker compose exec -T backend python -m scripts.encrypt_legacy_secrets --dry-run
    docker compose exec -T backend python -m scripts.encrypt_legacy_secrets --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.db import SessionLocal
from app.security import _PREFIX, decrypt, encrypt


def _is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_PREFIX)


def migrate_credentials(session, apply: bool) -> tuple[int, int]:
    rows = session.query(models.Credential).all()
    plain = [c for c in rows if c.config_text and not _is_encrypted(c.config_text)]
    for cred in plain:
        original = cred.config_text
        blob = encrypt(original)
        # Страховка: не записываем то, что не читается обратно.
        if decrypt(blob) != original:
            raise RuntimeError(f"round-trip провален для credential {cred.id}")
        if apply:
            cred.config_text = blob
    return len(rows), len(plain)


def migrate_reality_keys(session, apply: bool) -> tuple[int, int]:
    configs = (
        session.query(models.VPNConfig)
        .filter(models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality)
        .all()
    )
    touched = 0
    for cfg in configs:
        settings = cfg.settings or {}
        pk = settings.get("private_key")
        if not pk or _is_encrypted(str(pk)):
            continue
        blob = encrypt(str(pk))
        if decrypt(blob) != str(pk):
            raise RuntimeError(f"round-trip провален для config {cfg.id}")
        touched += 1
        if apply:
            cfg.settings = {**settings, "private_key": blob}
            flag_modified(cfg, "settings")
    return len(configs), touched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    group.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()
    apply = bool(args.apply)

    session = SessionLocal()
    try:
        total_creds, plain_creds = migrate_credentials(session, apply)
        total_cfgs, plain_keys = migrate_reality_keys(session, apply)
        if apply:
            session.commit()
        else:
            session.rollback()
    finally:
        session.close()

    mode = "ЗАПИСАНО" if apply else "dry-run (ничего не изменено)"
    print(f"credentials:      всего {total_creds}, было открытым текстом {plain_creds}")
    print(f"reality-конфигов: всего {total_cfgs}, ключей открытым текстом {plain_keys}")
    print(f"режим: {mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
