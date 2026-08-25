"""Xray-JSON для клиента: разбор наших URI и сборка профиля с автовыбором.

Тесты чистые — без БД и HTTP: модуль занимается только превращением строк в
структуру, и проверять это дешевле всего напрямую.
"""

import json

import pytest
from app.services import xray_client_config as xj


class _Cfg:
    """Заглушка SubLinkConfig — модулю нужны только protocol и uri."""

    def __init__(self, protocol, uri):
        self.protocol = protocol
        self.uri = uri


REALITY = (
    "vless://e1ddc2fe-0000-4000-8000-000000000001@81.90.31.111:443"
    "?encryption=none&security=reality&sni=vk.ru"
    "&pbk=luaJ3b6bEBqOSbkBm3umCjp-4cffjOj3CS87mZWvBE4"
    "&sid=dccc34dd1cda07f0&flow=xtls-rprx-vision&fp=firefox&type=tcp"
    "#⚡ Основной"
)
XHTTP = (
    "vless://26f19ef0-0000-4000-8000-000000000002@p01.grinwer.online:443"
    "?encryption=none&security=tls&sni=p01.grinwer.online&fp=firefox"
    "&type=xhttp&host=p01.grinwer.online&path=%2Fxh&mode=auto"
    "#\U0001f6e1️ Запасной"
)
WS = (
    "vless://1a83b90c-0000-4000-8000-000000000003@1dfa66176137387f.wgse.info:443"
    "?encryption=none&security=tls&sni=1dfa66176137387f.wgse.info&fp=firefox"
    "&type=ws&host=1dfa66176137387f.wgse.info&path=%2Fws"
    "#☁️ Резервный"
)
HY2 = (
    "hy2://warm-13-abc:pass@171.22.134.124:443"
    "?sni=9a2fbbafb4f83a30.wgse.info&obfs=salamander"
    "&obfs-password=scTH7VM7r5G4e47S0bxqmOdtGd5GsmlE&mport=20000-40000"
    "#\U0001f680 Быстрый 1"
)

FULL_SET = [
    _Cfg("vless-reality", REALITY),
    _Cfg("hysteria2", HY2),
    _Cfg("vless-xhttp", XHTTP),
    _Cfg("vless-ws-cdn", WS),
]


# ── разбор отдельных легов ─────────────────────────────────────────────


def test_reality_leg_maps_to_reality_outbound():
    ob = xj.uri_to_outbound(REALITY, "main")
    assert ob["protocol"] == "vless"
    assert ob["tag"] == "main"
    vnext = ob["settings"]["vnext"][0]
    assert vnext["address"] == "81.90.31.111"
    assert vnext["port"] == 443
    user = vnext["users"][0]
    assert user["id"] == "e1ddc2fe-0000-4000-8000-000000000001"
    assert user["encryption"] == "none"
    # vision живёт в users[], а не в streamSettings — частая ошибка переноса
    assert user["flow"] == "xtls-rprx-vision"
    stream = ob["streamSettings"]
    assert stream["network"] == "tcp"
    assert stream["security"] == "reality"
    assert stream["realitySettings"] == {
        "serverName": "vk.ru",
        "publicKey": "luaJ3b6bEBqOSbkBm3umCjp-4cffjOj3CS87mZWvBE4",
        "shortId": "dccc34dd1cda07f0",
        "fingerprint": "firefox",
    }


def test_xhttp_leg_keeps_mode_and_decodes_path():
    ob = xj.uri_to_outbound(XHTTP, "main-2")
    stream = ob["streamSettings"]
    assert stream["network"] == "xhttp"
    # path в URI percent-энкожен, в конфиге должен быть обычным
    assert stream["xhttpSettings"] == {
        "path": "/xh",
        "host": "p01.grinwer.online",
        "mode": "auto",
    }
    assert stream["security"] == "tls"
    assert stream["tlsSettings"]["serverName"] == "p01.grinwer.online"
    assert stream["tlsSettings"]["allowInsecure"] is False
    # у tls-легов flow нет, и пустым его ставить нельзя
    assert "flow" not in ob["settings"]["vnext"][0]["users"][0]


def test_ws_leg_carries_host_in_both_supported_forms():
    """Xray ≥ 26 ждёт независимое ``host``, ядра постарше — ``headers.Host``.
    Потеря Host = мёртвый лег: nginx на ноде разводит ws-запросы по нему."""
    ob = xj.uri_to_outbound(WS, "main-3")
    stream = ob["streamSettings"]
    assert stream["network"] == "ws"
    ws = stream["wsSettings"]
    assert ws["path"] == "/ws"
    assert ws["host"] == "1dfa66176137387f.wgse.info"
    assert ws["headers"] == {"Host": "1dfa66176137387f.wgse.info"}


def test_hysteria2_leg_is_refused():
    """hy2 у нас с obfs, а формат hysteria-outbound его не выражает — перенос
    дал бы неподключающийся сервер, поэтому лег отбрасывается целиком."""
    assert xj.uri_to_outbound(HY2, "main") is None


@pytest.mark.parametrize("uri", [
    "vless://id@host:443?security=reality&type=tcp",          # reality без pbk
    "vless://id@host:443?security=tls&type=grpc",             # транспорт не наш
    "vless://id@host:443?security=none&type=tcp",             # без шифрования
    "ss://whatever@host:443#x",                               # чужая схема
    "не-ссылка-вовсе",
    # Кривой порт: .port — ленивое свойство и бросает ValueError уже ПОСЛЕ
    # разбора, а не на urlsplit. Пропуск этого = 500 на выдаче конфигов.
    "vless://id@host:abc?security=tls&type=ws",
    "vless://id@host:99999?security=tls&type=ws",
])
def test_unparseable_legs_return_none(uri):
    assert xj.uri_to_outbound(uri, "main") is None


