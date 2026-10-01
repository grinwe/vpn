#!/usr/bin/env python3
"""Проверка IP-кандидатов по публичному белому списку мобильного интернета РФ.

Списки — скан-агрегаты сообщества (hxehex/russia-mobile-internet-whitelist),
не официальный реестр. Попадание в список = кандидат прошёл предварительный
фильтр, а НЕ гарантия доступности: в списке есть мусор (7.0.0.0/8 — DoD,
неанонсируемый диапазон) и адреса, попавшие туда со скана у конкретного
оператора в конкретный момент. Финальный гейт — пробер из целевого региона.

    ./scripts/check_whitelist_ip.py 1.2.3.4 5.6.7.8
    ./scripts/check_whitelist_ip.py --file candidates.txt
    ./scripts/check_whitelist_ip.py --refresh 1.2.3.4     # перекачать список

Код возврата: 0 — все проверенные IP в списке, 1 — есть непопавшие.
"""

from __future__ import annotations

import argparse
import ipaddress
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

CIDR_URL = (
    "https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist"
    "/main/cidrwhitelist.txt"
)
CACHE = Path(__file__).resolve().parent / ".cache" / "cidrwhitelist.txt"

# Заведомый мусор скана: неанонсируемые/служебные диапазоны, попавшие в список
# со сканов изнутри операторских сетей. Совпадение по ним ничего не значит.
JUNK = [ipaddress.ip_network(n) for n in ("7.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10")]


def fetch(refresh: bool) -> Path:
    if CACHE.exists() and not refresh:
        return CACHE
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"качаю {CIDR_URL} …", file=sys.stderr)
    # curl, а не urllib: системный python на дев-машине собран без рабочего ssl
    subprocess.run(
        ["curl", "-sSfL", "--max-time", "60", "-o", str(CACHE), CIDR_URL],
        check=True,
    )
    return CACHE


def load(path: Path) -> dict[int, list]:
    """Индекс префиксов по старшим 16 битам — линейный скан по 30k сетей медленный."""
    index: dict[int, list] = defaultdict(list)
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            net = ipaddress.ip_network(raw)
        except ValueError:
            continue
        lo = int(net.network_address) >> 16
        hi = int(net.broadcast_address) >> 16
        for key in range(lo, hi + 1):
            index[key].append(net)
    return index


def match(index: dict[int, list], ip: str) -> list:
    addr = ipaddress.ip_address(ip)
    return [n for n in index.get(int(addr) >> 16, []) if addr in n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ips", nargs="*", help="IP-адреса кандидатов")
    ap.add_argument("--file", type=Path, help="файл со списком IP (по одному в строке)")
    ap.add_argument("--refresh", action="store_true", help="перекачать список, игнорируя кэш")
    args = ap.parse_args()

    targets = list(args.ips)
    if args.file:
        targets += [ln.strip() for ln in args.file.read_text().splitlines() if ln.strip()]
    if not targets:
        ap.error("нечего проверять: передай IP или --file")

    index = load(fetch(args.refresh))
    total = len({n for nets in index.values() for n in nets})
    print(f"список загружен ({total} префиксов)\n")

    # Три исхода, а не два. «Не в списке» — это НЕ «адрес плохой»: список ловит
    # лишь малую часть разрешённых префиксов (из девяти доказанно рабочих
    # адресов конкурента в нём нашлись два). Отбраковывать по нему купленный
    # адрес — значит выбрасывать хорошее; см. docs/operations/whitelist_counters.md §3.1.
    hit = unknown = junk_cnt = bad = 0
    for ip in targets:
        try:
            hits = match(index, ip)
        except ValueError:
            print(f"  {ip:18} ошибка: не IP-адрес")
            bad += 1
            continue
        if not hits:
            print(f"  {ip:18} не определено — списку неизвестен (НЕ значит «плохой»)")
            unknown += 1
            continue
        junk = all(any(h.subnet_of(j) for j in JUNK) for h in hits)
        if junk:
            print(f"  {ip:18} мусор скана: {', '.join(str(h) for h in hits)}")
            junk_cnt += 1
        else:
            print(f"  {ip:18} В СПИСКЕ: {', '.join(str(h) for h in hits)}")
            hit += 1

    print(
        f"\nитого: в списке {hit}, не определено {unknown}, мусор скана {junk_cnt}"
        + (f", не IP {bad}" if bad else "")
    )
    print(
        "Попадание — дешёвый ПОЛОЖИТЕЛЬНЫЙ сигнал, годный как цель для ролла адреса.\n"
        "Непопадание не значит ничего: у списка плохой recall (обновлён 24.07.2026).\n"
        "Настоящая проверка — открыть адрес с мобильной SIM в затронутом регионе."
    )
    # Ненулевой код только на том, что реально плохо: не-IP и мусорные диапазоны.
    # «Не определено» — нормальный исход, скриптом не отбраковывается.
    return 1 if (bad or junk_cnt) else 0


if __name__ == "__main__":
    sys.exit(main())
