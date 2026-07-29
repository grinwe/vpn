"""Страница «починить и продлить» на домене саб-ссылки (?fix=1).

Главный инвариант: страница НЕ должна вести себя как выдача конфигов.
Открытие её браузером ничего не пишет в БД (ни аудита, ни отметки первой
выдачи), не жжёт шаг лестницы ротации и не ломает профиль тому, кто по
ошибке вставил ссылку с ?fix=1 в VPN-клиент как подписку.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app import models
from app.api import sub_fix
from app.time_utils import utcnow

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)

HTML = {"accept": "text/html,application/xhtml+xml"}
CLIENT_UA = {"accept": "*/*"}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Лимит POST'а ключуется по токену и живёт в памяти процесса — без
    сброса второй тест с тем же токеном ловит 429 от первого."""
    from app.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def sub_with_token(db_session, monkeypatch):
    monkeypatch.setenv("SUB_FIX_PAGE", "1")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-for-nonce")
    node = make_node(db_session, name="fix-node", host="203.0.113.200")
    make_config(db_session, node)
    plan = make_plan(db_session, name="fix-plan")
    user = make_user(db_session, telegram_id="fix-user")
    sub = make_subscription_with_device(db_session, user, plan, node)
    device = sub.devices[0]
    device.sub_token = "fixtoken1234567890"
    db_session.add(
        models.Credential(
            subscription_id=sub.id,
            device_id=device.id,
            node_id=node.id,
            proto="vless-reality",
            config_text="enc-stub",
            access_username=device.access_username,
            is_active=True,
        )
    )
    db_session.commit()
    return sub, device


def _counts(db):
    return (
        db.query(models.AuditLog).count(),
        db.query(models.OperatorNodeReport).count(),
    )


def test_get_page_writes_nothing(client, db_session, sub_with_token):
    """Открытие страницы — read-only. Иначе префетчер браузера отравлял бы
    онбординг-воронку и аудит выдачи."""
    sub, device = sub_with_token
    before = _counts(db_session)
    first_fetch_before = db_session.get(models.User, sub.user_id).first_config_fetch_at

    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Починить подключение" in resp.text

    db_session.expire_all()
    assert _counts(db_session) == before
    assert (
        db_session.get(models.User, sub.user_id).first_config_fetch_at
        == first_fetch_before
    )


def test_page_headers_do_not_leak_the_token(client, sub_with_token):
    """Referrer-policy обязателен: без него токен уедет в Referer при
    переходе в Telegram или на оплату."""
    _sub, device = sub_with_token
    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store, private"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-robots-tag"] == "noindex"


def test_vpn_client_still_gets_config_even_with_fix_param(client, sub_with_token):
    """Человек вставил ссылку с ?fix=1 в клиент как подписку — он обязан
    получить конфиг, а не HTML: иначе мы сами сломали ему профиль."""
    _sub, device = sub_with_token
    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=CLIENT_UA)
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "<html" not in resp.text.lower()


