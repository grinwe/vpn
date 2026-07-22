"""Харднинг под hy2-раскатку на флот (2026-07, после hy2-канарейки на *.wgse).

Канарейка вскрыла два структурных препятствия к раскатке hy2 на флот:

  * Фикс #2 (queue): застрявшая ``pending`` provisioning-таска висит вечно.
    ``enqueue_task`` дедупит по детерминированному ``provision-{id}``; если в
    RQ остался ЗОМБИ-``started`` джоб (воркер SIGKILL'нут мид-ран, а
    ``StartedJobRegistry.cleanup`` его не реклеймит), ре-энкью возвращает id
    зомби и НИЧЕГО не диспатчит — ``run_pending_rescue_tick`` «спасает» её
    каждую минуту вхолостую. Bootstrap-таска при этом держит
    ``uq_active_node_bootstrap`` и вешает реконсилер ноды. Проверяем:
      - ``enqueue_task(force=True)`` пуржит job-ключ ПЕРЕД дедупом;
      - pending-rescue эскалирует: >FORCE_AGE → force-редиспатч,
        >ABANDON_AGE → mark failed (без опасного ре-рана древних тасок).

Фикс #1 (hy2 config.yaml.j2 sentinel-userpass) — чисто ansible-шаблон, тут не
покрывается (нет j2-рендер-харнесса); проверяется ansible-lint + смоуком.
"""
from __future__ import annotations

import sys
import types
from datetime import timedelta

import pytest

from app import models
from app import queue as q
from app import worker
from app.time_utils import utcnow


# ─────────────────────────── enqueue_task(force=…) ───────────────────────────

class _FakeConn:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class _FakeJob:
    id = "provision-777"


class _FakeQueue:
    def __init__(self) -> None:
        self.connection = _FakeConn()
        self.enqueued: list[tuple] = []

    def enqueue(self, func, *args, **kwargs):  # noqa: ANN001
        self.enqueued.append((func, args, kwargs))
        return _FakeJob()


def _install_fake_rq(monkeypatch, *, fetch_status: str | None):
    """Фейковые rq-подмодули для импортов внутри enqueue_task.

    ``fetch_status`` — что вернёт ``Job.fetch(...).get_status()``:
      None → NoSuchJobError (джоба отсутствует);
      строка ("started"/"queued"/...) → живой/зомби-джоб в этом статусе.
    Консистентность с force: если ключ ``rq:job:<id>`` уже удалён из
    connection (force-пурж), fetch кидает NoSuchJobError — как реальный RQ.
    """
    class NoSuchJobError(Exception):
        pass

    class _ExistingJob:
        id = "provision-777"

        def get_status(self, refresh=False):  # noqa: ARG002
            return fetch_status

        def delete(self):
            pass

    class _Job:
        @staticmethod
        def fetch(job_id, connection=None):
            if connection is not None and f"rq:job:{job_id}" in getattr(
                connection, "deleted", ()
            ):
                raise NoSuchJobError()
            if fetch_status is None:
                raise NoSuchJobError()
            return _ExistingJob()

    class _Retry:
        def __init__(self, *a, **k):
            pass

    class _StartedJobRegistry:
        def __init__(self, *a, **k):
            pass

        def cleanup(self):
            pass

    rq = types.ModuleType("rq")
    rq.Retry = _Retry
    rq_job = types.ModuleType("rq.job")
    rq_job.Job = _Job
    rq_exc = types.ModuleType("rq.exceptions")
    rq_exc.NoSuchJobError = NoSuchJobError
    rq_reg = types.ModuleType("rq.registry")
    rq_reg.StartedJobRegistry = _StartedJobRegistry
    monkeypatch.setitem(sys.modules, "rq", rq)
    monkeypatch.setitem(sys.modules, "rq.job", rq_job)
    monkeypatch.setitem(sys.modules, "rq.exceptions", rq_exc)
    monkeypatch.setitem(sys.modules, "rq.registry", rq_reg)


