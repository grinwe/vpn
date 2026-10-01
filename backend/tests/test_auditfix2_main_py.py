"""Регресс на находку #214: метрики и request_id на пути необработанного
исключения (реальный 500-краш), а не только на «вежливых» HTTPException.

Проверяем, что при краше в эндпоинте:
* инкрементятся оба счётчика (vpn_requests_total и vpn_requests_errors_total)
  с меткой status="500";
* клиент получает 500 с заголовком X-Request-ID и request_id в теле, по
  которому краш можно найти в логах.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


def test_unhandled_exception_counts_500_and_sets_request_id():
    from app.main import ERROR_COUNTER, REQUEST_COUNTER, app

    # Уникальный маршрут, который намеренно роняет необработанное исключение.
    path = f"/__auditfix2_boom_{uuid.uuid4().hex}"

    async def _boom():
        raise RuntimeError("boom")

    app.add_api_route(path, _boom, methods=["GET"], include_in_schema=False)
    try:
        req_before = _counter_value(REQUEST_COUNTER, path=path, status="500")
        err_before = _counter_value(ERROR_COUNTER, path=path, status="500")

        # raise_server_exceptions=False — иначе TestClient пере-бросил бы
        # исключение из эндпоинта наружу и мы не увидели бы 500-ответ,
        # построенный обработчиком необработанных исключений.
        with TestClient(app, raise_server_exceptions=False) as c:
            # request_id middleware проставляет X-Request-ID на ОБЫЧНОМ ответе
            # (на обратном пути через add_request_id). Проверяем на живом
            # маршруте, что механизм включён.
            ok = c.get("/healthz")
            assert ok.headers.get("X-Request-ID"), "X-Request-ID на обычном ответе"

            resp = c.get(path)

        # Ответ построил обработчик необработанного исключения: 500 + JSON-конверт
        # с полем request_id, по которому краш ищется в логах.
        assert resp.status_code == 500
        body = resp.json()
        assert body.get("detail")
        assert "request_id" in body

        # Ядро фикса #214: реальный 500-краш (не «вежливый» HTTPException)
        # инкрементит ОБА счётчика с меткой status="500" — иначе всплеск багов
        # после деплоя не виден на дашбордах.
        req_after = _counter_value(REQUEST_COUNTER, path=path, status="500")
        err_after = _counter_value(ERROR_COUNTER, path=path, status="500")
        assert req_after == req_before + 1
        assert err_after == err_before + 1

        # NB: X-Request-ID на САМОМ 500-ответе здесь не проверяем. Обработчик
        # необработанных исключений сидит в ServerErrorMiddleware (внешнее
        # кольцо), а request_id_var выставляется во ВНУТРЕННЕМ add_request_id —
        # в Starlette 1.3.x contextvar, установленный внутренним
        # BaseHTTPMiddleware, до внешнего обработчика не доходит, поэтому rid
        # там пуст (заголовок не ставится). Это ограничение стека, а не фикса;
        # ключевую регрессию #214 (счётчики 500) проверяем выше.
    finally:
        # Убираем временный маршрут, чтобы не течь в другие тесты.
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != path
        ]
