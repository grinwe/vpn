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


def test_warm_pool_hy2_bundle_carries_username(db_session):
    """Warm-пул строит креды СВОИМ путём (warm_pool._build_credential_text), в
    обход provisioning-веток — и именно оттуда бандл достаётся новому юзеру
    целиком готовым. Если здесь останется голый пароль, каждый НОВЫЙ клиент
    получит нерабочий hy2, даже когда все выданные креды уже починены
    (ровно это и обнаружилось в проде 2026-07-25 после ре-минта assigned)."""
    from urllib.parse import urlsplit

    from app import models
    from app.services.warm_pool import _build_credential_text

    from .factories import make_config

    node = make_node(db_session, name="hy2-warm", host="203.0.113.80")
    cfg = make_config(
        db_session, node, name="hy2",
        protocol=models.VPNConfigProtocol.hysteria2, sni="warm.example.info",
    )

    uri = _build_credential_text(
        node, cfg, "warm-99-deadbeef", "warmPass1", "00000000-0000-0000-0000-000000000000"
    )

    userinfo = urlsplit(uri).netloc.split("@")[0]
    assert userinfo == "warm-99-deadbeef:warmPass1", uri


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


# ── P1: денежный путь (сверка lava, идемпотентность, IDOR) ───────────────


class _FakeLavaProvider:
    name = "lava_top"

    def __init__(self, rows):
        self._rows = rows

    def list_recent_invoices(self):
        return self._rows


def _patch_lava(monkeypatch, rows):
    monkeypatch.setenv("LAVA_TOP_API_KEY", "x")
    monkeypatch.setattr("app.queue.schedule_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.payments.get_provider", lambda name=None: _FakeLavaProvider(rows)
    )


def _pending_topup(db, user, *, amount=100.0):
    from app import models

    inv = models.Invoice(
        user_id=user.id, amount=amount, currency="RUB", kind="topup",
        status=models.InvoiceStatus.pending,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_reconcile_does_not_credit_when_amount_unparsable(db_session, monkeypatch):
    """Находка #7 (high): сверка суммы стояла под ``amount is not None``, т.е.
    непарсимая/отсутствующая сумма (пустой ещё фискальный чек, строковое поле,
    переименование в API) ЗАЧИСЛЯЛА счёт целиком без единой проверки. Должно
    быть fail-closed: не зачисляем и зовём админа."""
    from app import models

    user = make_user(db_session, telegram_id="lava-noamount")
    inv = _pending_topup(db_session, user, amount=5000.0)
    before = user.balance_kopecks or 0

    _patch_lava(monkeypatch, [
        {"invoice_id": inv.id, "amount": None, "currency": "RUB",
         "contract_id": "c-noamount", "completed": True},
    ])

    res = worker.run_lava_reconcile_tick()

    assert res["credited"] == 0
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.pending
    assert (db_session.get(models.User, user.id).balance_kopecks or 0) == before


def test_reconcile_skips_foreign_currency(db_session, monkeypatch):
    """Валюта продажи не сверялась вовсе — 100 USD закрывали счёт на 100 ₽."""
    from app import models

    user = make_user(db_session, telegram_id="lava-cur")
    inv = _pending_topup(db_session, user, amount=100.0)

    _patch_lava(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "USD",
         "contract_id": "c-cur", "completed": True},
    ])

    res = worker.run_lava_reconcile_tick()

    assert res["credited"] == 0
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.pending


def test_reconcile_alerts_on_sale_for_already_paid_invoice(db_session, monkeypatch):
    """Находка #6 (high): не-pending счёт пропускался немым continue. При
    неработающем вебхуке сверка — единственный канал, видящий карточные
    платежи, поэтому двойная оплата не замечалась вообще."""
    from app import models

    user = make_user(db_session, telegram_id="lava-double")
    inv = _pending_topup(db_session, user, amount=100.0)
    inv.status = models.InvoiceStatus.paid
    db_session.add(models.Payment(
        invoice_id=inv.id, provider="lava_top",
        external_id="c-second", amount=100.0, currency="RUB",
        status=models.PaymentStatus.pending,
    ))
    db_session.commit()

    alerts: list[str] = []
    monkeypatch.setattr(
        "app.services.admin_notify.notify_admins",
        lambda db, *, kind, text, dedup_key=None, extra=None, autocommit=False: (
            alerts.append(kind)
        ),
    )
    _patch_lava(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-second", "completed": True},
    ])

    worker.run_lava_reconcile_tick()

    assert "payment_double_paid" in alerts


