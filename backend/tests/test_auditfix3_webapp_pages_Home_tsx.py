"""Audit-fix #159: тумблер автопродления должен уважать busy.

Клик по автопродлению — это денежная настройка. Раньше div с onClick
не проверял busy и не имел disabled, поэтому быстрый двойной тап слал два
POST с одинаковым newValue (пропсы обновляются только после onRefresh),
и тумблер скакал. Фикс — ранний return по busy плюс визуальное приглушение.

Проверяем статически по исходнику Home.tsx (tsx нельзя гонять из pytest).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HOME_TSX = (
    Path(__file__).resolve().parents[2]
    / "webapp"
    / "src"
    / "pages"
    / "Home.tsx"
)

# Home.tsx лежит в webapp/ (../../webapp от tests/). В backend-only тест-образе
# смонтирован только backend/, поэтому файла нет — проверять нечего. Полный
# чекаут (CI) резолвит parents[2] в корень репо и гоняет грепы по-настоящему.
if not HOME_TSX.is_file():
    pytest.skip(
        "Home.tsx недоступен (backend-only тест-образ) — проверяется в полном "
        "чекауте/CI",
        allow_module_level=True,
    )


def _read() -> str:
    return HOME_TSX.read_text(encoding="utf-8")


def test_home_tsx_exists() -> None:
    assert HOME_TSX.is_file(), f"не найден {HOME_TSX}"


def test_toggle_auto_renew_guards_busy() -> None:
    src = _read()
    # Тело функции handleToggleAutoRenew — от объявления до следующей function.
    m = re.search(
        r"async function handleToggleAutoRenew\(\)\s*\{(.*?)\n\s*\}\n",
        src,
        re.DOTALL,
    )
    assert m, "не найдено тело handleToggleAutoRenew"
    body = m.group(1)
    # Ранний return по busy должен идти до вычисления newValue.
    guard = body.index("if (busy) return")
    new_value = body.index("const newValue")
    assert guard < new_value, "проверка busy должна стоять до newValue"


def test_toggle_container_dims_while_busy() -> None:
    src = _read()
    # Контейнер тумблера визуально приглушается и блокирует клики при busy.
    assert "opacity-50 pointer-events-none" in src, (
        "контейнер тумблера должен глушиться классами при busy"
    )
