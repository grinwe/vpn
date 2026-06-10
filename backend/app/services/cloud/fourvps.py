"""4vps.su (он же 4vds) cloud driver.

Mirrors the structure of :mod:`aeza` / :mod:`hetzner`. Talks raw HTTP to the
4vps.su API. 4vps использует «конверт» ответа ``{"error": bool, "data": ...,
"errorMessage": str}`` и требует в КАЖДОМ запросе ключ (``apikey``) и
``panel_id`` — мы храним их в одном ``CloudProvider.api_token_enc`` как
``panel_id:apikey`` (Fernet), см. :func:`_split_token`.

API: https://4vps.su/page/api

NB: фетчер не достаёт доку 4vps (SPA/блок), поэтому ТОЧНЫЕ пути info-методов и
имена параметров ``order`` вынесены в единый блок ``_API`` ниже и помечены
``# VERIFY`` — их надо сверить с живой докой (см.
docs/operations/hoster_api_epic.md, «Чек-лист спека»). Достоверно известен
только ``reinstall``. Структура драйвера (session/конверт/поллинг/маппинг в
CloudServer) от этих деталей не зависит и корректна.
"""
from __future__ import annotations

import logging
import secrets
import time

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

# ── Блок _API: ЕДИНСТВЕННОЕ место с wire-деталями 4vps ───────────────────────
_BASE = "https://4vps.su/api"
_TIMEOUT = 30
_POLL_TIMEOUT = 300
_POLL_INTERVAL = 6

# Имена auth-параметров (шлём в КАЖДОМ запросе). VERIFY: точные имена по доке.
_AUTH_KEY_PARAM = "apikey"
_AUTH_PANEL_PARAM = "panel_id"

# Логический метод -> (HTTP, path). VERIFY: префикс info-методов (info/ vs
# action/ vs корень) и имена методов. `reinstall` известен точно.
_METHODS: dict[str, tuple[str, str]] = {
    "balance": ("GET", "/info/getBalance"),       # VERIFY
    "datacenters": ("GET", "/info/getDatacenters"),  # VERIFY
    "tariffs": ("GET", "/info/getTariffs"),        # VERIFY
    "images": ("GET", "/info/getImages"),          # VERIFY
    "servers": ("GET", "/info/getServers"),        # VERIFY
    "order": ("POST", "/action/order"),            # VERIFY
    "reinstall": ("POST", "/action/reinstall"),    # известно точно
    "delete": ("POST", "/action/delete"),          # VERIFY
}

# Имена параметров заказа. VERIFY по доке (нужен ли SSH-ключ / домен?).
_ORDER_PARAM = {
    "tariff": "tariff",        # VERIFY
    "datacenter": "datacenter",  # VERIFY
    "ostempl": "ostempl",      # из #getImages
    "password": "password",    # min 6
    "period": "period",        # [720,2160,4320,8640] = 1/3/6/12 мес
}
_DEFAULT_PERIOD = 720  # 1 месяц
# ─────────────────────────────────────────────────────────────────────────────


def _split_token(token: str) -> tuple[str, str]:
    """``"panel_id:apikey"`` → ``(panel_id, apikey)``. Без ``:`` → весь токен
    трактуется как apikey (panel_id пуст)."""
    token = (token or "").strip()
    if ":" in token:
        panel_id, apikey = token.split(":", 1)
        return panel_id.strip(), apikey.strip()
    return "", token


def _gen_password() -> str:
    """Сильный рут-пароль для order/reinstall (4vps требует пароль). Мы ходим на
    ноду по SSH-КЛЮЧУ (provisioning_key), пароль здесь — для панели/требования
    API. VERIFY: инжектит ли 4vps наш SSH-ключ при заказе, или нужен
    password-auth/cloud-init для первого коннекта ansible."""
    return secrets.token_urlsafe(16)


