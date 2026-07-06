"""Audit-fix wave 2 tests for the warm credential pool.

Covers three findings on ``app.services.warm_pool``:

* #73 — ``try_assign_bundle`` anchors on the per-bundle minimum id, so two
  concurrent callers always land on two *distinct* bundles and cannot
  deadlock on each other's sibling rows.
* #76 — a failed warm run (ansible rc≠0) triggers a best-effort
  ``state=absent`` compensation for the just-used username, so a partial
  identity does not linger on the node.
* #71 — ``run_warm_pool_revoke_sweep`` physically removes ``revoked``
  bundles and backs off after repeated failures.

Ansible is monkey-patched throughout; we assert DB state, call payloads
and locking behaviour, not the playbook subprocess.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import warm_pool
from tests.factories import make_config, make_node


@pytest.fixture
def recording_run_playbook(monkeypatch: pytest.MonkeyPatch):
    """Stub ``run_playbook`` that records each call's ``extra_vars``.

    ``state['returncode']`` controls the exit code; ``state['calls']``
    accumulates the ``extra_vars`` payload of every invocation so a test
    can assert whether a compensating ``state=absent`` run happened.
    """
    state = {"returncode": 0, "stderr": "", "calls": []}

    def _stub(*args, **kwargs):  # noqa: ARG001
        state["calls"].append(kwargs.get("extra_vars") or {})
        return SimpleNamespace(
            returncode=state["returncode"], stdout="", stderr=state["stderr"]
        )

    monkeypatch.setattr(warm_pool, "run_playbook", _stub)
    monkeypatch.setattr(warm_pool, "build_inventory_for_node", lambda node: None)
    return state


def _seed_node(db: Session, name: str) -> models.VPNNode:
    node = make_node(db, name=name, host="198.51.100.30")
    make_config(
        db, node, name="vless-1",
        protocol=models.VPNConfigProtocol.vless_reality, port=443,
    )
    db.refresh(node)
    return node


@pytest.fixture(autouse=True)
def _clear_revoke_attempts():
    """The sweep's back-off counter is module-global — isolate tests."""
    with warm_pool._revoke_attempts_lock:
        warm_pool._revoke_attempts.clear()
    yield
    with warm_pool._revoke_attempts_lock:
        warm_pool._revoke_attempts.clear()


# ── #76: compensation on warm failure ────────────────────────────────


def test_warm_failure_triggers_compensating_absent(
    db_session: Session, recording_run_playbook
):
    node = _seed_node(db_session, "wp2-fail")
    recording_run_playbook["returncode"] = 2  # ansible fails

    result = warm_pool.warm_one_bundle(db_session, node)
    assert result is None

    # No warm rows persisted (unchanged invariant).
    assert db_session.query(models.Credential).count() == 0

    # Two ansible calls: the failed present, then a compensating absent
    # for the *same* username.
    calls = recording_run_playbook["calls"]
    assert len(calls) == 2
    present, absent = calls
    assert present["state"] == "present"
    assert absent["state"] == "absent"
    assert absent["username"] == present["username"]


def test_warm_success_does_not_compensate(
    db_session: Session, recording_run_playbook
):
    node = _seed_node(db_session, "wp2-ok")

    result = warm_pool.warm_one_bundle(db_session, node)
    assert result == 1

    calls = recording_run_playbook["calls"]
    assert len(calls) == 1  # only the present run, no absent cleanup
    assert calls[0]["state"] == "present"


# ── #73: two concurrent assigns pick two distinct bundles ────────────


