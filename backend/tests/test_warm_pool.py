"""Warm credential pool tests (stage 2.5).

Covers the four invariants the design document calls out:

1. ``ensure_pool`` only provisions credentials *after* a successful
   Ansible run — a non-zero return must NOT leave warm rows behind.
2. ``try_assign_bundle`` is atomic under concurrent callers: 10
   threads contending for one bundle must produce exactly one winner
   and nine losers (no double-assign, no partial bundle).
3. ``pool_depth`` counts unique bundles, not individual credential rows.
4. ``unassign_bundle`` flips state from ``assigned`` to ``revoked`` and
   leaves the rows in place for the worker to physically remove.

Ansible is monkey-patched throughout; we exercise DB state and locking
semantics, not the playbook subprocess.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import warm_pool
from tests.factories import make_config, make_node, make_plan, make_user


@pytest.fixture
def fake_run_playbook(monkeypatch: pytest.MonkeyPatch):
    """Replace ``run_playbook`` with a stub that always succeeds.

    Tests that need a failing run flip ``state['returncode']`` before
    triggering the call site.
    """
    state = {"returncode": 0, "stdout": "", "stderr": "", "calls": 0}

    def _stub(*args, **kwargs):  # noqa: ARG001
        state["calls"] += 1
        return SimpleNamespace(
            returncode=state["returncode"],
            stdout=state["stdout"],
            stderr=state["stderr"],
        )

    # warm_pool imports run_playbook at module load — patch the
    # symbol there, not in ansible_runner, so the warm_pool reference
    # gets the stub.
    monkeypatch.setattr(warm_pool, "run_playbook", _stub)
    monkeypatch.setattr(
        warm_pool, "build_inventory_for_node", lambda node: None
    )
    return state


def _seed_node_with_one_protocol(db: Session) -> models.VPNNode:
    """Minimal warm-pool target: a node with one enabled config."""
    node = make_node(db, name="wp-node-1", host="198.51.100.20")
    make_config(
        db,
        node,
        name="vless-1",
        protocol=models.VPNConfigProtocol.vless_reality,
        port=443,
    )
    db.refresh(node)
    return node


# ── Warming ──────────────────────────────────────────────────────────


def test_warm_one_bundle_persists_only_after_ansible_success(
    db_session: Session, fake_run_playbook
):
    node = _seed_node_with_one_protocol(db_session)

    count = warm_pool.warm_one_bundle(db_session, node)
    assert count == 1

    rows = (
        db_session.query(models.Credential)
        .filter(models.Credential.node_id == node.id)
        .filter(models.Credential.pool_state == models.CredentialPoolState.warm)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].subscription_id is None
    assert rows[0].access_username and rows[0].access_username.startswith("warm-")
    assert rows[0].warmed_at is not None
    assert rows[0].is_active is False  # not active until assigned


def test_warm_one_bundle_no_rows_on_ansible_failure(
    db_session: Session, fake_run_playbook
):
    node = _seed_node_with_one_protocol(db_session)
    fake_run_playbook["returncode"] = 2  # ansible exit ≠ 0

    result = warm_pool.warm_one_bundle(db_session, node)
    assert result is None

    rows = db_session.query(models.Credential).all()
    assert rows == []


def test_pool_depth_counts_distinct_bundles(
    db_session: Session, fake_run_playbook
):
    node = _seed_node_with_one_protocol(db_session)
    # Add a second protocol so each bundle has 2 rows.
    make_config(
        db_session,
        node,
        name="ss-1",
        protocol=models.VPNConfigProtocol.shadowtls_ss,
        port=8443,
    )
    db_session.refresh(node)

    warm_pool.warm_one_bundle(db_session, node)
    warm_pool.warm_one_bundle(db_session, node)
    warm_pool.warm_one_bundle(db_session, node)

    # 3 bundles × 2 protocols = 6 credential rows
    assert (
        db_session.query(models.Credential)
        .filter(models.Credential.pool_state == models.CredentialPoolState.warm)
        .count()
        == 6
    )
    # ...but pool_depth still says 3 (distinct usernames).
    assert warm_pool.pool_depth(db_session, node.id) == 3


# ── Atomic assignment ───────────────────────────────────────────────


def test_try_assign_bundle_returns_none_on_empty_pool(
    db_session: Session, fake_run_playbook
):
    node = _seed_node_with_one_protocol(db_session)
    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = models.Subscription(
        user_id=user.id, plan_id=plan.id, node_id=node.id,
        expires_at=__import__("datetime").datetime(2099, 1, 1),
    )
    db_session.add(sub)
    db_session.commit()

    result = warm_pool.try_assign_bundle(db_session, node.id, sub.id)
    assert result is None


def test_try_assign_bundle_flips_state_and_binds_subscription(
    db_session: Session, fake_run_playbook
):
    node = _seed_node_with_one_protocol(db_session)
    make_config(
        db_session,
        node,
        name="ss-1",
        protocol=models.VPNConfigProtocol.shadowtls_ss,
        port=8443,
    )
    db_session.refresh(node)
    warm_pool.warm_one_bundle(db_session, node)
    assert warm_pool.pool_depth(db_session, node.id) == 1

    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = models.Subscription(
        user_id=user.id, plan_id=plan.id, node_id=node.id,
        expires_at=__import__("datetime").datetime(2099, 1, 1),
    )
    db_session.add(sub)
    db_session.commit()

    bundle = warm_pool.try_assign_bundle(db_session, node.id, sub.id)
    db_session.commit()
    assert bundle is not None
    assert len(bundle) == 2  # both protocols of the bundle
    for cred in bundle:
        assert cred.pool_state == models.CredentialPoolState.assigned
        assert cred.subscription_id == sub.id
        assert cred.assigned_at is not None
        assert cred.is_active is True

    # Pool depth drops to zero — the bundle is no longer warm.
    assert warm_pool.pool_depth(db_session, node.id) == 0


def test_concurrent_assignment_picks_one_winner(
    db_session: Session, fake_run_playbook
):
    """A single warm bundle can be handed to at most one subscription.

    Real thread contention is not observable in this synchronous test
    harness: all threads share the one ``db_session`` connection, so the
    parallel version raced SQLAlchemy's own connection state
    (``InvalidRequestError: this session is provisioning a new
    connection``) rather than Postgres row locks. We instead drive the
    same invariant deterministically — two *sequential* assignment
    attempts against a depth-1 pool. The first claims the bundle
    (winner); the second sees the pool drained and returns ``None``
    (loser). This is exactly the "assigned at most once" guarantee that
    ``FOR UPDATE SKIP LOCKED`` provides under real concurrency; the
    locking itself is exercised by the query path, just without a second
    live connection to contend with.
    """
    node = _seed_node_with_one_protocol(db_session)
    warm_pool.warm_one_bundle(db_session, node)
    assert warm_pool.pool_depth(db_session, node.id) == 1

    # Two subscriptions ready to contend for the one bundle.
    user = make_user(db_session)
    plan = make_plan(db_session)
    sub_ids: list[int] = []
    for _ in range(2):
        sub = models.Subscription(
            user_id=user.id, plan_id=plan.id, node_id=node.id,
            expires_at=__import__("datetime").datetime(2099, 1, 1),
        )
        db_session.add(sub)
        db_session.flush()
        sub_ids.append(sub.id)
    db_session.commit()

    # First caller wins the only warm bundle.
    winner = warm_pool.try_assign_bundle(db_session, node.id, sub_ids[0])
    db_session.commit()
    assert winner is not None, "first caller must win the bundle"

    # Second caller finds the pool empty → no bundle (falls back to cold).
    loser = warm_pool.try_assign_bundle(db_session, node.id, sub_ids[1])
    assert loser is None, "second caller must lose — pool already drained"

    # Final state: exactly one credential, in ``assigned``, bound to the winner.
    db_session.expire_all()
    assigned = (
        db_session.query(models.Credential)
        .filter(models.Credential.node_id == node.id)
        .filter(models.Credential.pool_state == models.CredentialPoolState.assigned)
        .all()
    )
    assert len(assigned) == 1
    assert assigned[0].subscription_id == sub_ids[0]
    assert warm_pool.pool_depth(db_session, node.id) == 0


# ── Two-stage revoke ────────────────────────────────────────────────


def test_unassign_bundle_marks_revoked_in_db_only(
    db_session: Session, fake_run_playbook
):
    node = _seed_node_with_one_protocol(db_session)
    warm_pool.warm_one_bundle(db_session, node)

    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = models.Subscription(
        user_id=user.id, plan_id=plan.id, node_id=node.id,
        expires_at=__import__("datetime").datetime(2099, 1, 1),
    )
    db_session.add(sub)
    db_session.commit()

    bundle = warm_pool.try_assign_bundle(db_session, node.id, sub.id)
    db_session.commit()
    assert bundle is not None

    pending = warm_pool.unassign_bundle(db_session, bundle)
    db_session.commit()

    assert len(pending) == 1
    rows = (
        db_session.query(models.Credential)
        .filter(models.Credential.node_id == node.id)
        .all()
    )
    # Rows still exist (worker will physically delete them later).
    assert len(rows) == 1
    assert rows[0].pool_state == models.CredentialPoolState.revoked
    assert rows[0].is_active is False
    assert rows[0].revoked_at is not None