# ── сборка набора профилей ─────────────────────────────────────────────


def test_profiles_start_with_autoselect_then_one_per_leg():
    profiles = xj.build_profiles(FULL_SET)
    # 3 vless-лега (hy2 отброшен) + профиль автовыбора
    assert len(profiles) == 4
    assert profiles[0]["remarks"] == "\U0001f3af Автовыбор"
    # имена одиночных профилей берутся из #fragment, где уже стоит роль
    assert [p["remarks"] for p in profiles[1:]] == [
        "⚡ Основной",
        "\U0001f6e1️ Запасной",
        "☁️ Резервный",
    ]


def test_autoselect_balancer_covers_every_leg_by_prefix():
    auto = xj.build_profiles(FULL_SET)[0]
    tags = [o["tag"] for o in auto["outbounds"] if o["tag"].startswith("main")]
    assert tags == ["main", "main-2", "main-3"]

    balancer = auto["routing"]["balancers"][0]
    # селектор матчит по префиксу: один "main" обязан накрыть все три лега
    assert balancer["selector"] == ["main"]
    assert all(t.startswith(balancer["selector"][0]) for t in tags)
    assert balancer["strategy"] == {"type": "leastPing"}
    assert balancer["fallbackTag"] == "main"

    obs = auto["observatory"]
    assert obs["subjectSelector"] == ["main"]
    assert obs["probeInterval"].endswith("s")
    assert obs["enableConcurrency"] is True


def test_probe_interval_is_configurable_and_floored(monkeypatch):
    """Проба — это трафик, и с enableConcurrency он множится на число легов.
    Интервал обязан быть управляемым, но опускать его до секунд нельзя."""
    monkeypatch.delenv("SUB_XRAY_PROBE_INTERVAL_S", raising=False)
    assert xj.build_profiles(FULL_SET)[0]["observatory"]["probeInterval"] == "300s"

    monkeypatch.setenv("SUB_XRAY_PROBE_INTERVAL_S", "600")
    assert xj.build_profiles(FULL_SET)[0]["observatory"]["probeInterval"] == "600s"

    # Пол — 30 с: ниже проба начинает соперничать с полезным трафиком
    monkeypatch.setenv("SUB_XRAY_PROBE_INTERVAL_S", "5")
    assert xj.build_profiles(FULL_SET)[0]["observatory"]["probeInterval"] == "30s"

    # Мусор не должен ронять выдачу конфигов
    monkeypatch.setenv("SUB_XRAY_PROBE_INTERVAL_S", "быстро")
    assert xj.build_profiles(FULL_SET)[0]["observatory"]["probeInterval"] == "300s"


def test_autoselect_routes_traffic_through_balancer_not_outbound():
    auto = xj.build_profiles(FULL_SET)[0]
    rules = auto["routing"]["rules"]
    catch_all = rules[-1]
    assert catch_all["balancerTag"] == "auto"
    assert "outboundTag" not in catch_all
    # приватные сети — раньше catch-all, иначе локалка уедет в туннель
    assert rules[0]["outboundTag"] == "direct"
    assert "192.168.0.0/16" in rules[0]["ip"]


def test_single_leg_profile_has_no_balancer():
    single = xj.build_profiles(FULL_SET)[1]
    assert "balancers" not in single["routing"]
    assert "observatory" not in single
    assert single["routing"]["rules"][-1]["outboundTag"] == "proxy"
    assert single["outbounds"][0]["tag"] == "proxy"


def test_no_client_side_routing_rules_beyond_private_nets():
    """RU-split у нас на ноде. Если сюда once приедут доменные списки — это
    расхождение с серверной раскладкой, а не фича."""
    for profile in xj.build_profiles(FULL_SET):
        for rule in profile["routing"]["rules"]:
            assert "domain" not in rule
        assert "dns" not in profile


def test_one_usable_leg_falls_back_to_plain_list():
    only_one = [_Cfg("vless-reality", REALITY), _Cfg("hysteria2", HY2)]
    assert xj.build_profiles(only_one) is None
    assert xj.build_body(only_one) is None


def test_body_is_json_array_of_profiles():
    body = xj.build_body(FULL_SET)
    parsed = json.loads(body)
    assert isinstance(parsed, list) and len(parsed) == 4
    # кириллица и эмодзи должны ехать как есть, а не \uXXXX
    assert "Автовыбор" in body


# ── гейт выдачи ────────────────────────────────────────────────────────


def test_json_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SUB_XRAY_JSON", raising=False)
    assert xj.wants_xray_json("happ", "tok") is False


def test_allowlist_serves_only_listed_tokens(monkeypatch):
    monkeypatch.setenv("SUB_XRAY_JSON", "allowlist")
    monkeypatch.setenv("SUB_XRAY_JSON_TOKENS", "tok-a, tok-b")
    assert xj.wants_xray_json("happ", "tok-a") is True
    assert xj.wants_xray_json("happ", "tok-b") is True
    assert xj.wants_xray_json("happ", "tok-c") is False
    assert xj.wants_xray_json("happ", "") is False


@pytest.mark.parametrize("client,expected", [
    ("happ", True),
    ("v2raytun", True),
    ("hiddify", False),
    ("streisand", False),
    ("v2rayng", False),
    ("unknown", False),
])
def test_only_json_capable_clients_get_json(monkeypatch, client, expected):
    monkeypatch.setenv("SUB_XRAY_JSON", "on")
    assert xj.wants_xray_json(client, "tok") is expected


def test_bad_mode_value_reads_as_off(monkeypatch):
    monkeypatch.setenv("SUB_XRAY_JSON", "yes-please")
    assert xj.wants_xray_json("happ", "tok") is False
