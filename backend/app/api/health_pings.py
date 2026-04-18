"""Admin endpoints для визуализации health-ping телеметрии.

Источник данных — ``audit_logs`` rows c действиями:

* ``health_ping_request`` — строчка, записанная воркером ``run_user_health_ping_tick``
  в момент отправки «🛟 Как сейчас работает VPN?» активному юзеру;
  в ``extra`` лежат ``telegram_id``, ``subscription_id``, ``node_id``, ``node_name``.
* ``health_ping_response`` — ответ юзера; ``extra.answer ∈ {"ok", "bad"}``,
  ``extra.source ∈ {"prompted", "self_reported"}`` (новый ключ — у старых
  rows его нет и считается ``"prompted"`` по умолчанию), ``extra.node_id``.
* ``health_ping_opt_out`` — юзер нажал «больше не спрашивайте».

Всё ходит напрямую в JSONB — отдельной таблицы под пинги нет и на текущих
объёмах (~2.4k rows/день) не нужно. Если вырастет на порядок — выделим
``HealthPingResponse`` в миграцию; сейчас это оверинжиниринг.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, and_, case, func as sa_func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..time_utils import utcnow
from ._common import get_db

router = APIRouter()


# JSONB-выражения переиспользуются в нескольких запросах — собираем через хелперы.
def _extra_text(key: str):
    return sa_func.jsonb_extract_path_text(models.AuditLog.extra, key)


def _extra_node_id():
    """Integer node_id из ``extra`` или NULL."""
    txt = _extra_text("node_id")
    # ``nullif(x, '')`` чтобы пустая строка → NULL (до cast, иначе упадёт)
    return sa_func.nullif(txt, "").cast(Integer)


@router.get("/health-pings/summary", response_model=schemas.HealthPingSummaryOut)
def health_pings_summary(
    hours: int = Query(default=168, ge=1, le=720),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Суммарная статистика за последние ``hours`` часов.

    Возвращает три агрегата за один вызов:

    * ``totals`` — сколько пингов отправлено, сколько получено ответов
      (``ok``/``bad``), сколько ``bad`` пришли self-reported (красная зона),
      response_rate (clamped к 1.0).
    * ``per_node`` — разбивка по нодам, отсортирована в UI.
    * ``timeseries`` — точки для графика (``hour`` bucket если ``hours<=168``,
      иначе ``day``).
    """
    now = utcnow()
    from_ts = now - timedelta(hours=hours)
    bucket = "hour" if hours <= 168 else "day"

    # ── totals ──
    answer = _extra_text("answer")
    source = _extra_text("source")

    totals_row = (
        db.query(
            sa_func.count(
                case((models.AuditLog.action == "health_ping_request", 1))
            ).label("requests"),
            sa_func.count(
                case((models.AuditLog.action == "health_ping_response", 1))
            ).label("responses"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "ok",
                        ),
                        1,
                    )
                )
            ).label("ok"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "bad",
                        ),
                        1,
                    )
                )
            ).label("bad"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "bad",
                            # NULL source → prompted (legacy rows)
                            sa_func.coalesce(source, "prompted") == "prompted",
                        ),
                        1,
                    )
                )
            ).label("bad_prompted"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "bad",
                            source == "self_reported",
                        ),
                        1,
                    )
                )
            ).label("bad_self_reported"),
            sa_func.count(
                case((models.AuditLog.action == "health_ping_opt_out", 1))
            ).label("opt_outs"),
        )
        .filter(
            models.AuditLog.action.in_(
                ["health_ping_request", "health_ping_response", "health_ping_opt_out"]
            )
        )
        .filter(models.AuditLog.created_at >= from_ts)
        .one()
    )

    requests_n = int(totals_row.requests or 0)
    responses_n = int(totals_row.responses or 0)
    response_rate = 0.0
    if requests_n > 0:
        response_rate = min(1.0, responses_n / requests_n)

    totals = schemas.HealthPingTotals(
        requests=requests_n,
        responses=responses_n,
        ok=int(totals_row.ok or 0),
        bad=int(totals_row.bad or 0),
        bad_prompted=int(totals_row.bad_prompted or 0),
        bad_self_reported=int(totals_row.bad_self_reported or 0),
        opt_outs=int(totals_row.opt_outs or 0),
        response_rate=response_rate,
    )

    # ── per_node ──
    node_id_col = _extra_node_id().label("node_id")
    per_node_rows = (
        db.query(
            node_id_col,
            sa_func.count(
                case((models.AuditLog.action == "health_ping_request", 1))
            ).label("requests"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "ok",
                        ),
                        1,
                    )
                )
            ).label("ok"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "bad",
                        ),
                        1,
                    )
                )
            ).label("bad"),
        )
        .filter(
            models.AuditLog.action.in_(
                ["health_ping_request", "health_ping_response"]
            )
        )
        .filter(models.AuditLog.created_at >= from_ts)
        .group_by(node_id_col)
        .all()
    )

    # resolve node names
    node_ids = [r.node_id for r in per_node_rows if r.node_id is not None]
    node_names: dict[int, str] = {}
    if node_ids:
        for node in (
            db.query(models.VPNNode.id, models.VPNNode.name)
            .filter(models.VPNNode.id.in_(node_ids))
            .all()
        ):
            node_names[int(node.id)] = node.name

    per_node: list[schemas.HealthPingPerNode] = []
    for r in per_node_rows:
        nid = int(r.node_id) if r.node_id is not None else None
        name = node_names.get(nid) if nid is not None else None
        # Удалённая нода, но rows остались в audit
        if nid is not None and name is None:
            name = f"(удалена) #{nid}"
        ok_n = int(r.ok or 0)
        bad_n = int(r.bad or 0)
        total_resp = ok_n + bad_n
        ratio = (bad_n / total_resp) if total_resp > 0 else 0.0
        per_node.append(
            schemas.HealthPingPerNode(
                node_id=nid,
                node_name=name,
                requests=int(r.requests or 0),
                ok=ok_n,
                bad=bad_n,
                bad_ratio=ratio,
            )
        )

    # ── timeseries ──
    trunc_unit = "hour" if bucket == "hour" else "day"
    bucket_col = sa_func.date_trunc(trunc_unit, models.AuditLog.created_at).label(
        "bucket_ts"
    )
    ts_rows = (
        db.query(
            bucket_col,
            sa_func.count(case((answer == "ok", 1))).label("ok"),
            sa_func.count(case((answer == "bad", 1))).label("bad"),
        )
        .filter(models.AuditLog.action == "health_ping_response")
        .filter(models.AuditLog.created_at >= from_ts)
        .group_by(bucket_col)
        .order_by(bucket_col.asc())
        .all()
    )
    timeseries = [
        schemas.HealthPingTimeseriesPoint(
            bucket_ts=r.bucket_ts,
            ok=int(r.ok or 0),
            bad=int(r.bad or 0),
        )
        for r in ts_rows
    ]

    return schemas.HealthPingSummaryOut(
        from_ts=from_ts,
        to_ts=now,
        hours=hours,
        bucket=bucket,
        totals=totals,
        per_node=per_node,
        timeseries=timeseries,
    )