def test_plain_sub_link_is_untouched(client, sub_with_token):
    """Без ?fix выдача не меняется ни на байт."""
    _sub, device = sub_with_token
    resp = client.get(f"/api/sub/{device.sub_token}", headers=HTML)
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_unknown_token_serves_camo(client, monkeypatch):
    """Никаких «подписка не найдена»: разница в ответе — оракул для пробера.
    Код 200, как на корне домена (см. test_camo_is_indistinguishable…)."""
    monkeypatch.setenv("SUB_FIX_PAGE", "1")
    resp = client.get("/api/sub/definitely-not-a-token?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert "Internal Tools Portal" in resp.text
    assert "подписк" not in resp.text.lower()


def test_page_off_by_default(client, db_session, sub_with_token, monkeypatch):
    """Рубильник: при SUB_FIX_PAGE=0 страницы нет вовсе — отдаём конфиг."""
    monkeypatch.setenv("SUB_FIX_PAGE", "0")
    _sub, device = sub_with_token
    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert "text/plain" in resp.headers["content-type"]


def test_post_without_nonce_changes_nothing(client, db_session, sub_with_token):
    """CSRF: без валидного nonce действие не выполняется и в БД не пишет."""
    _sub, device = sub_with_token
    before = _counts(db_session)

    resp = client.post(f"/api/sub/{device.sub_token}?fix=1")
    assert resp.status_code == 200
    db_session.expire_all()
    assert _counts(db_session) == before, "жалоба без nonce записываться не должна"

    resp = client.post(f"/api/sub/{device.sub_token}?fix=1&n=deadbeefdeadbeef")
    assert resp.status_code == 200
    db_session.expire_all()
    assert _counts(db_session) == before


def test_post_with_nonce_runs_the_ladder(client, db_session, sub_with_token, monkeypatch):
    """Валидный nonce → шаг лестницы и жалоба в аудите.

    Жалоба считается по ЭТОМУ юзеру: БД в свите общая на сессию, и счётчик
    без фильтра ловил бы чужие записи."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    sub, device = sub_with_token
    nonce = sub_fix.make_nonce(device.sub_token)

    resp = client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

    db_session.expire_all()
    complaints = (
        db_session.query(models.AuditLog)
        .filter_by(
            action="complaint_received", target_type="user", target_id=sub.user_id
        )
        .count()
    )
    assert complaints == 1, "жалоба обязана пережить даже неудачную починку"


def test_nonce_is_bound_to_its_token(sub_with_token):
    """Nonce от чужой подписки не подходит — иначе одна утёкшая страница
    давала бы кнопку на все остальные."""
    _sub, device = sub_with_token
    other = sub_fix.make_nonce("some-other-token")
    assert sub_fix.nonce_valid(device.sub_token, other) is False
    assert sub_fix.nonce_valid(device.sub_token, sub_fix.make_nonce(device.sub_token))
    # Прошлое окно принимаем: страницу открыли 14 минут назад.
    assert sub_fix.nonce_valid(
        device.sub_token, sub_fix.make_nonce(device.sub_token, offset=1)
    )


def test_repeat_posts_are_rate_limited(client, sub_with_token):
    """Утёкший токен = деструктивная кнопка: без лимита им можно вычерпать
    пул нод. Ключ — токен, а не IP: у операторов CGNAT, и per-IP лимит
    выкосил бы половину абонентов из-за одного нетерпеливого."""
    _sub, device = sub_with_token
    nonce = sub_fix.make_nonce(device.sub_token)
    url = f"/api/sub/{device.sub_token}?fix=1&n={nonce}"

    codes = [client.post(url).status_code for _ in range(4)]
    assert 429 in codes, codes
    # GET-выдача конфигов при этом не задета — она на своём роуте.
    assert client.get(f"/api/sub/{device.sub_token}").status_code == 200


def test_device_name_is_escaped(client, db_session, sub_with_token):
    """Имя устройства задаёт пользователь — это XSS-вектор."""
    _sub, device = sub_with_token
    device.name = '<script>alert("xss")</script>'
    db_session.commit()

    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert "<script>alert" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_expired_subscription_gets_renewal_screen(client, db_session, sub_with_token):
    """Истёкшая подписка на странице не 403-ит (как в выдаче конфигов), а
    показывает продление — ради этого экрана эпик и затевался."""
    sub, device = sub_with_token
    sub.expires_at = utcnow() - timedelta(days=1)
    sub.status = models.SubscriptionStatus.expired
    db_session.commit()

    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert "закончилась" in resp.text.lower()


def test_frozen_subscription_explains_itself(client, db_session, sub_with_token):
    sub, device = sub_with_token
    sub.status = models.SubscriptionStatus.frozen
    db_session.commit()

    resp = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert "паузе" in resp.text.lower()


def test_alias_repairs_the_live_sibling(client, db_session, sub_with_token, monkeypatch):
    """Токен ревокнутого устройства (сохранённая ссылка после миграции)
    обязан чинить живого соседа — это тот же alias-инвариант, что у выдачи."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    sub, old_device = sub_with_token
    node = make_node(db_session, name="fix-node-2", host="203.0.113.201")
    cfg = make_config(db_session, node)
    live = models.Device(
        user_id=sub.user_id,
        subscription_id=sub.id,
        config_id=cfg.id,
        name="новое",
        status=models.DeviceStatus.active,
        access_username="user-live",
        sub_token="livetoken0987654321",
    )
    db_session.add(live)
    db_session.flush()
    db_session.add(
        models.Credential(
            subscription_id=sub.id,
            device_id=live.id,
            node_id=node.id,
            proto="vless-reality",
            config_text="enc-stub",
            access_username="user-live",
            is_active=True,
        )
    )
    old_device.status = models.DeviceStatus.revoked
    for cred in old_device.credentials:
        cred.is_active = False
    db_session.commit()

    resp = client.get(f"/api/sub/{old_device.sub_token}?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert "новое" in resp.text


def test_camo_is_indistinguishable_from_the_root(client, monkeypatch):
    """Код ответа и заголовки camo обязаны совпадать с корнем домена: иначе
    сам 404 становится оракулом «этого токена нет»."""
    monkeypatch.setenv("SUB_FIX_PAGE", "1")
    resp = client.get("/api/sub/nonexistent-token-xxx?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "strict-transport-security" in resp.headers


def test_bad_paid_param_does_not_break_the_hot_path(client, sub_with_token, monkeypatch):
    """?paid= нечисловым не должен ронять горячий роут в 422 — он общий с
    выдачей конфигов, и типизированный int делал бы это независимо от флагов."""
    _sub, device = sub_with_token
    resp = client.get(f"/api/sub/{device.sub_token}?fix=1&paid=abc", headers=HTML)
    assert resp.status_code == 200

    monkeypatch.setenv("SUB_FIX_PAGE", "0")
    resp = client.get(f"/api/sub/{device.sub_token}?paid=abc")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_rate_limit_answers_html_not_json(client, sub_with_token):
    """429 браузеру — человеческий экран: голый JSON и тупик для человека,
    и выдаёт себя проберу («скучный портал» отвечает ошибкой API)."""
    _sub, device = sub_with_token
    nonce = sub_fix.make_nonce(device.sub_token)
    url = f"/api/sub/{device.sub_token}?fix=1&n={nonce}"
    last = None
    for _ in range(4):
        last = client.post(url, headers=HTML)
    assert last.status_code == 429
    assert "text/html" in last.headers["content-type"]
    assert "Слишком часто" in last.text


def test_post_repair_checks_subscription_status(client, db_session, sub_with_token):
    """Право на починку проверяет ЯДРО, а не экран.

    Экран истёкшей подписки кнопку не рисует, но POST приходит по URL — и
    nonce для него достаётся бесплатно (POST без nonce возвращает страницу,
    а на ней валидный nonce). Без проверки в ядре человек в grace-периоде
    жёг бы слоты нод и прогоны ansible, а каждая «починка» вливала бы
    фальшивый fail-голос против здоровой ноды в крауд-матрицу.
    """
    sub, device = sub_with_token
    sub.expires_at = utcnow() - timedelta(hours=1)
    sub.status = models.SubscriptionStatus.expired
    db_session.commit()
    before = _counts(db_session)

    nonce = sub_fix.make_nonce(device.sub_token)
    resp = client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}")
    assert resp.status_code == 200

    db_session.expire_all()
    reports_before, reports_after = before[1], _counts(db_session)[1]
    assert reports_after == reports_before, "починки быть не должно"
