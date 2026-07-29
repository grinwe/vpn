"""Блок статуса подписки в VPN-клиенте и имена серверов в списке.

Раньше человек видел в клиенте двенадцать одинаковых строк «V8 сервер N» и
ничего о своей подписке: чтобы узнать срок, надо было идти в бота. Плюс мы
месяцами отдавали `subscription-userinfo` с одним ключом `expire`, из-за чего
Hiddify игнорировал заголовок целиком — вместе со ссылками на поддержку и сайт.
"""
from __future__ import annotations

from datetime import timedelta

from app.api_extensions import (
    _plural_hours,
    _relabel_uri,
    _sub_response_headers,
    _sub_status_banner,
)
from app.time_utils import utcnow


class _Sub:
    """Минимальная подписка: заголовкам нужны только срок и id."""

    def __init__(self, expires_in: timedelta | None):
        self.id = 1
        self.user_id = 1
        self.expires_at = (utcnow() + expires_in) if expires_in is not None else None


# ── subscription-userinfo ───────────────────────────────────────────────────


def test_userinfo_has_all_four_keys(monkeypatch):
    """Hiddify парсит заголовок только целиком (upload/download/total/expire);
    при неполном наборе он выбрасывает и его, и support-url, и web-page-url."""
    headers = _sub_response_headers(_Sub(timedelta(days=10)), "tok")
    info = headers["subscription-userinfo"]
    for key in ("upload=", "download=", "total=", "expire="):
        assert key in info, info


def test_userinfo_total_zero_means_unlimited(monkeypatch):
    """Тарифы у нас по устройствам, а per-user трафик не собирается вовсе —
    поэтому честный безлимит (total=0), а не выдуманная шкала."""
    headers = _sub_response_headers(_Sub(timedelta(days=10)), "tok")
    assert "total=0" in headers["subscription-userinfo"]


def test_no_userinfo_without_expiry():
    headers = _sub_response_headers(_Sub(None), "tok")
    assert "subscription-userinfo" not in headers


# ── блок статуса ────────────────────────────────────────────────────────────


def test_banner_blue_when_plenty_of_time():
    banner = _sub_status_banner(_Sub(timedelta(days=20)))
    assert banner["sub-info-color"] == "blue"
    assert "активна до" in banner["sub-info-text"]


def test_banner_red_on_last_days():
    banner = _sub_status_banner(_Sub(timedelta(days=2, hours=1)))
    assert banner["sub-info-color"] == "red"
    assert "2 дня" in banner["sub-info-text"]


def test_banner_switches_to_hours_on_final_day():
    """На финише «0 дней» бессмысленно — решение о продлении принимают в часах."""
    banner = _sub_status_banner(_Sub(timedelta(hours=2, minutes=30)))
    assert banner["sub-info-color"] == "red"
    assert "2 часа" in banner["sub-info-text"]


def test_banner_after_expiry():
    banner = _sub_status_banner(_Sub(timedelta(hours=-5)))
    assert banner["sub-info-color"] == "red"
    assert "закончилась" in banner["sub-info-text"]


def test_banner_text_fits_client_limit():
    """Happ обрезает sub-info-text на 200 символах."""
    for delta in (timedelta(days=40), timedelta(days=1), timedelta(hours=-1)):
        assert len(_sub_status_banner(_Sub(delta))["sub-info-text"]) <= 200


