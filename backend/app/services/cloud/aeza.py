"""Aeza cloud driver.

Mirrors the structure of :mod:`hetzner` / :mod:`vultr`. Talks raw HTTP to
the Aeza API. Authentication via API key in ``X-API-Key`` header.

API docs: https://wiki.aeza.net/en/api (unofficial / community)
"""
from __future__ import annotations

import logging
import time

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

API = "https://core.aeza.net/api"
POLL_TIMEOUT = 240


class AezaDriver:
    kind = "aeza"

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-API-Key": token,
                "Content-Type": "application/json",
            }
        )

    # ---------- public API ----------

    def create_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,
        user_data: str | None = None,
    ) -> CloudServer:
        body: dict = {
            "name": name,
            "locationId": region,
            "productId": plan,
            "osId": image,
            "quantity": 1,
            "term": "month",
        }
        if ssh_key_ids:
            body["sshKeyIds"] = ssh_key_ids

        data = self._post("/services", body)
        # Aeza returns {"data": {"items": [{"id": ..., ...}]}}
        items = data.get("data", {}).get("items") or data.get("items") or []
        if isinstance(data.get("data"), dict) and "id" in data["data"]:
            items = [data["data"]]
        if not items:
            raise DriverError(f"Aeza did not return service id: {data}")

        service = items[0]
        service_id = str(service.get("id") or "")
        if not service_id:
            raise DriverError(f"Aeza did not return service id: {data}")

        service = self._wait_running(service_id)
        ipv4 = service.get("ip") or service.get("ipv4") or ""
        ipv6 = service.get("ipv6") or None
        if not ipv4:
            # Try nested attributes
            attrs = service.get("attributes") or {}
            ipv4 = attrs.get("ip") or attrs.get("ipv4") or ""
            ipv6 = ipv6 or attrs.get("ipv6") or None
        if not ipv4:
            raise DriverError(
                f"Aeza service {service_id} has no public IPv4: {service}"
            )

        price = None
        try:
            price = float(service.get("price") or service.get("cost") or 0) or None
        except Exception:  # noqa: BLE001
            pass

        return CloudServer(
            external_id=service_id,
            ipv4=ipv4,
            ipv6=ipv6 if ipv6 else None,
            region=region,
            plan=plan,
            monthly_cost=price,
            raw=service,
        )

    def destroy_server(self, external_id: str) -> None:
        self._delete(f"/services/{external_id}")

    def list_regions(self) -> list[str]:
        data = self._get("/locations")
        items = data.get("data", {}).get("items") or data.get("items") or []
        if isinstance(data.get("data"), list):
            items = data["data"]
        return [str(loc.get("id") or loc.get("slug") or "") for loc in items if loc]

    # ---------- helpers ----------

    def _wait_running(self, service_id: str) -> dict:
        deadline = time.time() + POLL_TIMEOUT
        last: dict = {}
        while time.time() < deadline:
            data = self._get(f"/services/{service_id}")
            last = data.get("data") or data
            if isinstance(last, dict) and last.get("items"):
                last = last["items"][0] if last["items"] else last
            status = str(last.get("status") or "").lower()
            if status in ("active", "running"):
                return last
            logger.debug(
                "Aeza service %s status=%s, waiting…", service_id, status
            )
            time.sleep(5)
        raise DriverError(
            f"Aeza service {service_id} did not reach active within "
            f"{POLL_TIMEOUT}s; last status: {last.get('status')}"
        )

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    def _delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._session.request(
                method, f"{API}{path}", timeout=30, **kwargs
            )
        except requests.RequestException as exc:
            raise DriverError(f"Aeza API request failed: {exc}") from exc
        if resp.status_code == 204:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise DriverError(
                f"Aeza API {method} {path} -> {resp.status_code}: {payload}"
            )
        return payload
