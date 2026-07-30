"""Заголовки подписки собираются ПОД КЛИЕНТА, а не одним набором на всех.

Набор украшений у клиентов разный: у Happ богатый Advanced-слой за Provider
ID, у v2rayTun одна строка announce с раскраской и кликом, у Hiddify нет
ничего, кроме шкалы трафика и двух ссылок в меню профиля.

Ветвление появилось не ради красоты: имя ``announce`` у Happ и v2rayTun
общее, но синтаксис разный — цветовые коды ``#RRGGBB``, которые v2rayTun
красит, Happ показал бы буквально.
"""
from __future__ import annotations

import base64
from datetime import timedelta

from app.api_extensions import _client_kind, _sub_response_headers
from app.time_utils import utcnow

UA = {
    "happ": "Happ/2.4.1 (iPhone; iOS 18.5)",
    "v2raytun": "v2rayTun/5.24.76 (Android 13)",
    "hiddify": "HiddifyNext/4.1.1 (windows) like ClashMeta v2ray sing-box",
    "streisand": "Streisand/1.6.6 (iPhone)",
    "v2rayng": "v2rayNG/1.9.16",
}


class _Sub:
    def __init__(self, expires_in: timedelta | None = timedelta(days=20)):
        self.id = 1
        self.user_id = 1
        self.expires_at = (utcnow() + expires_in) if expires_in is not None else None


def _headers(ua: str | None):
    """``ua`` — либо ключ из UA, либо готовая строка User-Agent, либо None."""
    return _sub_response_headers(_Sub(), "tok", user_agent=UA.get(ua, ua))


def _prep(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "GV8_VPN_bot")
    monkeypatch.setenv("SUB_LINK_BASE_URL", "https://grn-ssync.pro")
    monkeypatch.setenv("SUB_FIX_ENTRYPOINTS", "all")
    monkeypatch.setenv("HAPP_PROVIDER_ID", "WreSqg1i")


# ── классификатор ───────────────────────────────────────────────────────


def test_hiddify_is_not_mistaken_for_v2raytun():
    """HiddifyNext называет себя «like ClashMeta v2ray sing-box» — подстрока
    «v2ray» не должна уводить его в чужую ветку."""
    assert _client_kind(UA["hiddify"]) == "hiddify"
    assert _client_kind(UA["v2raytun"]) == "v2raytun"
    assert _client_kind(UA["happ"]) == "happ"
    assert _client_kind(UA["streisand"]) == "streisand"
    assert _client_kind(UA["v2rayng"]) == "v2rayng"


def test_unknown_and_empty_ua_are_unknown():
    """Неопознанный клиент получает нынешний общий набор — ветвление не может
    сломать того, кого мы не узнали."""
    assert _client_kind(None) == "unknown"
    assert _client_kind("") == "unknown"
    assert _client_kind("curl/8.4.0") == "unknown"


# ── Happ ────────────────────────────────────────────────────────────────


def test_happ_keeps_everything_it_had(monkeypatch):
    """Happ-путь обязан остаться нынешним: providerid, sub-info-*, sub-expire."""
    _prep(monkeypatch)
    h = _headers("happ")
    assert h["providerid"] == "WreSqg1i"
    assert h["sub-info-text"].startswith("base64:")
    assert h["sub-info-button-text"].startswith("base64:")
    assert h["sub-expire"] == "1"
    assert h["notification-subs-expire"] == "1"
    # Цветовых кодов v2rayTun в его announce быть не может — он покажет их
    # текстом. С заданным providerid announce вообще молчит (auto-режим).
    assert "announce-url" not in h


def test_unknown_client_gets_the_happ_set(monkeypatch):
    """Для неопознанного UA поведение ровно как до ветвления."""
    _prep(monkeypatch)
    assert _headers(None)["providerid"] == "WreSqg1i"
    # Именно НЕПУСТОЙ неопознанный UA, а не None: это разные ветки.
    assert "sub-info-text" in _headers("curl/8.4.0")
    assert "sub-info-text" in _headers("SomeFutureClient/1.0")


# ── v2rayTun ────────────────────────────────────────────────────────────


