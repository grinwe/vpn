"""Netfix (сетевой аудит) — provisioning.py, hysteria2 credential builder.

Покрывает pure-функции без БД/сети:
  * #5 salamander-obfs: пустой obfs_password не должен оставлять голый
    ``obfs=`` в URI (иначе рассинхрон скрамблинга с сервером);
  * #6 self-signed: ``insecure``/``pin_sha256`` из settings прокидываются в
    URI, а по умолчанию отсутствуют (дефолт — строгая верификация);
  * #3 helper ``_is_ip_host`` (детект IP-хоста для предупреждения об ACME).
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from app.services.provisioning import _build_hysteria2_credential, _is_ip_host


def _node():
    # id нужен для diagnostics-warning'а в ветке «obfs без пароля»
    # (_build_hysteria2_credential логирует node.id); реальный VPNNode его
    # всегда имеет.
    return SimpleNamespace(id=1, host="203.0.113.7", region="ru-01")


def _config(settings=None):
    return SimpleNamespace(id=1, port=8443, sni="vpn.example.com", settings=settings or {})


def _query(uri: str) -> dict:
    return parse_qs(urlsplit(uri).query)


def test_hy2_obfs_dropped_when_password_empty() -> None:
    """obfs задан, obfs_password пуст → в URI НЕ должно быть ни obfs, ни
    obfs-password (симметрично отключению obfs на сервере)."""
    uri = _build_hysteria2_credential(
        _node(), _config({"obfs": "salamander", "obfs_password": ""}), "pw"
    )
    q = _query(uri)
    assert "obfs" not in q
    assert "obfs-password" not in q


def test_hy2_obfs_kept_when_password_present() -> None:
    uri = _build_hysteria2_credential(
        _node(),
        _config({"obfs": "salamander", "obfs_password": "s3cret"}),
        "pw",
    )
    q = _query(uri)
    assert q.get("obfs") == ["salamander"]
    assert q.get("obfs-password") == ["s3cret"]


def test_hy2_no_insecure_by_default() -> None:
    """Дефолтная ACME-нода: ни insecure, ни pinSHA256."""
    q = _query(_build_hysteria2_credential(_node(), _config(), "pw"))
    assert "insecure" not in q
    assert "pinSHA256" not in q


def test_hy2_self_signed_settings_emitted() -> None:
    """insecure/pin_sha256 из settings прокидываются в URI (self-signed нода)."""
    uri = _build_hysteria2_credential(
        _node(),
        _config({"insecure": True, "pin_sha256": "AB/CD+ef=="}),
        "pw",
    )
    q = _query(uri)
    assert q.get("insecure") == ["1"]
    # pinSHA256 url-энкодится, но parse_qs возвращает уже декодированное
    assert q.get("pinSHA256") == ["AB/CD+ef=="]


def test_is_ip_host() -> None:
    assert _is_ip_host("203.0.113.7") is True
    assert _is_ip_host("2001:db8::1") is True
    assert _is_ip_host("vpn.example.com") is False
    assert _is_ip_host("") is False
