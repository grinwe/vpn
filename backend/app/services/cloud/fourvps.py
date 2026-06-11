"""4vps.su (он же 4vds) cloud driver.

Mirrors the structure of :mod:`aeza` / :mod:`hetzner`. Talks raw HTTP to the
4vps.su API (https://4vps.su/page/api). Спека сверена по официальной доке.

Особенности 4vps:
* Auth: ``Authorization: Bearer <apikey>`` ХЕДЕР + ``panel_id`` параметром в
  запросах, где есть взаимодействие с панелью (locations/tariffs/order). Мы
  храним оба значения в одном ``CloudProvider.api_token_enc`` как
  ``panel_id:apikey`` (Fernet), см. :func:`_split_token`.
* Конверт ответа: ``{"error": bool, "data": ..., "errorMessage": str|dict}``.
* ``buyServer`` возвращает только ``{serverid, password}`` — БЕЗ IP. IP и статус
  берём поллингом ``/myservers`` (``serverlist[].ipv4`` / ``.status``).
* ``buyServer`` НЕ принимает SSH-ключ — свежий сервер поднимается с root +
  сгенерённым паролем (его возвращает API). Пароль прокидывается в
  ``CloudServer.root_password`` и сохраняется на ноде (provider_root_password_enc)
  для SSH-bootstrap'а (ansible ходит по ключу — нужен first-connect по паролю
  + установка ключа; см. docs/operations/hoster_api_epic.md, Фаза 1.5).
"""
from __future__ import annotations

import logging
import secrets
import time

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

_BASE = "https://4vps.su/api"
_TIMEOUT = 30
_POLL_TIMEOUT = 600
_POLL_INTERVAL = 8
# period аренды: [720, 2160, 4320, 8640] = 1/3/6/12 мес.
_DEFAULT_PERIOD = 720


def _split_token(token: str) -> tuple[str, str]:
    """``"panel_id:apikey"`` → ``(panel_id, apikey)``. Без ``:`` → весь токен =
    apikey (panel_id пуст)."""
    token = (token or "").strip()
    if ":" in token:
        panel_id, apikey = token.split(":", 1)
        return panel_id.strip(), apikey.strip()
    return "", token


