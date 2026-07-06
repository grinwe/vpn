"""Admin broadcasts — `/api/broadcasts/*`.

Создание/просмотр/отмена рассылок юзерам. Фактическая доставка
происходит в `run_broadcast_dispatch_tick` (backend/app/worker.py),
тут только CRUD + preview для UI.

Почему preview отдельный endpoint, а не вычисляется при create
------------------------------------------------------------
Админ обычно хочет увидеть «кому уйдёт» до нажатия Send — особенно
если фильтр `ids`, где опечатка = рассылка не тем. Поэтому
`/broadcasts/preview` принимает тот же `target_filter` и возвращает
count, вычисленный той же функцией-резолвером, что и dispatch-тик.

target_filter shapes
--------------------
* `{"type": "all"}` — все юзеры с `telegram_id is not null`.
* `{"type": "active"}` — подмножество all, у кого есть активная
  подписка. JOIN к `subscriptions` с `status=active`.
* `{"type": "ids", "ids": [1,2,3]}` — конкретные `User.id`, cap 5000
  — тот же лимит что и в `/api/users/ids`, чтобы не залипать на гигантских
  inline-массивах.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Header
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ..time_utils import utcnow
from ._common import (
    ADMIN_ACTOR_HEADER,
    _audit,
    _resolve_admin_actor,
    get_db,
)

router = APIRouter()

TEXT_MAX_LEN = 4000
IDS_MAX_LEN = 5000


class TargetFilter(BaseModel):
    """Discriminated-union-like payload для выбора аудитории.

    Мы не используем Pydantic Discriminated Unions потому что у нас
    три shape'а, и добавление нового типа ничего не должно ломать в
    старых клиентах — проще свитч по `type` в резолвере.
    """

    type: Literal["all", "active", "ids"]
    ids: list[int] | None = None

    @field_validator("ids")
    @classmethod
    def _cap_ids(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if len(v) > IDS_MAX_LEN:
            raise ValueError(f"ids превышает лимит {IDS_MAX_LEN}")
        return v


class BroadcastCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=TEXT_MAX_LEN)
    target_filter: TargetFilter


class BroadcastPreview(BaseModel):
    target_filter: TargetFilter


class BroadcastPreviewOut(BaseModel):
    recipient_count: int


class BroadcastOut(BaseModel):
    id: int
    created_at: Any
    created_by: str
    text: str
    target_filter: dict[str, Any]
    status: str
    total_recipients: int | None
    sent_count: int
    failed_count: int
    last_user_id_cursor: int
    started_at: Any | None
    completed_at: Any | None
    cancelled_reason: str | None

    class Config:
        from_attributes = True


class BroadcastListResponse(BaseModel):
    items: list[BroadcastOut]
    total: int
    has_more: bool


def _base_user_query(db: Session):
    """Все юзеры с telegram_id — общий base для `all` и `active`."""
    return db.query(models.User).filter(models.User.telegram_id.isnot(None))


def resolve_target_query(db: Session, tf: TargetFilter):
    """Вернуть SQLAlchemy Query по юзерам, подходящим под фильтр.

    Используется и на preview (для count), и в dispatch-тике (для
    батч-перечисления с курсором). Один источник правды — чтобы
    preview_count и реально разосланные количества совпадали.
    """
    if tf.type == "all":
        return _base_user_query(db)
    if tf.type == "active":
        return (
            _base_user_query(db)
            .join(
                models.Subscription,
                models.Subscription.user_id == models.User.id,
            )
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active
            )
            .distinct()
        )
    # ids
    ids = tf.ids or []
    if not ids:
        # Empty ids → пустой query (не `all`!), иначе случайный
        # отправитель с пустым массивом разошлёт всем юзерам.
        return _base_user_query(db).filter(models.User.id.in_([-1]))
    return _base_user_query(db).filter(models.User.id.in_(ids))


@router.post("/broadcasts/preview", response_model=BroadcastPreviewOut)
def preview_broadcast(
    body: BroadcastPreview,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    count = resolve_target_query(db, body.target_filter).count()
    return BroadcastPreviewOut(recipient_count=count)


@router.post("/broadcasts", response_model=BroadcastOut, status_code=201)
def create_broadcast(
    body: BroadcastCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
    actor_header: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    actor, _actor_type = _resolve_admin_actor(actor_header)
    # Защита от дублей: двойной клик по Send, ретрай HTTP-клиента или
    # залипший фронт создают две одинаковые queued-рассылки, и dispatch-тик
    # честно разошлёт обе всей базе. Отклоняем создание, если уже есть
    # активная (queued/sending) рассылка с тем же текстом и фильтром.
    # target_filter сравниваем в нормализованном виде (тот же exclude_none,
    # что и при insert ниже), чтобы JSONB-равенство совпало.
    normalized_filter = body.target_filter.model_dump(exclude_none=True)
    existing = (
        db.query(models.Broadcast)
        .filter(
            models.Broadcast.status.in_(
                (
                    models.BroadcastStatus.queued,
                    models.BroadcastStatus.sending,
                )
            ),
            models.Broadcast.text == body.text,
            models.Broadcast.target_filter == normalized_filter,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "уже есть активная рассылка с тем же текстом и фильтром "
                f"(id={existing.id})"
            ),
        )
    # Предпосчитываем count на create — UI покажет в detail-view до
    # первого тика dispatch. Если резолвер вернёт 0, всё равно создаём —
    # админ увидит completed+sent_count=0 и поймёт что фильтр пустой.
    count = resolve_target_query(db, body.target_filter).count()

    bc = models.Broadcast(
        created_by=actor,
        text=body.text,
        target_filter=normalized_filter,
        status=models.BroadcastStatus.queued,
        total_recipients=count,
    )
    db.add(bc)
    db.commit()
    db.refresh(bc)

    _audit(
        db,
        actor,
        "broadcast_create",
        "broadcast",
        bc.id,
        metadata={
            "target_filter": bc.target_filter,
            "total_recipients": count,
            "text_preview": body.text[:120],
        },
        actor_type=models.AuditActor.admin,
    )
    return bc


@router.get("/broadcasts", response_model=BroadcastListResponse)
def list_broadcasts(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(models.Broadcast)
    if status:
        q = q.filter(models.Broadcast.status == status)
    total = q.count()
    rows = (
        q.order_by(models.Broadcast.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return BroadcastListResponse(
        items=rows,
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/broadcasts/{broadcast_id}", response_model=BroadcastOut)
def get_broadcast(
    broadcast_id: int = Path(...),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    bc = db.get(models.Broadcast, broadcast_id)
    if bc is None:
        raise HTTPException(status_code=404, detail="broadcast not found")
    return bc


class CancelBroadcastRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


@router.post("/broadcasts/{broadcast_id}/cancel", response_model=BroadcastOut)
def cancel_broadcast(
    broadcast_id: int = Path(...),
    body: CancelBroadcastRequest | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
    actor_header: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    bc = db.get(models.Broadcast, broadcast_id)
    if bc is None:
        raise HTTPException(status_code=404, detail="broadcast not found")
    # Отменить можно только в queued/sending. completed/cancelled/failed
    # возвращают 409, чтобы двойной клик не попортил статистику.
    if bc.status not in (
        models.BroadcastStatus.queued,
        models.BroadcastStatus.sending,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"нельзя отменить broadcast в статусе {bc.status.value}",
        )
    actor, _actor_type = _resolve_admin_actor(actor_header)
    bc.status = models.BroadcastStatus.cancelled
    bc.completed_at = utcnow()
    if body and body.reason:
        bc.cancelled_reason = body.reason
    db.commit()
    db.refresh(bc)
    _audit(
        db,
        actor,
        "broadcast_cancel",
        "broadcast",
        bc.id,
        metadata={"reason": bc.cancelled_reason, "sent_count": bc.sent_count},
        actor_type=models.AuditActor.admin,
    )
    return bc
