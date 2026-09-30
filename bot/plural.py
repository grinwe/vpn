"""Склонение числительных для текстов бота.

Отдельный модуль без зависимостей от aiogram: его зовут и ``handlers``, и
``keyboards`` (кнопка «🎁 Забрать 3 дня бесплатно»). Бэкенд-версия
``_plural_days`` лежит в ``backend/app/api_extensions.py``, бот её
импортировать не может.
"""
from __future__ import annotations


def plural_days(n: int) -> str:
    """«1 день / 3 дня / 6 дней»: русские числительные требуют трёх форм."""
    n = int(n)
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    tail = n % 10
    if tail == 1:
        return f"{n} день"
    if tail in (2, 3, 4):
        return f"{n} дня"
    return f"{n} дней"