class FourVpsDriver:
    kind = "4vps"

    def __init__(self, token: str) -> None:
        self._panel_id, self._apikey = _split_token(token)
        if not self._apikey:
            raise DriverError("4vps provider token missing apikey")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

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
        password = _gen_password()
        params = {
            _ORDER_PARAM["tariff"]: plan,
            _ORDER_PARAM["datacenter"]: region,
            _ORDER_PARAM["ostempl"]: image,
            _ORDER_PARAM["password"]: password,
            _ORDER_PARAM["period"]: _DEFAULT_PERIOD,
        }
        data = self._call("order", params)
        # VERIFY: где serverid и IP в ответе. По поиску ответ содержит
        # serverInfo / dcInfo / ipPrice / ipList. Берём максимально терпимо.
        info = data if isinstance(data, dict) else {}
        server_info = info.get("serverInfo") or info
        server_id = str(
            server_info.get("serverid")
            or server_info.get("id")
            or info.get("serverid")
            or ""
        )
        if not server_id:
            raise DriverError(f"4vps order did not return serverid: {data}")

        ipv4 = self._extract_ip(info) or self._wait_ip(server_id)
        if not ipv4:
            raise DriverError(
                f"4vps server {server_id} has no IPv4 after {_POLL_TIMEOUT}s"
            )
        price = _to_float(
            server_info.get("price")
            or server_info.get("cost")
            or info.get("ipPrice")
        )
        return CloudServer(
            external_id=server_id,
            ipv4=ipv4,
            ipv6=None,
            region=region,
            plan=plan,
            monthly_cost=price,
            raw=info,
        )

    def reinstall_server(
        self, external_id: str, image: str, *, password: str | None = None
    ) -> None:
        """Переустановка ОС. Известно точно:
        ``POST /api/action/reinstall {serverid, ostempl, password(≥6)}`` →
        ``{"error":false,"data":"ok"}``."""
        self._call(
            "reinstall",
            {
                "serverid": external_id,
                "ostempl": image,
                "password": password or _gen_password(),
            },
        )

    def destroy_server(self, external_id: str) -> None:
        # VERIFY: путь/параметры delete.
        self._call("delete", {"serverid": external_id})

    def list_regions(self) -> list[str]:
        """Protocol-метод: id датацентров строками."""
        return [str(d.get("id") or d.get("datacenter") or "") for d in self.list_datacenters() if d]

    # ---- offerings (для admin-формы заказа) ----

    def list_datacenters(self) -> list[dict]:
        return _as_items(self._call("datacenters", {}))

    def list_plans(self) -> list[dict]:
        return _as_items(self._call("tariffs", {}))

    def list_images(self) -> list[dict]:
        return _as_items(self._call("images", {}))

    # ---------- helpers ----------

    def _extract_ip(self, info: dict) -> str:
        ip_list = info.get("ipList") or info.get("iplist") or []
        if isinstance(ip_list, list) and ip_list:
            first = ip_list[0]
            if isinstance(first, dict):
                return str(first.get("ip") or first.get("address") or "") or ""
            return str(first or "")
        si = info.get("serverInfo") or {}
        return str(si.get("ip") or si.get("ipv4") or info.get("ip") or "") or ""

    def _wait_ip(self, server_id: str) -> str:
        """Поллим #getServers, пока у сервера не появится IP. VERIFY: форма
        ответа getServers и поле IP."""
        deadline = time.time() + _POLL_TIMEOUT
        while time.time() < deadline:
            try:
                data = self._call("servers", {})
            except DriverError:
                time.sleep(_POLL_INTERVAL)
                continue
            for srv in _as_items(data):
                if str(srv.get("serverid") or srv.get("id") or "") == server_id:
                    ip = str(srv.get("ip") or srv.get("ipv4") or "")
                    if ip:
                        return ip
            time.sleep(_POLL_INTERVAL)
        return ""

    def _call(self, method_key: str, params: dict) -> dict | list:
        http, path = _METHODS[method_key]
        auth = {_AUTH_KEY_PARAM: self._apikey}
        if self._panel_id:
            auth[_AUTH_PANEL_PARAM] = self._panel_id
        merged = {**auth, **params}
        try:
            if http == "GET":
                resp = self._session.get(
                    f"{_BASE}{path}", params=merged, timeout=_TIMEOUT
                )
            else:
                resp = self._session.post(
                    f"{_BASE}{path}", data=merged, timeout=_TIMEOUT
                )
        except requests.RequestException as exc:
            raise DriverError(f"4vps {method_key} request failed: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise DriverError(
                f"4vps {method_key} -> {resp.status_code}: {payload}"
            )
        # 4vps-конверт: {"error": bool, "data": ..., "errorMessage": str}
        if isinstance(payload, dict) and payload.get("error"):
            raise DriverError(
                f"4vps {method_key} error: {payload.get('errorMessage') or payload}"
            )
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload


def _as_items(data) -> list[dict]:
    """Нормализует разнородный ответ (list / {items:[...]} / dict) в list[dict]."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return [d for d in data["items"] if isinstance(d, dict)]
        # dict вида {id: {...}} → значения
        vals = [v for v in data.values() if isinstance(v, dict)]
        if vals:
            return vals
    return []


def _to_float(v) -> float | None:
    try:
        return float(v) or None
    except (TypeError, ValueError):
        return None