def test_reconcile_matches_payment_row_by_contract_id(db_session, monkeypatch):
    """Находка #8: помечалась «последняя pending по id DESC», хотя
    contract_id продажи лежит в Payment.external_id с момента checkout'а."""
    from app import models

    user = make_user(db_session, telegram_id="lava-contract")
    inv = _pending_topup(db_session, user, amount=100.0)
    ours = models.Payment(
        invoice_id=inv.id, provider="lava_top",
        external_id="c-ours", amount=100.0, currency="RUB",
        status=models.PaymentStatus.pending,
    )
    other = models.Payment(
        invoice_id=inv.id, provider="lava_top",
        external_id="c-other", amount=100.0, currency="RUB",
        status=models.PaymentStatus.pending,
    )
    db_session.add_all([ours, other])
    db_session.commit()
    # other создан последним → слепой «id DESC» выбрал бы именно его.
    assert other.id > ours.id

    _patch_lava(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-ours", "completed": True},
    ])

    worker.run_lava_reconcile_tick()

    db_session.expire_all()
    assert db_session.get(models.Payment, ours.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, other.id).status == models.PaymentStatus.pending


def test_lava_sale_amount_accepts_string_and_alternative_fields():
    """Строковая сумма и форма ``amountTotal`` больше не читаются как None."""
    from app.services.payments.lava_top import _coerce_amount, _sale_amount

    assert _coerce_amount("100.00") == 100.0
    assert _coerce_amount(True) is None
    assert _coerce_amount("nope") is None
    assert _sale_amount({"receipt": {"amount": 50, "currency": "RUB"}}) == (50.0, "RUB")
    assert _sale_amount({"amountTotal": {"amount": "75.5", "currency": "RUB"}}) == (
        75.5, "RUB",
    )
    assert _sale_amount({"amount": 12, "currency": "USD"}) == (12.0, "USD")
    assert _sale_amount({"status": "COMPLETED"}) == (None, None)


def test_checkout_requires_ownership_proof_for_anonymous_caller(client, db_session):
    """Находка #10: ownership-guard стоял под ``if req_tg``, т.е. снимался
    отсутствием поля в теле, а эндпоинт анонимный."""
    from app import models

    victim = make_user(db_session, telegram_id="tg-victim")
    inv = _pending_topup(db_session, victim, amount=100.0)

    # Фикстура client ходит с админ-токеном; здесь нужен именно анонимный
    # вызов — эндпоинт открыт наружу без него.
    anon = {"X-Admin-Token": ""}

    # Без telegram_id вообще — раньше проходило дальше и создавало чекаут.
    res = client.post(f"/api/invoices/{inv.id}/checkout", json={}, headers=anon)
    assert res.status_code == 403, res.text

    # С ЧУЖИМ telegram_id — 403 и раньше, проверяем что не сломали.
    res2 = client.post(
        f"/api/invoices/{inv.id}/checkout",
        json={"telegram_id": "tg-attacker"},
        headers=anon,
    )
    assert res2.status_code == 403, res2.text
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.pending


# ── P1: ресинк не должен утаскивать креды соседних нод ───────────────────


def _diverse_setup(db_session, proto, uri_builder):
    """Юзер с домашней нодой A и диверс-кредом на ноде B (одна подписка,
    один device → ОДИН access_username, разные секреты)."""
    from app import models
    from app.security import encrypt

    from .factories import make_config, make_device

    node_a = make_node(db_session, name="diverse-a", host="203.0.113.10")
    node_b = make_node(db_session, name="diverse-b", host="203.0.113.11")
    cfg_a = make_config(db_session, node_a, name=f"{proto}-a", protocol=proto)
    cfg_b = make_config(db_session, node_b, name=f"{proto}-b", protocol=proto)
    user = make_user(db_session, telegram_id=f"tg-div-{proto.value}")
    plan = make_plan(db_session, name=f"plan-div-{proto.value}")
    sub = make_subscription(db_session, user, plan, node_a)  # homed on A
    device = make_device(db_session, sub, cfg_a, access_username="user-div-1")
    for node, cfg, secret in ((node_a, cfg_a, "AAA"), (node_b, cfg_b, "BBB")):
        db_session.add(models.Credential(
            subscription_id=sub.id, device_id=device.id, config_id=cfg.id,
            node_id=node.id, proto=proto.value,
            config_text=encrypt(uri_builder(node, cfg, secret)),
            access_username="user-div-1", is_active=True,
        ))
    db_session.commit()
    return node_a, node_b