def test_v2raytun_gets_clickable_coloured_announce(monkeypatch):
    """У v2rayTun нет ни блока, ни кнопки: «кнопочность» — слова внутри
    announce плюс announce-url, который делает текст тапабельным."""
    _prep(monkeypatch)
    h = _headers("v2raytun")

    assert h["announce-url"] == "https://grn-ssync.pro/tok?fix=1"
    text = base64.b64decode(h["announce"][len("base64:"):]).decode()
    assert "подключается?" in text
    # Цвет ставится перед КАЖДЫМ словом: инлайн-код красит одно следующее
    # слово, а не текст до следующего кода (живая проверка 2026-07-30).
    assert text.count("#e05252") == 4, text
    # Срок НЕ дублируем: клиент рисует «Активна до …» сам, рядом со шкалой.
    assert "активна до" not in text.lower()

    # Happ-специфика ему не уезжает: он её игнорирует, а мы не мусорим.
    for dead in ("providerid", "sub-info-text", "sub-expire", "notification-subs-expire"):
        assert dead not in h, dead


def test_v2raytun_says_nothing_without_the_page(monkeypatch):
    """Вести некуда — строки нет вовсе: срок клиент показывает сам, а
    объявление без ссылки не несёт ничего нового."""
    _prep(monkeypatch)
    monkeypatch.setenv("SUB_FIX_ENTRYPOINTS", "off")
    h = _headers("v2raytun")
    assert "announce" not in h
    assert "announce-url" not in h


# ── Hiddify ─────────────────────────────────────────────────────────────


def test_hiddify_gets_the_page_in_profile_menu(monkeypatch):
    """У Hiddify нет текстовых блоков вообще — вход на страницу отдаём
    единственным доступным способом, ссылкой в меню профиля."""
    _prep(monkeypatch)
    h = _headers("hiddify")
    assert h["profile-web-page-url"] == "https://grn-ssync.pro/tok?fix=1"
    # Шкала трафика и срок у него работают — их шлём всем.
    assert "subscription-userinfo" in h
    assert "profile-title" in h
    # А вот украшений, которых он не понимает, не шлём.
    for dead in ("announce", "sub-info-text", "providerid", "sub-expire"):
        assert dead not in h, dead


# ── общее для всех ──────────────────────────────────────────────────────


def test_shared_headers_go_to_everyone(monkeypatch):
    """Совпадающие по семантике заголовки — одни на всех: их понимают все
    клиенты одинаково."""
    _prep(monkeypatch)
    for kind in ("happ", "v2raytun", "hiddify", "streisand", None):
        h = _headers(kind)
        assert h["profile-title"] == "V8-VPN", kind
        assert "profile-update-interval" in h, kind
        assert "subscription-userinfo" in h, kind
        assert h["cache-control"] == "no-store, private", kind


def test_every_client_gets_latin1_safe_headers(monkeypatch):
    """Кириллица в заголовке роняет ВЕСЬ ответ подписки (latin-1). Новый
    v2rayTun-путь несёт русский текст, поэтому проверяем каждый клиент."""
    _prep(monkeypatch)
    for kind in ("happ", "v2raytun", "hiddify", None):
        for key, value in _headers(kind).items():
            key.encode("latin-1")
            value.encode("latin-1")


def test_known_but_unbranched_clients_lose_nothing(monkeypatch):
    """Streisand и v2rayNG классифицируются (пригодится для статистики), но
    веток под них нет — значит набор им остаётся ПРЕЖНИМ.

    Иначе правка была бы чистой потерей: раньше они получали announce со
    статусом подписки, а после ветвления не получили бы ничего взамен.
    """
    _prep(monkeypatch)
    for kind in ("streisand", "v2rayng"):
        h = _headers(kind)
        assert h["providerid"] == "WreSqg1i", kind
        assert "sub-info-text" in h, kind
        assert h["sub-expire"] == "1", kind


