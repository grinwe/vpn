"""Блок статуса подписки в VPN-клиенте и имена серверов в списке.

Раньше человек видел в клиенте двенадцать одинаковых строк «V8 сервер N» и
ничего о своей подписке: чтобы узнать срок, надо было идти в бота. Плюс мы
месяцами отдавали `subscription-userinfo` с одним ключом `expire`, из-за чего
Hiddify игнорировал заголовок целиком — вместе со ссылками на поддержку и сайт.
"""
from __future__ import annotations

from datetime import timedelta, timezone

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
    """Тарифы у нас по устройствам — честный безлимит (total=0), а вот
    download теперь настоящий: байты за период из тика traffic_stats."""
    headers = _sub_response_headers(_Sub(timedelta(days=10)), "tok")
    assert "total=0" in headers["subscription-userinfo"]


def test_userinfo_download_carries_period_usage():
    """Шкала в клиенте показывает реальный расход за оплаченный период —
    «12.5 GB/∞» вместо вечного «0B»."""
    sub = _Sub(timedelta(days=10))
    sub.traffic_used_bytes = 13_421_772_800
    headers = _sub_response_headers(sub, "tok")
    assert "download=13421772800" in headers["subscription-userinfo"]


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
    """Русский sub-info-текст едет ТЕЛОМ.

    Заголовки — latin-1, кириллицу туда не положить, а ``base64:``-форма для
    sub-info-* в доках Happ не описана (в отличие от announce). Тело же
    отдаётся в UTF-8, и документация Happ разрешает каждый параметр
    комментарием перед ссылками наравне с заголовками.
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

    Едет заголовком — значения ASCII, это безопасно. Показывает его сам
    клиент за ≤3 дня до конца; это Advanced-механизм — без providerid в
    ответе клиент его игнорирует, поэтому наличие заголовка ещё не значит
    «баннер виден».
    """
    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    headers = _sub_response_headers(_Sub(timedelta(days=20)), "tok")
    assert headers["sub-expire"] == "1"
    assert headers["sub-expire-button-link"].startswith("https://t.me/")

    # Без срока предупреждать не о чем.
    assert "sub-expire" not in _sub_response_headers(_Sub(None), "tok")

    monkeypatch.setenv("SUB_STATUS_BANNER", "0")
    assert "sub-expire" not in _sub_response_headers(_Sub(timedelta(days=20)), "tok")


def test_announce_rides_as_documented_base64(monkeypatch):
    """``announce`` — Standard-параметр Happ: работает без Provider ID, и
    форма ``base64:<b64>`` для него документирована. Это единственный видимый
    блок статуса до регистрации providerid."""
    import base64 as b64

    monkeypatch.delenv("HAPP_PROVIDER_ID", raising=False)
    headers = _sub_response_headers(_Sub(timedelta(days=20)), "tok")
    raw = headers["announce"]
    assert raw.startswith("base64:")
    text = b64.b64decode(raw[len("base64:"):]).decode("utf-8")
    assert "активна до" in text


def test_announce_yields_to_sub_info_when_provider_id_set(monkeypatch):
    """С providerid статус рисует sub-info — второй блок с тем же текстом был
    бы дублем, поэтому дефолт auto глушит announce. ``1`` — страховка, если
    sub-info не взлетит и с providerid; ``0`` — выключить совсем."""
    monkeypatch.setenv("HAPP_PROVIDER_ID", "pid-1")
    headers = _sub_response_headers(_Sub(timedelta(days=20)), "tok")
    assert "announce" not in headers
    assert headers["providerid"] == "pid-1"

    monkeypatch.setenv("SUB_STATUS_ANNOUNCE", "1")
    assert "announce" in _sub_response_headers(_Sub(timedelta(days=20)), "tok")

    monkeypatch.delenv("HAPP_PROVIDER_ID", raising=False)
    monkeypatch.setenv("SUB_STATUS_ANNOUNCE", "0")
    assert "announce" not in _sub_response_headers(_Sub(timedelta(days=20)), "tok")


def test_provider_id_travels_in_body_without_colon(monkeypatch):
    """Формат из доков — ``#providerid {id}``, БЕЗ двоеточия. С двоеточием
    клиент строку не узнает, и весь Advanced-слой останется мёртвым."""
    import base64 as b64

    from app.api_extensions import _sub_body

    monkeypatch.setenv("HAPP_PROVIDER_ID", "pid-1")

    class _Cfg:
        uri = "vless://u@h:443#x"

    body = b64.b64decode(_sub_body([_Cfg()], _Sub(timedelta(days=5)))).decode()
    assert body.splitlines()[0] == "#providerid pid-1"
    assert "#providerid:" not in body


