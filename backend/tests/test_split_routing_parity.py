"""Паритет RU split-routing между всеми четырьмя протоколами.

История, ради которой этот тест существует: правки RU-обхода в этом проекте
стабильно приземлялись в ОДИН протокол за раз. reality получил RU-правила
21.04.2026, xhttp — только 05.06.2026, ws-cdn не получал их никогда и до
2026-07-28 гнал через WG весь трафик, включая российский, потому что
дефолтный ``direct`` всё равно получает sockopt на wgN. Отдельно ловилась
регрессия, когда geoip-правило вело на ``direct`` вместо ``direct-local``
(e72e2db → 678fb3b): клиент видел IP exit-ноды на 2ip.ru.

Тест рендерит все шаблоны в трёх топологиях и проверяет инварианты,
нарушение любого из которых означает утечку трафика не в ту сторону.
Он намеренно НЕ ходит в БД и не требует сети.

hysteria2 живёт по другим правилам (YAML + ACL вместо JSON + routing.rules),
поэтому его инварианты проверяются отдельным блоком в конце файла — но список
РУ-зон и доменов у него ОБЩИЙ с xray-шаблонами (роль ru_direct_list), и это
тоже проверяется.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import pytest
import yaml

ROLES = Path(__file__).resolve().parents[2] / "infra" / "ansible" / "roles"

if not ROLES.is_dir():  # прогон без infra/ (напр. смонтирован только backend/)
    pytest.skip("infra/ansible/roles недоступна", allow_module_level=True)

# Единый источник правды для РУ-списков — роль-носитель. Все шаблоны рендерят
# ЕГО, каждый в свой синтаксис; захардкоженных копий быть не должно.
RU_LIST_VARS = yaml.safe_load(
    (ROLES / "ru_direct_list" / "defaults" / "main.yml").read_text()
)

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
    rendered = env.get_template(filename).render(
        **base_vars, **RU_LIST_VARS, **TOPOLOGIES[topology]
    )
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
    # ...и это должен быть freedom: подмена на blackhole «зарезала» бы РУ-сайты
    # вместо того, чтобы пускать их напрямую.
    assert outbounds["direct-local"]["protocol"] == "freedom"
    assert outbounds["block"]["protocol"] == "blackhole"

    # На relay-ноде дефолтный direct обязан быть привязан к туннелю — иначе
    # ВЕСЬ не-РУ трафик уйдёт напрямую с РУ-IP (то есть без VPN).
    primary = TOPOLOGIES[topology].get("xray_primary_interface")
    if primary:
        assert (
            outbounds["direct"]["streamSettings"]["sockopt"]["interface"] == primary
        )
    else:
        assert "streamSettings" not in outbounds["direct"]


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
    """Сам паритет: доменный список обязан совпадать во всех xray-шаблонах.

    Источник один (роль ``ru_direct_list``), но шаблон может отрендерить его
    криво или частично — тест сверяет результат, а не входные данные.
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


def test_dns_and_rule_shape_identical_across_protocols() -> None:
    """Не только домены: резолверы (и их ПОРЯДОК) и форма RU-правил тоже
    обязаны совпадать. Иначе один протокол начнёт классифицировать РУ-домены
    иначе, чем два других, на той же ноде."""
    ref_dns = None
    ref_shape = None
    for template in TEMPLATES:
        cfg = _render(template, "single-link-relay")
        dns = cfg["dns"]["servers"]
        # форма: последовательность (ip|domain, outboundTag) для RU-блока
        shape = [
            ("ip" if r.get("ip") else "domain", r["outboundTag"])
            for r in cfg["routing"]["rules"]
            if r.get("ip") or r.get("domain")
        ]
        if ref_dns is None:
            ref_dns, ref_shape = dns, shape
            continue
        assert dns == ref_dns, f"{template}: dns.servers разошлись с reality"
        assert shape == ref_shape, f"{template}: форма RU-правил разошлась"


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


# ── hysteria2 ────────────────────────────────────────────────────────────
# Другой формат (YAML + ACL вместо JSON + routing.rules), те же инварианты:
# РУ — мимо туннеля, всё остальное — в туннель, приватные сети закрыты.
# До 2026-07-28 секций outbounds/acl тут не было вовсе, и hy2 на relay-ноде
# выпускал ВЕСЬ трафик с российского IP — клиент показывал «подключено», а
# заблокированное оставалось заблокированным.

HY2_TEMPLATES = ROLES / "install_hysteria2" / "templates"
HY2_BASE_VARS = {
    "hysteria2_port": 443,
    "hysteria2_domain": "node1.example.com",
    "hysteria2_cert_path": "/etc/letsencrypt/live/node1/fullchain.pem",
    "hysteria2_key_path": "/etc/letsencrypt/live/node1/privkey.pem",
    "hysteria2_existing_userpass": {"user-1-2": "secret"},
}


