"""Integration tests for ``run_user_health_ping_tick`` jitter logic.

Май 2026 — переехали с fixed 24h на 7-14 дней (random per-user). Чтобы
растянуть «следующий eligible» без миграции схемы, мы пишем в
``User.health_ping_last_at`` не ``now``, а ``now + random(0, jitter)``.
Тесты проверяют именно этот forward-stretch и идемпотентность.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest import mock

from app import worker
from tests.factories import make_node, make_plan, make_subscription, make_user


def _msk_lunch_window() -> mock._patch:
    """Контекст-менеджер, который притворяется, что сейчас 12:00 MSK
    (09:00 UTC), иначе тик ранним-возвратом не запишет ничего из-за
    HEALTH_PING_HOUR_START/END.
    """
    # 12:00 MSK = 09:00 UTC. utcnow().hour == 9 → (9 + 3) % 24 == 12 — окно.
    # worker импортирует utcnow локально из .time_utils → патчим модуль-источник.
    fake_now = datetime(2026, 5, 12, 9, 30, 0)
    return mock.patch("app.time_utils.utcnow", return_value=fake_now)


def test_first_ping_stores_future_timestamp(db_session):
    """Сразу после первого пинга last_at должен быть > now (jitter сдвинут
    вперёд), причём в пределах [0, jitter_hours].
    """
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="111")
    make_subscription(db_session, user, plan, node)

    env = {
        "USER_HEALTH_PING_INTERVAL": "1800",
        "USER_HEALTH_PING_BATCH": "10",
        "USER_HEALTH_PING_DEBOUNCE_HOURS": "168",
        "USER_HEALTH_PING_DEBOUNCE_JITTER_HOURS": "168",
    }
    fake_now = datetime(2026, 5, 12, 9, 30, 0)
    with mock.patch.dict(os.environ, env), _msk_lunch_window():
        summary = worker.run_user_health_ping_tick()

    assert summary["queued"] == 1
    db_session.refresh(user)
    assert user.health_ping_last_at is not None
    delta = user.health_ping_last_at - fake_now
    assert timedelta(0) <= delta <= timedelta(hours=168)


def test_pinged_user_is_not_pinged_again_before_debounce(db_session):
    """После пинга юзер не должен попасть в выборку, пока не прошло
    минимум debounce_hours от записанного forward-stretched last_at.
    На практике — после первого тика следующий немедленно ничего не
    добавит.
    """
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="222")
    make_subscription(db_session, user, plan, node)

    env = {
        "USER_HEALTH_PING_INTERVAL": "1800",
        "USER_HEALTH_PING_BATCH": "10",
        "USER_HEALTH_PING_DEBOUNCE_HOURS": "168",
        "USER_HEALTH_PING_DEBOUNCE_JITTER_HOURS": "168",
    }
    with mock.patch.dict(os.environ, env), _msk_lunch_window():
        first = worker.run_user_health_ping_tick()
        second = worker.run_user_health_ping_tick()

    assert first["queued"] == 1
    assert second["queued"] == 0


def test_jitter_off_writes_exactly_now(db_session):
    """JITTER=0 — last_at == now (без stretch). Покрывает регрессию,
    если кто-то случайно поломает «if jitter_hours > 0 else timedelta(0)».
    """
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="333")
    make_subscription(db_session, user, plan, node)

    env = {
        "USER_HEALTH_PING_INTERVAL": "1800",
        "USER_HEALTH_PING_BATCH": "10",
        "USER_HEALTH_PING_DEBOUNCE_HOURS": "24",
        "USER_HEALTH_PING_DEBOUNCE_JITTER_HOURS": "0",
    }
    fake_now = datetime(2026, 5, 12, 9, 30, 0)
    with mock.patch.dict(os.environ, env), _msk_lunch_window():
        worker.run_user_health_ping_tick()

    db_session.refresh(user)
    assert user.health_ping_last_at == fake_now


def test_opted_out_user_not_pinged(db_session):
    """Sanity-check фильтра. Если кто-то поменяет filter-clause —
    тест поймает регрессию.
    """
    node = make_node(db_session)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="444")
    user.health_ping_opt_out = True
    db_session.commit()
    make_subscription(db_session, user, plan, node)

    env = {
        "USER_HEALTH_PING_INTERVAL": "1800",
        "USER_HEALTH_PING_BATCH": "10",
        "USER_HEALTH_PING_DEBOUNCE_HOURS": "168",
        "USER_HEALTH_PING_DEBOUNCE_JITTER_HOURS": "168",
    }
    with mock.patch.dict(os.environ, env), _msk_lunch_window():
        summary = worker.run_user_health_ping_tick()

    assert summary["queued"] == 0
    db_session.refresh(user)
    assert user.health_ping_last_at is None
