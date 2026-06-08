"""Client-driven control channel — Phase A endpoint.

POST /api/client/report-failure

Custom-клиент (Phase B roadmap'а) шлёт сигнал «не работает» через
CF Worker, который форвардит запрос сюда. Backend выбирает best
healthy alternative node и migrate'ит подписку клиента туда. Клиент
на следующем рефреше `/api/sub/{sub_token}` получает обновлённые
credentials.

Архитектура / threat model / decisions — в
``docs/operations/control_channel_roadmap.md``. Этот модуль —
implementation Phase A §3.

Auth-цепочка:
  Client ──HTTPS──► CF Worker ──HTTPS + X-Control-Channel-Secret──► Backend

X-Control-Channel-Secret — APP_SECRET_KEY проекта (тот же что Fernet'ит
WG private keys и др. секреты). Он уже есть в env'е worker'а и backend
контейнера, отдельную vault-переменную не заводим. CF Worker узнаёт
его через `wrangler secret put APP_SECRET_KEY`. Защита от:
  * прямого hit'а на наш origin без Worker'а (= обход rate-limit
    на Worker'е, если когда-нибудь добавим Durable Objects),
  * leak'нувшего client_id (он не даёт сам по себе доступ — нужен
    ещё APP_SECRET_KEY).

Client identity — через `X-Client-ID` header. Backend O(1) ищет
Device по индексу `client_id_hmac` (миграция 0037).
"""
from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..rate_limit import limiter
from ..services.provisioning import ProvisioningOrchestrator
from ..time_utils import utcnow
from ._common import _audit, get_db

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Auth helpers ────────────────────────────────────────────────────────


def _verify_control_secret(secret_header: str | None) -> None:
    """Compare `X-Control-Channel-Secret` against env APP_SECRET_KEY.

    Используется тот же ключ что Fernet'ит секреты в БД — отдельную
    vault-переменную не заводим, чтобы не плодить rotation surface
    (один compromise scenario = всё равно обновлять APP_SECRET_KEY).

    Если env не задан — 503 с понятным сообщением (deployment misconfig).
    Если headers нет или не совпадает — 401. Постоянное время сравнения
    через hmac.compare_digest, чтобы не палить длину секрета через timing.
    """
    import hmac

    expected = os.getenv("APP_SECRET_KEY", "")
    if not expected:
        # Deployment misconfig — endpoint висит, но никто не пройдёт
        # auth (что лучше чем accepting random posts на raw origin).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APP_SECRET_KEY is not configured",
        )
    if not secret_header or not hmac.compare_digest(secret_header, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid control-channel secret",
        )


def _client_id_from_request(request: Request) -> str:
    """Extract X-Client-ID for slowapi key_func (rate-limit per client).

    Без заголовка — пустая строка, slowapi применит ту же квоту к
    «все безымянные» (это OK — мы reject'нем такой запрос в handler'е
    через 400).
    """
    return request.headers.get("X-Client-ID", "") or "_anonymous"


# ── Schemas ─────────────────────────────────────────────────────────────


ReportKind = Literal["connect_failed", "user_reported", "health_check_failed"]


class ReportFailureRequest(BaseModel):
    """Body POST /api/client/report-failure.

    Лёгкий payload, ~100-200 байт — control-channel должен оставаться
    low-traffic чтобы не давать RKN сигнал. Никаких payload'ов VPN-трафика
    или privacy-sensitive данных здесь не передаётся.
    """

    kind: ReportKind
    ts: int = Field(..., description="Unix epoch секунды на стороне клиента")
    current_node_id: int | None = None
    fail_count: int = Field(default=1, ge=0, le=999)


class ReportFailureResponse(BaseModel):
    ok: bool
    # Когда повторно дёргать report не имеет смысла (= таска migrate
    # ещё в работе). Клиент уважает retry_after, не спамит.
    retry_after_sec: int
    # NULL если миграция не требуется (нет target ноды лучше / уже
    # был недавний migrate / сабка не active). Клиент в этом случае
    # должен показать юзеру «попробуй позже, оператор уведомлён».
    target_node_id: int | None = None
    target_node_name: str | None = None
    task_id: int | None = None
    # Action taken — для клиента UX и для отладки. Возможные:
    # "migrated" / "no_target_available" / "throttled" / "subscription_inactive".
    action: str