def test_provider_id_survives_banner_kill_switch(monkeypatch):
    """SUB_STATUS_BANNER=0 гасит блок статуса, но providerid активирует весь
    Advanced-слой (пуши об истечении и т.п.) и не должен выключаться вместе
    с ним."""
    import base64 as b64

    from app.api_extensions import _sub_body

    monkeypatch.setenv("HAPP_PROVIDER_ID", "pid-1")
    monkeypatch.setenv("SUB_STATUS_BANNER", "0")

    headers = _sub_response_headers(_Sub(timedelta(days=5)), "tok")
    assert headers["providerid"] == "pid-1"
    assert "sub-expire" not in headers
    assert "announce" not in headers

    class _Cfg:
        uri = "vless://u@h:443#x"

    body = b64.b64decode(_sub_body([_Cfg()], _Sub(timedelta(days=5)))).decode()
    assert body.splitlines()[0] == "#providerid pid-1"
    assert "sub-info" not in body


def test_kill_switch_silences_announce_too(monkeypatch):
    """SUB_STATUS_BANNER=0 — аварийный рубильник ВСЕГО блока статуса, включая
    announce. Без этого теста мутант `if _announce_enabled():` (без проверки
    рубильника) проходил весь файл: с providerid announce глушится в auto и
    сам по себе, и дырку было не видно."""
    monkeypatch.delenv("HAPP_PROVIDER_ID", raising=False)
    monkeypatch.setenv("SUB_STATUS_BANNER", "0")
    assert "announce" not in _sub_response_headers(_Sub(timedelta(days=20)), "tok")

    # Даже «слать всегда» не пробивает рубильник: он для аварий, а не режимов.
    monkeypatch.setenv("SUB_STATUS_ANNOUNCE", "1")
    assert "announce" not in _sub_response_headers(_Sub(timedelta(days=20)), "tok")


def test_expiry_push_needs_a_date():
    """notification-subs-expire без срока подписки — напоминание ни о чём."""
    assert "notification-subs-expire" not in _sub_response_headers(_Sub(None), "tok")


def test_broken_provider_id_is_dropped_not_mangled(monkeypatch):
    """Кривой Provider ID из vault не едет ВООБЩЕ: CR/LF в заголовке — 500 на
    всей выдаче, а кириллицу _header_safe завернул бы в base64: — и клиент
    получил бы в заголовке и теле два РАЗНЫХ id."""
    import base64 as b64

    from app.api_extensions import _sub_body

    for bad in ("pid\nInjected: 1", "пид-кириллицей", "pid с пробелом"):
        monkeypatch.setenv("HAPP_PROVIDER_ID", bad)
        headers = _sub_response_headers(_Sub(timedelta(days=5)), "tok")
        assert "providerid" not in headers, bad

        class _Cfg:
            uri = "vless://u@h:443#x"

        body = b64.b64decode(_sub_body([_Cfg()], _Sub(timedelta(days=5)))).decode()
        assert "#providerid" not in body, bad

    # Нормальный ID (alnum, дефис, подчёркивание) проходит как есть.
    monkeypatch.setenv("HAPP_PROVIDER_ID", "WreSqg1i")
    assert _sub_response_headers(_Sub(timedelta(days=5)), "tok")["providerid"] == "WreSqg1i"


def test_userinfo_expire_identical_for_naive_and_aware(monkeypatch):
    """naive-датам БД приписывается UTC явно: ``.timestamp()`` наивного
    datetime берёт ЛОКАЛЬНУЮ зону процесса, и вне UTC-контейнера дата, от
    которой клиент считает «через сколько платить», поехала бы.

    TZ здесь принудительно не-UTC: в UTC-харнессе тест без этого вакуумный —
    мутант с выпиленной нормализацией проходил его (проверено мутацией)."""
    import time

    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        naive = _Sub(timedelta(days=3))
        aware = _Sub(timedelta(days=3))
        aware.expires_at = naive.expires_at.replace(tzinfo=timezone.utc)

        def userinfo(sub):
            return _sub_response_headers(sub, "tok")["subscription-userinfo"]

        assert userinfo(naive) == userinfo(aware)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


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


def test_userinfo_expire_only_is_opt_in():
    """`?userinfo=expire` — диагностика шкалы «0B/∞»: без трафик-ключей Happ,
    по гипотезе, не рисует пустышку, сохранив дату. Дефолт трогать нельзя:
    Hiddify выбрасывает неполный userinfo целиком вместе с support-url."""
    sub = _Sub(timedelta(days=5))
    assert _sub_response_headers(sub, "tok", userinfo_expire_only=True)[
        "subscription-userinfo"
    ].startswith("expire=")
    assert _sub_response_headers(sub, "tok")["subscription-userinfo"].startswith(
        "upload=0; "
    )


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