def test_hy2_resync_does_not_push_neighbour_password(db_session):
    """Находка #11 (high): «домашняя» выборка фильтровалась только по
    Subscription.node_id, поэтому в ресинк ноды A попадал hy2-кред той же
    подписки, физически живущий на ноде B. Дедуп last-wins по username мог
    записать на A пароль соседней ноды — hy2 тихо переставал пускать."""
    from app import models
    from app.services.provisioning import (
        ProvisioningOrchestrator,
        _build_hysteria2_credential,
        _hy2_auth,
    )

    node_a, node_b = _diverse_setup(
        db_session,
        models.VPNConfigProtocol.hysteria2,
        lambda node, cfg, secret: _build_hysteria2_credential(
            node, cfg, _hy2_auth("user-div-1", secret)
        ),
    )

    orch = ProvisioningOrchestrator(db_session)
    tasks = orch.resync_node_hysteria2_clients(node_a)

    assert len(tasks) == 1
    clients = tasks[0].payload["clients"]
    assert clients == [{"username": "user-div-1", "password": "AAA"}], clients


def test_vless_resync_does_not_push_neighbour_uuid(db_session):
    """Та же болезнь в resync_node_clients: UUID кредо соседней ноды уезжал
    на эту и мог вытеснить настоящий при дедупе по (proto, username)."""
    import uuid as uuid_mod

    from app import models
    from app.services.provisioning import (
        ProvisioningOrchestrator,
        _build_vless_reality_credential,
    )

    uuid_a = str(uuid_mod.uuid4())
    uuid_b = str(uuid_mod.uuid4())
    secrets_by_node: dict[str, str] = {"AAA": uuid_a, "BBB": uuid_b}
    node_a, _node_b = _diverse_setup(
        db_session,
        models.VPNConfigProtocol.vless_reality,
        lambda node, cfg, secret: _build_vless_reality_credential(
            node, cfg, secrets_by_node[secret]
        ),
    )

    orch = ProvisioningOrchestrator(db_session)
    task = orch.resync_node_clients(node_a)

    assert task is not None
    pushed = task.payload["clients_by_proto"][
        models.VPNConfigProtocol.vless_reality.value
    ]
    assert [c["uuid"] for c in pushed] == [uuid_a], pushed


def test_renew_certs_failure_does_not_zero_health_score(db_session):
    """Находка #12 (high): renew_certs не был в bypass-списке, поэтому провал
    точечного certbot обнулял health_score живой ноды, а успех промоутил её и
    запускал полный ресинк с рестартом hysteria-server."""
    from app import models
    from app.services.provisioning import ProvisioningOrchestrator

    node = make_node(db_session, name="cert-node", host="203.0.113.12")
    node.status = models.VPNNodeStatus.active
    node.health_score = 100
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task("node", node.id, "renew_certs", {})
    db_session.commit()

    orch._handle_task_outcome(task, success=False)

    db_session.expire_all()
    fresh = db_session.get(models.VPNNode, node.id)
    assert fresh.health_score == 100
    assert fresh.status == models.VPNNodeStatus.active


def test_enqueue_force_purges_job_id_from_queue_list(monkeypatch):
    """Находка #14 (high): force-путь удалял только ключ rq:job:<id>, а id
    оставался в списке очереди — enqueue с тем же job_id клал его второй раз,
    и таска выполнялась дважды."""
    calls: list[tuple[str, object]] = []

    class _FakeConn:
        def delete(self, key):
            calls.append(("delete", key))

        def lrem(self, key, count, value):
            calls.append(("lrem", (key, count, value)))

    class _FakeRegistry:
        def __init__(self, *a, **kw):
            pass

        def remove(self, job_id):
            calls.append(("registry_remove", job_id))

        def cleanup(self):
            pass

    class _FakeQueue:
        key = "rq:queue:vpn-provisioning"
        connection = _FakeConn()
        failed_job_registry = _FakeRegistry()
        scheduled_job_registry = _FakeRegistry()
        deferred_job_registry = _FakeRegistry()

        def enqueue(self, *a, **kw):
            calls.append(("enqueue", kw.get("job_id")))

            class _Job:
                id = kw.get("job_id")

            return _Job()

    monkeypatch.setattr(q, "get_queue", lambda: _FakeQueue())
    monkeypatch.setattr("rq.registry.StartedJobRegistry", _FakeRegistry)

    class _NoSuchJob(Exception):
        pass

    monkeypatch.setattr("rq.job.Job.fetch", staticmethod(
        lambda *a, **kw: (_ for _ in ()).throw(_NoSuchJob())
    ))
    monkeypatch.setattr("rq.exceptions.NoSuchJobError", _NoSuchJob)

    q.enqueue_task(4242, node_id=1, force=True)

    ops = [c[0] for c in calls]
    assert "lrem" in ops, f"id не снят из FIFO очереди: {calls}"
    lrem_args = next(c[1] for c in calls if c[0] == "lrem")
    assert lrem_args == ("rq:queue:vpn-provisioning", 0, "provision-4242")


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
