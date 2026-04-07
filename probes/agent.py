"""Out-of-band probe agent.

Deployed *outside* the VPN service itself — one instance per source region
(ru-mts, ru-beeline, kz, eu, …). Each instance:

1. Pulls the current list of probe targets from the backend.
2. For each endpoint, runs a protocol-appropriate reachability check:
   - ``tcp``: open a TCP connection within ``PROBE_TIMEOUT`` seconds.
   - ``tls``: open a TCP connection AND complete a TLS handshake with the
     expected SNI. A cert-name mismatch is *not* a failure: for ShadowTLS
     we intentionally borrow the SNI of a popular site.
3. POSTs the outcome back to ``/api/nodes/{id}/probes`` tagged with the
   agent's own ``SOURCE_REGION`` label so backend aggregation can tell
   "blocked in RU" from "dead everywhere".

The agent is *intentionally* stateless and single-file. Restart it, move
it between regions, run ten of them — it doesn't matter. All state lives
in the backend.

Environment variables:

============================  ================================================
``BACKEND_URL``               base URL, e.g. ``https://vpn.example.com``
``BACKEND_TOKEN``             admin token (same one the API expects)
``SOURCE_REGION``             label written into every probe, e.g. ``ru-mts``
``SOURCE_KIND``               optional free-form tag, e.g. ``residential``
``PROBE_INTERVAL``            seconds between full sweeps (default: ``60``)
``PROBE_TIMEOUT``             per-endpoint timeout in seconds (default: ``5``)
``PROBE_CONCURRENCY``         how many endpoints to check in parallel (``16``)
``HTTP_TIMEOUT``              backend HTTP timeout in seconds (default: ``10``)
``LOG_LEVEL``                 Python log level (default: ``INFO``)
============================  ================================================
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger("probe-agent")


@dataclass
class ProbeOutcome:
    node_id: int
    result: str  # ok|timeout|refused|tls_fail|unknown
    latency_ms: int | None
    details: dict[str, Any]


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        logger.error("Environment variable %s is required", name)
        sys.exit(2)
    return value or ""


BACKEND_URL = _env("BACKEND_URL", required=True).rstrip("/")
BACKEND_TOKEN = _env("BACKEND_TOKEN", required=True)
SOURCE_REGION = _env("SOURCE_REGION", required=True)
SOURCE_KIND = _env("SOURCE_KIND") or None
PROBE_INTERVAL = int(_env("PROBE_INTERVAL", "60"))
PROBE_TIMEOUT = float(_env("PROBE_TIMEOUT", "5"))
PROBE_CONCURRENCY = int(_env("PROBE_CONCURRENCY", "16"))
HTTP_TIMEOUT = float(_env("HTTP_TIMEOUT", "10"))


async def _probe_tcp(host: str, port: int) -> ProbeOutcome:
    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=PROBE_TIMEOUT
        )
    except asyncio.TimeoutError:
        return ProbeOutcome(node_id=0, result="timeout", latency_ms=None, details={})
    except ConnectionRefusedError:
        return ProbeOutcome(node_id=0, result="refused", latency_ms=None, details={})
    except OSError as exc:
        # Network unreachable, DNS failure, routing black-hole — these all
        # manifest as OSError. From the probe's perspective they're
        # indistinguishable from "something on the path is dropping
        # packets", which in Russian ISP speak is usually a block.
        return ProbeOutcome(
            node_id=0, result="timeout", latency_ms=None, details={"os_error": str(exc)}
        )
    else:
        latency_ms = int((time.monotonic() - started) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return ProbeOutcome(node_id=0, result="ok", latency_ms=latency_ms, details={})


async def _probe_tls(host: str, port: int, sni: str | None) -> ProbeOutcome:
    started = time.monotonic()
    # For ShadowTLS we specifically do NOT verify the cert chain: the
    # server legitimately presents a cert for ``sni`` (e.g. gateway.icloud.com)
    # which it does not own. We care about "did a TLS handshake complete?",
    # not "is this a genuine icloud endpoint".
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni or host),
            timeout=PROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return ProbeOutcome(node_id=0, result="timeout", latency_ms=None, details={})
    except ConnectionRefusedError:
        return ProbeOutcome(node_id=0, result="refused", latency_ms=None, details={})
    except ssl.SSLError as exc:
        return ProbeOutcome(
            node_id=0, result="tls_fail", latency_ms=None, details={"ssl_error": str(exc)}
        )
    except OSError as exc:
        return ProbeOutcome(
            node_id=0, result="timeout", latency_ms=None, details={"os_error": str(exc)}
        )
    else:
        latency_ms = int((time.monotonic() - started) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return ProbeOutcome(node_id=0, result="ok", latency_ms=latency_ms, details={})


async def _probe_endpoint(
    sem: asyncio.Semaphore, target: dict[str, Any], endpoint: dict[str, Any]
) -> ProbeOutcome:
    async with sem:
        host = target["host"]
        port = int(endpoint["port"])
        kind = endpoint.get("kind") or "tcp"
        try:
            if kind == "tls":
                outcome = await _probe_tls(host, port, endpoint.get("sni"))
            else:
                outcome = await _probe_tcp(host, port)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Probe crashed for %s:%s", host, port)
            outcome = ProbeOutcome(
                node_id=0, result="unknown", latency_ms=None, details={"exception": str(exc)}
            )

    outcome.node_id = int(target["node_id"])
    outcome.details.update(
        {
            "port": port,
            "kind": kind,
            "protocol": endpoint.get("protocol"),
        }
    )
    return outcome


async def _fetch_targets(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    url = f"{BACKEND_URL}/api/probes/targets"
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data.get("targets", [])


async def _submit_outcome(session: aiohttp.ClientSession, outcome: ProbeOutcome) -> None:
    url = f"{BACKEND_URL}/api/nodes/{outcome.node_id}/probes"
    payload = {
        "source_region": SOURCE_REGION,
        "result": outcome.result,
        "latency_ms": outcome.latency_ms,
        "source_kind": SOURCE_KIND,
        "details": outcome.details,
    }
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.warning(
                    "Backend rejected probe for node %s: %s %s",
                    outcome.node_id,
                    resp.status,
                    text[:200],
                )
    except aiohttp.ClientError as exc:
        logger.warning("Failed to submit probe for node %s: %s", outcome.node_id, exc)


async def _sweep(session: aiohttp.ClientSession) -> None:
    try:
        targets = await _fetch_targets(session)
    except aiohttp.ClientError as exc:
        logger.warning("Failed to fetch probe targets: %s", exc)
        return

    if not targets:
        logger.info("No probe targets from backend")
        return

    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    tasks: list[asyncio.Task[ProbeOutcome]] = []
    for target in targets:
        for endpoint in target.get("endpoints", []):
            tasks.append(asyncio.create_task(_probe_endpoint(sem, target, endpoint)))

    outcomes = await asyncio.gather(*tasks, return_exceptions=False)
    summary: dict[str, int] = {}
    for outcome in outcomes:
        summary[outcome.result] = summary.get(outcome.result, 0) + 1
        await _submit_outcome(session, outcome)

    logger.info(
        "Sweep done from %s: %d endpoints, results=%s",
        SOURCE_REGION,
        len(outcomes),
        summary,
    )


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Starting probe agent region=%s backend=%s interval=%ss",
        SOURCE_REGION,
        BACKEND_URL,
        PROBE_INTERVAL,
    )

    # Probes authenticate with a scoped API token carrying probe:read +
    # probe:write, not the admin token — so a compromised probe rig
    # cannot touch users/invoices/nodes-CRUD.
    headers = {"X-Api-Token": BACKEND_TOKEN}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        while True:
            await _sweep(session)
            await asyncio.sleep(PROBE_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
