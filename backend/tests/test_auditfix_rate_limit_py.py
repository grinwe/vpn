"""Аудит-фикс #1: ключ rate-limit'а — реальный клиентский IP, не IP прокси.

Юнит-тесты key_func без БД: собираем starlette Request руками и проверяем
правила ключевания из ``app.rate_limit.rate_limit_key``:

* за доверенным nginx ключ берётся из X-Real-IP (fallback — последний
  элемент X-Forwarded-For, дописанный самим nginx);
* с недоверенного адреса заголовки игнорируются (анти-спуфинг);
* валидный X-Admin-Token (бот/админка) → уникальный ключ на запрос,
  кроме /api/agent/* (там «потолок на источник» намеренный).
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.config import get_settings
from app.rate_limit import rate_limit_key

ADMIN_TOKEN = "test-admin-token"  # ставится в conftest через ADMIN_API_TOKEN


def _make_request(
    path: str = "/api/sub/some-token",
    client_host: str = "172.18.0.5",
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "client": (client_host, 54321),
        "server": ("backend", 8000),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    """Гарантируем известный админ-токен независимо от порядка тестов."""
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_trusted_proxy_uses_x_real_ip():
    # nginx в docker-сети → ключ = X-Real-IP, а не IP nginx-контейнера
    req = _make_request(headers={"X-Real-IP": "203.0.113.7"})
    assert rate_limit_key(req) == "203.0.113.7"


def test_different_clients_get_different_buckets():
    # Главный сценарий находки: два юзера за одним nginx не делят бакет
    a = _make_request(headers={"X-Real-IP": "203.0.113.7"})
    b = _make_request(headers={"X-Real-IP": "198.51.100.9"})
    assert rate_limit_key(a) != rate_limit_key(b)


def test_xff_fallback_takes_last_element():
    # Первый элемент XFF спуфится клиентом; nginx дописывает реальный IP
    # последним ($proxy_add_x_forwarded_for) — берём именно его.
    req = _make_request(
        headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.7"},
    )
    assert rate_limit_key(req) == "203.0.113.7"


def test_untrusted_peer_headers_ignored():
    # Прямое соединение с публичного IP: заголовкам не верим
    req = _make_request(
        client_host="198.51.100.20",
        headers={"X-Real-IP": "6.6.6.6", "X-Forwarded-For": "6.6.6.6"},
    )
    assert rate_limit_key(req) == "198.51.100.20"


def test_trusted_peer_without_headers_keeps_peer_ip():
    # Бот-контейнер зовёт backend напрямую без прокси-заголовков
    req = _make_request(client_host="172.18.0.9")
    assert rate_limit_key(req) == "172.18.0.9"


def test_admin_token_exempts_from_per_ip_limits():
    # Валидный X-Admin-Token → уникальный ключ на каждый запрос:
    # регистрации через бота не упираются в 10/minute как в глобальный потолок
    req1 = _make_request(
        path="/api/users/register",
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    req2 = _make_request(
        path="/api/users/register",
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    key1, key2 = rate_limit_key(req1), rate_limit_key(req2)
    assert key1.startswith("admin:")
    assert key1 != key2


def test_invalid_admin_token_not_exempt():
    req = _make_request(
        path="/api/users/register",
        client_host="172.18.0.9",
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert rate_limit_key(req) == "172.18.0.9"


def test_agent_routes_keep_source_cap_for_admin():
    # /api/agent/* — «глобальный потолок на источник» намеренный
    # (api/agent.py), админ-токен там НЕ освобождает от IP-ключа.
    req = _make_request(
        path="/api/agent/ops/plan",
        client_host="172.18.0.9",
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    assert rate_limit_key(req) == "172.18.0.9"
