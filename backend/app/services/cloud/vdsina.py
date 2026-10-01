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
  поднимает ``DriverError`` на ``status != "ok"`` (бизнес-ответ, в т.ч. «нет
  данных») и ``TransientDriverError`` на транзиентном сетевом/HTTP-сбое
  (таймаут, 429, 5xx). offerings-методы (``list_*``) гасят в ПУСТОЙ список
  только бизнес-``DriverError`` (реально пусто — форма дегрейдит, как у 4vps),
  а ``TransientDriverError`` ПРОБРАСЫВАЮТ — иначе оператор видит пустой каталог
  вместо признака сбоя (роут ``/offerings`` отдаёт 502). Идемпотентные GET
  ретраятся с backoff (см. ``_call``); POST/PUT/DELETE — НЕ ретраятся (заказ
  не идемпотентен, ретрай задвоил бы оплаченный сервер).
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
import re
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
# Ретрай транзиентных сбоев ТОЛЬКО на идемпотентных GET (offerings/баланс/поллинг):
# доп. попыток после первой и базовый (экспоненциальный) backoff в секундах.
_RETRY_ATTEMPTS = int(os.getenv("VDSINA_RETRY_ATTEMPTS", "2"))
_RETRY_BACKOFF = float(os.getenv("VDSINA_RETRY_BACKOFF", "0.5"))
# HTTP-коды, которые считаем транзиентными (сервер занят/лимитирует), а не
# бизнес-ошибкой запроса. 429 — rate-limit (уважаем Retry-After).
_RETRY_CODES = {429, 500, 502, 503, 504}
# Имя, под которым авто-регистрируем наш provisioning-pubkey в VDSina (идемпотентно).
_KEY_NAME = "vpn-provisioning"
# VDSina валидирует ``host`` как ДОМЕННОЕ имя (FQDN с реальным TLD), а не
# свободный лейбл — голое имя ноды («vdsina-ru-01») даёт
# "hostname must be a valid domain name" (проверено 2026-06-15). Синтезируем
# валидный FQDN из имени; хостнейм всё равно перетрёт bootstrap_node, поле чисто
# для прохождения валидации. Домен override через env (дефолт — RFC-2606
# example.com: реальный TLD .com проходит валидатор, никуда не резолвится).
_HOST_DOMAIN = os.getenv("VDSINA_HOST_DOMAIN", "example.com")


