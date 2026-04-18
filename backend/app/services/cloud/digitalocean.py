"""DigitalOcean cloud driver.

Mirrors :mod:`hetzner` and :mod:`vultr`. Talks raw HTTP to the DO v2
API. Image can be a slug ("ubuntu-22-04-x64") or a snapshot id.

API docs: https://docs.digitalocean.com/reference/api/api-reference/
"""
from __future__ import annotations

import logging
import time

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

API = "https://api.digitalocean.com/v2"
POLL_TIMEOUT = 240


class DigitalOceanDriver:
    kind = "digitalocean"

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
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
            "region": region,
            "size": plan,
            "image": int(image) if image.isdigit() else image,
            "ipv6": True,
            "monitoring": True,
        }
        if ssh_key_ids:
            # DO accepts numeric ids or fingerprints; pass through as-is.
            body["ssh_keys"] = [
                int(k) if str(k).isdigit() else k for k in ssh_key_ids
            ]
        if user_data:
            body["user_data"] = user_data

        data = self._post("/droplets", body)
        droplet = data.get("droplet") or {}
        droplet_id = droplet.get("id")
        if not droplet_id:
            raise DriverError(f"DigitalOcean did not return droplet id: {data}")

        droplet = self._wait_active(droplet_id)
        ipv4 = _pick_ip(droplet, "v4", "public")
        ipv6 = _pick_ip(droplet, "v6", "public")
        if not ipv4:
            raise DriverError(f"DO droplet {droplet_id} has no public IPv4")

        price = None
        try:
            price = float(droplet.get("size", {}).get("price_monthly") or 0) or None
        except Exception:  # noqa: BLE001
            price = None

        return CloudServer(
            external_id=str(droplet_id),
            ipv4=ipv4,
            ipv6=ipv6,
            region=region,
            plan=plan,
            monthly_cost=price,
            raw=droplet,
        )

    def destroy_server(self, external_id: str) -> None:
        self._delete(f"/droplets/{external_id}")

    def list_regions(self) -> list[str]:
        data = self._get("/regions")
        return [r["slug"] for r in data.get("regions", []) if r.get("available")]

    # ---------- helpers ----------

    def _wait_active(self, droplet_id: int) -> dict:
        deadline = time.time() + POLL_TIMEOUT
        last: dict = {}
        while time.time() < deadline:
            data = self._get(f"/droplets/{droplet_id}")
            last = data.get("droplet") or {}
            if last.get("status") == "active":
                return last
            time.sleep(4)
        raise DriverError(
            f"DO droplet {droplet_id} did not reach active state within {POLL_TIMEOUT}s; "
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
            raise DriverError(f"DigitalOcean API request failed: {exc}") from exc
        if resp.status_code == 204:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise DriverError(
                f"DigitalOcean API {method} {path} -> {resp.status_code}: {payload}"
            )
        return payload


def _pick_ip(droplet: dict, family: str, kind: str) -> str | None:
    nets = (droplet.get("networks") or {}).get(family) or []
    for net in nets:
        if net.get("type") == kind and net.get("ip_address"):
            return net["ip_address"]
    return None
