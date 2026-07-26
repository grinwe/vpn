"""Правки админки 2026-07-26: графики за период, переход «кред → юзер».

Контекст: оператор смотрит админку с телефона; графики были прибиты к 24 часам,
а ссылки из списка нод на юзера вели в пустой список.
"""
from __future__ import annotations

from datetime import timedelta

from app import models
from app.time_utils import utcnow

from .factories import make_node, make_user


def _sample(db, node, *, minutes_ago: int, users: int, up: int, down: int):
    row = models.NodeTrafficSample(
        node_id=node.id,
        observed_at=utcnow() - timedelta(minutes=minutes_ago),
        active_users=users,
        uplink_bytes=up,
        downlink_bytes=down,
    )
    db.add(row)
    return row


def test_traffic_history_accepts_month_window(client, db_session):
    """30 дней — верхняя граница retention (TRAFFIC_SAMPLE_RETENTION_DAYS=30).
    Раньше API резал на 168 часах и отдавал 422 на любой период длиннее недели,
    то есть «за месяц» показать было нечем."""
    node = make_node(db_session, name="chart-node", host="203.0.113.90")
    _sample(db_session, node, minutes_ago=10, users=3, up=100, down=200)
    db_session.commit()

    res = client.get(f"/api/nodes/{node.id}/traffic-history?hours=720")
    assert res.status_code == 200, res.text
    assert res.json()["samples"]

    # За границей retention данных всё равно нет — но и 422 быть не должно.
    assert client.get(f"/api/nodes/{node.id}/traffic-history?hours=721").status_code == 422


def test_traffic_history_downsamples_preserving_semantics(client, db_session):
    """Ключевое в агрегации: трафик — это ДЕЛЬТА за тик, поэтому по бакету он
    суммируется; active_users — мгновенный счётчик, сумма дала бы бессмыслицу
    (10 тиков по 3 юзера → «30 юзеров»), поэтому берём пик."""
    node = make_node(db_session, name="chart-node-2", host="203.0.113.91")
    # 240 сэмплов: трафик по 10 байт, юзеров ровно 3 в каждом.
    # max_points снизу ограничен 24 (меньше — график всё равно нечитаем),
    # поэтому и точек берём заведомо больше.
    for i in range(240):
        _sample(db_session, node, minutes_ago=240 - i, users=3, up=10, down=0)
    db_session.commit()

    res = client.get(
        f"/api/nodes/{node.id}/traffic-history?hours=24&max_points=24"
    )
    assert res.status_code == 200, res.text
    samples = res.json()["samples"]

    assert len(samples) <= 24, "серия не схлопнулась"
    # Трафик сохранён целиком: сумма по бакетам == сумма исходных дельт.
    assert sum(s["uplink_bytes"] for s in samples) == 2400
    # Юзеры остались пиком, а не суммой.
    assert all(s["active_users"] == 3 for s in samples), samples


def test_traffic_history_keeps_raw_series_when_small(client, db_session):
    """Короткое окно не должно агрегироваться: на 24ч точек мало, и сглаживание
    только съело бы детали."""
    node = make_node(db_session, name="chart-node-3", host="203.0.113.92")
    for i in range(5):
        _sample(db_session, node, minutes_ago=10 * i, users=i, up=1, down=1)
    db_session.commit()

    res = client.get(f"/api/nodes/{node.id}/traffic-history?hours=24")
    assert len(res.json()["samples"]) == 5


def test_users_search_finds_by_numeric_id(client, db_session):
    """Переход «кред → юзер» из списка нод ведёт на /users?telegram_id=…;
    поиск обязан находить по этому значению, иначе ссылка открывает пустой
    список (регрессия audit #165)."""
    user = make_user(db_session, telegram_id="770123")

    res = client.get("/api/users?search=770123")
    assert res.status_code == 200, res.text
    found = [u for u in res.json() if u["id"] == user.id]
    assert found, "юзер не найден поиском по telegram_id"