class TransientDriverError(DriverError):
    """Транзиентный сетевой/HTTP-сбой VDSina (таймаут, обрыв TCP, 429, 5xx).

    Подкласс ``DriverError``, поэтому все существующие ``except DriverError``
    ловят его как раньше. Но offerings-методы (``list_*``) отличают его от
    бизнес-``DriverError`` (реально пустой каталог): транзиентный НЕ глушится в
    ``[]``, а пробрасывается — оператор в форме заказа увидит «не удалось
    загрузить каталог» (роут отдаёт 502), а не молчаливо пустые списки.
    ``retry_after`` — пауза из заголовка Retry-After (429), если провайдер её дал.
    """

    def __init__(self, *args: Any, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


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
            # host — отдельное поле, VDSina валидирует его как FQDN (см.
            # _HOST_DOMAIN). name остаётся свободным лейблом для панели.
            "host": _hostname_fqdn(name),
            "ssh-key": key_id,
            # ip4 — кол-во IPv4 (обязательное, иначе "Validation Error" без
            # деталей). Сверено по офиц. PDF + terraform-провайдеру: без него
            # POST /server падает. 1 адрес — дефолт для VPN-ноды.
            "ip4": 1,
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
        return ipv4, self._server_cost(srv), srv

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

    def reboot_server(self, external_id: str) -> None:
        """PUT /v1/server.reboot/{id} — мягкая (ACPI) перезагрузка. Форма экшена
        как у reinstall/prolong. Если эндпоинт ответит ошибкой — вызывающий
        (reboot-роут) падёт на SSH-фолбэк, так что путь best-effort."""
        self._call("PUT", f"/server.reboot/{external_id}", None)

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
        """GET /v1/datacenter → ``[{id,name,country,active}]``. Реально пусто → [];
        транзиентный сбой пробрасываем (роут отдаст 502, не путаем с «пусто»)."""
        try:
            data = self._call("GET", "/datacenter", None)
        except TransientDriverError:
            raise  # сетевой/HTTP сбой ≠ пустой каталог — не глушим в []
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
        except TransientDriverError:
            raise  # сетевой/HTTP сбой ≠ пустой каталог — не глушим в []
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
            except TransientDriverError:
                # Транзиентный сбой на одной группе → пробрасываем: иначе форма
                # получит ЧАСТИЧНЫЙ каталог без признака сбоя (хуже, чем 502).
                raise
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
        НЕ константа (зависит от аккаунта) — админ выбирает в форме по имени.
        Реально пусто → []; транзиентный сбой пробрасываем (роут отдаст 502)."""
        try:
            data = self._call("GET", "/template", None)
        except TransientDriverError:
            raise  # сетевой/HTTP сбой ≠ пустой каталог — не глушим в []
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

    def _server_cost(self, srv: dict) -> float | None:
        """Стоимость сервера из ответа GET /server/{id} (за период ``period``,
        как в list_plans). VDSina кладёт тариф во вложенный объект
        ``server_plan`` с полем цены ``cost`` (сверено по hugmouse/go-vdsina);
        дефис-вариант ключа и top-level ``cost`` подстрахованы. Если цены в
        объекте сервера нет — подтягиваем из /server-plan (list_plans) по id
        тарифа, иначе node.monthly_cost у vdsina-нод осталось бы пустым и
        cloud-billing занижал бы расходы флота."""
        srv = srv or {}
        plan = srv.get("server_plan") or srv.get("server-plan")
        plan_id: Any = None
        if isinstance(plan, dict):
            cost = _to_float(plan.get("cost"))
            if cost is not None:
                return cost
            plan_id = plan.get("id")
        elif plan is not None:
            plan_id = plan  # server-plan мог прийти голым id
        top = _to_float(srv.get("cost"))
        if top is not None:
            return top
        # В объекте сервера цены нет — ищем тариф в offerings по id.
        pid = _to_int(plan_id)
        if pid is not None:
            for p in self.list_plans():
                if _to_int(p.get("id")) == pid:
                    return p.get("price")
        return None

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
        """HTTP-вызов VDSina с ретраем транзиентных сбоев.

        Ретраим ТОЛЬКО идемпотентные GET (offerings/баланс/поллинг): POST/PUT/
        DELETE не идемпотентны (заказ/reinstall/prolong списывают деньги), их
        повтор задвоил бы операцию — поэтому одна попытка. Бизнес-``DriverError``
        (``status != "ok"``) не ретраится: ответ детерминирован.
        """
        idempotent = method.upper() == "GET"
        attempts = (_RETRY_ATTEMPTS + 1) if idempotent else 1
        last: TransientDriverError | None = None
        for i in range(attempts):
            try:
                return self._request_once(method, path, body)
            except TransientDriverError as exc:
                last = exc
                if i + 1 >= attempts:
                    break
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else _RETRY_BACKOFF * (2 ** i)
                )
                logger.warning(
                    "VDSina %s %s транзиентный сбой (%s) — ретрай %d/%d через %.1fs",
                    method, path, exc, i + 1, attempts - 1, delay,
                )
                time.sleep(delay)
        assert last is not None  # цикл прерывается только через break по last
        raise last

    def _request_once(self, method: str, path: str, body: dict | None) -> Any:
        """Одна HTTP-попытка. Транзиентные сбои → ``TransientDriverError``,
        бизнес-ошибки (``status != "ok"``) → ``DriverError``."""
        try:
            resp = self._session.request(
                method, f"{self._base}{path}", json=body, timeout=_TIMEOUT
            )
        except requests.RequestException as exc:
            # Таймаут/обрыв TCP — транзиентно (ретраибельно на GET).
            raise TransientDriverError(
                f"VDSina {method} {path} failed: {exc}"
            ) from exc
        if resp.status_code == 204:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            if resp.status_code >= 400:
                # 429/5xx — сервер занят/лимитирует: транзиентно (ретрай GET).
                # 429 несёт Retry-After — уважаем паузу провайдера.
                if resp.status_code in _RETRY_CODES:
                    raise TransientDriverError(
                        f"VDSina {method} {path} -> {resp.status_code}",
                        retry_after=_parse_retry_after(resp),
                    )
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
        # На "Validation Error" VDSina кладёт пофайловый разбор в ``data``
        # ({"ip4":"required",...}) — без него причина теряется. Сёрфим, если есть.
        err_data = payload.get("data")
        if err_data:
            detail = f"{detail} (data={err_data})"
        raise DriverError(f"VDSina {method} {path}: {detail}")


def _hostname_fqdn(name: str) -> str:
    """Валидный FQDN для поля ``host`` VDSina (требует домен, не лейбл).
    Санитизируем имя ноды в hostname-лейбл и вешаем _HOST_DOMAIN."""
    label = re.sub(r"[^a-z0-9-]+", "-", (name or "node").lower())
    label = re.sub(r"-{2,}", "-", label).strip("-")[:63].strip("-")
    return f"{label or 'node'}.{_HOST_DOMAIN}"


def _parse_retry_after(resp: Any) -> float | None:
    """Заголовок ``Retry-After`` (секунды) из 429 → пауза перед ретраем.
    HTTP-date форму не парсим (VDSina отдаёт секунды) — вернём None, тогда
    ``_call`` использует экспоненциальный backoff."""
    val = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return None


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
