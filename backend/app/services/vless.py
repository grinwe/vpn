"""Helpers for VLESS + Reality node provisioning.

VLESS Reality needs three server-side secrets that must stay consistent
between what xray is running and what we hand out to clients:

* an x25519 keypair — xray holds the private key, clients embed the public
  key in the ``vless://…`` URI;
* a ``shortId`` — a short hex string xray uses to pin the server identity
  during the Reality handshake; it must be present both in xray config
  and in the client URI.

Authority for these values lives in the backend, not on the node. We
generate them when we first create a VLESS ``VPNConfig``, store the
public key + shortId in plaintext (they're on the wire anyway) and
encrypt the private key at rest. When ansible provisions the node we
hand everything over via ``extra_vars`` so the installer is pure
template rendering — no state generation on the target side. A replaced
node can be rebuilt from the same DB row and stay bit-for-bit compatible
with already-issued client credentials.

The x25519 wire format xray expects is the raw 32-byte scalar encoded
with urlsafe base64, padding stripped. Identical to what ``xray x25519``
emits if you run it locally.
"""
from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_reality_keypair() -> tuple[str, str]:
    """Return ``(public_key_b64, private_key_b64)`` in xray's wire format."""
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url_nopad(pub_raw), _b64url_nopad(priv_raw)


def generate_short_id() -> str:
    """Return a Reality ``shortId`` — 8 bytes of hex, xray's typical length."""
    return secrets.token_hex(8)


def generate_wireguard_keypair() -> tuple[str, str]:
    """Return ``(public_key, private_key)`` in WireGuard's wire format.

    WireGuard uses X25519 like Reality but encodes keys with the standard
    base64 alphabet (``+/=``), not url-safe — matching the output of
    ``wg genkey`` / ``wg pubkey``.
    """
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub_raw).decode("ascii"), base64.b64encode(priv_raw).decode("ascii")