# ── Endpoint ────────────────────────────────────────────────────────────


@router.post("/client/report-failure", response_model=ReportFailureResponse)
@limiter.limit(
    # 5 reports / 30 минут per client_id. На реальном поломанном линке
    # клиент должен сам экспоненциально backoff'нуть — а 5 окон в 30 мин
    # даёт оператору достаточно сигналов чтобы заметить системный
    # outage, но не даёт abuse'у залить нас. Слабее (e.g. 1/min) сделать
    # можно через env override SLOWAPI_STORAGE_URI на Redis.
    "5/30 minutes",
    key_func=_client_id_from_request,
)
def report_failure(
    request: Request,  # noqa: ARG001 — нужен для slowapi key_func
    body: ReportFailureRequest,
    x_control_channel_secret: str | None = Header(default=None),
    x_client_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ReportFailureResponse:
    _verify_control_secret(x_control_channel_secret)

    if not x_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Client-ID header is required",
        )

    # Resolve Device by client_id_hmac (O(1) через индекс из миграции 0037).
    device = (
        db.query(models.Device)
        .filter(models.Device.client_id_hmac == x_client_id)
        .first()
    )
    if device is None:
        # Не палим знание о существовании client_id через разный код
        # ответа — для «нет клиента» и «неверный secret» отдаём похожий
        # 401. Но логируем для оператора (возможно client_id_hmac
        # потерялся при rolling-upgrade).
        logger.warning(
            "client_control: unknown X-Client-ID=%s (kind=%s)",
            x_client_id, body.kind,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown client_id",
        )

    sub = db.get(models.Subscription, device.subscription_id) if device.subscription_id else None
    if sub is None:
        logger.warning(
            "client_control: device %s has no subscription (kind=%s)",
            device.id, body.kind,
        )
        return ReportFailureResponse(
            ok=False,
            retry_after_sec=300,
            action="subscription_inactive",
        )

    return _do_failover(
        db,
        sub,
        kind=body.kind,
        actor="client_control",
        device_id=device.id,
        fail_count=body.fail_count,
        client_ts=body.ts,
    )


# ── Admin manual trigger (Phase A.8) ────────────────────────────────────


class AdminReportFailureRequest(BaseModel):
    """Тот же flow что у клиента, но дёргает оператор из админ-UI.

    Юзер написал в саппорт через второй канал (Telegram-бот через другой
    VPN, e-mail) → оператор кликает «Report failure for user» на странице
    юзера → backend мигрирует. Без custom-клиента.
    """

    subscription_id: int
    kind: ReportKind = "user_reported"


class AdminReportFailureResponse(ReportFailureResponse):
    subscription_id: int


