"""Helpers for ShadowTLS v3 + shadowsocks-rust node provisioning.

ShadowTLS wraps a normal shadowsocks stream in an outer TLS 1.3
handshake that impersonates a reputable domain (``www.cloudflare.com``
by default — real TLS cert, real HTTP/2 ALPN, resistant to active
probing that killed v1 / v2 obfuscators). Two secrets live behind that
wrapper and must stay consistent between the node and every client
URI we hand out:

* **shadowtls_password** — the outer shadow-tls channel password;
  shadow-tls on the node verifies it before stripping TLS and
  forwarding to loopback.
* **ss_password** — the inner shadowsocks-rust password. We use
  ``2022-blake3-aes-128-gcm``, which takes a 16-byte pre-shared key
  encoded as base64 (22 chars, no padding). DO NOT swap the method
  without updating :func:`build_credential` — the URI format depends
  on the cipher family.

Authority model mirrors :mod:`.vless`: the backend generates both
secrets once, stores them encrypted in the ``VPNConfig.settings``
blob, and ships them via ansible ``extra_vars`` on every site.yml
run. The node is stateless — rebuilding it from scratch with the
same DB row produces a bit-for-bit identical listener, so already-
issued client credentials keep working.

The URI we emit is the sing-box / Hiddify / v2rayTun compatible
``ss://…?plugin=shadow-tls;…`` form. Every client that speaks SS2022
+ the shadow-tls plugin imports it with a single paste — there is no
manual outbound chaining required like with stock sing-box configs.
"""
from __future__ import annotations

import base64
import secrets
from urllib.parse import quote

# Keep in sync with roles/install_shadowtls_stack/defaults/main.yml
# (``shadowtls_ss_method``). The URI format and key length below are
# method-specific — a mismatch means clients fail to parse the link.
SS_METHOD = "2022-blake3-aes-128-gcm"
SS_KEY_BYTES = 16  # 2022-blake3-aes-128-gcm requires a 16-byte PSK.

# Default handshake domain. Cloudflare serves real TLS 1.3 + HTTP/2 on
# this host; the shadow-tls server re-uses its certificate chain
# verbatim, so active probes see a perfectly valid Cloudflare response.
DEFAULT_HANDSHAKE_DOMAIN = "www.cloudflare.com"

# Default exposed port on the node. Picked to match the Nodes.tsx
# admin form default so the UI and the backend agree without extra
# round-tripping.
DEFAULT_PORT = 8443


def _b64_nopad(data: bytes) -> str:
    return base64.b64encode(data).rstrip(b"=").decode("ascii")


def generate_ss_password() -> str:
    """Return a base64-encoded 16-byte PSK for ``2022-blake3-aes-128-gcm``."""
    return _b64_nopad(secrets.token_bytes(SS_KEY_BYTES))


def generate_shadowtls_password() -> str:
    """Return a random 32-char URL-safe password for the outer TLS channel."""
    return secrets.token_urlsafe(24)


def build_credential(
    *,
    host: str,
    port: int,
    ss_password: str,
    shadowtls_password: str,
    handshake_domain: str,
    name: str,
) -> str:
    """Return a Hiddify-importable ``ss://`` URI chaining shadow-tls.

    Format (as consumed by sing-box 1.8+, Hiddify, v2rayTun, NekoBox):

        ss://BASE64(method:ss_password)@host:port
            ?plugin=shadow-tls;version=3;host=<handshake>;password=<stpwd>
            #<name>

    ``BASE64`` is standard padding-stripped base64 over the literal
    ``method:ss_password`` bytes. The plugin params are ';'-delimited
    and URL-encoded as a whole — Hiddify's parser tolerates both the
    ';' and '%3B' forms, but raw ';' is the canonical sing-box style.
    """
    userinfo_raw = f"{SS_METHOD}:{ss_password}".encode("utf-8")
    userinfo_b64 = _b64_nopad(userinfo_raw)

    plugin_opts = (
        f"shadow-tls;version=3;host={handshake_domain};password={shadowtls_password}"
    )
    # URL-encode only the plugin value — host/port/fragment stay raw
    # so the link remains human-readable in the admin UI.
    plugin_param = quote(plugin_opts, safe="=;")
    fragment = quote(name, safe="")
    return (
        f"ss://{userinfo_b64}@{host}:{port}"
        f"?plugin={plugin_param}#{fragment}"
    )
