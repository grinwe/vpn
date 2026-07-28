"""Реферальная программа: награда днями и приглашение в момент «вау».

Было: обе стороны получали фиксированные 50 ₽ (REFERRAL_BONUS_KOPECKS), а поля
`bonus_days` / `reward_days`, лежащие в модели с самой первой миграции, не
читал никто. Фиксированная сумма обесценивается при каждом повышении прайса, а
«месяц подписки» читается одинаково всегда и стоит нам маржи, а не выручки.

Приглашение позвать друга шлём один раз — сразу после первого скачивания
конфига: до этого человеку нечего рекомендовать.
"""
from __future__ import annotations

from app import models
from app.services import balance as balance_svc

from .factories import make_plan, make_user


def _visible_30d_plan(db_session, price: float = 150.0):
    """Прайс, по которому оценивается подарок (самый дешёвый видимый 30-дневный)."""
    plan = make_plan(db_session)
    plan.price = price
    plan.duration_days = 30
    plan.is_visible = True
    db_session.commit()
    return plan


def test_days_to_kopecks_follows_price(db_session):
    _visible_30d_plan(db_session, price=150.0)
    # 150 ₽ за 30 дней → 5 ₽ в день.
    assert balance_svc.days_to_kopecks(db_session, 30) == 15000
    assert balance_svc.days_to_kopecks(db_session, 7) == 3500
    assert balance_svc.days_to_kopecks(db_session, 0) == 0


def test_days_to_kopecks_tracks_price_change(db_session):
    """Подарок не должен застывать: подняли прайс — подорожал и день."""
    plan = _visible_30d_plan(db_session, price=150.0)
    before = balance_svc.days_to_kopecks(db_session, 30)
    plan.price = 300.0
    db_session.commit()
    assert balance_svc.days_to_kopecks(db_session, 30) == before * 2


def test_referral_bonus_credits_days_not_flat_sum(db_session):
    _visible_30d_plan(db_session, price=150.0)
    user = make_user(db_session, telegram_id="ref-owner")
    tx = balance_svc.referral_bonus(
        db_session, user.id, reference="test:1", days=30
    )
    assert tx.amount_kopecks == 15000  # месяц подписки, а не 5000 копеек
    assert tx.kind == models.BalanceTxKind.bonus


def test_referral_bonus_without_price_falls_back(db_session):
    """Нет видимого 30-дневного плана — оценить подарок в днях нечем, но реферер
    свою работу сделал: начисляем легаси-сумму, а не ноль и не отказ."""
    user = make_user(db_session, telegram_id="no-price")
    tx = balance_svc.referral_bonus(
        db_session, user.id, reference="test:2", days=30
    )
    assert tx.amount_kopecks == balance_svc.REFERRAL_BONUS_KOPECKS


def test_referral_bonus_legacy_flat_sum_still_works(db_session):
    """Без days падаем на прод-константу — прод-env её задаёт явно."""
    user = make_user(db_session, telegram_id="legacy")
    tx = balance_svc.referral_bonus(db_session, user.id, reference="test:3")
    assert tx.amount_kopecks == balance_svc.REFERRAL_BONUS_KOPECKS


def test_invitee_gets_days_on_trial(db_session):
    """Приглашённому — подарок из bonus_days его кода; реферер за триал НЕ
    получает ничего (иначе рефералка вырождается в фарм триалов)."""
    from app.services.trial import activate_trial

    _visible_30d_plan(db_session, price=150.0)
    owner = make_user(db_session, telegram_id="owner-1")
    code = models.ReferralCode(owner_id=owner.id, code="abc123", bonus_days=7,
                               reward_days=30)
    db_session.add(code)
    invitee = make_user(db_session, telegram_id="invitee-1")
    invitee.referred_by_id = owner.id
    db_session.commit()

    activate_trial(db_session, invitee.id)
    db_session.commit()

    invite_tx = (
        db_session.query(models.BalanceTransaction)
        .filter_by(reference=f"referral_signup:{invitee.id}")
        .one()
    )
    assert invite_tx.amount_kopecks == balance_svc.days_to_kopecks(db_session, 7)

    owner_rows = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=owner.id)
        .all()
    )
    assert owner_rows == []


