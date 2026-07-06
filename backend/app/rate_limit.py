"""Shared SlowAPI rate-limiter instance.

Imported by main.py (to wire middleware) and by route modules (to apply
per-route ``@limiter.limit(...)`` decorators).

Ключ лимита — реальный клиентский IP, а не адрес соседнего контейнера.
uvicorn запущен без ``--proxy-headers``, поэтому ``request.client.host``
для всего внешнего трафика — это IP nginx-контейнера: наивный
``get_remote_address`` схлопывал бы каждый бакет на всех пользователей
сразу (429 всем при пике на /api/sub), а вызовы бота (один IP
bot-контейнера) упирались бы в per-route лимиты как в глобальный потолок
(например ``10/minute`` на register = 10 регистраций/мин на весь сервис).

Правила ключевания (``rate_limit_key``):

* запрос с валидным ``X-Admin-Token`` (бот, админка — server-to-server)
  получает уникальный ключ на каждый запрос, т.е. фактически освобождён
  от per-IP лимитов. Исключение — ``/api/agent/*``: там «глобальный
  потолок на источник» через default key_func поставлен намеренно
  (см. api/agent.py), поэтому ключ остаётся IP-шным;
* запрос с доверенного прокси (private/loopback-сеть докера либо
  ``RATE_LIMIT_TRUSTED_PROXIES`` — CSV из CIDR) ключуется по
  ``X-Real-IP``: nginx ставит его безусловно из ``$remote_addr``,
  сквозь nginx его не подделать. Фолбэк — ПОСЛЕДНИЙ адрес
  ``X-Forwarded-For`` (его дописывает сам nginx через
  ``$proxy_add_x_forwarded_for``; первый элемент спуфится клиентом);
* запрос с недоверенного адреса ключуется по peer-IP, заголовки
  игнорируются (защита от спуфинга ключа).
"""

import hmac
import ipaddress
import os
import uuid

from slowapi import Limiter
from starlette.requests import Request

from .config import get_settings

_storage_uri = os.getenv("SLOWAPI_STORAGE_URI") or os.getenv("REDIS_URL") or "memory://"

# Доверенные прокси: только с этих peer-адресов верим X-Real-IP /
# X-Forwarded-For. По умолчанию — loopback + приватные сети (docker bridge).
_DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fd00::/8",
)


def _parse_networks(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            # Кривой CIDR в env не должен ронять импорт модуля — молча
            # пропускаем, остальные сети продолжают работать.
            continue
    return tuple(nets)


_trusted_proxy_nets = _parse_networks(
    os.getenv("RATE_LIMIT_TRUSTED_PROXIES") or ",".join(_DEFAULT_TRUSTED_PROXIES)
)


def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _trusted_proxy_nets)


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _real_client_ip(request: Request) -> str:
    """Реальный клиентский IP с учётом доверенного nginx-прокси."""
    peer = request.client.host if request.client else "127.0.0.1"
    if not _is_trusted_proxy(peer):
        return peer
    # nginx ставит X-Real-IP безусловно (proxy_set_header ... $remote_addr) —
    # клиентский заголовок сквозь nginx не проходит.
    real_ip = request.headers.get("x-real-ip", "").strip()
    if _valid_ip(real_ip):
        return real_ip
    # Фолбэк: последний элемент X-Forwarded-For — его дописал сам nginx
    # ($proxy_add_x_forwarded_for). Первый элемент подделывается клиентом.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.rsplit(",", 1)[-1].strip()
        if _valid_ip(last):
            return last
    return peer


def _is_admin_request(request: Request) -> bool:
    """Валидный X-Admin-Token? (constant-time, как в auth.require_admin)."""
    token = request.headers.get("x-admin-token")
    if not token:
        return False
    expected = get_settings().admin_api_token
    if not expected:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def rate_limit_key(request: Request) -> str:
    """Default key_func лимитера — см. правила в докстринге модуля."""
    if _is_admin_request(request) and not request.url.path.startswith("/api/agent/"):
        # Уникальный ключ на каждый запрос: бакет не накапливается, т.е.
        # server-to-server трафик бота/админки не режется per-IP лимитами.
        # (Пустой ключ у slowapi тоже скипает лимит, но пишет error-лог на
        # каждый запрос.) Ключи короткоживущие — TTL окна лимита.
        return f"admin:{uuid.uuid4().hex}"
    return _real_client_ip(request)


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=["300/minute", "60/second"],
    storage_uri=_storage_uri,
)
