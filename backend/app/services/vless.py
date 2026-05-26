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
import re
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# UUID v4 wire format used in xray.clients[] and vless:// URIs.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_VLESS_URI_USER_RE = re.compile(r"^vless://([^@/?#\s]+)@", re.IGNORECASE)


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


def extract_uuid_from_vless_url(value: str | None) -> str | None:
    """Return the VLESS user UUID embedded in ``value``, or ``None``.

    Accepts either a bare UUID string or a full ``vless://UUID@host:port?...``
    URI — operators paste whatever the user sends from Hiddify. The
    returned UUID is always lowercased so substring matches against
    ``Credential.config_text`` (which the URL-builder writes lowercase)
    are stable.
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    m = _VLESS_URI_USER_RE.match(s)
    if m:
        s = m.group(1)
    m = _UUID_RE.search(s)
    if m:
        return m.group(0).lower()
    return None


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
