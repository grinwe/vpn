"""Конфиг wg0 на exit-ноде обязан быть валидным для wg.

История, ради которой тест существует: 2026-07-28 в шаблон добавили блок
PostUp/PostDown, а перед ним — Jinja-комментарий с обоими strip-маркерами
(``{#- … -#}``). Закрывающий маркер съел перевод строки, и в отрендеренном
конфиге получилось ``ListenPort = 51820PostUp = iptables …`` одной строкой.

Ломалось это молча и предельно неприятно:

* уже поднятые пиры продолжали работать (они живут в ядре, не в файле);
* ``wg syncconf`` падал с «Servname not supported for ai_socktype» ⇒ новый
  relay-пир на exit не доезжал НИКОГДА — attach проходил «успешно», а
  туннель не поднимался (нода avps-ru-01: 943 КБ отправлено, 0 получено);
* ``wg-quick up`` после ребута exit'а не поднял бы интерфейс вовсе ⇒ все
  relay'и через этот exit легли бы разом.

Тест намеренно не ходит в сеть и в БД: он рендерит шаблон и проверяет
структуру. Дешёвая страховка от повторения — маркеры strip легко вернуть
случайно, а цена ошибки фронтальная.
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest

ROLES = Path(__file__).resolve().parents[2] / "infra" / "ansible" / "roles"

if not ROLES.is_dir():  # прогон без infra/ (смонтирован только backend/)
    pytest.skip("infra/ansible/roles недоступна", allow_module_level=True)

TEMPLATE = ROLES / "wg_exit_node" / "templates" / "wg0.conf.j2"

# Ключи, которые wg-quick отдаёт в `wg setconf` и которые обязаны быть
# самостоятельными строками. PostUp/PostDown wg-quick забирает себе, но и они
# должны стоять отдельно — иначе strip не распознает их и отдаст в wg как мусор.
_INTERFACE_KEYS = ("PrivateKey", "Address", "ListenPort")


def _render(*, peers: list[dict] | None = None) -> str:
    # trim_blocks=True — КАК РЕНДЕРИТ ANSIBLE, и это принципиально: он срезает
    # перевод строки сразу после закрывающего тега блока/комментария. С
    # дефолтами Jinja тот же шаблон рендерится иначе, и первая версия этого
    # теста именно поэтому зеленела на всё ещё сломанном шаблоне. Тест обязан
    # воспроизводить рантайм, а не абстрактную Jinja.
    env = jinja2.Environment(
        undefined=jinja2.ChainableUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    return env.from_string(TEMPLATE.read_text()).render(
        wg_exit_private_key="cHJpdmF0ZS1rZXktc3R1Yg=",
        wg_exit_address_v4="10.77.0.1/24",
        wg_exit_port=51820,
        wg_exit_network_v4="10.77.0.0/24",
        wg_exit_wan_iface="ens3",
        wg_exit_peers=peers or [],
    )


def test_interface_keys_are_on_their_own_lines():
    """Ровно тот дефект: PostUp приклеился к ListenPort."""
    rendered = _render()
    for line in rendered.splitlines():
        keys_on_line = [k for k in (*_INTERFACE_KEYS, "PostUp", "PostDown") if k in line]
        assert len(keys_on_line) <= 1, (
            f"на одной строке два ключа {keys_on_line}: {line[:120]!r}"
        )


def test_listen_port_value_is_a_bare_number():
    """`ListenPort = 51820PostUp = …` — это то, на чём wg говорит
    «Servname not supported for ai_socktype»."""
    rendered = _render()
    ports = re.findall(r"^ListenPort\s*=\s*(.+)$", rendered, re.MULTILINE)
    assert ports, "ListenPort вообще не отрендерился"
    for value in ports:
        assert value.strip().isdigit(), f"ListenPort = {value!r}"


def test_every_key_line_has_a_value():
    """Страховка от обратной ошибки: strip-маркер, съевший строку так, что
    ключ остался без значения."""
    rendered = _render()
    for line in rendered.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        assert "=" in stripped, f"строка без присваивания: {stripped[:120]!r}"
        key, _, value = stripped.partition("=")
        assert key.strip(), f"пустой ключ: {stripped[:120]!r}"
        assert value.strip(), f"пустое значение у {key.strip()!r}"


def test_peers_render_as_separate_sections():
    """Пир — это то, ради чего файл вообще перезаписывают: если секции
    склеятся, `wg syncconf` снова начнёт падать, и на exit не доедет уже
    ни один relay."""
    rendered = _render(
        peers=[
            {"name": "relay-a", "public_key": "cGVlci1vbmU=",
             "allowed_ips_v4": "10.77.0.2/32"},
            {"name": "relay-b", "public_key": "cGVlci10d28=",
             "allowed_ips_v4": "10.77.0.3/32"},
        ]
    )
    assert rendered.count("[Peer]") == 2, rendered
    for line in rendered.splitlines():
        assert not ("[Peer]" in line and "PublicKey" in line), line[:120]
