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


def test_hysteria2_leg_carries_obfs_in_finalmask():
    """obfs у hy2 живёт не в настройках протокола, а в finalmask.udp как маска
    salamander. Без неё сервер не ответит — на ноде обфускация включена."""
    ob = xj.uri_to_outbound(HY2, "solo")
    assert ob["protocol"] == "hysteria"
    assert ob["settings"]["address"] == "171.22.134.124"
    assert ob["settings"]["port"] == 443
    assert ob["settings"]["version"] == 2
    # auth — наша пара логин:пароль, а не UUID
    assert ob["settings"]["auth"] == "warm-13-abc:pass"

    stream = ob["streamSettings"]
    assert stream["network"] == "hysteria"
    assert stream["hysteriaSettings"] == {"version": 2, "auth": "warm-13-abc:pass"}
    assert stream["tlsSettings"]["serverName"] == "9a2fbbafb4f83a30.wgse.info"
    # hysteria2 ходит поверх QUIC — без явного h3 часть стеков рвёт хендшейк
    assert stream["tlsSettings"]["alpn"] == ["h3"]
    assert stream["finalmask"] == {
        "udp": [{
            "type": "salamander",
            "settings": {"password": "scTH7VM7r5G4e47S0bxqmOdtGd5GsmlE"},
        }],
    }


def test_port_hopping_is_off_unless_enabled(monkeypatch):
    """Хоппинг упирается не в клиента, а в ноду: без DNAT-правила пакеты на
    порты диапазона уходят в никуда, и рабочий лег становится молчащим."""
    monkeypatch.delenv("SUB_XRAY_HY2_HOP", raising=False)
    ob = xj.uri_to_outbound(HY2, "solo")
    assert "quicParams" not in ob["streamSettings"]["finalmask"]


def test_port_hopping_carries_range_from_the_uri(monkeypatch):
    monkeypatch.setenv("SUB_XRAY_HY2_HOP", "all")
    ob = xj.uri_to_outbound(HY2, "solo", hy2_hop=True)
    quic = ob["streamSettings"]["finalmask"]["quicParams"]
    # диапазон — ровно тот, что в ссылке; интервал строкой, объект Xray не примет
    assert quic["udpHop"]["ports"] == "20000-40000"
    assert quic["udpHop"]["interval"] == "10-30"
    # обфускация никуда не девается — она в той же секции, но по своему ключу
    assert ob["streamSettings"]["finalmask"]["udp"][0]["type"] == "salamander"


@pytest.mark.parametrize("value,token,expected", [
    ("off", "tok", False),
    ("", "tok", False),
    ("all", "tok", True),
    ("on", "tok", True),
    ("tok-a,tok-b", "tok-a", True),
    ("tok-a,tok-b", "tok-c", False),
    ("tok-a", "", False),
])
def test_hop_gate_accepts_off_all_or_token_list(monkeypatch, value, token, expected):
    monkeypatch.setenv("SUB_XRAY_HY2_HOP", value)
    assert xj.hy2_hop_enabled(token) is expected


def test_hop_gate_reaches_the_profile(monkeypatch):
    """Гейт должен работать пер-подписочно — значит токен обязан доехать от
    build_body до самого лега, а не потеряться по дороге."""
    monkeypatch.setenv("SUB_XRAY_HY2_HOP", "тот-самый-токен")

    def hop_of(profiles):
        for p in profiles:
            for o in p["outbounds"]:
                if o["protocol"] == "hysteria":
                    return "quicParams" in o["streamSettings"].get("finalmask", {})
        raise AssertionError("hy2-лег потерялся")

    assert hop_of(xj.build_profiles(FULL_SET, "тот-самый-токен")) is True
    assert hop_of(xj.build_profiles(FULL_SET, "чужой-токен")) is False
    assert hop_of(xj.build_profiles(FULL_SET)) is False


