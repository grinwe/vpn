"""Cloudflare DNS client for the WS+CDN front zone (operator-aware routing).

When a ``vless-ws-cdn`` / ``vless-xhttp`` VPNConfig is created on a node, the
backend mints a random **DNS-only** A-record ``<rand>.<WSCDN_FRONT_DOMAIN>``
→ node IP in Cloudflare (grey cloud, proxied=False). The node serves TLS
itself (Let's Encrypt per subdomain); CF is used ONLY as a DNS host, not as
a proxy/CDN. Random hex labels (not node names) keep records
un-fingerprintable.

NB: CF *proxying* (orange cloud) for WS/xhttp transport is DEAD — RKN DPI
kills the Cloudflare leg on 4G (origin proven alive, client gets no data;
see project_cf_ws_cdn_dead). Never set proxied=True for node transport.

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
import time

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.cloudflare.com/client/v4"
_TIMEOUT = 10

# delete_record: транзиентную ошибку (5xx / сетевой таймаут) ретраим, прежде
# чем сдаться — иначе один CF-хиккап навсегда оставляет A-запись в зоне
# (вызывающий код к этому моменту уже стёр record_id).
_DELETE_RETRIES = 2
_DELETE_RETRY_PAUSE = 0.5

# Zone ids are stable for the process lifetime — resolve once PER zone.
# Multi-zone now: ws-cdn and xhttp can live on different front domains
# (e.g. ws→wgse.info, xhttp→grwr.ink), so cache is keyed by domain.
_zone_id_cache: dict[str, str] = {}


class CloudflareError(RuntimeError):
    """CF API call failed or the integration is not configured.

    ``status_code`` — HTTP-статус ответа CF (или ``None`` для сетевой
    ошибки/таймаута), нужен чтобы отличить 404 (запись уже удалена → тихий
    no-op) от остальных ошибок (5xx / протухший токен → ретрай / эскалация).
    """

    def __init__(self, *args, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


def front_domain() -> str:
    """Default (ws-cdn) front zone."""
    return os.getenv("WSCDN_FRONT_DOMAIN", "").strip().rstrip(".")


def _token() -> str:
    # Single token; must hold Zone.DNS:Edit on the front zone (wgse.info).
    # Keep grwr.ink access too UNTIL old CF-proxied xhttp records are torn
    # down — delete_record() resolves the zone from the stored cf_front_domain.
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
        raise CloudflareError(
            f"CF API {method} {path} → {resp.status_code}: {errors}",
            status_code=resp.status_code,
        )
    return body


def _get_zone_id(domain: str | None = None) -> str:
    domain = (domain or front_domain()).strip().rstrip(".")
    if not domain:
        raise CloudflareError("CF front domain not set (WSCDN_FRONT_DOMAIN)")
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
    node_ip: str,
    *,
    label: str | None = None,
    domain: str | None = None,
    proxied: bool = False,
) -> dict:
    """Create a random A-record for a node in ``domain``'s zone (default
    ws-cdn front). Returns ``{"subdomain": "<rand>.<front>",
    "record_id": "...", "front_domain": "<front>"}``.

    ``proxied`` defaults to **False** (DNS-only / grey cloud → record points
    straight at the node IP; node serves TLS itself). CF *proxying* for
    WS/xhttp transport is dead (RKN kills the CF leg) — do not pass True.
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
            "proxied": proxied,
            # proxied=True → CF ignores ttl (forces "auto"=1). DNS-only honors
            # it: keep low so a recycled label can't serve a stale IP.
            "ttl": 1 if proxied else 120,
            "comment": "vless-node-front (managed by backend)",
        },
    )
    record_id = body["result"]["id"]
    logger.info(
        "cf_dns: created %s A %s → %s (id=%s)",
        "proxied" if proxied else "dns-only", fqdn, node_ip, record_id,
    )
    return {"subdomain": fqdn, "record_id": record_id, "front_domain": domain}


def _is_record_gone(exc: CloudflareError) -> bool:
    """True если ошибка CF означает «записи уже нет» → удаление идемпотентно.

    Признаки: HTTP 404 либо код ошибки CF 81044 (``Record does not exist``,
    иногда приходит не с 404-статусом)."""
    if getattr(exc, "status_code", None) == 404:
        return True
    return "81044" in str(exc)


def delete_record(record_id: str, *, domain: str | None = None) -> None:
    """Delete a DNS record in ``domain``'s zone. Idempotent — a missing
    record (404 / CF-код 81044) is a no-op. ``domain`` must match the zone the
    record was created in (stored as ``cf_front_domain``).

    Транзиентные ошибки (5xx / сетевой таймаут / протухший токен) ретраятся
    ``_DELETE_RETRIES`` раз с паузой. Если запись так и не удалилась и это НЕ
    404 — логируем ERROR (не warning): вызывающий код сейчас сотрёт record_id,
    так что стейл-запись должна всплыть в алертах и её дочистят руками.
    Не пробрасываем исключение осознанно — часть вызовов идёт в цикле по
    ``node.configs`` при удалении ноды (nodes.py), и raise оборвал бы весь
    teardown на первом же CF-хиккапе."""
    if not record_id:
        return
    last_exc: CloudflareError | None = None
    for attempt in range(_DELETE_RETRIES + 1):
        try:
            _request(
                "DELETE", f"/zones/{_get_zone_id(domain)}/dns_records/{record_id}"
            )
            logger.info("cf_dns: deleted record %s", record_id)
            return
        except CloudflareError as exc:
            if _is_record_gone(exc):
                # Записи уже нет → удаление успешно (идемпотентность).
                logger.info(
                    "cf_dns: record %s already gone (404) — no-op", record_id
                )
                return
            last_exc = exc
            if attempt < _DELETE_RETRIES:
                time.sleep(_DELETE_RETRY_PAUSE)
    logger.error(
        "cf_dns: delete record %s FAILED after %d attempts (non-404) — "
        "record may be left STALE in the zone, needs manual cleanup: %s",
        record_id,
        _DELETE_RETRIES + 1,
        last_exc,
    )