@router.get(
    "/health-pings/recent-bad", response_model=schemas.HealthPingRecentBadOut
)
def health_pings_recent_bad(
    limit: int = Query(default=50, ge=1, le=500),
    node_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Список последних «плохих» ответов — основной drill-in из дашборда.

    Если передан ``node_id`` — фильтрует по ``extra.node_id``. User/plan
    join'ятся опционально: row может ссылаться на удалённую подписку или
    юзера (LEFT JOIN не упадёт).
    """
    answer = _extra_text("answer")
    source = _extra_text("source")
    node_id_col = _extra_node_id()

    q = (
        db.query(
            models.AuditLog.id,
            models.AuditLog.created_at,
            models.AuditLog.actor,
            models.AuditLog.target_id,
            node_id_col.label("node_id"),
            sa_func.coalesce(source, "prompted").label("source"),
        )
        .filter(models.AuditLog.action == "health_ping_response")
        .filter(answer == "bad")
    )
    if node_id is not None:
        q = q.filter(node_id_col == node_id)

    rows = q.order_by(models.AuditLog.created_at.desc()).limit(limit).all()
    if not rows:
        return schemas.HealthPingRecentBadOut(items=[])

    # Resolve nodes
    nids = {int(r.node_id) for r in rows if r.node_id is not None}
    node_names: dict[int, str] = {}
    if nids:
        for n in (
            db.query(models.VPNNode.id, models.VPNNode.name)
            .filter(models.VPNNode.id.in_(nids))
            .all()
        ):
            node_names[int(n.id)] = n.name

    # Resolve users — actor хранится как str(user.id) для самого юзера,
    # либо как telegram_id. Пробуем сначала как id, потом как telegram_id.
    user_ids_numeric: set[int] = set()
    telegram_ids: set[str] = set()
    for r in rows:
        actor = r.actor
        if actor is None:
            continue
        if actor.isdigit():
            user_ids_numeric.add(int(actor))
        else:
            telegram_ids.add(actor)

    users_by_id: dict[int, models.User] = {}
    if user_ids_numeric:
        for u in (
            db.query(models.User).filter(models.User.id.in_(user_ids_numeric)).all()
        ):
            users_by_id[u.id] = u
    users_by_tg: dict[str, models.User] = {}
    if telegram_ids:
        for u in (
            db.query(models.User)
            .filter(models.User.telegram_id.in_(telegram_ids))
            .all()
        ):
            if u.telegram_id:
                users_by_tg[u.telegram_id] = u

    # Resolve subscriptions → plans
    sub_ids = {int(r.target_id) for r in rows if r.target_id is not None}
    subs_by_id: dict[int, models.Subscription] = {}
    plan_names: dict[int, str] = {}
    if sub_ids:
        for s in (
            db.query(models.Subscription)
            .filter(models.Subscription.id.in_(sub_ids))
            .all()
        ):
            subs_by_id[s.id] = s
        plan_ids = {s.plan_id for s in subs_by_id.values() if s.plan_id is not None}
        if plan_ids:
            for p in (
                db.query(models.Plan.id, models.Plan.name)
                .filter(models.Plan.id.in_(plan_ids))
                .all()
            ):
                plan_names[int(p.id)] = p.name

    items: list[schemas.HealthPingRecentBadItem] = []
    for r in rows:
        nid = int(r.node_id) if r.node_id is not None else None
        nname = node_names.get(nid) if nid is not None else None
        if nid is not None and nname is None:
            nname = f"(удалена) #{nid}"

        user: models.User | None = None
        if r.actor is not None:
            if r.actor.isdigit():
                user = users_by_id.get(int(r.actor))
            else:
                user = users_by_tg.get(r.actor)

        sid = int(r.target_id) if r.target_id is not None else None
        sub = subs_by_id.get(sid) if sid is not None else None
        pname = plan_names.get(sub.plan_id) if sub and sub.plan_id else None

        items.append(
            schemas.HealthPingRecentBadItem(
                created_at=r.created_at,
                telegram_id=user.telegram_id if user else None,
                user_id=user.id if user else None,
                node_id=nid,
                node_name=nname,
                subscription_id=sid,
                plan_name=pname,
                source=str(r.source or "prompted"),
            )
        )

    return schemas.HealthPingRecentBadOut(items=items)


@router.get(
    "/nodes/{node_id}/health-pings",
    response_model=schemas.NodeHealthPingStatsOut,
)
def node_health_pings(
    node_id: int,
    hours: int = Query(default=24, ge=1, le=720),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Узкий endpoint для виджета на карточке ноды в ``Nodes.tsx``.

    Возвращает агрегаты за окно ``hours`` плюс timestamp последней
    жалобы (для подписи «последняя жалоба в HH:MM»).
    """
    # 404 на несуществующую ноду — чтобы UI мог корректно обработать.
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    now = utcnow()
    from_ts = now - timedelta(hours=hours)

    answer = _extra_text("answer")
    node_id_col = _extra_node_id()

    row = (
        db.query(
            sa_func.count(
                case((models.AuditLog.action == "health_ping_request", 1))
            ).label("requests"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "ok",
                        ),
                        1,
                    )
                )
            ).label("ok"),
            sa_func.count(
                case(
                    (
                        and_(
                            models.AuditLog.action == "health_ping_response",
                            answer == "bad",
                        ),
                        1,
                    )
                )
            ).label("bad"),
        )
        .filter(
            models.AuditLog.action.in_(
                ["health_ping_request", "health_ping_response"]
            )
        )
        .filter(models.AuditLog.created_at >= from_ts)
        .filter(node_id_col == node_id)
        .one()
    )

    last_bad_at = (
        db.query(sa_func.max(models.AuditLog.created_at))
        .filter(models.AuditLog.action == "health_ping_response")
        .filter(models.AuditLog.created_at >= from_ts)
        .filter(answer == "bad")
        .filter(node_id_col == node_id)
        .scalar()
    )

    ok_n = int(row.ok or 0)
    bad_n = int(row.bad or 0)
    total_resp = ok_n + bad_n
    ratio = (bad_n / total_resp) if total_resp > 0 else 0.0

    return schemas.NodeHealthPingStatsOut(
        node_id=node_id,
        hours=hours,
        requests=int(row.requests or 0),
        ok=ok_n,
        bad=bad_n,
        bad_ratio=ratio,
        last_bad_at=last_bad_at,
    )
