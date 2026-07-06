"""Model-level invariants from audit wave 2 (findings 129/130/131/134).

Pure ``Base.metadata`` inspection — no live DB needed. Guards against
someone silently dropping the FK indexes, loosening the sub-link RESTRICT
guard, re-nulling status/flag columns, or reintroducing the
node_traffic_samples index drift.
"""
from __future__ import annotations

from app import models  # noqa: F401 — register models on Base.metadata
from app.db import Base


def _table(name: str):
    return Base.metadata.tables[name]


def _index_names(table_name: str) -> set[str]:
    return {ix.name for ix in _table(table_name).indexes}


def _col(table_name: str, col: str):
    return _table(table_name).c[col]


# ── #134: node_traffic_samples index drift ───────────────────────────
def test_node_traffic_samples_composite_index_declared() -> None:
    names = _index_names("node_traffic_samples")
    assert "ix_node_traffic_samples_node_observed" in names
    ix = next(
        i
        for i in _table("node_traffic_samples").indexes
        if i.name == "ix_node_traffic_samples_node_observed"
    )
    assert [c.name for c in ix.columns] == ["node_id", "observed_at"]


def test_node_traffic_samples_no_standalone_node_id_index() -> None:
    # Composite covers node_id as a prefix; the standalone index=True was
    # dropped because migration 0019 never created it (drift source).
    assert "ix_node_traffic_samples_node_id" not in _index_names(
        "node_traffic_samples"
    )


# ── #129: FK indexes ─────────────────────────────────────────────────
def test_hot_fk_columns_are_indexed() -> None:
    expected = {
        "subscriptions": ["user_id", "plan_id", "node_id"],
        "devices": ["subscription_id", "config_id"],
        "credentials": ["subscription_id", "device_id", "config_id"],
        "payments": ["subscription_id"],
        "invoices": ["user_id", "subscription_id"],
        "vpn_configs": ["node_id"],
    }
    for table, cols in expected.items():
        for col in cols:
            assert _col(table, col).index is True, f"{table}.{col} not indexed"


def test_subscriptions_status_expires_composite_index() -> None:
    assert "ix_subscriptions_status_expires_at" in _index_names(
        "subscriptions"
    )


# ── #130: sub-link RESTRICT guard on devices.subscription_id ──────────
def test_devices_subscription_fk_is_restrict() -> None:
    fk = next(iter(_col("devices", "subscription_id").foreign_keys))
    assert fk.ondelete == "RESTRICT"


# ── #131: status/flag columns are NOT NULL with a server_default ──────
def test_status_and_flag_columns_not_null_with_server_default() -> None:
    cols = {
        "vpn_nodes": ["status", "is_active"],
        "subscriptions": ["status", "auto_renew", "traffic_used_mb"],
        "devices": ["status"],
        "plans": ["is_visible", "max_devices", "price"],
        "payments": ["status", "amount"],
        "invoices": ["status"],
        "vpn_configs": ["is_enabled"],
        "credentials": ["is_active"],
        "cloud_providers": ["is_active"],
    }
    for table, names in cols.items():
        for name in names:
            c = _col(table, name)
            assert c.nullable is False, f"{table}.{name} still nullable"
            assert c.server_default is not None, (
                f"{table}.{name} has no server_default"
            )