def test_hiddify_entry_is_support_url_not_just_profile_page(monkeypatch):
    """У Hiddify support-url ведёт на страницу.

    profile-web-page-url на актуальных версиях не отображается (баги
    hiddify-app#2063, #1722), а блока статуса и объявлений у него нет вовсе.
    Значит support-url — его ЕДИНСТВЕННЫЙ достижимый вход, и уводить его в
    Telegram значит оставить второго по массовости клиента без входа ровно
    тогда, когда Telegram недоступен.
    """
    _prep(monkeypatch)
    h = _headers("hiddify")
    assert h["support-url"] == "https://grn-ssync.pro/tok?fix=1"
    assert h["profile-web-page-url"] == "https://grn-ssync.pro/tok?fix=1"

    # А у Happ иконка остаётся телеграмной: у него вход есть большой кнопкой.
    assert _headers("happ")["support-url"].startswith("https://t.me/")


def test_v2raytun_announce_has_its_own_kill_switch(monkeypatch):
    """Рубильник SUB_STATUS_ANNOUNCE гасит и v2rayTun-рендер — он единственный
    ещё не проверен на живом устройстве, и выключать его должно быть чем-то
    точнее общего SUB_STATUS_BANNER."""
    _prep(monkeypatch)
    assert "announce" in _headers("v2raytun")

    monkeypatch.setenv("SUB_STATUS_ANNOUNCE", "0")
    assert "announce" not in _headers("v2raytun")
    assert "announce-url" not in _headers("v2raytun")


def test_v2raytun_announce_survives_providerid(monkeypatch):
    """В режиме auto announce гаснет у Happ при заданном providerid (там его
    заменяет sub-info), но у v2rayTun он ЕДИНСТВЕННЫЙ способ показать статус —
    гасить его из-за чужого providerid значит оставить клиента ни с чем."""
    _prep(monkeypatch)          # providerid задан
    monkeypatch.delenv("SUB_STATUS_ANNOUNCE", raising=False)
    assert "announce" in _headers("v2raytun")
    assert "announce" not in _headers("happ"), "у Happ дубля быть не должно"


def test_body_directives_skip_clients_that_cannot_read_them(monkeypatch):
    """#providerid и #sub-info-* в теле — fallback ДЛЯ HAPP. v2rayTun и Hiddify
    их не читают, а тело человек может открыть глазами: мусорить не надо."""
    from app.api_extensions import _sub_body

    _prep(monkeypatch)

    class _Cfg:
        uri = "vless://u@h:443#x"

    for kind in ("happ", "unknown"):
        body = base64.b64decode(_sub_body([_Cfg()], _Sub(), token="tok", client=kind)).decode()
        assert "#providerid" in body, kind
        assert "#sub-info-text" in body, kind

    for kind in ("v2raytun", "hiddify"):
        body = base64.b64decode(_sub_body([_Cfg()], _Sub(), token="tok", client=kind)).decode()
        assert "#" not in body.split("vless://")[0], kind
        assert body.strip() == "vless://u@h:443#x", kind


def test_vary_header_declares_ua_dependency(monkeypatch):
    """Ответ зависит от клиента — говорим это вслух любому кэшу на пути."""
    _prep(monkeypatch)
    assert _headers("happ")["vary"] == "User-Agent"


def test_support_rung_is_quieter_than_all(monkeypatch):
    """Режим ``support`` — переходная ступень раската: ссылка на страницу уже
    есть, громкого призыва ещё нет. У Happ на этой ступени крупная кнопка не
    появляется, и ломать лестницу ради одного клиента незачем."""
    _prep(monkeypatch)
    monkeypatch.setenv("SUB_FIX_ENTRYPOINTS", "support")

    h = _headers("v2raytun")
    text = base64.b64decode(h["announce"][7:]).decode()
    assert h["announce-url"] == "https://grn-ssync.pro/tok?fix=1", "ссылка есть"
    assert "#e05252" not in text, "громкого призыва на этой ступени нет"

    # У Hiddify в support работает support-url, а второго пункта меню с тем
    # же адресом быть не должно — это дубль.
    hid = _headers("hiddify")
    assert hid["support-url"] == "https://grn-ssync.pro/tok?fix=1"
    assert "profile-web-page-url" not in hid
