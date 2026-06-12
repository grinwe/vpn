"""ISPsystem BILLmanager cloud driver (generic — DataCheap / UFO / AdminVPS / …).

Многие RU-хостеры крутят ISPsystem BILLmanager (``<host>/billmgr?func=…``). Один
драйвер обслуживает их все; конкретный хост + креды — в ``CloudProvider.api_token_enc``
как JSON ``{"base_url","username","password"}`` (целиком Fernet-шифрован).

Контракт сверен по офиц. ISPsystem-докам (b6sa/v6, «Service management via API» +
VDS-reference, upd 2026-01-30) + рабочим примерам PQ.Hosting/the.hosting, 2026-06,
и прошёл 3-линзовый адверсариал-ревью (исправлены orphan-leak/$-wrap/IP-list/reinstall).

Особенности billmgr:
* Auth: ``authinfo=<user>:<password>`` параметром в КАЖДОМ запросе (stateless).
  Формат ответа ``out=json`` → ``{"doc": …}``; ошибка в ``doc.error``. Значения
  billmgr часто заворачивает в ``{"$": value}`` → разворачиваем ``_scalar``.
* Заказ — одним выстрелом: ``func=vds.order.param … &skipbasket=on&sok=ok`` →
  СПИСЫВАЕТ С БАЛАНСА сразу (минуя корзину). ``autoprolong=1`` — иначе нода
  удалится в конце периода. id новой услуги в ответе НЕ приходит → находим её в
  ``func=vds`` по ``domain`` (=имя ноды), берём свежайшую.
* НЕТ инъекции SSH-ключа → root-пароль (как 4vps). VMmanager генерит свой; мы
  СТАВИМ СВОЙ известный через ``func=service.changepassword`` ПОСЛЕ ``active``
  (тогда надёжно) → возвращаем как ``CloudServer.root_password`` (ssh_bootstrap
  поставит наш ключ по паролю).
* Статус+IP: ``func=vds`` → ``doc.elem[].{ip,item_status}``; ``item_status`` 1=ordered
  2=active 3=suspended 4=deleted 5=processing. Поллим до 2 + непустой ip.
* Переустановка ОС: ``vds.reinstall`` НЕТ → ``func=vds.edit … &ostempl=…``. Удаление:
  ``func=vds.delete … &elid=<id>&sok=ok``.

Блокирующий ``create_server`` (БЕЗ order_server) — заказ+поллинг идёт В ФОНЕ
(node_spawner._finalize_spawn, демон-тред → НЕ в HTTP-запросе, 502 не грозит),
который сам сохранит ``root_password``. По форме как hetzner (blocking), по сути
no-key + root-password как 4vps. **Orphan-guard:** если оплаченная услуга не
поднялась / пароль не встал — сносим её (``vds.delete``) + ERROR-лог с id, чтобы
ретраи не плодили оплаченных сирот.

⚠️ UNCONFIRMED (боевой smoke на UFO до прода): точные имена slist-полей offerings;
поведение skipbasket при нехватке баланса (спишет vs unpaid); `period=1` = 1 МЕСЯЦ
для выбранного pricelist?; доступен ли `vds.edit ostempl` под клиентским токеном;
не IP-whitelist'нут ли authinfo; точная форма elem (ip строка/список/$-обёртка).
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

import requests

from .base import CloudServer, DriverError

logger = logging.getLogger(__name__)

_TIMEOUT = 40
_POLL_TIMEOUT = 900
_POLL_INTERVAL = 12
_PW_RETRIES = 3
_PW_INTERVAL = 3
# item_status у billmgr-VDS
_ST_ACTIVE = "2"
_ST_DELETED = "4"


def _parse_token(token: str) -> tuple[str, str, str]:
    """``api_token_enc`` (расшифрованный) → ``(base_url, username, password)``.
    Формат — JSON ``{"base_url","username","password"}``."""
    try:
        data = json.loads(token or "{}")
    except (ValueError, TypeError) as exc:
        raise DriverError(
            "billmgr token must be JSON {base_url, username, password}"
        ) from exc
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not base_url or not username or not password:
        raise DriverError(
            "billmgr token JSON needs non-empty base_url, username, password"
        )
    return base_url, username, password


def _gen_password() -> str:
    """Рут-пароль для service.changepassword. Только буквы+цифры (спецсимволы
    некоторые панели/ОС отклоняют), длина с запасом."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