def test_force_purges_job_key_before_dedup(monkeypatch):
    """force=True с ЖИВЫМ-выглядящим (зомби) started-джобом всё равно
    пуржит ключ и кладёт свежую джобу — дедуп его не проглатывает."""
    fake_q = _FakeQueue()
    monkeypatch.setattr(q, "get_queue", lambda: fake_q)
    # Зомби: Job.fetch вернёт started (без force дедуп вернул бы его id).
    _install_fake_rq(monkeypatch, fetch_status="started")

    job_id = q.enqueue_task(777, None, force=True)

    assert "rq:job:provision-777" in fake_q.connection.deleted
    assert len(fake_q.enqueued) == 1  # свежий enqueue состоялся
    assert job_id == "provision-777"


def test_no_force_dedups_against_zombie(monkeypatch):
    """Без force зомби-started джоб проглатывает ре-энкью (документируем
    ровно тот баг, ради которого добавлен force): ключ НЕ пуржится и
    свежего enqueue нет."""
    fake_q = _FakeQueue()
    monkeypatch.setattr(q, "get_queue", lambda: fake_q)
    _install_fake_rq(monkeypatch, fetch_status="started")

    q.enqueue_task(777, None, force=False)

    assert fake_q.connection.deleted == []
    assert fake_q.enqueued == []  # дедуп — свежего enqueue нет


def test_force_absent_job_still_enqueues(monkeypatch):
    """force при отсутствующей джобе (NoSuchJobError) — просто свежий
    enqueue, лишний delete безвреден."""
    fake_q = _FakeQueue()
    monkeypatch.setattr(q, "get_queue", lambda: fake_q)
    _install_fake_rq(monkeypatch, fetch_status=None)

    job_id = q.enqueue_task(777, None, force=True)

    assert "rq:job:provision-777" in fake_q.connection.deleted
    assert len(fake_q.enqueued) == 1
    assert job_id == "provision-777"


# ────────────────────────── pending-rescue тиры ──────────────────────────

class _FakeTask:
    def __init__(self, task_id: int, created_at) -> None:
        self.id = task_id
        self.created_at = created_at
        self.status = models.ProvisioningTaskStatus.pending
        self.finished_at = None
        self.error_message = None


class _FakeQuery:
    def __init__(self, tasks):
        self._tasks = tasks

    def filter(self, *a, **k):  # noqa: ARG002
        return self

    def all(self):
        return self._tasks


class _FakeSession:
    def __init__(self, tasks):
        self._tasks = tasks
        self.committed = False

    def query(self, *a, **k):  # noqa: ARG002
        return _FakeQuery(self._tasks)

    def commit(self):
        self.committed = True

    def close(self):
        pass


@pytest.fixture
def _rescue_env(monkeypatch):
    """Тик не перепланирует себя, тир-пороги фиксированы, enqueue пишет вызовы."""
    monkeypatch.setattr(q, "schedule_tick", lambda *a, **k: None)
    monkeypatch.setenv("PENDING_RESCUE_AGE", "60")
    monkeypatch.setenv("PENDING_RESCUE_FORCE_AGE", "1800")
    monkeypatch.setenv("PENDING_RESCUE_ABANDON_AGE", "86400")
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        q, "enqueue_task",
        lambda task_id, node_id, *, force=False: (
            calls.append((task_id, force)) or f"provision-{task_id}"
        ),
    )
    return calls


def test_pending_rescue_tiers(monkeypatch, _rescue_env):
    """Три pending-таски разного возраста → нормальный / force / abandon."""
    now = utcnow()
    normal = _FakeTask(1, now - timedelta(seconds=120))     # >AGE, <FORCE
    forced = _FakeTask(2, now - timedelta(seconds=3600))    # >FORCE, <ABANDON
    ancient = _FakeTask(3, now - timedelta(days=3))         # >ABANDON
    session = _FakeSession([normal, forced, ancient])
    monkeypatch.setattr("app.db.SessionLocal", lambda: session)

    out = worker.run_pending_rescue_tick()

    calls = dict(_rescue_env)  # {task_id: force}
    # normal → enqueue force=False
    assert calls[1] is False
    # forced → enqueue force=True (пуржит зомби-джоб)
    assert calls[2] is True
    # ancient → НЕ энкьюился, помечен failed терминально
    assert 3 not in calls
    assert ancient.status == models.ProvisioningTaskStatus.failed
    assert ancient.finished_at is not None
    assert "abandoned" in (ancient.error_message or "")
    assert session.committed is True  # abandon → коммит
    assert out == {"scanned": 3, "rescued": 2, "abandoned": 1}