def _gen_password() -> str:
    """Рут-пароль для buyServer/reinstall (4vps требует/возвращает пароль).
    Берём только буквы+цифры — 4vps валидирует пароль и спецсимволы иногда
    отклоняет; длина с запасом > минимума (6)."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


class FourVpsDriver:
    kind = "4vps"

    def __init__(self, token: str) -> None:
        self._panel_id, self._apikey = _split_token(token)
        if not self._apikey:
            raise DriverError("4vps provider token missing apikey")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._apikey}",
                "Accept": "application/json",
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
        ssh_key_ids: list[str] | None = None,  # 4vps не принимает SSH-ключ
        user_data: str | None = None,          # 4vps не поддерживает user_data
    ) -> CloudServer:
        password = _gen_password()
        # POST /api/action/buyServer — tarif/datacenter/ostempl/name + period.
        data = self._call(
            "POST",
            "/action/buyServer",
            {
                "tarif": plan,
                "datacenter": region,
                "ostempl": image,
                "name": name,
                "password": password,
                "period": _DEFAULT_PERIOD,
            },
        )
        server_id = str((data or {}).get("serverid") or "")
        if not server_id:
            raise DriverError(f"4vps buyServer did not return serverid: {data}")
        # 4vps сам генерит/возвращает пароль — берём его, иначе наш.
        root_password = str((data or {}).get("password") or password)

        # buyServer не отдаёт IP — поллим /myservers, пока сервер не active с IP.
        ipv4, status, srv = self._wait_active(server_id)
        if not ipv4:
            raise DriverError(
                f"4vps server {server_id} got no IPv4 within {_POLL_TIMEOUT}s "
                f"(last status={status})"
            )
        return CloudServer(
            external_id=server_id,
            ipv4=ipv4,
            ipv6=None,
            region=region,
            plan=plan,
            monthly_cost=_to_float((srv or {}).get("price")),
            root_password=root_password,
            raw=srv or data,
        )

    def reinstall_server(
        self, external_id: str, image: str, *, password: str | None = None
    ) -> None:
        """POST /api/action/reinstall {serverid, ostempl, password(≥6)}."""
        self._call(
            "POST",
            "/action/reinstall",
            {
                "serverid": external_id,
                "ostempl": image,
                "password": password or _gen_password(),
            },
        )

    def destroy_server(self, external_id: str) -> None:
        """POST /api/action/deleteServer {serverid}."""
        self._call("POST", "/action/deleteServer", {"serverid": external_id})

    def reboot_server(self, external_id: str) -> None:
        self._call("POST", "/action/reboot", {"serverid": external_id})

    def renew_server(self, external_id: str) -> None:
        """POST /api/action/continueServer — продлить на месяц (списывает с баланса)."""
        self._call("POST", "/action/continueServer", {"serverid": external_id})

    def get_balance(self) -> float | None:
        """GET /api/userBalance → data.userBalance. Единицы — как у 4vps
        (в копейках; price тарифов — в рублях). Возвращаем сырое число."""
        data = self._call("GET", "/userBalance", {})
        return _to_float((data or {}).get("userBalance"))

    def set_autoprolong(self, external_id: str, enabled: bool = True) -> bool:
        """Включить/выключить авто-продление. 4vps `/action/autoprolong` —
        ТОГГЛ: возвращает НОВОЕ состояние (data: true/false). Дёргаем и, если
        состояние не совпало с желаемым, дёргаем второй раз. Возвращает
        итоговое состояние."""
        state = False
        for _ in range(2):
            data = self._call("POST", "/action/autoprolong", {"serverid": external_id})
            state = bool(data)
            if state == enabled:
                return state
        return state

    def list_regions(self) -> list[str]:
        """Protocol-метод: id датацентров строками."""
        return [str(d["id"]) for d in self.list_datacenters() if d.get("id")]

    # ---- offerings (для admin-формы заказа) ----

    def list_datacenters(self) -> list[dict]:
        """GET /api/getDcList → data.dcList {id: {dc_name, cpu_name, flag, ...}}."""
        data = self._call("GET", "/getDcList", {})
        dc_list = (data or {}).get("dcList") or {}
        out: list[dict] = []
        for key, dc in dc_list.items() if isinstance(dc_list, dict) else []:
            if not isinstance(dc, dict):
                continue
            out.append(
                {
                    "id": dc.get("id") or _to_int(key),
                    "name": dc.get("dc_name") or dc.get("t_name") or str(key),
                    "flag": dc.get("flag"),
                    "cpu_name": dc.get("cpu_name"),
                }
            )
        return out

    def list_plans(self) -> list[dict]:
        """GET /api/getTarifList → data.tarifList. Каждый тариф несёт osList +
        osNames, поэтому образы для UI берутся прямо отсюда (см. list_images —
        у 4vps образы зависят от тарифа+ДЦ)."""
        data = self._call("GET", "/getTarifList", {})
        tarif_list = (data or {}).get("tarifList") or {}
        out: list[dict] = []
        for key, t in tarif_list.items() if isinstance(tarif_list, dict) else []:
            if not isinstance(t, dict):
                continue
            os_names = t.get("osNames") or {}
            out.append(
                {
                    "id": t.get("id") or _to_int(key),
                    "name": t.get("nameFull") or t.get("name") or str(key),
                    "price": _to_float(t.get("price")),
                    "cpu": t.get("cpu_number"),
                    "ram_mib": t.get("ram_mib"),
                    "rom": t.get("rom"),
                    # образы этого тарифа: [{id, name}] из osNames
                    "images": [
                        {"id": _to_int(oid), "name": oname}
                        for oid, oname in os_names.items()
                    ]
                    if isinstance(os_names, dict)
                    else [],
                }
            )
        return out

    def list_images(self, tarif: str | int | None = None, dc: str | int | None = None) -> list[dict]:
        """GET /api/getImages/{TARIF_ID}/{DC_ID} → data.images {id: name}.
        У 4vps образы зависят от тарифа+ДЦ. Без них вернём []: образы для UI
        берутся из list_plans()[].images (osNames). offerings зовёт без
        аргументов — отдаём [], плюс осведомляем в эпике."""
        if tarif is None or dc is None:
            return []
        data = self._call("GET", f"/getImages/{tarif}/{dc}", {})
        images = (data or {}).get("images") or {}
        if isinstance(images, dict):
            return [{"id": _to_int(k), "name": v} for k, v in images.items()]
        return []

    # ---------- helpers ----------

    def _wait_active(self, server_id: str) -> tuple[str, str, dict]:
        """Поллим /myservers, пока сервер не получит IP и status=active.
        Возвращаем (ipv4, last_status, server_dict)."""
        deadline = time.time() + _POLL_TIMEOUT
        last_status = ""
        last_srv: dict = {}
        while time.time() < deadline:
            try:
                data = self._call("GET", "/myservers", {})
            except DriverError:
                time.sleep(_POLL_INTERVAL)
                continue
            for srv in (data or {}).get("serverlist") or []:
                if str(srv.get("id") or "") == server_id:
                    last_srv = srv
                    last_status = str(srv.get("status") or "")
                    ipv4 = str(srv.get("ipv4") or "")
                    if ipv4 and last_status.lower() == "active":
                        return ipv4, last_status, srv
                    # IP уже есть, но статус ещё не active — подождём ещё.
                    break
            time.sleep(_POLL_INTERVAL)
        # таймаут — отдадим что есть (IP мог появиться без active)
        return str(last_srv.get("ipv4") or ""), last_status, last_srv

    def _call(self, method: str, path: str, params: dict) -> dict | list:
        # panel_id шлём всегда (где взаимодействие с панелью — он нужен; где не
        # нужен — безвреден). apikey — в Authorization-хедере (см. __init__).
        merged = dict(params)
        if self._panel_id and "panel_id" not in merged:
            merged["panel_id"] = self._panel_id
        try:
            if method == "GET":
                resp = self._session.get(
                    f"{_BASE}{path}", params=merged, timeout=_TIMEOUT
                )
            else:
                resp = self._session.post(
                    f"{_BASE}{path}", data=merged, timeout=_TIMEOUT
                )
        except requests.RequestException as exc:
            raise DriverError(f"4vps {method} {path} failed: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise DriverError(f"4vps {method} {path} -> {resp.status_code}: {payload}")
        # Конверт {"error": bool, "data": ..., "errorMessage": str|dict}
        if isinstance(payload, dict) and payload.get("error"):
            msg = payload.get("errorMessage")
            if isinstance(msg, dict):  # напр. {redirect, message}
                msg = msg.get("message") or msg
            raise DriverError(f"4vps {path} error: {msg or payload}")
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload


def _to_float(v) -> float | None:
    try:
        return float(v) or None
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
