"""Аудит-фикс #222: Redis-фолбэк не должен залипать навсегда.

Раньше ``queue.get_redis`` был обёрнут в ``@lru_cache`` — первый же неуспешный
ping (типично при одновременном рестарте контейнеров, когда Redis поднимается
на пару секунд позже backend'а) кешировал ``None`` до конца жизни процесса, и
весь API навсегда переключался на inline-исполнение ansible. Проверяем, что:

* отрицательный результат больше НЕ кешируется навечно — после кулдауна
  get_redis повторяет попытку и восстанавливается, как только Redis ожил;
* внутри кулдауна повторных попыток нет (не долбим недоступный Redis);
* уход задачи в inline-фолбэк виден в prometheus-счётчике.

Тесты не требуют ни живого Redis, ни пакета ``redis``/``rq``: подключение
подменяется фейковым модулем в ``sys.modules``.
"""
from __future__ import annotations

import logging
import sys
import types

import pytest

from app import queue as q


@pytest.fixture(autouse=True)
def _reset_queue_state(monkeypatch):
    """Сбрасываем модульный кеш соединения между тестами."""
    monkeypatch.setattr(q, "_redis_client", None, raising=False)
    monkeypatch.setattr(q, "_redis_last_fail", 0.0, raising=False)
    monkeypatch.setattr(q, "_queues", {}, raising=False)
    yield
    q._redis_client = None
    q._redis_last_fail = 0.0
    q._queues = {}


def _install_fake_redis(monkeypatch, *, ping_results):
    """Подсовываем фейковый модуль ``redis`` с управляемым поведением ping().

    ``ping_results`` — список: True → ping ок, иначе исключение при вызове.
    Возвращает список созданных клиентов для ассертов.
    """
    created: list = []
    seq = iter(ping_results)

    class _FakeRedis:
        def __init__(self):
            created.append(self)

        @classmethod
        def from_url(cls, url):  # noqa: ARG002
            return cls()

        def ping(self):
            ok = next(seq)
            if ok is not True:
                raise ConnectionError("redis down")
            return True

    fake_mod = types.ModuleType("redis")
    fake_mod.Redis = _FakeRedis
    monkeypatch.setitem(sys.modules, "redis", fake_mod)
    monkeypatch.setattr(q, "_backend_enabled", lambda: True)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    return created


def test_negative_result_not_cached_forever(monkeypatch):
    # Первый ping падает, второй — ок. Кулдаун = 0, чтобы повтор был сразу.
    created = _install_fake_redis(monkeypatch, ping_results=[False, True])
    monkeypatch.setattr(q, "REDIS_RETRY_COOLDOWN", 0.0)

    # Redis лежал в момент старта → None, но НЕ закешировано навечно.
    assert q.get_redis() is None
    # Redis ожил → следующая попытка возвращает живой клиент.
    client = q.get_redis()
    assert client is not None
    assert client is created[-1]
    # Живой клиент теперь кешируется (быстрый путь без нового подключения).
    assert q.get_redis() is client


def test_cooldown_suppresses_retries(monkeypatch):
    # Только одна попытка ping доступна — второй next() кинул бы StopIteration,
    # если бы кулдаун не задушил повторную попытку.
    _install_fake_redis(monkeypatch, ping_results=[False])
    monkeypatch.setattr(q, "REDIS_RETRY_COOLDOWN", 9999.0)

    assert q.get_redis() is None
    # В пределах кулдауна повторного подключения не происходит.
    assert q.get_redis() is None


def test_enqueue_inline_fallback_counts_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(q, "get_queue", lambda: None)
    before = q.INLINE_FALLBACK_COUNTER._value.get()

    # Харнесс на старте гоняет alembic-миграции, а их fileConfig
    # (disable_existing_loggers) глушит уже созданные логгеры приложения:
    # ``app.queue`` приходит в тест с ``disabled=True``, и его записи не
    # доходят до caplog (пропадают ещё до хендлеров). Ре-активируем логгер,
    # иначе fallback-варнинг не будет виден и проверка залогированности
    # ложно упадёт, хотя код отработал верно.
    logging.getLogger(q.__name__).disabled = False

    with caplog.at_level("WARNING", logger=q.__name__):
        job_id = q.enqueue_task(1234, None)

    assert job_id is None
    assert q.INLINE_FALLBACK_COUNTER._value.get() == before + 1
    # Fallback обязан быть залогирован WARNING'ом из очереди — по нему ops
    # видит утечку ansible-ранов в API-процесс. Реальный текст:
    # "...task <id> falls back to inline execution".
    fallback_logs = [
        r
        for r in caplog.records
        if r.name == q.__name__ and r.levelno == logging.WARNING
    ]
    assert any("inline" in r.getMessage().lower() for r in fallback_logs)