def test_two_bundles_two_assigns_distinct_winners(
    db_session: Session, recording_run_playbook
):
    """Two bundles, two sequential assigns → two distinct winners.

    Проверяет логику per-bundle-min anchor из try_assign_bundle: каждый вызов
    забирает ЦЕЛЫЙ бандл (все protocol-строки одного access_username) и
    переводит его в ``assigned``, поэтому anchor-запрос следующего вызова
    (фильтр ``pool_state == warm``) уже не видит первый бандл и обязан выбрать
    ДРУГОЙ — из пула глубины 2 два присвоения дают два РАЗНЫХ бандла.

    Изначально тест гонял два потока с отдельными сессиями, но синхронный
    тест-харнесс на одной БД не воспроизводит настоящую гонку (потоки делят
    состояние SQLAlchemy → InvalidRequestError, один поток «выигрывал» оба).
    Реальную конкуренцию за строки обеспечивает FOR UPDATE SKIP LOCKED в
    самом коде; здесь честно проверяем именно ВЫБОР разных бандлов, без
    потоков.
    """
    from tests.factories import make_plan, make_user

    node = _seed_node(db_session, "wp2-race2")
    warm_pool.warm_one_bundle(db_session, node)
    warm_pool.warm_one_bundle(db_session, node)
    assert warm_pool.pool_depth(db_session, node.id) == 2

    user = make_user(db_session)
    plan = make_plan(db_session)
    import datetime

    sub_ids: list[int] = []
    for _ in range(2):
        sub = models.Subscription(
            user_id=user.id, plan_id=plan.id, node_id=node.id,
            expires_at=datetime.datetime(2099, 1, 1),
        )
        db_session.add(sub)
        db_session.flush()
        sub_ids.append(sub.id)
    db_session.commit()

    usernames: list[str] = []
    for sub_id in sub_ids:
        bundle = warm_pool.try_assign_bundle(db_session, node.id, sub_id)
        # Пул ещё не исчерпан на этом шаге → бандл обязан найтись.
        assert bundle is not None
        db_session.commit()
        usernames.append(bundle[0].access_username)

    # Оба присвоения удались на *разных* бандлах — anchor-выбор ни разу не
    # отдал уже присвоенный бандл повторно.
    assert len(usernames) == 2
    assert len(set(usernames)) == 2
    # Пул опустел ровно на два бандла.
    assert warm_pool.pool_depth(db_session, node.id) == 0


# ── #71: physical-revoke sweep ───────────────────────────────────────


def test_revoke_sweep_removes_revoked_bundle(
    db_session: Session, recording_run_playbook
):
    node = _seed_node(db_session, "wp2-sweep")
    warm_pool.warm_one_bundle(db_session, node)
    # Move the warm bundle to revoked as invalidate_node_warm_pool does.
    warm_pool.invalidate_node_warm_pool(db_session, node.id, reason="test")
    assert (
        db_session.query(models.Credential)
        .filter(models.Credential.pool_state == models.CredentialPoolState.revoked)
        .count()
        == 1
    )

    recording_run_playbook["calls"].clear()
    summary = warm_pool.run_warm_pool_revoke_sweep(db_session)

    assert list(summary.values()) == [True]
    # Ansible was invoked with state=absent and the rows are gone.
    assert any(c.get("state") == "absent" for c in recording_run_playbook["calls"])
    assert db_session.query(models.Credential).count() == 0


def test_revoke_sweep_backs_off_after_max_attempts(
    db_session: Session, recording_run_playbook
):
    node = _seed_node(db_session, "wp2-sweep-fail")
    warm_pool.warm_one_bundle(db_session, node)
    warm_pool.invalidate_node_warm_pool(db_session, node.id, reason="test")

    recording_run_playbook["returncode"] = 2  # physical revoke keeps failing

    # Run the sweep MAX times — each fails, the bundle stays revoked.
    for _ in range(warm_pool.WARM_POOL_REVOKE_MAX_ATTEMPTS):
        summary = warm_pool.run_warm_pool_revoke_sweep(db_session)
        assert list(summary.values()) == [False]

    # Rows still present (nothing deleted on failure).
    assert (
        db_session.query(models.Credential)
        .filter(models.Credential.pool_state == models.CredentialPoolState.revoked)
        .count()
        == 1
    )

    # Next tick: bundle is over the attempt cap → skipped, no ansible call.
    recording_run_playbook["calls"].clear()
    summary = warm_pool.run_warm_pool_revoke_sweep(db_session)
    assert summary == {}
    assert recording_run_playbook["calls"] == []
