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

    def order_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,  # 4vps не принимает SSH-ключ
        user_data: str | None = None,          # 4vps не поддерживает user_data
    ) -> tuple[str, str]:
        """Быстрый шаг заказа (POST /action/buyServer) БЕЗ ожидания IP.

        Возвращает ``(external_id, root_password)`` за ~секунды. IP появляется
        позже — его берёт :meth:`wait_for_ipv4` (поллинг /myservers до 600s).

        Вынесено из :meth:`create_server`, чтобы HTTP-роут /nodes/spawn мог
        зафиксировать заказанный сервер в БД СРАЗУ (без блокировки запроса на
        долгом поллинге → nginx proxy_read_timeout 60s → CF 502) и не плодить
        осиротевшие оплаченные VPS. См. ``node_spawner.spawn_node_async``.
        """
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
        return server_id, root_password

    def wait_for_ipv4(self, external_id: str) -> tuple[str, float | None, dict]:
        """Дождаться, пока заказанный сервер получит IP и станет active.

        Возвращает ``(ipv4, monthly_cost, raw)``. Бросает :class:`DriverError`,
        если IP не появился за ``_POLL_TIMEOUT`` (600s). Блокирует — вызывается
        в фоне (``node_spawner._finalize_spawn``), не в HTTP-запросе.
        """
        ipv4, status, srv = self._wait_active(external_id)
        if not ipv4:
            raise DriverError(
                f"4vps server {external_id} got no IPv4 within {_POLL_TIMEOUT}s "
                f"(last status={status})"
            )
        return ipv4, _to_float((srv or {}).get("price")), (srv or {})

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
        """Блокирующий заказ (order + ожидание IP). Используется autoscale-тиком
        (RQ-воркер, без HTTP-таймаута). HTTP-роут идёт через ``order_server`` +
        ``wait_for_ipv4`` (см. ``node_spawner.spawn_node_async``)."""
        server_id, root_password = self.order_server(
            name=name, region=region, plan=plan, image=image,
            ssh_key_ids=ssh_key_ids, user_data=user_data,
        )
        # buyServer не отдаёт IP — поллим /myservers, пока сервер не active с IP.
        ipv4, monthly_cost, srv = self.wait_for_ipv4(server_id)
        return CloudServer(
            external_id=server_id,
            ipv4=ipv4,
            ipv6=None,
            region=region,
            plan=plan,
            monthly_cost=monthly_cost,
            root_password=root_password,
            raw=srv,
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
        (в копейках; price тарифов — в рублях). Возвращаем сырое число.

        НЕ через _to_float: тот схлопывает 0 в None (``float(v) or None``),
        а баланс 0.00 — валидное значение. При None cloud-billing воркер
        пропускает провайдера — low-balance алерт молчал бы ровно когда
        деньги кончились (та же ловушка описана в vdsina.get_balance)."""
        data = self._call("GET", "/userBalance", {})
        try:
            return float((data or {}).get("userBalance"))
        except (TypeError, ValueError):
            return None

    def set_autoprolong(self, external_id: str, enabled: bool = True) -> bool:
        """Включить/выключить авто-продление. 4vps `/action/autoprolong` —
        ТОГГЛ (возвращает НОВОЕ состояние, data: true/false), а не установка
        значения. Слепой тоггл опасен: если состояние УЖЕ желаемое, первый
        вызов его инвертирует, и корректность держится только на втором вызове;
        падение второго (сеть/429) молча оставляет автопродление выключенным, а
        вызывающий код глотает ошибку (node_spawner) → нода сносится хостером в
        конце периода. Поэтому сначала читаем текущее состояние из /myservers и
        тоггаем ТОЛЬКО при несовпадении. Возвращает итоговое состояние."""
        current = self._read_autoprolong(external_id)
        if current is not None:
            if current == enabled:
                # уже нужное состояние — тоггл не трогаем (он бы инвертировал).
                return current
            # знаем исходное → ровно один целенаправленный тоггл + проверка.
            data = self._call("POST", "/action/autoprolong", {"serverid": external_id})
            state = bool(data)
            if state != enabled:
                raise DriverError(
                    f"4vps autoprolong {external_id}: toggle landed on {state}, "
                    f"expected {enabled}"
                )
            return state
        # состояние прочитать не удалось (сервер/поле не найдены) — осторожный
        # слепой тоггл до 2 раз, но при неуспехе БРОСАЕМ DriverError, а не тихо
        # возвращаем инвертированное состояние.
        state = False
        for _ in range(2):
            data = self._call("POST", "/action/autoprolong", {"serverid": external_id})
            state = bool(data)
            if state == enabled:
                return state
        raise DriverError(
            f"4vps autoprolong {external_id}: could not reach {enabled} "
            f"after 2 toggles (last={state})"
        )

    def _read_autoprolong(self, external_id: str) -> bool | None:
        """Прочитать текущее состояние автопродления сервера из /myservers.

        Возвращает True/False, либо None если сервер не найден / поле
        отсутствует / запрос упал (тогда вызывающий делает осторожный слепой
        тоггл). Точное написание поля в /myservers у 4vps не задокументировано
        (док перечисляет ``{id,name,ipv4,status,tid,dc,price,…}``); эндпоинт —
        ``/action/autoprolong``, поэтому ищем ключ, содержащий ``prolong``
        (регистр игнорируем). Если реальный ответ API называет поле иначе —
        сверить и поправить подстроку здесь."""
        try:
            data = self._call("GET", "/myservers", {})
        except DriverError:
            return None
        for srv in (data or {}).get("serverlist") or []:
            if not isinstance(srv, dict):
                continue
            if str(srv.get("id") or "") != str(external_id):
                continue
            for key, val in srv.items():
                if "prolong" in str(key).lower():
                    return _truthy(val)
            return None
        return None

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
        """Тарифы 4vps = ПРЕСЕТЫ (cx01/cx11/…), и они лежат НЕ на верхнем уровне.

        Реальная структура ``getTarifList``::

            tarifList = {
              "<locId>": {
                 "clusterInfo": {"id": locId, "dc_name", "flag", "presets": [13,14,…]},
                 "presets": {"13": {"id":13,"name":"cx01","cpu_number":1,
                                    "ram_mib":1024,"rom":10240,
                                    "commentParsed":{"price":590}}, …}
              }, …
            }

        Ключи ``tarifList`` — это ЛОКАЦИИ (= id из ``getDcList`` = параметр
        ``datacenter`` для buyServer), а тарифы — это ``presets`` внутри. Раньше
        мы ошибочно отдавали ключи локаций как тарифы → форма слала
        ``tarif=<locId>`` (невалидно), 4vps резал заказ. Возвращаем
        объединённый каталог пресетов (dedupe по id) — это валидные значения
        ``tarif``. Каталог общий по всем локациям; если конкретный пресет в
        выбранной локации недоступен (напр. у ОАЭ нет cx01/cx11), buyServer
        вернёт понятную ошибку — её теперь видно в форме (роут отдаёт 400)."""
        data = self._call("GET", "/getTarifList", {})
        tarif_list = (data or {}).get("tarifList") or {}
        by_id: dict[int, dict] = {}
        for loc in tarif_list.values() if isinstance(tarif_list, dict) else []:
            presets = (loc or {}).get("presets") if isinstance(loc, dict) else None
            if not isinstance(presets, dict):
                continue
            for pid, pr in presets.items():
                if not isinstance(pr, dict):
                    continue
                _id = pr.get("id") or _to_int(pid)
                if _id is None or _id in by_id:
                    continue
                cp = pr.get("commentParsed") or {}
                by_id[_id] = {
                    "id": _id,
                    "name": pr.get("name") or str(_id),
                    "price": _to_float(pr.get("price") or cp.get("price")),
                    "cpu": pr.get("cpu_number"),
                    "ram_mib": pr.get("ram_mib"),
                    "rom": pr.get("rom"),
                    # образы зависят от пары (пресет, локация) → list_images;
                    # форма берёт общий каталог из offerings.images.
                    "images": [],
                }
        return [by_id[k] for k in sorted(by_id)]

    def list_images(
        self, tarif: str | int | None = None, dc: str | int | None = None
    ) -> list[dict]:
        """GET /api/getImages/{PRESET_ID}/{LOCATION_ID} → data.images {id: name}.

        У 4vps образы зависят от пары (пресет, локация); ``PRESET_ID`` — это
        ``tarif`` (id пресета, напр. 13=cx01), ``LOCATION_ID`` — ``datacenter``
        (id локации, напр. 10=Финляндия). Без аргументов (offerings) каталог ОС
        у 4vps фактически общий — дёргаем getImages для ПЕРВОЙ валидной пары
        (пресет, локация) из getTarifList и отдаём её. Так заполняется
        ``offerings.images`` и форма показывает список ОС."""
        if tarif is not None and dc is not None:
            data = self._call("GET", f"/getImages/{tarif}/{dc}", {})
            images = (data or {}).get("images") or {}
            if isinstance(images, dict):
                return [{"id": _to_int(k), "name": v} for k, v in images.items()]
            return []
        # no-arg (offerings): репрезентативный каталог из первой валидной пары.
        try:
            tl = (self._call("GET", "/getTarifList", {}) or {}).get("tarifList") or {}
        except DriverError:
            return []
        for loc_id, loc in tl.items() if isinstance(tl, dict) else []:
            ci = (loc or {}).get("clusterInfo") if isinstance(loc, dict) else None
            presets = (ci or {}).get("presets") or []
            if not presets:
                continue
            try:
                data = self._call("GET", f"/getImages/{presets[0]}/{loc_id}", {})
            except DriverError:
                continue
            images = (data or {}).get("images") or {}
            if isinstance(images, dict) and images:
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


def _truthy(v) -> bool:
    """Нормализовать «включённость» из ответа 4vps: строки ``"1"/"true"/"on"``
    и числа/bool. Всё прочее (в т.ч. ``"0"/"false"/None``) → False."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(v)
