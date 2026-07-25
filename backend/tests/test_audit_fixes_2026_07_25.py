"""Регресс-тесты фиксов недельного аудита 2026-07-25.

Отчёт: docs/operations/audit_week_2026_07_25.md. Один файл на весь набор —
каждый тест назван по номеру находки, чтобы связь «находка → защита» не
терялась при последующих правках.
"""
import pytest

from app import queue as q
from app import worker
from app.api_webapp import issue_token
from app.config import get_settings

from .factories import make_node, make_plan, make_subscription, make_user


def _auth_headers(user_id: int) -> dict:
    settings = get_settings()
    token = issue_token(user_id, settings.webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


# ── P0-1: авто-активация триала не должна сносить живую подписку ──────────


def test_trial_autoactivate_blocked_when_user_has_live_sub(client, db_session):
    """Находка #1 (critical): /subscriptions/activate — это СМЕНА тарифа
    (отзывает active/frozen подписки + ревокает девайсы), поэтому webapp не
    имеет права звать его автоматически из триал-баннера у юзера с живой
    подпиской. Бэкенд обязан сказать «нельзя» флагом."""
    user = make_user(db_session, telegram_id="tg-trial-live")
    node = make_node(db_session, name="node-trial-live")
    plan = make_plan(db_session, name="plan-trial-live")
    make_subscription(db_session, user, plan, node)

    res = client.get("/api/webapp/me", headers=_auth_headers(user.id))
    assert res.status_code == 200, res.text
    balance = res.json()["balance"]
    # Сам бонус забрать можно — он просто ляжет на баланс и пойдёт на продление.
    assert balance["trial_available"] is True
    # А вот тратить его на «активировать план» автоматом — нет.
    assert balance["trial_autoactivate_allowed"] is False


def test_trial_autoactivate_allowed_for_user_without_sub(client, db_session):
    """Обратная сторона #1: у нового юзера (ровно та воронка, ради которой
    авто-активацию и вводили) поведение прежнее — бонус сразу тратится."""
    user = make_user(db_session, telegram_id="tg-trial-fresh")

    res = client.get("/api/webapp/me", headers=_auth_headers(user.id))
    assert res.status_code == 200, res.text
    balance = res.json()["balance"]
    assert balance["trial_available"] is True
    assert balance["trial_autoactivate_allowed"] is True


def test_trial_autoactivate_false_after_trial_claimed(client, db_session):
    """Флаг не должен «разрешать» авто-активацию после того, как триал уже
    забран — иначе повторный тап (или гонка вкладок) сменит тариф."""
    from app.time_utils import utcnow

    user = make_user(db_session, telegram_id="tg-trial-used")
    user.trial_activated_at = utcnow()
    db_session.commit()

    res = client.get("/api/webapp/me", headers=_auth_headers(user.id))
    assert res.status_code == 200, res.text
    balance = res.json()["balance"]
    assert balance["trial_available"] is False
    assert balance["trial_autoactivate_allowed"] is False


# ── P0-2: self-reschedule always-on тиков ────────────────────────────────


class _EmptySession:
    """Сессия-заглушка: тик доходит до выборки, находит пусто и выходит."""

    def query(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return []

    def commit(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize(
    "tick, env_var, default_interval",
    [
        (worker.run_cert_renewal_tick, "CERT_RENEWAL_INTERVAL", 86400),
        (
            worker.run_reality_dest_health_tick,
            "REALITY_DEST_HEALTH_INTERVAL",
            86400,
        ),
    ],
)
def test_always_on_ticks_reschedule_with_full_interval(
    monkeypatch, tick, env_var, default_interval
):
    """Находки #2/#4 (high): в теле тика self-reschedule шёл с
    ``min(interval, 300)`` — clamp из bootstrap-ветки, где он означает «первый
    прогон ≤5 мин». В теле это давало certbot --force-renewal каждые 5 минут
    (лимит LE «5 дубликатов в неделю») и dest-порог «2 раза подряд» = 10 минут
    вместо двух суток. Плюс ``schedule_tick`` вообще не был импортирован в этих
    двух функциях: NameError глотался except'ом → тик не перепланировался
    никогда. Тест ловит оба: вызов состоялся И период не обрезан."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        q, "schedule_tick",
        lambda path, delay, **kw: calls.append((path, delay)),
    )
    monkeypatch.setattr("app.db.SessionLocal", _EmptySession)
    monkeypatch.delenv(env_var, raising=False)

    tick()

    assert calls, (
        f"{tick.__name__} не перепланировал себя — self-reschedule мёртв "
        "(проверь импорт schedule_tick внутри функции)"
    )
    _path, delay = calls[0]
    assert delay == default_interval, (
        f"{tick.__name__} перепланировался через {delay}с вместо "
        f"{default_interval}с — вернулся clamp min(interval, 300)"
    )


def test_cert_renewal_tick_honours_custom_interval(monkeypatch):
    """Кастомный CERT_RENEWAL_INTERVAL должен доезжать до планировщика
    как есть — иначе kill-switch/замедление тика через env не работает."""
    calls: list[int] = []
    monkeypatch.setattr(
        q, "schedule_tick", lambda path, delay, **kw: calls.append(delay)
    )
    monkeypatch.setattr("app.db.SessionLocal", _EmptySession)
    monkeypatch.setenv("CERT_RENEWAL_INTERVAL", "43200")

    worker.run_cert_renewal_tick()

    assert calls == [43200]


# ── P0-3: hy2-URI обязан нести пару username:password ────────────────────


def test_hy2_uri_carries_username_and_password(db_session):
    """Находка #3 (high): URI отдавал голый пароль в userinfo, а нода на
    ``auth.type: userpass`` держит карту username→password и делит присланную
    строку по первому ':'. Ссылка без имени не проходила auth НИКОГДА — вся
    недельная реанимация hy2 стояла на мёртвом формате."""
    from urllib.parse import urlsplit

    from app import models
    from app.services.provisioning import _build_hysteria2_credential, _hy2_auth

    from .factories import make_config

    node = make_node(db_session, name="hy2-uri", host="203.0.113.77")
    cfg = make_config(
        db_session, node, name="hy2",
        protocol=models.VPNConfigProtocol.hysteria2, sni="hy2.example.info",
    )

    uri = _build_hysteria2_credential(node, cfg, _hy2_auth("user-7-9", "pw123"))

    userinfo = urlsplit(uri).netloc.split("@")[0]
    assert userinfo == "user-7-9:pw123"


def test_rebuild_remints_legacy_hy2_uri_with_username(db_session):
    """Легаси-креды в БД лежат в старом формате. Ре-минт обязан дошить
    username из access_username, НЕ трогая пароль (он уже лежит на ноде под
    этим именем) — иначе фикс не чинит существующих юзеров."""
    from app import models
    from app.security import decrypt, encrypt
    from app.services.provisioning import ProvisioningOrchestrator

    from .factories import make_config, make_device

    node = make_node(db_session, name="hy2-legacy", host="203.0.113.78")
    cfg = make_config(
        db_session, node, name="hy2",
        protocol=models.VPNConfigProtocol.hysteria2, sni="legacy.example.info",
    )
    user = make_user(db_session, telegram_id="tg-hy2-legacy")
    plan = make_plan(db_session, name="plan-hy2-legacy")
    sub = make_subscription(db_session, user, plan, node)
    device = make_device(db_session, sub, cfg, access_username="user-legacy-1")
    legacy_uri = f"hy2://oldPass123@{node.host}:{cfg.port}?sni=legacy.example.info#hy2-x"
    db_session.add(models.Credential(
        subscription_id=sub.id, device_id=device.id, config_id=cfg.id,
        node_id=node.id, proto=models.VPNConfigProtocol.hysteria2.value,
        config_text=encrypt(legacy_uri), access_username="user-legacy-1",
        is_active=True,
    ))
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    orch.rebuild_subscription_config_text(sub)

    cred = (
        db_session.query(models.Credential)
        .filter(models.Credential.subscription_id == sub.id)
        .one()
    )
    rebuilt = decrypt(cred.config_text)
    assert rebuilt.startswith("hy2://user-legacy-1:oldPass123@"), rebuilt


def test_hy2_resync_pushes_pair_from_uri(db_session):
    """Ресинк обязан класть на ноду ТУ ЖЕ пару, что у клиента в ссылке —
    иначе auth не сойдётся даже при верном формате URI."""
    from app import models
    from app.security import encrypt
    from app.services.provisioning import (
        ProvisioningOrchestrator,
        _build_hysteria2_credential,
        _hy2_auth,
    )

    from .factories import make_config, make_device

    node = make_node(db_session, name="hy2-resync", host="203.0.113.79")
    cfg = make_config(
        db_session, node, name="hy2",
        protocol=models.VPNConfigProtocol.hysteria2, sni="resync.example.info",
    )
    user = make_user(db_session, telegram_id="tg-hy2-resync")
    plan = make_plan(db_session, name="plan-hy2-resync")
    sub = make_subscription(db_session, user, plan, node)
    device = make_device(db_session, sub, cfg, access_username="user-rs-1")
    uri = _build_hysteria2_credential(node, cfg, _hy2_auth("user-rs-1", "rsPass9"))
    db_session.add(models.Credential(
        subscription_id=sub.id, device_id=device.id, config_id=cfg.id,
        node_id=node.id, proto=models.VPNConfigProtocol.hysteria2.value,
        config_text=encrypt(uri), access_username="user-rs-1", is_active=True,
    ))
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    tasks = orch.resync_node_hysteria2_clients(node)

    assert len(tasks) == 1
    clients = tasks[0].payload["clients"]
    assert clients == [{"username": "user-rs-1", "password": "rsPass9"}]


def test_cert_renewal_tick_disabled_by_zero_interval(monkeypatch):
    """CERT_RENEWAL_INTERVAL=0 — задокументированный kill-switch: тик не
    перепланирует себя и затухает."""
    calls: list[int] = []
    monkeypatch.setattr(
        q, "schedule_tick", lambda path, delay, **kw: calls.append(delay)
    )
    monkeypatch.setattr("app.db.SessionLocal", _EmptySession)
    monkeypatch.setenv("CERT_RENEWAL_INTERVAL", "0")

    worker.run_cert_renewal_tick()

    assert calls == []