def _escalate_node_failure_reports(db: Session, node_id: int) -> None:
    """Crowdsourced node health: many user "не работает" → cool the node out.

    Counts DISTINCT subscriptions that reported failure on ``node_id`` within
    ``NODE_FAILURE_REPORT_WINDOW_MIN`` (default 60). At
    ``NODE_FAILURE_BAN_THRESHOLD`` (default 4) the node is pulled from the pool
    via ``cooldown_until`` (``choose_node`` skips it) for
    ``NODE_FAILURE_COOLDOWN_HOURS`` (default 2), a diagnose is enqueued, and a
    speaking admin push goes out. Idempotent: a node already in cooldown is
    not re-cooled / re-pushed (so the threshold fires once per outage, not per
    report). The per-sub 5-min failover throttle + DISTINCT counting keep one
    impatient user from tripping it alone.
    """
    from datetime import timedelta

    from sqlalchemy import func as sa_func

    from ..services import diagnostics_state
    from ..services.admin_notify import notify_node_diagnosis

    window_min = int(os.getenv("NODE_FAILURE_REPORT_WINDOW_MIN", "60"))
    threshold = int(os.getenv("NODE_FAILURE_BAN_THRESHOLD", "4"))
    cooldown_h = int(os.getenv("NODE_FAILURE_COOLDOWN_HOURS", "2"))

    node = db.get(models.VPNNode, node_id)
    if node is None:
        return
    now = utcnow()

    # Already cooled down for this outage → don't re-cool / re-spam.
    if node.cooldown_until is not None and node.cooldown_until > now:
        return

    cutoff = now - timedelta(minutes=window_min)
    reports = (
        db.query(sa_func.count(sa_func.distinct(models.AuditLog.target_id)))
        .filter(models.AuditLog.action == "client_reported_failure")
        .filter(models.AuditLog.target_type == "subscription")
        .filter(models.AuditLog.created_at >= cutoff)
        .filter(models.AuditLog.extra.contains({"current_node_id": node_id}))
        .scalar()
    ) or 0
    if reports < threshold:
        return

    # Pull from the pool + open a diagnose incident (so the reachability tick
    # doesn't double-diagnose) + record the action.
    node.cooldown_until = now + timedelta(hours=cooldown_h)
    diagnostics_state.mark_diagnosed(node, now)
    _audit(
        db,
        actor="crowd-health",
        action="node_user_reports_threshold",
        target_type="vpn_node",
        target_id=node.id,
        metadata={
            "reports": reports,
            "window_min": window_min,
            "threshold": threshold,
            "cooldown_until": node.cooldown_until.isoformat(),
        },
        actor_type=models.AuditActor.system,
    )

    # Enqueue the staged diagnose (the orchestrator gate skips it if the
    # operator hard-disabled diagnostics for this node).
    try:
        orchestrator = ProvisioningOrchestrator(db)
        task = orchestrator.create_task(
            "node", node.id, "diagnose",
            {"auto_triggered": True, "symptom": "user_reports", "reports": reports},
        )
        db.commit()
        orchestrator.run_task_async(task, node=node)
    except Exception:  # noqa: BLE001
        logger.exception("crowd-health: diagnose enqueue failed for node %s", node.id)
        if db.is_active:
            db.rollback()

    # Speaking admin push (synthetic check) unless alerts muted.
    if not diagnostics_state.is_alerts_muted(node, now):
        try:
            notify_node_diagnosis(
                db,
                target_kind="node",
                target=node,
                checks=[{
                    "name": "user_reports",
                    "status": "fail",
                    "latency_ms": None,
                    "message": (
                        f"{reports} юзеров сообщили «не работает» за {window_min}мин → "
                        f"нода выведена из пула на {cooldown_h}ч, запущена диагностика"
                    ),
                    "details": {"reports": reports},
                }],
                autocommit=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("crowd-health: push failed for node %s", node.id)
    db.commit()


def _do_failover(
    db: Session,
    sub: models.Subscription,
    *,
    kind: ReportKind,
    actor: str,
    device_id: int | None = None,
    fail_count: int = 1,
    client_ts: int | None = None,
) -> ReportFailureResponse:
    """Shared body для client/admin триггеров — select target + migrate.

    Не делает auth (caller отвечает) и не делает rate-limit. Только
    select target + миграция + audit + response.
    """
    from datetime import timedelta

    if sub.status != models.SubscriptionStatus.active:
        return ReportFailureResponse(
            ok=False,
            retry_after_sec=600,
            action="subscription_inactive",
        )

    recent_migrate_cutoff = utcnow() - timedelta(minutes=5)
    recent_migrate = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action == "client_reported_failure")
        .filter(models.AuditLog.target_type == "subscription")
        .filter(models.AuditLog.target_id == sub.id)
        .filter(models.AuditLog.created_at >= recent_migrate_cutoff)
        .first()
    )
    if recent_migrate:
        return ReportFailureResponse(
            ok=True,
            retry_after_sec=300,
            action="throttled",
        )

    old_node_id = sub.node_id
    orchestrator = ProvisioningOrchestrator(db)
    try:
        # Тот же путь, что админская «обновить подписку» (migrate-auto):
        # choose_node сам подберёт свободную healthy-ноду (честя cooldown,
        # disable-флаги и NodeUserBan этого юзера), мигрирует с сохранением
        # sub_token и АВТО-БАНИТ старую ноду для юзера (NodeUserBan) — чтобы
        # auto-pick больше не вернул его на проблемную ноду.
        new_node, _new_device, task, banned_old = (
            orchestrator.migrate_subscription_to_free_node(sub, banned_by=actor)
        )
        task_id = task.id if task else None
    except RuntimeError:
        # Свободной ноды нет: пул пуст / все unhealthy / все в cooldown /
        # все в бан-листе юзера. Юзер остаётся на текущей — алертим.
        _audit(
            db,
            actor=actor,
            action="client_reported_failure_no_target",
            target_type="subscription",
            target_id=sub.id,
            metadata={
                "kind": kind,
                "current_node_id": old_node_id,
                "fail_count": fail_count,
                "device_id": device_id,
            },
            actor_type=models.AuditActor.system,
        )
        return ReportFailureResponse(
            ok=False,
            retry_after_sec=900,
            action="no_target_available",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "client_control: migrate_subscription_to_free_node failed for sub %s",
            sub.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"migration failed: {exc}",
        ) from exc

    _audit(
        db,
        actor=actor,
        action="client_reported_failure",
        target_type="subscription",
        target_id=sub.id,
        metadata={
            "kind": kind,
            "fail_count": fail_count,
            "current_node_id": old_node_id,
            "target_node_id": new_node.id,
            "target_node_name": new_node.name,
            "banned_old_node": banned_old,
            "task_id": task_id,
            "device_id": device_id,
            "client_ts": client_ts,
        },
        actor_type=models.AuditActor.system,
    )

    # Краудсорс здоровья ноды: каждый user-report «не работает» на старой
    # ноде голосует за её «плохость». По порогу — выводим из пула (cooldown)
    # + диагностика + admin-push. Best-effort — не ломаем основной flow.
    if old_node_id:
        try:
            _escalate_node_failure_reports(db, old_node_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "client_control: crowd-health escalation failed for node %s",
                old_node_id,
            )

    return ReportFailureResponse(
        ok=True,
        retry_after_sec=300,
        target_node_id=new_node.id,
        target_node_name=new_node.name,
        task_id=task_id,
        action="migrated",
    )