def test_first_config_fetch_marks_user_and_queues_invite(db_session):
    """Первое скачивание конфига → метка + ровно одно приглашение."""
    from app.api_extensions import _mark_first_config_fetch

    user = make_user(db_session, telegram_id="fetcher")
    sub = type("SubStub", (), {"user_id": user.id})()

    _mark_first_config_fetch(db_session, sub)
    db_session.refresh(user)
    assert user.first_config_fetch_at is not None

    # Повторные фетчи (клиент дёргает ссылку каждые пару часов) не должны
    # порождать новых приглашений.
    _mark_first_config_fetch(db_session, sub)
    invites = (
        db_session.query(models.AuditLog)
        .filter_by(action="referral_invite", target_id=user.id)
        .all()
    )
    assert len(invites) == 1
    extra = invites[0].extra or {}
    assert extra["telegram_id"] == "fetcher"
    assert extra["reward_days"]


def test_referral_invite_is_deliverable(db_session):
    """Kind должен быть в allowlist поллера, иначе приглашение осядет в БД."""
    from app.api_extensions import ADMIN_NOTIFICATION_ACTIONS

    assert "referral_invite" in ADMIN_NOTIFICATION_ACTIONS


def test_one_referral_code_per_user(db_session):
    """Бот и мини-апп минтили коды разного формата, и человек видел две разные
    ссылки. Хелпер должен отдавать один и тот же код."""
    from app.api_extensions import ensure_referral_code

    user = make_user(db_session, telegram_id="single-code")
    first = ensure_referral_code(db_session, user)
    db_session.commit()
    second = ensure_referral_code(db_session, user)
    assert first.code == second.code
    assert db_session.query(models.ReferralCode).filter_by(owner_id=user.id).count() == 1


# ── рекламные метки: CAC и ROI ──────────────────────────────────────────────
# Воронка по метке была, но без затрат отвечала только на «сколько пришло».


