"""Паритет RU split-routing между тремя vless-шаблонами.

История, ради которой этот тест существует: правки RU-обхода в этом проекте
стабильно приземлялись в ОДИН протокол за раз. reality получил RU-правила
21.04.2026, xhttp — только 05.06.2026, ws-cdn не получал их никогда и до
2026-07-28 гнал через WG весь трафик, включая российский, потому что
дефолтный ``direct`` всё равно получает sockopt на wgN. Отдельно ловилась
регрессия, когда geoip-правило вело на ``direct`` вместо ``direct-local``
(e72e2db → 678fb3b): клиент видел IP exit-ноды на 2ip.ru.

Тест рендерит все три шаблона в трёх топологиях и проверяет инварианты,
нарушение любого из которых означает утечку трафика не в ту сторону.
Он намеренно НЕ ходит в БД и не требует сети.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import pytest

ROLES = Path(__file__).resolve().parents[2] / "infra" / "ansible" / "roles"

# Минимальные обязательные переменные каждого шаблона: тест проверяет
# структуру роутинга, а не конкретные значения ключей/портов.
TEMPLATES = {
    "reality": (
        ROLES / "install_vless_reality" / "templates",
        "config.json.j2",
        {
            "vless_reality_port": 443,
            "vless_reality_dest": "www.samsung.com:443",
            "vless_reality_sni": "www.samsung.com",
            "vless_reality_private_key": "test-key",
            "vless_reality_short_id": "ab12",
        },
    ),
    "xhttp": (
        ROLES / "install_vless_xhttp" / "templates",
        "config_xhttp.json.j2",
        {"vless_xhttp_internal_port": 10443},
    ),
    "ws_cdn": (
        ROLES / "install_vless_ws_cdn" / "templates",
        "config_ws_cdn.json.j2",
        {"vless_ws_cdn_path": "/ws"},
    ),
}

# Топологии, в которых шаблон обязан рендериться корректно. Разница между
# ними — ровно те переменные, что приезжают из relay.py.
TOPOLOGIES = {
    "direct-node": {},
    "single-link-relay": {"xray_primary_interface": "wg0"},
    "multi-link-relay": {
        "xray_primary_interface": "wg0",
        "xray_relay_outbounds": [
            {"interface": "wg0", "emails": ["user-1-2"]},
            {"interface": "wg1", "emails": ["user-3-4"]},
        ],
    },
}

CASES = [(t, topo) for t in TEMPLATES for topo in TOPOLOGIES]


def _render(template: str, topology: str) -> dict:
    directory, filename, base_vars = TEMPLATES[template]
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(directory)),
        undefined=jinja2.StrictUndefined,
    )
    # Фильтры ansible, используемые шаблонами.
    env.filters["to_json"] = json.dumps
    env.filters["regex_replace"] = lambda s, pattern, repl: re.sub(pattern, repl, s)
    rendered = env.get_template(filename).render(**base_vars, **TOPOLOGIES[topology])
    return json.loads(rendered)


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_renders_valid_json(template: str, topology: str) -> None:
    cfg = _render(template, topology)
    assert cfg["routing"]["rules"], "пустой routing.rules"


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_ru_rules_win_over_fan_out(template: str, topology: str) -> None:
    """RU-правила обязаны стоять ВЫШЕ fan-out по user (first-match-wins).

    Если они опустятся ниже, юзер с ``exit_id`` начнёт гнать российский
    трафик через зарубежный exit — РУ-сайты ответят капчей или блоком.
    """
    rules = _render(template, topology)["routing"]["rules"]
    ru = [i for i, r in enumerate(rules) if r.get("outboundTag") == "direct-local"]
    fan_out = [
        i
        for i, r in enumerate(rules)
        if str(r.get("outboundTag", "")).startswith("direct-wg")
    ]
    assert len(ru) == 2, f"ожидались 2 RU-правила (домены + geoip), найдено {len(ru)}"
    if fan_out:
        assert max(ru) < min(fan_out), "RU-правило оказалось ниже fan-out по user"


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_ru_rules_point_at_direct_local(template: str, topology: str) -> None:
    """geoip:ru и domain-правило ведут именно на ``direct-local``.

    Регрессия e72e2db: правило вело на ``direct``, у которого на relay-ноде
    висит sockopt на wgN — то есть «RU-обход» отправлял трафик в туннель.
    """
    cfg = _render(template, topology)
    rules = cfg["routing"]["rules"]
    geoip_ru = [r for r in rules if "geoip:ru" in (r.get("ip") or [])]
    domain_rules = [r for r in rules if r.get("domain")]
    assert geoip_ru, "нет правила с geoip:ru"
    assert domain_rules, "нет доменного RU-правила"
    for rule in geoip_ru + domain_rules:
        assert rule["outboundTag"] == "direct-local", rule

    outbounds = {o["tag"]: o for o in cfg["outbounds"]}
    assert "direct-local" in outbounds, "нет outbound direct-local"
    # Без sockopt — в этом весь смысл: выход с IP самой ноды, мимо туннеля.
    assert "streamSettings" not in outbounds["direct-local"]


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_private_networks_blocked_first(template: str, topology: str) -> None:
    """Приватные сети и loopback ноды — в blackhole, до RU-правил.

    Иначе клиент дотягивается до gRPC StatsService ноды (перечисление всех
    email'ов и их трафика) и до 169.254.169.254.
    """
    cfg = _render(template, topology)
    rules = cfg["routing"]["rules"]
    blocked = [
        i
        for i, r in enumerate(rules)
        if r.get("outboundTag") == "block" and "geoip:private" in (r.get("ip") or [])
    ]
    assert blocked, "нет правила geoip:private → block"
    ru = [i for i, r in enumerate(rules) if r.get("outboundTag") == "direct-local"]
    assert min(blocked) < min(ru), "block приватных сетей стоит ниже RU-правил"
    assert any(o["tag"] == "block" for o in cfg["outbounds"])
    # geoip:private не должен остаться в правиле, ведущем наружу.
    for rule in rules:
        if rule.get("outboundTag") != "block":
            assert "geoip:private" not in (rule.get("ip") or []), rule


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_dns_and_domain_strategy_present(template: str, topology: str) -> None:
    """Без ``IPIfNonMatch`` и секции dns доменные правила работают только по
    отснифанному SNI, а IP-назначения не классифицируются вовсе."""
    cfg = _render(template, topology)
    assert cfg["routing"].get("domainStrategy") == "IPIfNonMatch"
    assert cfg.get("dns", {}).get("servers"), "нет dns.servers"


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_fan_out_rules_have_matching_outbound(template: str, topology: str) -> None:
    """Каждое правило ``direct-wgN`` имеет свой outbound.

    Висячий тег xray логирует как «non existing outTag» и молча роняет
    трафик на дефолтный outbound.
    """
    cfg = _render(template, topology)
    tags = {o["tag"] for o in cfg["outbounds"]}
    for rule in cfg["routing"]["rules"]:
        tag = str(rule.get("outboundTag", ""))
        if tag.startswith("direct-wg"):
            assert tag in tags, f"правило ссылается на несуществующий outbound {tag}"


@pytest.mark.parametrize(("template", "topology"), CASES)
def test_quic_sniffing_enabled(template: str, topology: str) -> None:
    """Без ``quic`` в destOverride для UDP/443 не извлекается SNI и доменные
    RU-правила на HTTP/3 не срабатывают."""
    for inbound in _render(template, topology)["inbounds"]:
        if inbound["tag"] == "api-in":
            continue
        assert "quic" in inbound["sniffing"]["destOverride"], inbound["tag"]


def test_ru_domain_lists_are_identical_across_protocols() -> None:
    """Сам паритет: доменный список обязан совпадать во всех трёх шаблонах.

    Пока RU-блок продублирован копипастой (вынести его в общий макрос —
    отдельная задача), это единственное, что мешает правке приземлиться в
    один протокол и разъехаться с остальными.
    """
    lists = {}
    for template in TEMPLATES:
        rules = _render(template, "multi-link-relay")["routing"]["rules"]
        domains = [r["domain"] for r in rules if r.get("domain")]
        assert len(domains) == 1, f"{template}: ожидалось одно доменное правило"
        lists[template] = domains[0]

    reference = lists["reality"]
    for template, domains in lists.items():
        assert domains == reference, (
            f"доменный список {template} разошёлся с reality: "
            f"лишние {set(domains) - set(reference)}, "
            f"недостающие {set(reference) - set(domains)}"
        )


def test_brand_rules_do_not_catch_foreign_domains() -> None:
    """Регрессия на якорь по первому лейблу.

    ``^(yandex|mail|vk|ok|...)\\..+$`` ловил mail.google.com, mail.proton.me,
    ok.google.com — они уходили напрямую с РУ-IP. Для proton это была жёсткая
    поломка: в РФ заблокирован, домен просто не открывался.
    """
    rules = _render("reality", "multi-link-relay")["routing"]["rules"]
    domains = next(r["domain"] for r in rules if r.get("domain"))
    patterns = [
        re.compile(d[len("regexp:") :]) for d in domains if d.startswith("regexp:")
    ]
    suffixes = [d[len("domain:") :] for d in domains if d.startswith("domain:")]

    def matches(host: str) -> bool:
        if any(p.search(host) for p in patterns):
            return True
        return any(host == s or host.endswith("." + s) for s in suffixes)

    for host in (
        "mail.google.com",
        "mail.proton.me",
        "mail.yahoo.com",
        "ok.google.com",
        "hh.com",
        "ria.city",
    ):
        assert not matches(host), f"{host} ошибочно уходит в RU-обход"

    for host in (
        "vk.com",
        "www.vk.com",
        "m.vk.com",
        "sun9-1.userapi.com",
        "mail.ru",
        "gosuslugi.ru",
        "xn--80aswg.xn--p1ai",
        "avatars.mds.yandex.net",
        "okko.tv",
    ):
        assert matches(host), f"{host} НЕ попал в RU-обход"