# ── Admin trigger ──────────────────────────────────────────────────────


from ..auth import require_admin  # noqa: E402


@router.post(
    "/admin/client-control/report-for-subscription",
    response_model=AdminReportFailureResponse,
)
def admin_report_failure(
    body: AdminReportFailureRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
) -> AdminReportFailureResponse:
    """Имитирует client report от имени оператора.

    Bypass'ит X-Control-Channel-Secret и HMAC client_id — оператор
    идентифицирован через admin token. Полезно:
    * для саппорта (юзер не может дотянуться до бота через VPN, написал
      через второй канал — оператор кликает кнопку),
    * для интеграционного тестирования control-channel'а до выпуска
      custom-клиента.

    Логика идентична `report_failure`, но resolve sub'ы — по
    `subscription_id` напрямую из body (admin знает что хочет).
    """
    sub = db.get(models.Subscription, body.subscription_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    base = _do_failover(
        db,
        sub,
        kind=body.kind,
        actor="admin_panel",
    )
    return AdminReportFailureResponse(
        subscription_id=sub.id,
        **base.model_dump(),
    )


# ── Operator-aware routing (Phase 1) — bot-driven «VPN не работает» ──────
#
# Бот зовёт report-broken по telegram_id (admin-auth) → мигрируем на
# свободную ноду (auto-ban старой) + создаём OperatorNodeReport(pending).
# Бот спрашивает оператора → report-operator. «Всё равно не работает» →
# report-still-broken (target=fail) + чат с админом. Watcher через 15м
# проставит ok по факту переподключения.
# См. docs/operations/operator_routing_roadmap.md.

_OPERATORS = {
    "mts",
    "beeline",
    "megafon",
    "tele2",
    "home_wifi",
    "other",
    "unknown",
}

# Анти-абьюз: один report-broken на юзера в это окно — иначе тапами
# юзер вычерпает себе пул нод через авто-баны.
_REPORT_BROKEN_THROTTLE_MIN = 5


class ReportBrokenRequest(BaseModel):
    telegram_id: str
    # Оператора можно прислать сразу; обычно ставится отдельно (report-operator).
    operator: str | None = None


class ReportBrokenResponse(BaseModel):
    action: Literal[
        "migrated", "throttled", "no_subscription", "no_target", "user_not_found"
    ]
    report_id: int | None = None
    new_node_name: str | None = None
    new_node_region: str | None = None
    task_id: int | None = None
    retry_after_sec: int | None = None


@router.post(
    "/admin/client-control/report-broken",
    response_model=ReportBrokenResponse,
)
def report_broken_by_telegram(
    body: ReportBrokenRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001 — bot shared token
) -> ReportBrokenResponse:
    """Юзер тапнул «VPN не работает» в боте → мигрируем + заводим репорт.

    Resolve user по telegram_id, берём первую активную подписку, гоним
    ``migrate_subscription_to_free_node`` (свободная нода + бан старой) и
    пишем ``OperatorNodeReport(outcome=pending)`` со снапшотом
    access_username новой ноды — watcher по нему проставит исход. Оператор
    приходит отдельным тапом (report-operator).
    """
    from datetime import timedelta

    user = (
        db.query(models.User)
        .filter(models.User.telegram_id == str(body.telegram_id))
        .first()
    )
    if user is None:
        return ReportBrokenResponse(action="user_not_found")

    cutoff = utcnow() - timedelta(minutes=_REPORT_BROKEN_THROTTLE_MIN)
    recent = (
        db.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.user_id == user.id)
        .filter(models.OperatorNodeReport.reported_at >= cutoff)
        .first()
    )
    if recent is not None:
        return ReportBrokenResponse(
            action="throttled",
            retry_after_sec=_REPORT_BROKEN_THROTTLE_MIN * 60,
        )

    sub = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .order_by(models.Subscription.id)
        .first()
    )
    if sub is None or sub.node is None or sub.plan is None:
        return ReportBrokenResponse(action="no_subscription")

    old_node = sub.node
    orchestrator = ProvisioningOrchestrator(db)
    try:
        new_node, device, task, _banned = (
            orchestrator.migrate_subscription_to_free_node(
                sub,
                ban_reason="user reported VPN broken (operator-routing)",
                banned_by=f"user:{user.telegram_id}",
            )
        )
    except RuntimeError:
        # Нет свободной ноды (пул пуст / нездоровы / все в бан-листе) → бот
        # отправит в чат с админом.
        return ReportBrokenResponse(action="no_target")

    operator = body.operator if body.operator in _OPERATORS else None
    report = models.OperatorNodeReport(
        user_id=user.id,
        subscription_id=sub.id,
        device_id=device.id,
        operator=operator,
        failed_node_id=old_node.id,
        target_node_id=new_node.id,
        target_access_username=device.access_username,
        outcome="pending",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    _audit(
        db,
        f"user:{user.telegram_id}",
        "client_reported_failure",
        "subscription",
        sub.id,
        metadata={
            "report_id": report.id,
            "failed_node_id": old_node.id,
            "target_node_id": new_node.id,
            "source": "bot_vpn_broken",
        },
        actor_type=models.AuditActor.user,
    )
    return ReportBrokenResponse(
        action="migrated",
        report_id=report.id,
        new_node_name=new_node.name,
        new_node_region=new_node.region,
        task_id=task.id if task else None,
    )


class SetOperatorRequest(BaseModel):
    report_id: int
    operator: str


@router.post("/admin/client-control/report-operator")
def report_set_operator(
    body: SetOperatorRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
):
    """Проставить оператора на репорте (юзер тапнул выбор в боте)."""
    report = db.get(models.OperatorNodeReport, body.report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    report.operator = (
        body.operator if body.operator in _OPERATORS else "unknown"
    )
    db.commit()
    return {"report_id": report.id, "operator": report.operator}


class ReportIdRequest(BaseModel):
    report_id: int


@router.post("/admin/client-control/report-still-broken")
def report_still_broken(
    body: ReportIdRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
):
    """«Всё равно не работает» после миграции → target-нода тоже fail.

    Самый весомый негативный сигнал: юзер реально попробовал target-ноду,
    не помогло. Бот после этого ведёт в чат с админом.
    """
    report = db.get(models.OperatorNodeReport, body.report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    report.outcome = "fail"
    report.resolved_at = utcnow()
    db.commit()
    return {"report_id": report.id, "outcome": report.outcome}


@router.get("/admin/client-control/report-status/{report_id}")
def report_status(
    report_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
):
    """On-demand статус репорта для условного пуша бота.

    ``reconnected`` — переподключился ли юзер на новую ноду с момента
    репорта (по NodeTrafficSample), считается на лету (не ждём watcher).
    Бот шлёт «всё ещё не работает» только если reconnected=False.
    """
    from ..services.operator_reports import report_reconnected

    report = db.get(models.OperatorNodeReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {
        "report_id": report.id,
        "outcome": report.outcome,
        "reconnected": report_reconnected(db, report),
    }


# ── Operator × node матрица (advisory, Phase 1) ──────────────────────────


@router.get("/admin/operator-routing/matrix")
def operator_routing_matrix(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
):
    """Агрегат `(нода × оператор) → ok/fail` из репортов с recency-decay.

    fail для (node, op): репорт где failed_node=node (юзер сам пометил)
    ИЛИ target_node=node & outcome∈{fail,inconclusive}. ok: target_node=
    node & outcome=ok. Считаем РАЗНЫЕ device (fallback на user). `confident`
    при total≥K. Окно/K — env `OPERATOR_MATRIX_WINDOW_HOURS`/`_MIN_DEVICES`.
    Advisory — choose_node это пока не использует.
    """
    from datetime import timedelta

    window_h = int(os.getenv("OPERATOR_MATRIX_WINDOW_HOURS", "24"))
    min_devices = int(os.getenv("OPERATOR_MATRIX_MIN_DEVICES", "5"))
    cutoff = utcnow() - timedelta(hours=window_h)

    reports = (
        db.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.reported_at >= cutoff)
        .all()
    )

    # (node_id, operator) -> {"ok": set(device-keys), "fail": set(device-keys)}
    cells: dict[tuple[int, str], dict[str, set]] = {}
    node_ids: set[int] = set()

    def _dev_key(r: models.OperatorNodeReport) -> str:
        return f"d{r.device_id}" if r.device_id is not None else f"u{r.user_id}"

    for r in reports:
        op = r.operator or "unknown"
        key = _dev_key(r)
        if r.failed_node_id is not None:
            cells.setdefault((r.failed_node_id, op), {"ok": set(), "fail": set()})[
                "fail"
            ].add(key)
            node_ids.add(r.failed_node_id)
        if r.target_node_id is not None:
            cell = cells.setdefault(
                (r.target_node_id, op), {"ok": set(), "fail": set()}
            )
            if r.outcome == "ok":
                cell["ok"].add(key)
            elif r.outcome in ("fail", "inconclusive"):
                cell["fail"].add(key)
            node_ids.add(r.target_node_id)

    names: dict[int, str] = {}
    if node_ids:
        names = {
            n.id: n.name
            for n in db.query(models.VPNNode)
            .filter(models.VPNNode.id.in_(node_ids))
            .all()
        }

    out_cells = []
    for (node_id, op), sig in cells.items():
        ok = len(sig["ok"])
        fail = len(sig["fail"])
        total = ok + fail
        out_cells.append(
            {
                "node_id": node_id,
                "node_name": names.get(node_id),
                "operator": op,
                "ok": ok,
                "fail": fail,
                "total": total,
                "score": round(ok / total, 3) if total else None,
                "confident": total >= min_devices,
            }
        )
    out_cells.sort(key=lambda c: (c["node_name"] or "", c["operator"]))

    return {
        "window_hours": window_h,
        "min_devices": min_devices,
        "cells": out_cells,
    }


@router.get("/admin/operator-routing/reports")
def operator_routing_reports(
    limit: int = 200,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
):
    """Сырые репорты (последние N) для админ-таблицы под матрицей."""
    limit = max(1, min(limit, 1000))
    reports = (
        db.query(models.OperatorNodeReport)
        .order_by(models.OperatorNodeReport.reported_at.desc())
        .limit(limit)
        .all()
    )
    node_ids = {
        nid
        for r in reports
        for nid in (r.failed_node_id, r.target_node_id)
        if nid is not None
    }
    names: dict[int, str] = {}
    if node_ids:
        names = {
            n.id: n.name
            for n in db.query(models.VPNNode)
            .filter(models.VPNNode.id.in_(node_ids))
            .all()
        }
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "subscription_id": r.subscription_id,
            "operator": r.operator,
            "failed_node_id": r.failed_node_id,
            "failed_node_name": names.get(r.failed_node_id),
            "target_node_id": r.target_node_id,
            "target_node_name": names.get(r.target_node_id),
            "outcome": r.outcome,
            "reported_at": r.reported_at.isoformat() if r.reported_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        }
        for r in reports
    ]