def _render_hy2(topology: str) -> dict:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(HY2_TEMPLATES)),
        undefined=jinja2.StrictUndefined,
    )
    env.filters["dictsort"] = lambda d: sorted(d.items())
    # ``lookup('password', ...)`` — ansible-only, генерит sentinel-пароль.
    env.globals["lookup"] = lambda *a, **k: "x" * 32
    rendered = env.get_template("config.yaml.j2").render(
        **HY2_BASE_VARS, **RU_LIST_VARS, **TOPOLOGIES[topology]
    )
    return yaml.safe_load(rendered)


@pytest.mark.parametrize("topology", list(TOPOLOGIES))
def test_hy2_renders_valid_yaml(topology: str) -> None:
    cfg = _render_hy2(topology)
    assert cfg["auth"]["userpass"], "пустой userpass → hysteria уходит в краш-луп"


def test_hy2_direct_node_has_no_split_sections() -> None:
    """На ноде без relay-линков туннеля нет — секции не должны появляться.

    Иначе ``bindDevice`` укажет на несуществующий интерфейс и весь трафик
    ноды умрёт.
    """
    cfg = _render_hy2("direct-node")
    assert "outbounds" not in cfg
    assert "acl" not in cfg


@pytest.mark.parametrize("topology", ["single-link-relay", "multi-link-relay"])
def test_hy2_relay_binds_tunnel(topology: str) -> None:
    """На relay-ноде есть оба outbound'а, tunnel забинден на wgN и стоит
    ПЕРВЫМ: при неприменившемся ACL hysteria шлёт всё в первый outbound, и
    деградация должна быть в сторону «РУ-сайты видят зарубежный IP», а не
    «VPN не работает вовсе»."""
    cfg = _render_hy2(topology)
    outbounds = cfg["outbounds"]
    assert [o["name"] for o in outbounds] == ["tunnel", "local"]
    assert outbounds[0]["direct"]["bindDevice"] == "wg0"
    # local — БЕЗ bindDevice: это и есть «мимо туннеля, с IP самой ноды».
    assert "bindDevice" not in outbounds[1].get("direct", {})


@pytest.mark.parametrize("topology", ["single-link-relay", "multi-link-relay"])
def test_hy2_acl_order_and_catchall(topology: str) -> None:
    """Порядок ACL несущий: приватные сети режем первыми, РУ уводим в local,
    и последним правилом всё остальное обязано уйти в туннель."""
    acl = _render_hy2(topology)["acl"]["inline"]
    assert acl[0] == "reject(geoip:private)"
    assert acl[-1] == "tunnel(all)"
    assert "local(geoip:ru)" in acl
    # Ни одно правило не ссылается на несуществующий outbound.
    used = {r.split("(", 1)[0] for r in acl}
    assert used <= {"reject", "local", "tunnel"}, used


def test_hy2_uses_the_same_ru_list_as_xray() -> None:
    """Списки РУ-зон и доменов у hysteria2 и xray обязаны совпадать.

    Ровно ради этого списки вынесены в роль ``ru_direct_list``: до неё
    правка приземлялась в один протокол и разъезжалась с остальными.
    """
    acl = _render_hy2("single-link-relay")["acl"]["inline"]
    hy2_suffixes = {
        r[len("local(suffix:") : -1] for r in acl if r.startswith("local(suffix:")
    }

    rules = _render("reality", "single-link-relay")["routing"]["rules"]
    xray_domains = next(r["domain"] for r in rules if r.get("domain"))
    xray_zones = {
        d[len("regexp:\\.") : -1] for d in xray_domains if d.startswith("regexp:")
    }
    xray_suffixes = {
        d[len("domain:") :] for d in xray_domains if d.startswith("domain:")
    }

    assert hy2_suffixes == xray_zones | xray_suffixes, (
        f"списки разошлись: только у hy2 {hy2_suffixes - (xray_zones | xray_suffixes)}, "
        f"только у xray {(xray_zones | xray_suffixes) - hy2_suffixes}"
    )


def test_hy2_geoip_path_matches_xray_geoip_role() -> None:
    """ACL ссылается на geoip.dat, который качает роль xray_geoip — второй
    копии файла и второго таймера обновления быть не должно."""
    cfg = _render_hy2("single-link-relay")
    assert cfg["acl"]["geoip"] == "/usr/local/share/xray/geoip.dat"

    fetcher = (ROLES / "xray_geoip" / "files" / "xray-geoip-fetch.sh").read_text()
    assert "/usr/local/share/xray/geoip.dat" in fetcher