def test_hysteria2_with_unknown_obfs_is_refused():
    """Незнакомую обфускацию выразить нечем, а без неё сервер молчит — такой
    лег честнее не отдать, чем отдать неподключающимся."""
    weird = HY2.replace("obfs=salamander", "obfs=нечто-новое")
    assert xj.uri_to_outbound(weird, "solo") is None


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
    # автовыбор + по профилю на каждый лег, в исходном порядке
    assert len(profiles) == 5
    assert profiles[0]["remarks"] == "\U0001f3af Автовыбор"
    # имена одиночных профилей берутся из #fragment, где уже стоит роль
    assert [p["remarks"] for p in profiles[1:]] == [
        "⚡ Основной",
        "\U0001f680 Быстрый 1",
        "\U0001f6e1️ Запасной",
        "☁️ Резервный",
    ]


def test_hysteria2_participates_in_the_balancer():
    """hy2 — равноправный лег автовыбора: проверено вживую, что при мёртвых
    vless балансировщик уводит трафик на hysteria."""
    auto = xj.build_profiles(FULL_SET)[0]
    protocols = {o["protocol"] for o in auto["outbounds"]}
    assert "hysteria" in protocols

    # и она должна попадать под префиксный селектор, иначе не пингуется
    selector = auto["routing"]["balancers"][0]["selector"][0]
    hy2_tags = [o["tag"] for o in auto["outbounds"] if o["protocol"] == "hysteria"]
    assert hy2_tags and all(t.startswith(selector) for t in hy2_tags)


def test_autoselect_balancer_covers_every_leg_by_prefix():
    auto = xj.build_profiles(FULL_SET)[0]
    tags = [o["tag"] for o in auto["outbounds"] if o["tag"].startswith("main")]
    # все четыре лега набора, включая hy2 — ни один не теряется по дороге
    assert tags == ["main", "main-2", "main-3", "main-4"]

    balancer = auto["routing"]["balancers"][0]
    # селектор матчит по префиксу: один "main" обязан накрыть весь набор
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


def test_no_client_side_routing_rules_beyond_private_nets(monkeypatch):
    """С выключенным ``SUB_XRAY_RU_DIRECT`` — как до него: ровно два правила
    (приватные сети, catch-all), доменных списков и dns нет. Это базовая
    выдача, на которую откатываемся значением ``off``."""
    monkeypatch.delenv("SUB_XRAY_RU_DIRECT", raising=False)
    profiles = xj.build_profiles(FULL_SET)
    for profile in profiles:
        for rule in profile["routing"]["rules"]:
            assert "domain" not in rule
        assert "dns" not in profile
    auto, single = profiles[0], profiles[1]
    assert [r["ruleTag"] for r in auto["routing"]["rules"] if "ruleTag" in r] == [
        "private-direct"
    ]
    assert len(auto["routing"]["rules"]) == 2
    assert auto["routing"]["rules"][-1]["balancerTag"] == "auto"
    assert len(single["routing"]["rules"]) == 2
    assert single["routing"]["rules"][-1]["outboundTag"] == "proxy"


# ── клиентское правило ru-direct ───────────────────────────────────────


def _rule_tags(profile):
    return [r.get("ruleTag") for r in profile["routing"]["rules"]]


def test_ru_direct_rule_sits_between_private_and_catch_all(monkeypatch):
    """На direct-нодах без туннеля серверный split невозможен, поэтому РУ-домены
    уводятся ещё на клиенте. Порядок несущий: private → ru-direct → catch-all
    (first-match-wins) — ниже catch-all правило не сработает никогда."""
    monkeypatch.setenv("SUB_XRAY_RU_DIRECT", "all")
    profiles = xj.build_profiles(FULL_SET)
    auto, singles = profiles[0], profiles[1:]

    assert _rule_tags(auto) == ["private-direct", "ru-direct", None]
    assert auto["routing"]["rules"][-1]["balancerTag"] == "auto"
    for single in singles:
        assert _rule_tags(single) == ["private-direct", "ru-direct", None]
        assert single["routing"]["rules"][-1]["outboundTag"] == "proxy"

    ru = auto["routing"]["rules"][1]
    assert ru["outboundTag"] == "direct"
    # Зоны — regexp с экранированной точкой, бренды — domain: (суффикс).
    assert "regexp:\\.ru$" in ru["domain"]
    assert "domain:vk.com" in ru["domain"]
    # geoip:ru на клиенте нет намеренно — зависимость от geoip.dat в ядре.
    assert "ip" not in ru
    # dns по-прежнему нет: РУ-домен резолвит сам клиент и идёт напрямую.
    assert "dns" not in auto


