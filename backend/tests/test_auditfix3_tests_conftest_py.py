"""Аудит-фикс conftest, находка 198.

_clean_tables раньше делал TRUNCATE только ПОСЛЕ yield. Из-за этого сид-планы
из миграции 0007_seed_plans (INSERT в plans при upgrade) были видны только
самому первому тесту сессии, а все последующие стартовали с пустой таблицей —
скрытая зависимость от порядка прогона (потенциальный флак).

Фикс: TRUNCATE теперь выполняется и ДО, и ПОСЛЕ yield, поэтому каждый тест
гарантированно стартует с пустой БД независимо от позиции в прогоне.

Тесты ниже подтверждают инвариант «чистый старт»: если бы сид-планы (или
данные соседнего теста) протекали, эти проверки бы падали в зависимости от
порядка. Мы намеренно делаем два теста, пишущих в таблицу plans, чтобы
показать: второй тоже видит пустую таблицу на старте.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from tests.factories import make_plan


def _plan_count(db: Session) -> int:
    return db.query(models.Plan).count()


def test_starts_empty_even_though_seed_migration_inserts_plans(db_session: Session) -> None:
    # Миграция 0007_seed_plans вставляет 6 планов при upgrade. Если бы TRUNCATE
    # стоял только после yield, этот тест — окажись он первым в сессии — увидел
    # бы сид-планы. Инвариант: старт всегда пустой.
    assert _plan_count(db_session) == 0


def test_second_writer_also_starts_empty(db_session: Session) -> None:
    # Тест намеренно пишет план — чтобы проверить, что данные не протекают в
    # соседний тест (следующий стартует с TRUNCATE до yield).
    assert _plan_count(db_session) == 0
    make_plan(db_session, name="auditfix3-leak-check")
    assert _plan_count(db_session) == 1


def test_no_bleed_from_previous_writer(db_session: Session) -> None:
    # Если бы очистка была только «после» и порядок совпал, здесь мог бы
    # висеть план из предыдущего теста. Должна быть пустая таблица.
    assert _plan_count(db_session) == 0
