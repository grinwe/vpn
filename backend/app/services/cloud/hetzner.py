"""Hetzner Cloud driver.

We use raw ``requests`` rather than the official ``hcloud`` SDK so that the
backend image does not have to pin a third-party SDK and so that the error
handling stays uniform across drivers.
"""
from __future__ import annotations

import logging
import time

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

API = "https://api.hetzner.cloud/v1"
POLL_TIMEOUT = 180  # seconds — server should be "running" well within this


class HetznerDriver:
    kind = "hetzner"

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

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
            "server_type": plan,
            "image": image,
            "location": region,
            "start_after_create": True,
        }
        if ssh_key_ids:
            body["ssh_keys"] = ssh_key_ids
        if user_data:
            body["user_data"] = user_data

        data = self._post("/servers", body)
        server = data.get("server") or {}
        server_id = server.get("id")
        if not server_id:
            raise DriverError(f"Hetzner did not return server id: {data}")

        # Poll until the server is 'running' — otherwise SSH will race.
        server = self._wait_running(server_id)
        public_net = server.get("public_net") or {}
        ipv4 = (public_net.get("ipv4") or {}).get("ip")
        ipv6 = (public_net.get("ipv6") or {}).get("ip")
        if not ipv4:
            raise DriverError(f"Hetzner server {server_id} has no public IPv4")

        price = None
        try:
            price = float(
                (server.get("server_type") or {})
                .get("prices", [{}])[0]
                .get("price_monthly", {})
                .get("gross")
                or 0
            ) or None
        except Exception:  # noqa: BLE001
            price = None

        return CloudServer(
            external_id=str(server_id),
            ipv4=ipv4,
            ipv6=ipv6,
            region=region,
            plan=plan,
            monthly_cost=price,
            raw=server,
        )

    def destroy_server(self, external_id: str) -> None:
        self._delete(f"/servers/{external_id}")

    def list_regions(self) -> list[str]:
        data = self._get("/locations")
        return [loc["name"] for loc in data.get("locations", [])]

    # ---------- helpers ----------

    def _wait_running(self, server_id: int) -> dict:
        deadline = time.time() + POLL_TIMEOUT
        last: dict = {}
        while time.time() < deadline:
            data = self._get(f"/servers/{server_id}")
            last = data.get("server") or {}
            if last.get("status") == "running":
                return last
            time.sleep(3)
        raise DriverError(
            f"Hetzner server {server_id} did not reach running state within {POLL_TIMEOUT}s; "
            f"last status: {last.get('status')}"
        )

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    def _delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._session.request(method, f"{API}{path}", timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise DriverError(f"Hetzner API request failed: {exc}") from exc
        if resp.status_code == 204:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise DriverError(
                f"Hetzner API {method} {path} -> {resp.status_code}: {payload}"
            )
        return payload