def test_ru_direct_rule_serialises_with_single_backslash(monkeypatch):
    """В JSON должно уехать ``"regexp:\\\\.ru$"`` — ровно как в конфиге ноды;
    двойное экранирование сделало бы regexp матчем литерального бэкслеша."""
    monkeypatch.setenv("SUB_XRAY_RU_DIRECT", "all")
    body = xj.build_body(FULL_SET)
    assert '"regexp:\\\\.ru$"' in body
    assert '"regexp:\\\\\\\\.ru$"' not in body


def test_ru_direct_gate_is_per_token(monkeypatch):
    """CSV токенов — правило только у перечисленных, остальные как раньше."""
    monkeypatch.setenv("SUB_XRAY_RU_DIRECT", "тот-самый-токен, ещё-один")

    def has_rule(profiles):
        return all("ru-direct" in _rule_tags(p) for p in profiles)

    assert has_rule(xj.build_profiles(FULL_SET, "тот-самый-токен")) is True
    assert has_rule(xj.build_profiles(FULL_SET, "ещё-один")) is True
    assert has_rule(xj.build_profiles(FULL_SET, "чужой-токен")) is False
    assert has_rule(xj.build_profiles(FULL_SET)) is False
    # и не «у части профилей»: либо во всех, либо ни в одном
    assert not any(
        "ru-direct" in _rule_tags(p) for p in xj.build_profiles(FULL_SET, "чужой")
    )


@pytest.mark.parametrize("value,token,expected", [
    ("off", "tok", False),
    ("", "tok", False),
    ("all", "tok", True),
    ("on", "tok", True),
    ("tok-a,tok-b", "tok-a", True),
    ("tok-a,tok-b", "tok-c", False),
    ("tok-a", "", False),
])
def test_ru_direct_gate_accepts_off_all_or_token_list(monkeypatch, value, token, expected):
    monkeypatch.setenv("SUB_XRAY_RU_DIRECT", value)
    assert xj.ru_direct_enabled(token) is expected


def test_single_leg_falls_back_to_plain_list():
    """Автовыбор из одного элемента — тот же сервер, но без запасного."""
    only_one = [_Cfg("vless-reality", REALITY)]
    assert xj.build_profiles(only_one) is None
    assert xj.build_body(only_one) is None


def test_client_allowlist_narrows_the_rollout(monkeypatch):
    """«Формат клиент понимает» и «мы проверили на живом устройстве» — разные
    утверждения; рычаг позволяет катить только на проверенных."""
    monkeypatch.setenv("SUB_XRAY_JSON", "on")

    monkeypatch.setenv("SUB_XRAY_JSON_CLIENTS", "happ")
    assert xj.wants_xray_json("happ", "tok") is True
    assert xj.wants_xray_json("v2raytun", "tok") is False

    monkeypatch.setenv("SUB_XRAY_JSON_CLIENTS", "happ, v2raytun")
    assert xj.wants_xray_json("v2raytun", "tok") is True

    # Пусто или мусор — все умеющие, а не «никто»: рычаг не должен молча
    # выключать фичу из-за опечатки в group_vars.
    monkeypatch.setenv("SUB_XRAY_JSON_CLIENTS", "")
    assert xj.wants_xray_json("happ", "tok") is True
    monkeypatch.setenv("SUB_XRAY_JSON_CLIENTS", "нечто")
    assert xj.wants_xray_json("happ", "tok") is True
    # но клиент вне списка умеющих не получает JSON ни при каких значениях
    assert xj.wants_xray_json("hiddify", "tok") is False


def test_body_is_json_array_of_profiles():
    body = xj.build_body(FULL_SET)
    parsed = json.loads(body)
    assert isinstance(parsed, list) and len(parsed) == 5
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