def test_banner_button_needs_bot_username(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    banner = _sub_status_banner(_Sub(timedelta(days=1)))
    assert banner["sub-info-button-text"] == "Продлить"
    assert banner["sub-info-button-link"].endswith("?start=account")

    monkeypatch.delenv("BOT_USERNAME", raising=False)
    banner = _sub_status_banner(_Sub(timedelta(days=1)))
    # Без ссылки кнопку не показываем: Happ отрисовал бы её мёртвой.
    assert "sub-info-button-text" not in banner


def test_support_url_points_to_bot(monkeypatch):
    """Иконка Telegram в строке подписки — это support-url на t.me."""
    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    headers = _sub_response_headers(_Sub(timedelta(days=5)), "tok")
    assert headers["support-url"].startswith("https://t.me/")
    assert headers["notification-subs-expire"] == "1"


def test_plural_hours_russian_forms():
    assert _plural_hours(1) == "1 час"
    assert _plural_hours(2) == "2 часа"
    assert _plural_hours(5) == "5 часов"
    assert _plural_hours(11) == "11 часов"
    assert _plural_hours(21) == "21 час"


# ── имена серверов ──────────────────────────────────────────────────────────


def test_labels_say_role_not_protocol():
    """Человеку нужна роль («что пробовать первым»), а не Reality/XHTTP."""
    assert "Основной 1" in _relabel_uri("vless://x@h:443#old", "vless-reality", 1)
    assert "Быстрый 1" in _relabel_uri("hy2://x@h:443#old", "hysteria2", 1)
    assert "Запасной 2" in _relabel_uri("vless://x@h:443#old", "vless-xhttp", 2)
    assert "Резервный 3" in _relabel_uri("vless://x@h:443#old", "vless-ws-cdn", 3)


def test_labels_never_leak_country_or_protocol():
    """Имя не должно палить страну (СОРМ/приватность) и не должно быть
    техножаргоном — это исходный замысел, его легко случайно сломать."""
    uri = _relabel_uri("vless://x@h:443#reality-Russia", "vless-reality", 1)
    label = uri.split("#", 1)[1]
    for leak in ("Russia", "RU", "reality", "Reality", "xhttp", "ws-cdn"):
        assert leak not in label, label


def test_relabel_keeps_uri_body_intact():
    src = "vless://uuid@host:443?security=reality&sni=ozon.ru#old-name"
    out = _relabel_uri(src, "vless-reality", 1)
    assert out.split("#", 1)[0] == src.split("#", 1)[0]


def test_server_numbers_are_per_node_and_order_is_by_role(db_session):
    """«Основной 2» и «Быстрый 2» обязаны означать ОДИН сервер, а сверху списка
    должно стоять то, что пробуют первым: при ручном выборе человек тыкает в
    первую строку."""
    from app.api_extensions import _decrypt_configs
    from app.security import encrypt

    class _Cred:
        def __init__(self, proto, node_id):
            self.proto = proto
            self.node_id = node_id
            self.is_active = True
            self.id = node_id * 10
            self.config_text = encrypt(f"vless://u@h{node_id}:443#raw")

    creds = [
        _Cred("vless-ws-cdn", 1),
        _Cred("hysteria2", 2),
        _Cred("vless-reality", 1),
        _Cred("hysteria2", 1),
        _Cred("vless-reality", 2),
    ]
    sub = _Sub(timedelta(days=5))
    out = _decrypt_configs(creds, sub=sub, device_id=None)
    labels = [c.uri.split("#", 1)[1] for c in out]

    # Порядок ролей: основные → быстрые → запасные/резервные.
    assert labels[0].endswith("Основной 1") or labels[0].endswith("Основной 2")
    assert [lb.split()[1] for lb in labels] == [
        "Основной", "Основной", "Быстрый", "Быстрый", "Резервный",
    ], labels

    # Номер = сервер: две ноды дают ровно два разных номера, а не пять.
    numbered = [lb for lb in labels if lb.split()[-1].isdigit()]
    assert {lb.split()[-1] for lb in numbered} == {"1", "2"}, labels

    # Одинокая роль номер не носит: «Резервный 1» при единственной строке
    # выглядит так, будто остальные резервные потерялись.
    assert "☁️ Резервный" in labels

    # И один номер закреплён за одним хостом: «Основной 1» и «Быстрый 1» —
    # это один сервер, иначе номера в списке ничего не значат.
    host_by_number: dict[str, str] = {}
    for cfg in out:
        body, label = cfg.uri.split("#", 1)
        host = body.split("@", 1)[1]
        number = label.split()[-1]
        if not number.isdigit():
            continue
        assert host_by_number.setdefault(number, host) == host, (number, host)


# ── заголовки обязаны уезжать клиенту ───────────────────────────────────────


def test_headers_are_latin1_encodable():
    """HTTP-заголовки — latin-1. Кириллица в них роняет ВЕСЬ ответ подписки в
    500: не «пропала подпись», а «VPN не настраивается ни у кого». Именно так
    блок статуса и сломал выдачу до этой проверки."""
    headers = _sub_response_headers(_Sub(timedelta(days=5)), "tok")
    for key, value in headers.items():
        key.encode("latin-1")
        value.encode("latin-1")  # упадёт ровно на том, что уронило бы прод


def test_banner_travels_in_the_body_not_in_headers(monkeypatch):
    """Блок статуса едет ТЕЛОМ.

    Заголовки — latin-1, поэтому русский текст пришлось бы кодировать в
    ``base64:``. Формально это уезжает, но Happ такое значение в sub-info не
    разворачивает: на живом клиенте блок просто не появился, хотя все
    заголовки доехали (прод, 2026-07-29). Тело же отдаётся в UTF-8, и
    документация Happ прямо разрешает те же параметры комментарием.
    """
    import base64 as b64

    from app.api_extensions import _sub_body

    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    sub = _Sub(timedelta(days=20))

    headers = _sub_response_headers(sub, "tok")
    assert not any(k.startswith("sub-info") for k in headers)

    class _Cfg:
        uri = "vless://u@h:443#⚡ Основной 1"

    body = b64.b64decode(_sub_body([_Cfg()], sub)).decode("utf-8")
    assert "#sub-info-text: Подписка активна до" in body
    assert "#sub-info-color: blue" in body
    assert body.rstrip().endswith("vless://u@h:443#⚡ Основной 1")


def test_native_expire_notice_goes_in_headers(monkeypatch):
    """Родное «подписка заканчивается через N д.» + кнопка «Продлить».

    Заголовком, а не телом: директивы из тела клиент не разобрал ни в base64,
    ни в plain (живое устройство, 2026-07-29), а заголовки читает. Здесь это
    возможно ровно потому, что значения ASCII — русский текст в заголовок не
    положить, он его роняет.
    """
    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    headers = _sub_response_headers(_Sub(timedelta(days=20)), "tok")
    assert headers["sub-expire"] == "1"
    assert headers["sub-expire-button-link"].startswith("https://t.me/")

    # Без срока предупреждать не о чем.
    assert "sub-expire" not in _sub_response_headers(_Sub(None), "tok")

    monkeypatch.setenv("SUB_STATUS_BANNER", "0")
    assert "sub-expire" not in _sub_response_headers(_Sub(timedelta(days=20)), "tok")


def test_banner_can_be_switched_off(monkeypatch):
    """Отрисовку делает чужой клиент, проверить её со своей стороны нельзя —
    значит выключение обязано стоить переменную окружения, а не откат."""
    import base64 as b64

    from app.api_extensions import _sub_body

    monkeypatch.setenv("SUB_STATUS_BANNER", "0")

    class _Cfg:
        uri = "vless://u@h:443#x"

    body = b64.b64decode(_sub_body([_Cfg()], _Sub(timedelta(days=5)))).decode()
    assert "sub-info" not in body
    assert body.strip() == "vless://u@h:443#x"


def test_buttons_lead_to_the_account_not_to_start(monkeypatch):
    """Человек, нажавший «продлить» в VPN-клиенте, уже знает чего хочет —
    приветственный экран бота между ним и оплатой лишний."""
    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    monkeypatch.delenv("TELEGRAM_MINIAPP_SHORT_NAME", raising=False)

    banner = _sub_status_banner(_Sub(timedelta(days=5)))
    assert banner["sub-info-button-link"].endswith("?start=account")
    headers = _sub_response_headers(_Sub(timedelta(days=5)), "tok")
    assert headers["support-url"].endswith("?start=account")

    # С коротким именем из BotFather — кабинет открывается одним тапом.
    monkeypatch.setenv("TELEGRAM_MINIAPP_SHORT_NAME", "app")
    banner = _sub_status_banner(_Sub(timedelta(days=5)))
    assert banner["sub-info-button-link"] == "https://t.me/GV8_VPN_bot/app"


def test_plain_format_is_opt_in(monkeypatch):
    """`?fmt=plain` — диагностика, а не смена поведения: клиенты параметр не
    шлют, и для них тело обязано остаться base64."""
    import base64 as b64

    from app.api_extensions import _sub_body

    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")

    class _Cfg:
        uri = "vless://u@h:443#x"

    sub = _Sub(timedelta(days=20))
    default = _sub_body([_Cfg()], sub)
    plain = _sub_body([_Cfg()], sub, plain=True)

    assert b64.b64decode(default).decode() == plain
    assert plain.startswith("#sub-info-text: ")
