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
POLL_INTERVAL = 3  # seconds between status polls


def _monthly_price(server: dict) -> float | None:
    """Месячная цена из server_type.prices (gross). None — если не распарсилась.

    У Hetzner ``prices`` — массив ПО ЛОКАЦИЯМ, и цена одного и того же типа
    сервера различается между локациями (напр. ashburn vs fsn1). Берём элемент
    с ``location`` == фактическая локация сервера (``datacenter.location.name``),
    а не произвольный первый — иначе в monthly_cost попадёт чужая цифра.
    """
    try:
        prices = (server.get("server_type") or {}).get("prices") or []
        if not prices:
            return None
        # Фактическая локация размещения сервера (из ответа API по этому серверу).
        loc = (
            ((server.get("datacenter") or {}).get("location") or {}).get("name")
        )
        price = next(
            (p for p in prices if p.get("location") == loc),
            prices[0],  # страховка: формат сменился или локация не совпала
        )
        return float(
            (price.get("price_monthly") or {}).get("gross") or 0
        ) or None
    except Exception:  # noqa: BLE001
        return None


class HetznerDriver:
    kind = "hetzner"

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    # ---------- public API ----------

    def order_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,
        user_data: str | None = None,
    ) -> tuple[str, str]:
        """Быстрый заказ (POST /servers) БЕЗ ожидания IP → ``(external_id, root_password)``.

        ``node_spawner.spawn_node_async`` фиксирует external_id в БД сразу после
        заказа — оплаченный сервер привязан к строке VPNNode с момента создания,
        и при провале дальнейшего поллинга/бутстрапа сирот не остаётся (error-ноду
        можно снести из админки через destroy_node).
        """
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
        # root_password приходит только если ssh-ключи не инжектились (иначе null)
        return str(server_id), str(data.get("root_password") or "")

    def wait_for_ipv4(self, external_id: str) -> tuple[str, float | None, dict]:
        """Дождаться running+IPv4 заказанного сервера → ``(ipv4, monthly_cost, raw)``.

        Блокирует — вызывается в фоне (``node_spawner._finalize_spawn``). Сервер
        тут НЕ сносим: external_id уже зафиксирован в БД вызывающим кодом.
        """
        server = self._wait_running(external_id)
        ipv4 = ((server.get("public_net") or {}).get("ipv4") or {}).get("ip")
        if not ipv4:
            raise DriverError(f"Hetzner server {external_id} has no public IPv4")
        return ipv4, _monthly_price(server), server

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
        """Блокирующий заказ (order + ожидание running/IP). Используется путями
        без ``order_server``-сплита (autoscale-тик). external_id наружу при провале
        не попадает, поэтому на любом сбое после успешного POST сервер best-effort
        сносится (orphan-guard) — иначе оплаченный сервер повисает у хостера
        незамеченным, а ретрай спавна плодит второй."""
        server_id, root_password = self.order_server(
            name=name, region=region, plan=plan, image=image,
            ssh_key_ids=ssh_key_ids, user_data=user_data,
        )
        try:
            ipv4, price, server = self.wait_for_ipv4(server_id)
        except DriverError:
            logger.error(
                "hetzner: сервер %s (%s) создан, но не дождались running/IPv4 — "
                "сношу оплаченный заказ (orphan-guard)", server_id, name,
            )
            self._safe_destroy(server_id)
            raise

        public_net = server.get("public_net") or {}
        ipv6 = (public_net.get("ipv6") or {}).get("ip")
        return CloudServer(
            external_id=str(server_id),
            ipv4=ipv4,
            ipv6=ipv6,
            region=region,
            plan=plan,
            monthly_cost=price,
            root_password=root_password or None,
            raw=server,
        )

    def destroy_server(self, external_id: str) -> None:
        self._delete(f"/servers/{external_id}")

    def list_regions(self) -> list[str]:
        data = self._get("/locations")
        return [loc["name"] for loc in data.get("locations", [])]

    # ---------- helpers ----------

    def _safe_destroy(self, server_id: int | str) -> None:
        """Best-effort снос (orphan-guard). Даже если не вышло — id уже в
        ERROR-логе выше, оператор снесёт вручную в панели Hetzner."""
        try:
            self.destroy_server(str(server_id))
        except DriverError:
            logger.exception(
                "hetzner orphan-guard: не смог снести сервер %s — снеси ВРУЧНУЮ "
                "в панели Hetzner", server_id,
            )

    def _wait_running(self, server_id: int | str) -> dict:
        deadline = time.time() + POLL_TIMEOUT
        last: dict = {}
        last_err: DriverError | None = None
        while time.time() < deadline:
            try:
                data = self._get(f"/servers/{server_id}")
            except DriverError as exc:
                # Транзиентный сбой поллинга (сеть/429/5xx) не должен обрывать
                # ожидание: сервер уже создан и оплачивается — ждём до дедлайна.
                last_err = exc
                time.sleep(POLL_INTERVAL)
                continue
            last = data.get("server") or {}
            if last.get("status") == "running":
                return last
            time.sleep(POLL_INTERVAL)
        suffix = f"; last poll error: {last_err}" if last_err else ""
        raise DriverError(
            f"Hetzner server {server_id} did not reach running state within {POLL_TIMEOUT}s; "
            f"last status: {last.get('status')}{suffix}"
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
