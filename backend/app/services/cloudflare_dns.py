"""Cloudflare DNS client for the WS+CDN front zone (operator-aware routing).

When a ``vless-ws-cdn`` VPNConfig is created on a node, the backend mints a
random proxied A-record ``<rand>.<WSCDN_FRONT_DOMAIN>`` → node IP in
Cloudflare (orange cloud ON). Clients then connect to the CF edge (WSS),
CF proxies the WebSocket to the node origin — so the origin IP is hidden
and the user↔CF leg rides un-blockable shared CF edge IPs (survives the
RKN subnet-flagging that kills Reality; see operator_routing_roadmap /
project_ru_nodes_burnable).

Config (env, wired via deploy_app_stack):
- ``CLOUDFLARE_DNS_TOKEN``  — API token with Zone.DNS:Edit on the front zone
  (reuses ``vault_cloudflare_api_token``).
- ``WSCDN_FRONT_DOMAIN``    — the dedicated clean front zone, e.g. ``wgse.info``.

The record lifecycle is tied to the VPNConfig: ``cf_record_id`` +
``cf_subdomain`` are stored in ``VPNConfig.settings`` so delete-config can
tear the record down.
"""
from __future__ import annotations

import logging
import os
import secrets

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.cloudflare.com/client/v4"
_TIMEOUT = 10

# Zone ids are stable for the process lifetime — resolve once PER zone.
# Multi-zone now: ws-cdn and xhttp can live on different front domains
# (e.g. ws→wgse.info, xhttp→grwr.ink), so cache is keyed by domain.
_zone_id_cache: dict[str, str] = {}


class CloudflareError(RuntimeError):
    """CF API call failed or the integration is not configured."""


def front_domain() -> str:
    """Default (ws-cdn) front zone."""
    return os.getenv("WSCDN_FRONT_DOMAIN", "").strip().rstrip(".")


def xhttp_front_domain() -> str:
    """xhttp front zone — falls back to the ws-cdn front when not split."""
    return (os.getenv("XHTTP_FRONT_DOMAIN", "").strip().rstrip(".")
            or front_domain())


def _token() -> str:
    # Single token; must hold Zone.DNS:Edit on EVERY front zone in use
    # (wgse.info AND grwr.ink when split). CF tokens scope to multiple zones.
    return os.getenv("CLOUDFLARE_DNS_TOKEN", "").strip()


def is_configured(domain: str | None = None) -> bool:
    """True iff the token and the (given or default) front domain are set."""
    return bool(_token() and (domain or front_domain()))


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, **kwargs) -> dict:
    if not _token():
        raise CloudflareError(
            "Cloudflare not configured (set CLOUDFLARE_DNS_TOKEN)"
        )
    url = f"{_API_BASE}{path}"
    try:
        resp = requests.request(
            method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise CloudflareError(f"CF request failed: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not resp.ok or not body.get("success", False):
        errors = body.get("errors") or resp.text
        raise CloudflareError(f"CF API {method} {path} → {resp.status_code}: {errors}")
    return body


def _get_zone_id(domain: str | None = None) -> str:
    domain = (domain or front_domain()).strip().rstrip(".")
    if not domain:
        raise CloudflareError("CF front domain not set (WSCDN_FRONT_DOMAIN / XHTTP_FRONT_DOMAIN)")
    if domain in _zone_id_cache:
        return _zone_id_cache[domain]
    body = _request("GET", "/zones", params={"name": domain})
    result = body.get("result") or []
    if not result:
        raise CloudflareError(
            f"CF zone for {domain!r} not found — is the zone added + NS "
            f"delegated + active, and does CLOUDFLARE_DNS_TOKEN cover it?"
        )
    _zone_id_cache[domain] = result[0]["id"]
    return _zone_id_cache[domain]


def create_node_record(
    node_ip: str, *, label: str | None = None, domain: str | None = None
) -> dict:
    """Create a random proxied A-record for a node in ``domain``'s zone
    (default ws-cdn front). Returns ``{"subdomain": "<rand>.<front>",
    "record_id": "...", "front_domain": "<front>"}``.

    ``label`` lets the caller pin a name (e.g. for a retry); default is a
    fresh random hex label — less fingerprintable than node names.
    """
    domain = (domain or front_domain()).strip().rstrip(".")
    label = (label or secrets.token_hex(8)).lower()
    fqdn = f"{label}.{domain}"
    body = _request(
        "POST",
        f"/zones/{_get_zone_id(domain)}/dns_records",
        json={
            "type": "A",
            "name": fqdn,
            "content": node_ip,
            "proxied": True,
            "ttl": 1,  # 1 = auto; ignored when proxied
            "comment": "vless-cdn-front (managed by backend)",
        },
    )
    record_id = body["result"]["id"]
    logger.info("cf_dns: created proxied A %s → %s (id=%s)", fqdn, node_ip, record_id)
    return {"subdomain": fqdn, "record_id": record_id, "front_domain": domain}


def delete_record(record_id: str, *, domain: str | None = None) -> None:
    """Delete a DNS record in ``domain``'s zone. Idempotent — a missing
    record (404) is a no-op. ``domain`` must match the zone the record was
    created in (stored as ``cf_front_domain``)."""
    if not record_id:
        return
    try:
        _request("DELETE", f"/zones/{_get_zone_id(domain)}/dns_records/{record_id}")
        logger.info("cf_dns: deleted record %s", record_id)
    except CloudflareError as exc:
        # Already gone / not-found → fine. Anything else: log, don't block
        # the config-delete path on a CF hiccup.
        logger.warning("cf_dns: delete record %s failed (ignored): %s", record_id, exc)