class BillmgrDriver:
    kind = "billmgr"

    def __init__(self, token: str) -> None:
        self._base, self._user, self._password = _parse_token(token)
        self._session = requests.Session()

    # ---------- public API ----------

    def create_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,  # billmgr не инжектит SSH-ключ
        user_data: str | None = None,          # billmgr не поддерживает user_data
    ) -> CloudServer:
        """Блокирующий заказ: vds.order.param (skipbasket→баланс) → найти услугу по
        domain → дождаться active+ip → поставить наш root-пароль. Идёт в ФОНЕ
        (node_spawner._finalize_spawn), который сохранит CloudServer.root_password.
        На любом провале ПОСЛЕ списания — сносим услугу (orphan-guard)."""
        dc, pl, tpl = str(region), str(plan), str(image)
        if not dc or not pl or not tpl:
            raise DriverError(
                f"billmgr order needs datacenter/pricelist/ostempl ids "
                f"(got region={region!r}, plan={plan!r}, image={image!r})"
            )
        # 1) заказ + оплата с баланса. autoprolong=1 — иначе нода удалится в конце
        #    периода. domain=name — по нему находим услугу (id в ответе не приходит).
        self._call(
            "vds.order.param",
            pricelist=pl, datacenter=dc, ostempl=tpl,
            period="1", autoprolong="1", domain=name, skipbasket="on", sok="ok",
        )
        # 2) найти услугу по domain + дождаться active с IP.
        service_id, ipv4, cost, elem = self._wait_active(name)
        if not service_id:
            # заказ СПИСАЛ баланс, но услуга не появилась в func=vds — снести нечем.
            logger.error(
                "billmgr: заказ %s на %s СПИСАЛ баланс, но услуга не найдена в "
                "func=vds — проверь панель ВРУЧНУЮ (возможен orphan)", name, self._base,
            )
            raise DriverError(f"billmgr order {name} charged but service not found")
        if not ipv4:
            # услуга есть, но не поднялась (timeout / unpaid low-balance) → сносим
            # оплаченный залипший заказ, чтобы ретраи не плодили сирот.
            logger.error(
                "billmgr: услуга %s (%s) не поднялась за %ss — сношу оплаченный "
                "заказ (orphan-guard)", service_id, name, _POLL_TIMEOUT,
            )
            self._safe_destroy(service_id)
            raise DriverError(
                f"billmgr service {service_id} ({name}) got no IPv4 within "
                f"{_POLL_TIMEOUT}s — destroyed"
            )
        # 3) поставить ИЗВЕСТНЫЙ root-пароль (услуга active → changepassword надёжен).
        #    Не вышло после ретраев → no-key нода без пароля бесполезна → сносим.
        root_password = self._set_password(service_id)
        if not root_password:
            logger.error(
                "billmgr: не смог поставить root-пароль на %s — сношу (no-key нода "
                "без пароля не забутстрапится)", service_id,
            )
            self._safe_destroy(service_id)
            raise DriverError(
                f"billmgr service {service_id}: changepassword failed — destroyed"
            )
        return CloudServer(
            external_id=str(service_id),
            ipv4=ipv4,
            ipv6=None,
            region=dc,
            plan=pl,
            monthly_cost=cost,
            root_password=root_password,
            raw=elem,
        )

    def destroy_server(self, external_id: str) -> None:
        """func=vds.delete&elid=<id>&sok=ok."""
        self._call("vds.delete", elid=external_id, sok="ok")

    def reinstall_server(
        self, external_id: str, image: str, *, password: str | None = None
    ) -> None:
        """ОС-переустановка = vds.edit с новым ostempl (vds.reinstall в billmgr НЕТ).
        vds.edit регенерит пароль server-side → ОБЯЗАНЫ поставить переданный, иначе
        node_spawner-сохранённый пароль не совпадёт. changepassword тут НЕ глушим —
        пусть reinstall упадёт громко, чем тихо разъедется пароль (Permission denied
        на bootstrap)."""
        if not str(image):
            raise DriverError("billmgr reinstall needs an ostempl id")
        self._call("vds.edit", elid=external_id, ostempl=str(image), sok="ok")
        if password:
            self._call(
                "service.changepassword",
                elid=external_id, passwd=password, confirm=password, sok="ok",
            )

    def list_regions(self) -> list[str]:
        return [
            str(d["id"]) for d in self.list_datacenters() if d.get("id") is not None
        ]

    # ---- offerings (admin-форма заказа) ----
    # billmgr НЕ отдаёт отдельных списков клиенту — они в select-list'ах (slist)
    # ВНУТРИ order-формы (vds.order.param без sok). Имена/форма slist панель-зависимы
    # (UNCONFIRMED) → парсим защитно, дегрейдим в []. До боевого smoke админ берёт
    # id руками из ответа no-sok формы (логируем сырой slist в debug).

    def list_datacenters(self) -> list[dict]:
        return self._slist_options("datacenter")

    def list_plans(self) -> list[dict]:
        return self._slist_options("pricelist")

    def list_images(self) -> list[dict]:
        return self._slist_options("ostempl")

    # ---------- helpers ----------

    def _safe_destroy(self, service_id: str) -> None:
        """Best-effort снос (orphan-guard). Даже если не вышло — id уже в ERROR-логе
        выше, оператор снесёт вручную."""
        try:
            self.destroy_server(service_id)
        except DriverError:
            logger.exception(
                "billmgr orphan-guard: не смог снести %s — снеси ВРУЧНУЮ в панели %s",
                service_id, self._base,
            )

    def _set_password(self, service_id: str) -> str:
        """Поставить известный root-пароль через service.changepassword (с ретраями).
        Возвращает пароль либо "" если все попытки провалились."""
        pw = _gen_password()
        for _ in range(_PW_RETRIES):
            try:
                self._call(
                    "service.changepassword",
                    elid=service_id, passwd=pw, confirm=pw, sok="ok",
                )
                return pw
            except DriverError:
                time.sleep(_PW_INTERVAL)
        return ""

    def _slist_options(self, field: str) -> list[dict]:
        """Опции select-list'а ``field`` из order-формы (vds.order.param без sok).
        Защитно: разные версии billmgr кладут slist по-разному."""
        try:
            doc = self._call("vds.order.param")
        except DriverError:
            return []
        slists = doc.get("slist")
        entries: list = []
        if isinstance(slists, list):
            for s in slists:
                if isinstance(s, dict) and (s.get("$name") == field or s.get("name") == field):
                    entries = s.get("val") or s.get("value") or []
                    break
        elif isinstance(slists, dict):
            entries = slists.get(field) or []
        if not entries:
            logger.debug("billmgr slist[%s] пуст/неизвестной формы: %r", field, slists)
        out: list[dict] = []
        for opt in entries if isinstance(entries, list) else []:
            if not isinstance(opt, dict):
                continue
            key = opt.get("$key") or opt.get("key") or opt.get("id")
            label = opt.get("$") or opt.get("label") or opt.get("name") or str(key)
            if key is not None:
                out.append({"id": key, "name": label})
        return out

    def _wait_active(self, name: str) -> tuple[str, str, float | None, dict]:
        """Поллим func=vds, пока услуга с ``domain==name`` не станет active+IP.
        Возвращаем ``(service_id, ipv4, monthly_cost, elem)``. item_status/id/ip
        могут быть $-обёрнуты → всё через _scalar/_extract_ip."""
        deadline = time.time() + _POLL_TIMEOUT
        last_id = ""
        last_elem: dict = {}
        while time.time() < deadline:
            try:
                doc = self._call("vds")
            except DriverError:
                time.sleep(_POLL_INTERVAL)
                continue
            elem = _find_by_domain(doc, name)
            if elem:
                last_elem = elem
                last_id = str(_scalar(elem.get("id") or elem.get("elid")) or "")
                ipv4 = _extract_ip(elem)
                status = str(_scalar(elem.get("item_status")) or "")
                if ipv4 and status == _ST_ACTIVE:
                    return last_id, ipv4, _to_float(elem.get("cost")), elem
                if status == _ST_DELETED:
                    # терминально — дальше ждать смысла нет (вернём без ip → orphan-guard).
                    return last_id, "", _to_float(elem.get("cost")), elem
            time.sleep(_POLL_INTERVAL)
        return last_id, _extract_ip(last_elem), _to_float(last_elem.get("cost")), last_elem

    def _call(self, func: str, **params: Any) -> dict:
        """POST к ``<base>/billmgr?func=<func>`` с authinfo + out=json. Возвращает
        ``doc``. Бросает DriverError на doc.error / HTTP-ошибке (с телом ответа —
        чтобы 'insufficient funds' было видно в логах)."""
        body = {
            "func": func,
            "out": "json",
            "authinfo": f"{self._user}:{self._password}",
            "lang": "en",
            **{k: str(v) for k, v in params.items()},
        }
        try:
            resp = self._session.post(f"{self._base}/billmgr", data=body, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise DriverError(f"billmgr {func} request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise DriverError(
                f"billmgr {func} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DriverError(f"billmgr {func}: non-JSON response") from exc
        doc = payload.get("doc") if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            doc = payload if isinstance(payload, dict) else {}
        err = doc.get("error")
        if err:
            msg = err
            if isinstance(err, dict):
                msg = err.get("msg") or err.get("$") or err.get("text") or err
            raise DriverError(f"billmgr {func} error: {msg}")
        return doc


def _find_by_domain(doc: dict, name: str) -> dict | None:
    """Найти в ответе func=vds элемент с ``domain==name`` (свежайший по id)."""
    elems = doc.get("elem")
    if isinstance(elems, dict):
        elems = [elems]
    if not isinstance(elems, list):
        return None
    matched = [
        e for e in elems
        if isinstance(e, dict) and _scalar(e.get("domain")) == name
    ]
    if not matched:
        return None
    return max(matched, key=lambda e: _to_int(e.get("id") or e.get("elid")) or 0)


def _extract_ip(elem: dict) -> str:
    """IPv4 услуги. ``ip`` бывает строкой ('1.2.3.4' / '1.2.3.4 2001:..'),
    $-обёрткой, ИЛИ списком (строк/объектов) — перебираем ВСЕ, берём первый
    адрес без ':'."""
    ip = (elem or {}).get("ip")
    items = ip if isinstance(ip, list) else [ip]
    for it in items:
        val = _scalar(it)
        if isinstance(val, dict):
            val = val.get("ip") or val.get("name") or val.get("$")
        if isinstance(val, str):
            for tok in val.replace(",", " ").split():
                if tok and ":" not in tok:
                    return tok
    return ""


def _scalar(v: Any) -> Any:
    """billmgr-JSON часто заворачивает значение в {'$': value}. Разворачиваем."""
    if isinstance(v, dict):
        return v.get("$", v)
    if isinstance(v, list) and v:
        return _scalar(v[0])
    return v


def _to_int(v: Any) -> int | None:
    try:
        return int(_scalar(v))
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        return float(_scalar(v)) or None
    except (TypeError, ValueError):
        return None
