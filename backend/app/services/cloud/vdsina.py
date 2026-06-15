"""VDSina cloud driver.

Mirrors :mod:`fourvps` / :mod:`hetzner`. Talks raw HTTP to the VDSina public
API. ВАЖНО: vdsina.com и vdsina.ru — РАЗНЫЕ API-инсталляции, токен доменно-
специфичен; дефолт ``.com`` (наш аккаунт там), override env ``VDSINA_API_BASE``.
Спека сверена по двум независимым community-
клиентам (scinfra-pro/terraform-provider-vdsina, hugmouse/go-vdsina) + офиц. PDF
(vdsina.ru/files/docs/public_api.pdf), 2026-06-12.

Особенности VDSina:
* Auth: ``Authorization: <token>`` — ГОЛЫЙ токен, БЕЗ "Bearer" (так в офиц. доке;
  у terraform-провайдера ошибочно Bearer — НЕ копировать).
* Конверт: ``{"status":"ok"|"error","status_msg":str,"data":...}``. ``_call``
  поднимает ``DriverError`` на ``status != "ok"``; offerings-методы
  (``list_*``) гасят это в пустой список (форма дегрейдит, как у 4vps).
* Заказ ``POST /v1/server`` ПРИНИМАЕТ ``ssh-key`` (id ключа) → бокс поднимается
  С НАШИМ ключом, без парольного bootstrap (как hetzner). Если у провайдера ключ
  не задан — АВТО-регистрируем наш provisioning-pubkey на VDSina
  (``_ensure_key_id``) и инжектим его. Никогда не уходим в «без ключа»: пароль
  VDSina генерит сам и эндпоинт пароля бывает не сразу готов — это уже ломало
  bootstrap на оплаченном боксе, поэтому путь без ключа выпилен.
* IP появляется только после ``status==active``; поллим ``GET /v1/server/{id}``
  (``data.ip`` — МАССИВ ``[{ip,type}]``). Поля заказа дефисные: ``server-plan``,
  ``ssh-key``. id (datacenter/server-plan/template) — ЧИСЛОВЫЕ (не строковые
  слаги): валидируем до оплаты, иначе fast-fail.

``ssh_key_ids`` конструктора (из ``CloudProvider.ssh_key_ids``) нужен reinstall'у
(его сигнатура ключи не получает) — чтобы при ротации переинжектить наш ключ.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

# VDSina .ru и .com — РАЗНЫЕ инсталляции публичного API; постоянный токен из
# панели валиден ТОЛЬКО на своём домене (токен из cp.vdsina.com на .ru даёт
# 401 "Incorrect token", проверено 2026-06-15). Наш аккаунт на .com → дефолт
# .com. Override через env VDSINA_API_BASE, если появится .ru-аккаунт.
_BASE = os.getenv("VDSINA_API_BASE", "https://userapi.vdsina.com/v1").rstrip("/")
_TIMEOUT = 30
_POLL_TIMEOUT = 600
_POLL_INTERVAL = 8
# Имя, под которым авто-регистрируем наш provisioning-pubkey в VDSina (идемпотентно).
_KEY_NAME = "vpn-provisioning"


class VdsinaDriver:
    kind = "vdsina"

    def __init__(
        self,
        token: str,
        ssh_key_ids: list | None = None,
        base: str | None = None,
    ) -> None:
        token = (token or "").strip()
        if not token:
            raise DriverError("VDSina provider has no API token configured")
        # API-инсталляция: .com (дефолт _BASE) или .ru (kind=vdsina_ru, base из
        # get_driver). Токен валиден только на своём домене.
        self._base = (base or _BASE).rstrip("/")
        # явный ключ провайдера; иначе авто-регистрируем (см. _ensure_key_id).
        self._ssh_key_id = _first_int(ssh_key_ids)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": token,  # ГОЛЫЙ токен, без "Bearer"
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ---------- order / spawn ----------

    def order_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,
        user_data: str | None = None,  # VDSina не поддерживает user_data
    ) -> tuple[str, str]:
        """Быстрый заказ (POST /v1/server) БЕЗ ожидания IP → ``(external_id, "")``.

        Пароль всегда пуст: бокс поднимается с НАШИМ ssh-ключом (явным из
        провайдера или авто-зарегистрированным), ansible ходит по ключу — как
        hetzner. id (datacenter/server-plan/template) валидируем ДО оплаты.
        """
        dc, sp, tpl = _to_int(region), _to_int(plan), _to_int(image)
        missing = [
            n for n, v in (("datacenter", dc), ("server-plan", sp), ("template", tpl))
            if v is None
        ]
        if missing:
            raise DriverError(
                f"VDSina: нечисловые id [{', '.join(missing)}] "
                f"(region={region!r}, plan={plan!r}, image={image!r}). Бери id из "
                f"offerings (/datacenter, /server-plan, /template) — это НЕ слаги."
            )
        key_id = _first_int(ssh_key_ids)
        if key_id is None:
            key_id = self._ensure_key_id()
        if key_id is None:
            raise DriverError(
                "VDSina: ssh-ключ не задан и авто-регистрация provisioning-pubkey "
                "не удалась (ANSIBLE_PRIVATE_KEY_FILE не задан?). Без ключа бокс "
                "будет недоступен — задай ssh_key_ids у провайдера."
            )
        body: dict[str, Any] = {
            "datacenter": dc,
            "server-plan": sp,
            "template": tpl,
            "name": name,
            "host": name,
            "ssh-key": key_id,
        }
        data = self._call("POST", "/server", body)
        server_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        if not server_id:
            raise DriverError(f"VDSina /server did not return id: {data}")
        return server_id, ""  # ключ инжектится → пароль не нужен

    def wait_for_ipv4(self, external_id: str) -> tuple[str, float | None, dict]:
        """Дождаться IP+active заказанного сервера → ``(ipv4, monthly_cost, raw)``.
        Блокирует — вызывается в фоне (``node_spawner._finalize_spawn``)."""
        ipv4, status, srv = self._wait_active(external_id)
        if not ipv4:
            raise DriverError(
                f"VDSina server {external_id} got no IPv4 within {_POLL_TIMEOUT}s "
                f"(last status={status})"
            )
        return ipv4, None, srv

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
        """Блокирующий заказ (order + ожидание IP). Используется autoscale-тиком
        (RQ-воркер, без HTTP-таймаута). HTTP-роут /nodes/spawn идёт через
        ``order_server`` + ``wait_for_ipv4`` (см. node_spawner.spawn_node_async)."""
        server_id, _ = self.order_server(
            name=name, region=region, plan=plan, image=image,
            ssh_key_ids=ssh_key_ids, user_data=user_data,
        )
        ipv4, monthly_cost, srv = self.wait_for_ipv4(server_id)
        return CloudServer(
            external_id=server_id,
            ipv4=ipv4,
            ipv6=None,
            region=region,
            plan=plan,
            monthly_cost=monthly_cost,
            root_password=None,  # бокс с ключом → пароль не нужен (как hetzner)
            raw=srv,
        )

    # ---------- lifecycle ----------

    def reinstall_server(
        self, external_id: str, image: str, *, password: str | None = None
    ) -> None:
        """PUT /v1/server.reinstall/{id} {template, ssh-key}. VDSina САМ генерит
        новый рут-пароль (наш ``password`` он не принимает), поэтому ОБЯЗАТЕЛЬНО
        переинжектим ssh-ключ, чтобы ansible продолжил ходить по ключу (иначе
        нода после ротации недоступна)."""
        tpl = _to_int(image)
        if tpl is None:
            raise DriverError(
                f"VDSina reinstall: числовой template id ожидается, получено {image!r}"
            )
        body: dict[str, Any] = {"template": tpl}
        key_id = self._ensure_key_id()
        if key_id is not None:
            body["ssh-key"] = key_id
        else:
            logger.warning(
                "VDSina reinstall %s БЕЗ ssh-ключа — нода станет недоступна "
                "(нет provisioning-pubkey для переинъекции)", external_id,
            )
        self._call("PUT", f"/server.reinstall/{external_id}", body)

    def destroy_server(self, external_id: str) -> None:
        self._call("DELETE", f"/server/{external_id}", None)

    def renew_server(self, external_id: str) -> None:
        """PUT /v1/server.prolong/{id} — продлить (списывает с баланса)."""
        self._call("PUT", f"/server.prolong/{external_id}", None)

    def set_autoprolong(self, external_id: str, enabled: bool = True) -> bool:
        """PUT /v1/server/{id} {autoprolong:"0"|"1"} (СТРОКА, не bool). VDSina
        ставит значение напрямую (не тоггл, как 4vps) — возвращаем запрошенное."""
        self._call(
            "PUT", f"/server/{external_id}",
            {"autoprolong": "1" if enabled else "0"},
        )
        return enabled

    def get_balance(self) -> float | None:
        """GET /v1/account.balance → ``data.real`` (строка ₽; тратимый остаток).
        0.00 сохраняем как 0.0 (НЕ схлопываем в None — иначе low-balance алерт
        молчит ровно когда деньги кончились)."""
        data = self._call("GET", "/account.balance", None)
        if isinstance(data, dict) and data.get("real") is not None:
            try:
                return float(data["real"])
            except (TypeError, ValueError):
                return None
        return None

    # ---------- offerings (admin-форма заказа) ----------

    def list_regions(self) -> list[str]:
        return [
            str(d["id"]) for d in self.list_datacenters() if d.get("id") is not None
        ]

    def list_datacenters(self) -> list[dict]:
        """GET /v1/datacenter → ``[{id,name,country,active}]``. Ошибка/пусто → []."""
        try:
            data = self._call("GET", "/datacenter", None)
        except DriverError:
            return []
        out: list[dict] = []
        for dc in data if isinstance(data, list) else []:
            if isinstance(dc, dict):
                out.append({
                    "id": dc.get("id"),
                    "name": dc.get("name"),
                    "country": dc.get("country"),
                    "active": dc.get("active"),
                })
        return out

    def list_plans(self) -> list[dict]:
        """Тарифы: GET /v1/server-group (группы) → для каждой GET
        /v1/server-plan/{groupId}. dedupe по id. ``cost`` — за период ``period``."""
        try:
            groups = self._call("GET", "/server-group", None)
        except DriverError:
            return []
        out: list[dict] = []
        seen: set[Any] = set()
        for g in groups if isinstance(groups, list) else []:
            gid = g.get("id") if isinstance(g, dict) else None
            if gid is None:
                continue
            try:
                plans = self._call("GET", f"/server-plan/{gid}", None)
            except DriverError:
                continue
            for p in plans if isinstance(plans, list) else []:
                if not isinstance(p, dict):
                    continue
                pid = p.get("id")
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                spec = p.get("data") or {}
                out.append({
                    "id": pid,
                    "name": p.get("name") or str(pid),
                    "price": _to_float(p.get("cost")),
                    "full_price": _to_float(p.get("full_cost")),
                    "period": p.get("period"),
                    "cpu": _spec(spec.get("cpu")),
                    "ram": _spec(spec.get("ram")),
                    "disk": _spec(spec.get("disk")),
                })
        return out

    def list_images(self) -> list[dict]:
        """GET /v1/template → ОС-образы ``[{id,name,active,ssh_key}]``. id Ubuntu
        НЕ константа (зависит от аккаунта) — админ выбирает в форме по имени."""
        try:
            data = self._call("GET", "/template", None)
        except DriverError:
            return []
        out: list[dict] = []
        for t in data if isinstance(data, list) else []:
            if isinstance(t, dict):
                out.append({
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "active": t.get("active"),
                    "ssh_key": t.get("ssh-key"),
                })
        return out

    # ---------- helpers ----------

    def _ensure_key_id(self) -> int | None:
        """id ssh-ключа для инъекции. Явный из конструктора > авто-регистрация
        нашего provisioning-pubkey на VDSina (идемпотентно по имени _KEY_NAME).
        Кэшируем на инстансе. None — если pubkey недоступен."""
        if self._ssh_key_id is not None:
            return self._ssh_key_id
        from ..ssh_bootstrap import provisioning_pubkey  # lazy: избегаем циклов

        pub = provisioning_pubkey()
        if not pub:
            return None
        try:
            keys = self._call("GET", "/ssh-key", None)
        except DriverError:
            keys = []
        for k in keys if isinstance(keys, list) else []:
            if isinstance(k, dict) and k.get("name") == _KEY_NAME:
                self._ssh_key_id = _to_int(k.get("id"))
                if self._ssh_key_id is not None:
                    return self._ssh_key_id
        data = self._call("POST", "/ssh-key", {"name": _KEY_NAME, "data": pub})
        self._ssh_key_id = _to_int(data.get("id")) if isinstance(data, dict) else None
        return self._ssh_key_id

    def _wait_active(self, server_id: str) -> tuple[str, str, dict]:
        """Поллим GET /v1/server/{id}, пока status=active и есть IPv4.
        Возвращаем ``(ipv4, last_status, server_dict)``."""
        deadline = time.time() + _POLL_TIMEOUT
        last_status = ""
        last_srv: dict = {}
        while time.time() < deadline:
            try:
                data = self._call("GET", f"/server/{server_id}", None)
            except DriverError:
                time.sleep(_POLL_INTERVAL)
                continue
            if isinstance(data, dict):
                last_srv = data
                last_status = str(data.get("status") or "")
                ipv4 = _extract_ipv4(data)
                if ipv4 and last_status.lower() == "active":
                    return ipv4, last_status, data
            time.sleep(_POLL_INTERVAL)
        return _extract_ipv4(last_srv), last_status, last_srv

    def _call(self, method: str, path: str, body: dict | None) -> Any:
        try:
            resp = self._session.request(
                method, f"{self._base}{path}", json=body, timeout=_TIMEOUT
            )
        except requests.RequestException as exc:
            raise DriverError(f"VDSina {method} {path} failed: {exc}") from exc
        if resp.status_code == 204:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            if resp.status_code >= 400:
                raise DriverError(f"VDSina {method} {path} -> {resp.status_code}")
            return payload
        if payload.get("status") == "ok":
            return payload.get("data")
        # status != "ok": реальная ошибка ИЛИ пустой список ("No X information").
        # Не угадываем по тексту — поднимаем; offerings-методы (list_*) ловят и
        # дегрейдят в []. Денежные пути (order/create/balance) ошибку увидят.
        # VDSina кладёт КОНКРЕТИКУ в ``description`` (status_msg часто generic
        # «Bad Request»/«Unauthorized») — сёрфим оба, иначе причина теряется.
        msg = payload.get("status_msg") or ""
        desc = payload.get("description")
        detail = f"{msg}: {desc}" if desc else (msg or str(payload))
        raise DriverError(f"VDSina {method} {path}: {detail}")


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v) or None
    except (TypeError, ValueError):
        return None


def _first_int(lst: list | None) -> int | None:
    for x in lst or []:
        i = _to_int(x)
        if i is not None:
            return i
    return None


def _spec(v: Any) -> Any:
    """Поле плана VDSina ``{value, bytes, for}`` → человекочитаемое ``value``."""
    if isinstance(v, dict):
        return v.get("value") or v.get("bytes")
    return v


def _extract_ipv4(srv: dict) -> str:
    """``data.ip`` в одиночном GET — МАССИВ ``[{ip,type}]``; в листинге — объект.
    Берём первый адрес без ':' (IPv4)."""
    ip = (srv or {}).get("ip")
    entries = ip if isinstance(ip, list) else ([ip] if isinstance(ip, dict) else [])
    for e in entries:
        if isinstance(e, dict):
            val = str(e.get("ip") or "")
            if val and ":" not in val:
                return val
    if isinstance(ip, str):
        return ip
    return ""
