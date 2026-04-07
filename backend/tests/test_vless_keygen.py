"""Sanity checks on the VLESS Reality key generator.

Reality uses raw X25519 keypairs encoded as unpadded base64url. If the
shape ever drifts (padding re-added, bytes flipped, wrong curve) the
client handshake fails with an opaque error, so we pin the invariants
in a test.
"""
from __future__ import annotations

import base64


def test_generate_reality_keypair_shape() -> None:
    from app.services.vless import generate_reality_keypair

    pub, priv = generate_reality_keypair()

    # Base64url, no padding, 32 raw bytes → 43 chars.
    assert len(pub) == 43
    assert len(priv) == 43
    assert "=" not in pub and "=" not in priv

    # Round-trip through base64url — must decode to exactly 32 bytes.
    def _decode(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "==")

    assert len(_decode(pub)) == 32
    assert len(_decode(priv)) == 32


def test_generate_reality_keypair_is_unique() -> None:
    from app.services.vless import generate_reality_keypair

    # Two calls must never collide. This would only fail if someone
    # accidentally memoized the keypair at module level.
    a = generate_reality_keypair()
    b = generate_reality_keypair()
    assert a != b


def test_generate_short_id_shape() -> None:
    from app.services.vless import generate_short_id

    sid = generate_short_id()
    # 8 hex bytes = 16 lowercase hex chars. Reality accepts up to 8 bytes.
    assert len(sid) == 16
    int(sid, 16)  # must parse as hex