def test_ad_link_cac_and_roi(client, db_session):
    resp = client.post(
        "/api/admin/ad-links",
        json={"name": "нарезчик №1", "tag": "clip1", "cost_kopecks": 300000},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cost_kopecks"] == 300000
    # Никто ещё не заплатил — делить не на что, а не «CAC = 0».
    assert body["cac_kopecks"] is None
    assert body["roi"] == 0.0


def test_ad_link_without_cost_has_no_cac(client):
    """Бесплатное размещение (обмен, свой канал) — CAC и ROI не считаем."""
    resp = client.post("/api/admin/ad-links", json={"name": "обмен", "tag": "swap1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost_kopecks"] is None
    assert resp.json()["cac_kopecks"] is None
    assert resp.json()["roi"] is None


def test_ad_link_zero_cost_is_stored_as_null(client, db_session):
    """0 ₽ — это «бесплатно», а не делитель: иначе CAC делил бы на ноль."""
    created = client.post(
        "/api/admin/ad-links", json={"name": "нулевая", "tag": "zero1", "cost_kopecks": 5000}
    ).json()
    resp = client.patch(f"/api/admin/ad-links/{created['id']}", json={"cost_kopecks": 0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost_kopecks"] is None
    assert resp.json()["roi"] is None


# ── оффер про российские сайты ──────────────────────────────────────────────
# С 15.04.2026 Яндекс/банки/маркетплейсы закрываются при включённом VPN. У нас
# есть раздельный маршрут, но он живёт не везде — текст обязан это оговаривать.


def _read(path: str) -> str:
    from pathlib import Path

    import pytest

    p = Path(__file__).resolve().parents[2] / path
    if not p.is_file():
        pytest.skip(f"{path} недоступен (backend-only тест-образ)")
    return p.read_text(encoding="utf-8")


def test_bot_help_has_ru_sites_section():
    handlers = _read("bot/handlers.py")
    keyboards = _read("bot/keyboards.py")
    assert "_HELP_RU_SITES" in handlers
    # Кнопка обязана вести на существующий обработчик, иначе раздел недостижим.
    assert 'callback_data="help:ru_sites"' in keyboards
    assert 'F.data == "help:ru_sites"' in handlers


def test_ru_sites_copy_does_not_overpromise():
    """Сплит работает на РУ-нодах и на Reality/XHTTP, но не на ws-cdn и hy2 —
    текст не должен обещать, что «всегда всё работает»."""
    handlers = _read("bot/handlers.py")
    start = handlers.index("_HELP_RU_SITES = (")
    copy = handlers[start:start + 1200]
    assert "не на всех серверах" in copy
    assert "поддержку" in copy


def test_webapp_help_mentions_ru_sites():
    help_page = _read("webapp/src/pages/Help.tsx")
    assert "Российские сайты и банки" in help_page
    assert "не на всех серверах" in help_page


# ── текст приглашения ───────────────────────────────────────────────────────
# Первая версия ушла живому пользователю с «дарим тебе 3 дней»: сломанное
# склонение плюс исторический дефальт 3 в коде вместо задуманных 30.


def test_plural_days_russian_forms():
    from app.api_extensions import _plural_days

    assert _plural_days(1) == "1 день"
    assert _plural_days(3) == "3 дня"
    assert _plural_days(30) == "30 дней"
    assert _plural_days(11) == "11 дней"  # 11-14 — исключение из правила
    assert _plural_days(22) == "22 дня"
    assert _plural_days(105) == "105 дней"


def test_referral_code_defaults_are_current():
    """Дефолты модели должны совпадать с тем, что обещает текст: иначе человек
    получит «3 дня» там, где мы задумывали месяц."""
    code = models.ReferralCode(owner_id=1, code="x")
    # SQLAlchemy проставляет python-дефолты на flush; проверяем саму колонку.
    assert models.ReferralCode.__table__.c.reward_days.default.arg == 30
    assert models.ReferralCode.__table__.c.bonus_days.default.arg == 7
    assert code is not None


def test_invite_not_sent_to_existing_users(db_session):
    """Гейт «колонка пуста» на новой колонке означает «пуста у всех», из-за
    чего приглашение ушло существующим пользователям. Проставленная метка
    обязана его останавливать."""
    from app.api_extensions import _mark_first_config_fetch
    from app.time_utils import utcnow

    user = make_user(db_session, telegram_id="old-timer")
    user.first_config_fetch_at = utcnow()  # как после бэкфилла миграции 0064
    db_session.commit()

    sub = type("SubStub", (), {"user_id": user.id})()
    _mark_first_config_fetch(db_session, sub)

    invites = (
        db_session.query(models.AuditLog)
        .filter_by(action="referral_invite", target_id=user.id)
        .count()
    )
    assert invites == 0


def test_ack_stores_message_id_for_recall(client, db_session):
    """Без сохранённого message_id ошибочная рассылка необратима: Bot API
    удаляет свои сообщения 48 часов, но только по id."""
    log = models.AuditLog(
        actor="system",
        actor_type=models.AuditActor.system,
        action="referral_invite",
        target_type="user",
        target_id=1,
        extra={"telegram_id": "42"},
    )
    db_session.add(log)
    db_session.commit()

    resp = client.post(
        f"/api/notifications/{log.id}/ack",
        json={"message_id": 777, "chat_id": 42},
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(log)
    assert log.action == "referral_invite:delivered"
    assert log.extra["message_id"] == 777
    assert log.extra["chat_id"] == 42


def test_ack_without_body_still_works(db_session, client):
    """Старый бот (без message_id в теле) не должен ломаться на новом бэкенде."""
    log = models.AuditLog(
        actor="system",
        actor_type=models.AuditActor.system,
        action="config_ready",
        target_type="user",
        target_id=1,
        extra={"telegram_id": "42"},
    )
    db_session.add(log)
    db_session.commit()

    resp = client.post(f"/api/notifications/{log.id}/ack")
    assert resp.status_code == 200, resp.text
    db_session.refresh(log)
    assert log.action == "config_ready:delivered"
