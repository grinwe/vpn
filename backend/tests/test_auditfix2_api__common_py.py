"""Аудит-фиксы #41/#42 для ``app.api._common``.

#41 — ``_audit(..., commit=False)`` только стейджит строку в сессию, не
      коммитит; ``_get_or_create_user`` пишет User и его audit-строку
      одной транзакцией (нет окна «мутация есть, следа нет»).
#42 — гонка в ``_get_or_create_user``: параллельное создание юзера с тем
      же ``telegram_id`` не должно валиться необработанным 500 —
      IntegrityError ловится, откат + перечитывание существующей строки.
"""
from __future__ import annotations

from app import models
from app.api._common import _audit, _get_or_create_user
from app.db import SessionLocal


def test_audit_commit_false_stages_without_commit(db_session):
    """commit=False добавляет строку в сессию, но не фиксирует её."""
    log = _audit(
        db_session,
        "actor-x",
        "test_action",
        "user",
        None,
        commit=False,
    )
    # В нашей сессии объект виден (identity map), но во второй сессии до
    # commit ничего нет — значит транзакция ещё открыта.
    other = SessionLocal()
    try:
        assert (
            other.query(models.AuditLog)
            .filter_by(action="test_action")
            .count()
            == 0
        )
    finally:
        other.close()

    db_session.commit()
    assert log.id is not None
    assert (
        db_session.query(models.AuditLog).filter_by(action="test_action").count()
        == 1
    )


def test_audit_commit_true_is_default(db_session):
    """Дефолт (commit=True) коммитит сразу — обратная совместимость."""
    _audit(db_session, "actor-y", "committed_action", "user", None)
    other = SessionLocal()
    try:
        assert (
            other.query(models.AuditLog)
            .filter_by(action="committed_action")
            .count()
            == 1
        )
    finally:
        other.close()


def test_get_or_create_user_is_atomic_with_audit(db_session):
    """Создание юзера и его audit-строки — одна транзакция."""
    user = _get_or_create_user(db_session, telegram_id="tg-atomic-1")
    assert user.id is not None
    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="user_created", target_id=user.id)
        .one()
    )
    assert audit.actor == "tg-atomic-1"
    assert audit.target_type == "user"


def test_get_or_create_user_returns_existing(db_session):
    """Повторный вызов возвращает того же юзера, второй раз не пишет аудит."""
    first = _get_or_create_user(db_session, telegram_id="tg-dup-1")
    second = _get_or_create_user(db_session, telegram_id="tg-dup-1")
    assert first.id == second.id
    assert (
        db_session.query(models.AuditLog)
        .filter_by(action="user_created", target_id=first.id)
        .count()
        == 1
    )


def test_get_or_create_user_race_returns_existing(db_session):
    """Гонка: другой запрос успел закоммитить того же юзера первым.

    Юзер уже есть в БД (закоммичен независимой сессией), поэтому наш SELECT
    находит его сразу — двойной тап «купить», где первый запрос уже победил.
    Никакого 500, возвращается существующая строка.
    """
    other = SessionLocal()
    try:
        racer = models.User(telegram_id="tg-race-1")
        other.add(racer)
        other.commit()
        racer_id = racer.id
    finally:
        other.close()

    user = _get_or_create_user(db_session, telegram_id="tg-race-1")
    assert user.telegram_id == "tg-race-1"
    assert user.id == racer_id


def test_get_or_create_user_race_integrity_error_branch(db_session, monkeypatch):
    """Гонка на самой вставке: SELECT пуст, но INSERT ловит unique-конфликт.

    Детерминированно эмулируем окно между SELECT и flush: заставляем первый
    ``.first()`` вернуть None (юзера якобы нет), при этом строка с тем же
    ``telegram_id`` уже закоммичена другой сессией. flush → IntegrityError,
    ветка except делает rollback + перечитывание и возвращает чужую строку
    вместо необработанного 500.
    """
    from sqlalchemy.orm import Query

    other = SessionLocal()
    try:
        racer = models.User(telegram_id="tg-race-2")
        other.add(racer)
        other.commit()
        racer_id = racer.id
    finally:
        other.close()

    real_first = Query.first
    state = {"force_none": True}

    def patched_first(self):
        # Гасим только самый первый SELECT внутри вызова — имитируем, что на
        # момент проверки строки ещё не было; повторный SELECT в except идёт
        # по-настоящему.
        if state["force_none"]:
            state["force_none"] = False
            return None
        return real_first(self)

    monkeypatch.setattr(Query, "first", patched_first)

    user = _get_or_create_user(db_session, telegram_id="tg-race-2")
    assert user.id == racer_id
    assert user.telegram_id == "tg-race-2"
