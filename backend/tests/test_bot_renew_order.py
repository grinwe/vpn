"""/renew в боте продлевает правильную подписку (план триала 3 дня, «Бот»).

``/api/users/by_telegram/{tg}`` отдаёт подписки без ORDER BY. С короткими
триалами у юзера чаще бывает и старая истёкшая платная подписка, и истёкший
триал, и ``renewable[0]`` продлевал случайную. Бот сортирует сам: сначала
active, затем самая свежая по id. Бэкенд-запрос не трогаем: у него другие
потребители (карточки кабинета, админка).
"""
from __future__ import annotations

import pytest

from tests._bot_stubs import (
    FakeBot,
    FakeMessage,
    bot_sources_available,
    install_bot_stubs,
)

if not bot_sources_available():
    pytest.skip("bot/handlers.py недоступен в этом образе", allow_module_level=True)


@pytest.fixture()
def handlers(monkeypatch):
    return install_bot_stubs(monkeypatch)


def _renew_payloads(handlers, monkeypatch, subs: list[dict]) -> list[dict]:
    """Подменяем ``_fetch_json``: список подписок и отказ создания счёта
    (дальше выбора подписки хэндлер нам не нужен)."""
    payloads: list[dict] = []

    async def _fake(method, url, **kw):
        if url.endswith("/api/users/by_telegram/100"):
            return 200, subs
        if url.endswith("/api/invoices"):
            payloads.append(kw.get("json") or {})
            return 500, None
        raise AssertionError(f"неожиданный запрос {method} {url}")

    monkeypatch.setattr(handlers, "_fetch_json", _fake)
    return payloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subs", "expected_id"),
    [
        # Живая подписка важнее истёкших, где бы она ни стояла в списке.
        (
            [
                {"id": 5, "status": "expired", "plan_id": 1},
                {"id": 9, "status": "active", "plan_id": 1},
                {"id": 12, "status": "expired", "plan_id": 1},
            ],
            9,
        ),
        # Только истёкшие: самая свежая (истёкший триал после старой платной).
        (
            [
                {"id": 12, "status": "expired", "plan_id": 1},
                {"id": 3, "status": "expired", "plan_id": 2},
                {"id": 20, "status": "blocked", "plan_id": 1},
            ],
            12,
        ),
    ],
)
async def test_renew_picks_active_then_newest(handlers, monkeypatch, subs, expected_id):
    payloads = _renew_payloads(handlers, monkeypatch, subs)

    await handlers.cmd_renew(FakeMessage(FakeBot()))

    assert [p["subscription_id"] for p in payloads] == [expected_id]
    assert payloads[0]["action"] == "renewal"
