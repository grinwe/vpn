"""Регресс-тесты фиксов недельного аудита 2026-07-25.

Отчёт: docs/operations/audit_week_2026_07_25.md. Один файл на весь набор —
каждый тест назван по номеру находки, чтобы связь «находка → защита» не
терялась при последующих правках.
"""
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
