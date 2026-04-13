"""Regression tests for the ansible_runner inventory builder.

Locks in the fixes for audit finding #55:

 - YAML injection via ``node.name`` / ``node.host`` / ``node.ssh_port``.
   The renderer uses a raw ``str.format()`` on a template string, so a
   newline, colon or quote in any of these fields would let the caller
   inject arbitrary keys (``ansible_host``, ``ansible_user``,
   ``ansible_ssh_private_key_file``) and redirect the next playbook run
   to an attacker-controlled box using the backend's SSH credentials.

 - Temp-file contract: ``build_inventory_for_node`` returns a
   ``delete=False`` path and the caller must ``unlink`` it. These tests
   verify the returned file is readable, writable, and can be removed.

None of these tests touch the database — they import
``validate_node_identity_fields``, ``validate_node_name``,
``InvalidNodeIdentity`` and ``build_inventory_for_node`` directly and
exercise them with a ``SimpleNamespace`` stub VPNNode.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.ansible_runner import (
    InvalidNodeIdentity,
    build_inventory_for_node,
    validate_node_identity_fields,
    validate_node_name,
)


# ---------------------------------------------------------------------------
# validate_node_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "good",
    [
        "a",
        "node-1",
        "vpn-nl-01",
        "0abc",
        "x" * 63,
    ],
)
def test_validate_node_name_accepts_dns_safe(good: str) -> None:
    validate_node_name(good)  # must not raise


@pytest.mark.parametrize(
    "bad",
    [
        "",
        None,
        "-leading-dash",
        "UPPER",
        "has space",
        "has.dot",
        "has_underscore",
        "has:colon",
        "has\nnewline",
        "x" * 64,  # one past the 63-char DNS limit
        "\u0441\u043b\u043e\u0432\u043e",  # non-ASCII
    ],
)
def test_validate_node_name_rejects_unsafe(bad) -> None:
    with pytest.raises(InvalidNodeIdentity):
        validate_node_name(bad)


# ---------------------------------------------------------------------------
# validate_node_identity_fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "198.51.100.10",
        "example.com",
        "vpn-nl-01.example.org",
        "[2001:db8::1]",
        "2001:db8::1",
    ],
)
def test_validate_identity_accepts_real_hosts(host: str) -> None:
    validate_node_identity_fields("node-1", host, 22)


@pytest.mark.parametrize(
    "bad_host",
    [
        "",
        None,
        "1.2.3.4\nmalicious: key",
        'has "quote"',
        "has space",
        "has|pipe",
        "has/slash",
        "has@at",
    ],
)
def test_validate_identity_rejects_bad_hosts(bad_host) -> None:
    with pytest.raises(InvalidNodeIdentity):
        validate_node_identity_fields("node-1", bad_host, 22)


@pytest.mark.parametrize("port", [0, -1, 65536, 70000])
def test_validate_identity_rejects_out_of_range_ports(port: int) -> None:
    with pytest.raises(InvalidNodeIdentity):
        validate_node_identity_fields("node-1", "198.51.100.10", port)


@pytest.mark.parametrize("port", [None, "22", 22.0, True])
def test_validate_identity_rejects_non_int_ports(port) -> None:
    with pytest.raises(InvalidNodeIdentity):
        validate_node_identity_fields("node-1", "198.51.100.10", port)


# ---------------------------------------------------------------------------
# build_inventory_for_node — happy path + rejection + cleanup contract
# ---------------------------------------------------------------------------


def _stub_node(**kwargs) -> SimpleNamespace:
    """Minimal VPNNode stand-in for tests that never touch the DB."""
    defaults = dict(name="test-node-1", host="198.51.100.10", ssh_port=22)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_build_inventory_happy_path_writes_readable_file() -> None:
    node = _stub_node()
    path = build_inventory_for_node(node)
    try:
        assert path.exists()
        content = path.read_text()
        assert "test-node-1" in content
        assert "198.51.100.10" in content
        assert "ansible_port: 22" in content
        assert "ansible_user: root" in content
    finally:
        path.unlink()
    assert not path.exists()


def test_build_inventory_respects_custom_ansible_user() -> None:
    node = _stub_node()
    path = build_inventory_for_node(node, ansible_user="ubuntu")
    try:
        assert "ansible_user: ubuntu" in path.read_text()
    finally:
        path.unlink()


def test_build_inventory_rejects_yaml_injection_via_name(tmp_path: Path) -> None:
    """A newline in node.name must be rejected *before* any tempfile is created."""
    node = _stub_node(name="evil\n    ansible_host: attacker.example")
    before = set(Path("/tmp").glob("*-inventory.yml"))
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(node)
    after = set(Path("/tmp").glob("*-inventory.yml"))
    # No new inventory file should have leaked.
    assert after == before


def test_build_inventory_rejects_colon_in_name() -> None:
    """A bare colon in node.name would break YAML key/value alignment."""
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(name="foo:bar"))


def test_build_inventory_rejects_whitespace_in_host() -> None:
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(host="198.51.100.10 malicious"))


def test_build_inventory_rejects_quote_in_host() -> None:
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(host='1.2.3.4"evil'))


def test_build_inventory_rejects_port_out_of_range() -> None:
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(ssh_port=0))
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(ssh_port=65536))


def test_build_inventory_rejects_empty_name() -> None:
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(name=""))


def test_build_inventory_rejects_leading_hyphen_in_name() -> None:
    """Leading hyphens would confuse ``ansible-playbook --limit=-foo``."""
    with pytest.raises(InvalidNodeIdentity):
        build_inventory_for_node(_stub_node(name="-bad"))
