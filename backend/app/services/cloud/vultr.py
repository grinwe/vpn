"""Vultr cloud driver.

Mirrors the structure of :mod:`hetzner` so that ``get_driver`` dispatch
stays trivial. We talk raw HTTP to the Vultr v2 API rather than pulling
in the official SDK — same rationale as Hetzner.

API docs: https://www.vultr.com/api/
"""
from __future__ import annotations

import ipaddress
import logging
import time

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

API = "https://api.vultr.com/v2"
POLL_TIMEOUT = 240  # Vultr is a bit slower to reach "active" than Hetzner


def _is_public_ipv4(ip: str | None) -> bool:
    """Публичный (маршрутизируемый) IPv4? Отсекаем пустое, 0.0.0.0 и RFC1918.

    Vultr отдаёт ``internal_ip`` (приватный VPC-адрес 10.x/172.x) и, пока
    инстанс поднимается, ``main_ip`` может быть ещё пуст или ``0.0.0.0``.
    Приватный адрес в ``node.host`` = нода-зомби: спавн формально успешен,
    а Ansible и клиенты ходят на недостижимый IP.
    """
    if not ip or ip == "0.0.0.0":
        return False
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


class VultrDriver:
    kind = "vultr"

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
            "region": region,
            "plan": plan,
            "label": name,
            "hostname": name,
            # Vultr distinguishes between os_id (numeric) and image_id
            # (snapshot/marketplace strings). For our use we always pass
            # an os_id integer when image looks numeric, otherwise treat
            # it as an OS slug — Vultr accepts both via "image_id".
            "image_id": image,
        }
        if image.isdigit():
            body.pop("image_id")
            body["os_id"] = int(image)
        if ssh_key_ids:
            body["sshkey_id"] = ssh_key_ids
        if user_data:
            # Vultr expects base64-encoded cloud-init.
            import base64

            body["user_data"] = base64.b64encode(user_data.encode()).decode()

        data = self._post("/instances", body)
        instance = data.get("instance") or {}
        instance_id = instance.get("id")
        if not instance_id:
            raise DriverError(f"Vultr did not return instance id: {data}")

        instance = self._wait_running(instance_id)
        # Только публичный main_ip: internal_ip у Vultr — приватный VPC-адрес,
        # он бы прошёл validate_node_identity_fields и уехал в node.host.
        ipv4 = instance.get("main_ip")
        ipv6 = instance.get("v6_main_ip") or None
        if not _is_public_ipv4(ipv4):
            raise DriverError(
                f"Vultr instance {instance_id} has no public IPv4 (got {ipv4!r})"
            )

        price = None
        try:
            price = float(instance.get("plan_price_monthly") or 0) or None
        except Exception:  # noqa: BLE001
            price = None

        return CloudServer(
            external_id=str(instance_id),
            ipv4=ipv4,
            ipv6=ipv6 if ipv6 and ipv6 != "::" else None,
            region=region,
            plan=plan,
            monthly_cost=price,
            raw=instance,
        )

    def destroy_server(self, external_id: str) -> None:
        self._delete(f"/instances/{external_id}")

    def list_regions(self) -> list[str]:
        data = self._get("/regions")
        return [r["id"] for r in data.get("regions", [])]

    # ---------- helpers ----------

    def _wait_running(self, instance_id: str) -> dict:
        deadline = time.time() + POLL_TIMEOUT
        last: dict = {}
        while time.time() < deadline:
            data = self._get(f"/instances/{instance_id}")
            last = data.get("instance") or {}
            # Vultr reports two stages: ``status`` (active/pending) and
            # ``server_status`` (ok/installingbooting/none). Wait for
            # both — otherwise SSH races boot. Также ждём публичный main_ip:
            # он присваивается не мгновенно, а без него ipv4 бесполезен.
            if (
                last.get("status") == "active"
                and last.get("server_status") in ("ok", "installed")
                and _is_public_ipv4(last.get("main_ip"))
            ):
                return last
            time.sleep(4)
        raise DriverError(
            f"Vultr instance {instance_id} did not reach active state within {POLL_TIMEOUT}s; "
            f"last status: {last.get('status')}/{last.get('server_status')}, "
            f"main_ip: {last.get('main_ip')!r}"
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
            raise DriverError(f"Vultr API request failed: {exc}") from exc
        if resp.status_code == 204:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise DriverError(
                f"Vultr API {method} {path} -> {resp.status_code}: {payload}"
            )
        return payload
