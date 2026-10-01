"""Разовая миграция: дошифровать секреты, осевшие в БД открытым текстом.

Аудит 2026-07-25. До фикса `security._cipher()` при отсутствии `APP_SECRET_KEY`
молча возвращал None, и всё писалось plaintext'ом.

Первая версия скрипта дошифровывала ДВА поля (`Credential.config_text` и
легаси-ключ `VPNConfig.settings["private_key"]`), тогда как код при записи
шифрует двенадцать разных мест. Всё остальное, созданное в окне без ключа,
осталось бы в базе открытым текстом навсегда — и молча: про эти поля скрипт
просто не знал, а отчёт «плейнтекста нет» относился к двум полям из двенадцати.
Отсюда явная таблица `SPECS` (модель → поле) и отчёт, который отличает
«остаточного plaintext нет» от «поле не проверялось».

Шифруем ПРЯМО В ТОМ ЖЕ ПОЛЕ, не перекладывая в соседнее `*_enc`: читатели идут
через `decrypt()`, который на плейнтексте no-op, поэтому колонка спокойно держит
оба вида значений — это и позволяет мигрировать без окна простоя.

Идемпотентно по префиксу `enc:v1:` — уже зашифрованные значения пропускаются,
скрипт безопасно гонять повторно.

Коды возврата: 0 — все поля проверены и остаточного plaintext нет; 1 — plaintext
остался (dry-run); 2 — не все поля проверены (--only или ошибка обхода), т.е.
про часть базы скрипт НИЧЕГО не утверждает.

Запуск (на web-хосте):
    docker compose exec -T backend python -m scripts.encrypt_legacy_secrets --dry-run
    docker compose exec -T backend python -m scripts.encrypt_legacy_secrets --apply
    docker compose exec -T backend python -m scripts.encrypt_legacy_secrets \
        --dry-run --only Credential.config_text
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.db import SessionLocal
from app.security import _PREFIX, decrypt, encrypt


@dataclass(frozen=True)
class FieldSpec:
    """Одно шифруемое поле: колонка целиком либо ключ внутри JSON-колонки."""

    model: type
    column: str
    json_key: str | None = None
    writer: str = ""  # где в коде это поле шифруют — чтобы таблицу было с чем сверять

    @property
    def key(self) -> str:
        base = f"{self.model.__name__}.{self.column}"
        return base if self.json_key is None else f"{base}[{self.json_key}]"


# Таблица обязана покрывать ВСЕ места, где код зовёт encrypt() на запись:
# пропущенное поле — это секрет, который останется в базе открытым текстом
# навсегда, причём отчёт этого не покажет. Ровно так вышло с reality-ключом:
# скрипт знал только легаси-имя `private_key`, а штатный node_spawner пишет
# `private_key_enc` — ключи всех нод, заказанных через него, были невидимы.
# Сверяется тестом test_spec_table_covers_all_enc_columns.
SPECS: list[FieldSpec] = [
    FieldSpec(
        models.Credential, "config_text",
        writer="provisioning/warm_pool: config_text=encrypt(cred_text)",
    ),
    FieldSpec(
        models.Device, "connection_uri",
        writer="provisioning: connection_uri=encrypt(device_uri)",
    ),
    FieldSpec(
        models.CloudProvider, "api_token_enc",
        writer="api/cloud.py, api.py: api_token_enc=_encrypt(payload.api_token)",
    ),
    FieldSpec(
        models.VPNNode, "provider_root_password_enc",
        writer="node_spawner: node.provider_root_password_enc = encrypt(...)",
    ),
    FieldSpec(
        models.WGExitNode, "wg_private_key_enc",
        writer="api/exits.py, node_spawner: wg_private_key_enc=encrypt(priv)",
    ),
    FieldSpec(
        models.WGExitNode, "provider_root_password_enc",
        writer="node_spawner: exit_node.provider_root_password_enc = encrypt(...)",
    ),
    FieldSpec(
        models.RelayExitLink, "wg_client_private_key_enc",
        writer="api/exits.py: wg_client_private_key_enc=_encrypt(priv)",
    ),
    # ── поля внутри JSONB: пишем через flag_modified ──
    FieldSpec(
        models.VPNConfig, "settings", "private_key_enc",
        writer="node_spawner.ensure_reality_config",
    ),
    # Легаси-имя того же reality-ключа (ufo-ru-01/02/03, aeza-ru-01). Читатель
    # _collect_site_extra_vars смотрит оба имени и оба гонит через decrypt.
    FieldSpec(
        models.VPNConfig, "settings", "private_key",
        writer="легаси-схема: ключ клали в settings открытым текстом",
    ),
    FieldSpec(
        models.VPNConfig, "settings", "ss_password_enc",
        writer="node_spawner.ensure_shadowtls_config",
    ),
    FieldSpec(
        models.VPNConfig, "settings", "shadowtls_password_enc",
        writer="node_spawner.ensure_shadowtls_config",
    ),
    FieldSpec(
        models.VPNNode, "relay_config", "wg_private_key_enc",
        writer="relay.build_relay_config",
    ),
]


@dataclass
class FieldReport:
    """Итог по одному полю. `checked=False` — про поле НИЧЕГО не известно."""

    spec: FieldSpec
    checked: bool = False
    reason: str = ""       # почему поле не проверялось
    total: int = 0         # строк, где поле заполнено
    encrypted: int = 0     # из них уже с префиксом enc:v1:
    plaintext: int = 0     # из них открытым текстом
    written: int = 0       # реально зашифровано (только под --apply)
    non_string: int = 0    # не-строки в JSON: шифровать нечего

    @property
    def remaining_plaintext(self) -> int:
        return self.plaintext - self.written


_CANARY = "encrypt_legacy_secrets::canary"


def assert_cipher_is_real() -> None:
    """Отказаться работать, если шифрование выродилось в no-op.

    Под `ALLOW_PLAINTEXT_SECRETS=1` encrypt() возвращает вход КАК ЕСТЬ, decrypt()
    на нём — тоже как есть: round-trip сходится, счётчики растут, скрипт бодро
    рапортует «зашифровано N» и не шифрует НИЧЕГО. Оператор при этом уверен, что
    дыра закрыта. Поэтому шифр проверяется канарейкой до первой записи.
    """
    try:
        probe = encrypt(_CANARY)
    except RuntimeError as exc:  # APP_SECRET_KEY не задан вовсе
        raise SystemExit(f"шифрование недоступно: {exc}") from exc
    if not (probe or "").startswith(_PREFIX) or decrypt(probe) != _CANARY:
        raise SystemExit(
            "encrypt() не шифрует (ALLOW_PLAINTEXT_SECRETS=1 без APP_SECRET_KEY?) — "
            "миграция отменена: она переписала бы plaintext плейнтекстом и "
            "отрапортовала успех."
        )


def _read(row: object, spec: FieldSpec) -> object:
    raw = getattr(row, spec.column)
    if spec.json_key is None:
        return raw
    return raw.get(spec.json_key) if isinstance(raw, dict) else None


def _write(row: object, spec: FieldSpec, blob: str) -> None:
    if spec.json_key is None:
        setattr(row, spec.column, blob)
        return
    payload = dict(getattr(row, spec.column) or {})
    payload[spec.json_key] = blob
    setattr(row, spec.column, payload)
    # JSONB не обёрнут в MutableDict — без flag_modified UPDATE может не уехать,
    # и «мигрированный» секрет тихо останется в базе открытым текстом.
    flag_modified(row, spec.column)


def migrate_field(session: Session, spec: FieldSpec, *, apply: bool) -> FieldReport:
    """Обойти все строки модели и зашифровать плейнтекстовые значения поля."""
    rep = FieldReport(spec=spec)
    for row in session.query(spec.model).order_by(spec.model.id).all():
        value = _read(row, spec)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            # Не-строка в JSON: шифровать нечего, а str() исказил бы тип значения.
            rep.non_string += 1
            continue
        rep.total += 1
        if value.startswith(_PREFIX):
            rep.encrypted += 1
            continue
        rep.plaintext += 1
        blob = encrypt(value)
        # Страховка: не записываем то, что не читается обратно. Проверка префикса
        # обязательна — без неё no-op-шифр прошёл бы round-trip и «мигрировал».
        if not blob.startswith(_PREFIX) or decrypt(blob) != value:
            raise RuntimeError(f"round-trip провален: {spec.key} id={row.id}")
        if apply:
            _write(row, spec, blob)
            rep.written += 1
    rep.checked = True
    return rep


def run_migration(
    session: Session, *, apply: bool, only: set[str] | None = None
) -> list[FieldReport]:
    """Прогнать таблицу SPECS. Коммит покомандно — по одному полю за раз."""
    assert_cipher_is_real()
    reports: list[FieldReport] = []
    for spec in SPECS:
        if only is not None and spec.key not in only:
            reports.append(FieldReport(spec=spec, reason="не выбрано (--only)"))
            continue
        try:
            rep = migrate_field(session, spec, apply=apply)
            # Коммитим после каждого поля: сбой на дальнем поле не должен
            # откатывать уже дошифрованное — иначе повторный прогон начинает с нуля.
            if apply:
                session.commit()
            else:
                session.rollback()
        except SQLAlchemyError as exc:
            # Схема старше кода (колонки ещё нет), нет прав и т.п. Честно печатаем
            # «не проверено» вместо тихого нуля — иначе отчёт соврёт, что чисто.
            session.rollback()
            rep = FieldReport(
                spec=spec, reason=f"ошибка обхода: {type(exc).__name__}: {exc}"[:200]
            )
        reports.append(rep)
    return reports


def print_report(reports: list[FieldReport], *, apply: bool) -> None:
    print(f"\n{'поле':<46}{'всего':>7}{'зашифр.':>9}{'plaintext':>11}  статус")
    for rep in reports:
        if not rep.checked:
            print(
                f"{rep.spec.key:<46}{'—':>7}{'—':>9}{'—':>11}  "
                f"НЕ ПРОВЕРЕНО: {rep.reason}"
            )
            continue
        if rep.plaintext == 0:
            status = "чисто"
        elif apply:
            status = f"ЗАШИФРОВАНО {rep.written}"
        else:
            status = f"ТРЕБУЕТ ШИФРОВАНИЯ {rep.plaintext}"
        if rep.non_string:
            status += f" (+{rep.non_string} не-строк пропущено)"
        print(
            f"{rep.spec.key:<46}{rep.total:>7}{rep.encrypted:>9}"
            f"{rep.plaintext:>11}  {status}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    group.add_argument("--apply", action="store_true", help="записать изменения")
    parser.add_argument(
        "--only",
        action="append",
        metavar="Model.field",
        help="обработать только указанные поля (можно повторять); остальные "
             "в отчёте помечаются «НЕ ПРОВЕРЕНО»",
    )
    args = parser.parse_args()
    apply = bool(args.apply)

    known = {spec.key for spec in SPECS}
    only = set(args.only) if args.only else None
    unknown = sorted(only - known) if only else []
    if unknown:
        raise SystemExit(
            "неизвестные поля в --only: " + ", ".join(unknown)
            + "\nдоступные: " + ", ".join(sorted(known))
        )

    session = SessionLocal()
    try:
        reports = run_migration(session, apply=apply, only=only)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print_report(reports, apply=apply)
    unchecked = [r for r in reports if not r.checked]
    remaining = sum(r.remaining_plaintext for r in reports)
    print(f"режим: {'ЗАПИСАНО' if apply else 'dry-run (ничего не изменено)'}")
    if remaining:
        print(f"осталось plaintext-значений: {remaining} — прогони с --apply")
    if unchecked:
        print(
            f"полей НЕ проверено: {len(unchecked)} из {len(SPECS)} — это НЕ то же "
            "самое, что «плейнтекста нет»"
        )
        return 2
    if remaining:
        return 1
    print("остаточного plaintext нет ни в одном поле таблицы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
