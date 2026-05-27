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

X-Control-Channel-Secret — shared secret между Worker и backend
(env CONTROL_CHANNEL_SECRET). Защита от:
  * прямого hit'а на наш origin без Worker'а (= обход rate-limit
    Durable Objects на Worker'е),
  * leak'нувшего client_id (он не даёт сам по себе доступ — нужен
    ещё shared secret).

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
from ..services.failover import select_target_node
from ..services.provisioning import ProvisioningOrchestrator
from ..time_utils import utcnow
from ._common import _audit, get_db

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Auth helpers ────────────────────────────────────────────────────────


def _verify_control_secret(secret_header: str | None) -> None:
    """Compare `X-Control-Channel-Secret` against env CONTROL_CHANNEL_SECRET.

    Если env не задан — 503 с понятным сообщением (deployment misconfig).
    Если headers нет или не совпадает — 401. Постоянное время сравнения
    через hmac.compare_digest, чтобы не палить длину секрета через timing.
    """
    import hmac

    expected = os.getenv("CONTROL_CHANNEL_SECRET", "")
    if not expected:
        # Deployment misconfig — endpoint висит, но никто не пройдёт
        # auth (что лучше чем accepting random posts на raw origin).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="control channel secret is not configured",
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

    target = select_target_node(
        db,
        current_node_id=sub.node_id or 0,
        plan_pool_id=(
            sub.plan.server_pools[0].id
            if (sub.plan and sub.plan.server_pools)
            else None
        ),
    )
    if target is None:
        _audit(
            db,
            actor=actor,
            action="client_reported_failure_no_target",
            target_type="subscription",
            target_id=sub.id,
            metadata={
                "kind": kind,
                "current_node_id": sub.node_id,
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

    orchestrator = ProvisioningOrchestrator(db)
    try:
        # migrate_subscription_to_new_node preserve'ит sub_token + sам
        # выбирает target если не передать target_node_id — мы уже
        # подобрали через select_target_node, передаём явно.
        _new_node, _new_device, task = orchestrator.migrate_subscription_to_new_node(
            sub, target_node_id=target.id,
        )
        task_id = task.id if task else None
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "client_control: migrate_subscription_to_new_node failed for sub %s → node %s",
            sub.id, target.id,
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
            "current_node_id": sub.node_id,
            "target_node_id": target.id,
            "target_node_name": target.name,
            "task_id": task_id,
            "device_id": device_id,
            "client_ts": client_ts,
        },
        actor_type=models.AuditActor.system,
    )
    return ReportFailureResponse(
        ok=True,
        retry_after_sec=300,
        target_node_id=target.id,
        target_node_name=target.name,
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
