"""Phase 1 operator-aware routing — resolve OperatorNodeReport outcomes.

After a user taps «VPN не работает» the backend migrates them and stores
an ``OperatorNodeReport(outcome="pending")`` with the new device's
``access_username`` on the target node. This module's tick (run every
~5 min) resolves reports older than ``T_RECONNECT`` by checking whether
the client's username showed up in a ``NodeTrafficSample`` after the
report — i.e. did the client actually reconnect.

NB: reconnect засчитывается по трафику на ЛЮБОЙ ноде текущего диверс-набора
устройства, а не только на primary-target ноде. У диверс-девайса после
``failover_device`` активные ``Credential`` на N нодах, у каждой свой
``access_username``; клиент с HAPP url-test/lowestdelay штатно оседает на
лучшем по пингу соседе — его трафик светится под ДРУГИМ username на ДРУГОЙ
ноде, и проверка одной target-ноды его не находила (audit сеть #1).

See docs/operations/operator_routing_roadmap.md.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

logger = logging.getLogger(__name__)


def _username_in_details(details, username: str) -> bool:
    """Есть ли ``username`` в списке юзеров какого-либо протокола сэмпла.

    ``details = {"<proto>": {"users": [...], ...}, "_errors": {...}}`` —
    список юзеров лежит ПЕР-протокол (та же вложенная форма, что толерантно
    читает ``api/nodes.py::list_node_users``). ``_errors`` и не-dict payload
    (legacy int и т.п.) пропускаем.
    """
    for proto, payload in (details or {}).items():
        if proto == "_errors" or not isinstance(payload, dict):
            continue
        users = payload.get("users")
        if isinstance(users, list) and username in users:
            return True
    return False


def _reconnect_targets(
    db: Session, report: models.OperatorNodeReport
) -> dict[int, set[str]]:
    """Все ``node_id -> {access_username, ...}``, по которым засчитываем
    reconnect репорта.

    Не только primary-target нода: диверс-девайс после ``failover_device``
    имеет активные ``Credential`` на N нодах, у каждой свой
    ``access_username``, а клиент с HAPP url-test штатно оседает на лучшем
    по пингу соседе — его трафик светится под ДРУГИМ username на ДРУГОЙ
    ноде (audit сеть #1). Считаем reconnect по любому из набора.

    Всегда включаем ``(target_node_id, target_access_username)`` (даже если
    кред уже не активен — исторический таргет репорта), плюс все активные
    креды устройства с проставленными node_id и access_username.
    """
    targets: dict[int, set[str]] = defaultdict(set)
    if report.target_node_id and report.target_access_username:
        targets[report.target_node_id].add(report.target_access_username)
    if report.device_id:
        rows = (
            db.query(
                models.Credential.node_id,
                models.Credential.access_username,
            )
            .filter(
                models.Credential.device_id == report.device_id,
                models.Credential.is_active.is_(True),
                models.Credential.node_id.isnot(None),
                models.Credential.access_username.isnot(None),
            )
            .all()
        )
        for node_id, username in rows:
            targets[node_id].add(username)
    return targets


def report_reconnected(db: Session, report: models.OperatorNodeReport) -> bool:
    """Did the user reconnect (on any diverse-set node) since the report?

    True iff какой-либо из reconnect-таргетов устройства (см.
    :func:`_reconnect_targets` — primary-target ноды И всех активных
    диверс-соседей) показался в ``NodeTrafficSample.details["<proto>"]
    ["users"]`` своей ноды с ``observed_at >= reported_at``. ``details``
    keyed by protocol (см. ``traffic_stats.to_details``); список юзеров
    лежит на уровень глубже, ПЕР-протокол — та же вложенная форма, что
    толерантно читает ``api/nodes.py::list_node_users``.

    Ограничение (audit сеть #5, Low/P3): признак — ФАКТ трафика (xray
    StatsService учитывает юзера только когда он прогнал байты). Клиент,
    который переподключился, но простаивает (экран погашен, только
    keepalive), в users-список не попадёт → здесь False. Поэтому
    ``inconclusive`` НЕ равно «провал»: это отсутствие позитивного трафик-
    подтверждения, а не негативный сигнал (в отличие от явного ``fail`` из
    «всё равно не работает»). Отдельного online-без-трафика сигнала пока
    нет (``Device.last_seen_at`` с 2026-09 пишется тиком traffic_stats, но он
    трафик-основан и несёт то же ограничение; xray online-list не собирается)
    — устранение остатка требует правок вне этого файла (см. StructuredOutput).

    Используется и watcher'ом (исход для матрицы), и on-demand ботом для
    условного пуша «всё ещё не работает» (нуджим только если reconnect НЕ
    наблюдали).
    """
    targets = _reconnect_targets(db, report)
    if not targets:
        return False
    # Тянем node_id + JSONB-details (не весь ORM-объект) — on-demand путь бота.
    samples = (
        db.query(
            models.NodeTrafficSample.node_id,
            models.NodeTrafficSample.details,
        )
        .filter(
            models.NodeTrafficSample.node_id.in_(targets.keys()),
            models.NodeTrafficSample.observed_at >= report.reported_at,
        )
        .all()
    )
    for node_id, details in samples:
        for username in targets.get(node_id, ()):
            if _username_in_details(details, username):
                return True
    return False


def resolve_pending_reports(db: Session) -> dict:
    """Mark pending reports ``ok``/``inconclusive`` by observed reconnect.

    A report flips to ``ok`` if the client reconnected on the new node
    (see :func:`report_reconnected`), else ``inconclusive`` — a weaker
    signal than the explicit ``fail`` set by «всё равно не работает».
    """
    window_min = int(os.getenv("OPERATOR_RECONNECT_WINDOW_MIN", "10"))
    now = utcnow()
    cutoff = now - timedelta(minutes=window_min)

    pending = (
        db.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.outcome == "pending")
        .filter(models.OperatorNodeReport.reported_at <= cutoff)
        .all()
    )

    # Собираем reconnect-таргеты каждого репорта: не только target-нода, но
    # ВСЕ активные (node_id, access_username) его диверс-набора (audit сеть
    # #1 — клиент штатно оседает на соседе, светясь под другим username на
    # другой ноде). Затем грузим сэмплы каждой ноды ОДИН раз за тик (нижняя
    # граница = min(reported_at) по всем репортам, чьи таргеты её касаются),
    # а не по разу на репорт: на массовой аварии десятки «не работает» бьют
    # в общий диверс-набор нод, тяжёлые JSONB-сэмплы иначе перечитывались бы
    # кратно (audit #255). Композитный индекс (node_id, observed_at) из
    # миграции 0019 обслуживает фильтр.
    targets_by_report: dict[int, dict[int, set[str]]] = {}
    node_min_reported: dict[int, datetime] = {}
    for report in pending:
        targets = _reconnect_targets(db, report)
        if not targets:
            continue
        targets_by_report[report.id] = targets
        for node_id in targets:
            prev = node_min_reported.get(node_id)
            if prev is None or report.reported_at < prev:
                node_min_reported[node_id] = report.reported_at

    samples_by_node: dict[int, list] = {}
    for node_id, min_reported in node_min_reported.items():
        samples_by_node[node_id] = (
            db.query(
                models.NodeTrafficSample.observed_at,
                models.NodeTrafficSample.details,
            )
            .filter(
                models.NodeTrafficSample.node_id == node_id,
                models.NodeTrafficSample.observed_at >= min_reported,
            )
            .all()
        )

    reconnected_ids: set[int] = set()
    for report in pending:
        targets = targets_by_report.get(report.id)
        if not targets:
            continue
        found = False
        for node_id, usernames in targets.items():
            for observed_at, details in samples_by_node.get(node_id, ()):
                if observed_at < report.reported_at:
                    continue
                if any(_username_in_details(details, u) for u in usernames):
                    found = True
                    break
            if found:
                break
        if found:
            reconnected_ids.add(report.id)

    ok = 0
    inconclusive = 0
    for report in pending:
        reconnected = report.id in reconnected_ids
        outcome = "ok" if reconnected else "inconclusive"
        # Conditional-обновление: перетираем ТОЛЬКО строки, оставшиеся pending.
        # Если между выборкой и резолвом юзер тапнул «всё равно не работает»
        # (report_still_broken пишет outcome='fail' в отдельной сессии), WHERE
        # outcome='pending' не сматчит строку — явный fail сохраняется, watcher
        # не затирает самый весомый негативный сигнал (audit #104).
        updated = (
            db.query(models.OperatorNodeReport)
            .filter(
                models.OperatorNodeReport.id == report.id,
                models.OperatorNodeReport.outcome == "pending",
            )
            .update(
                {"outcome": outcome, "resolved_at": now},
                synchronize_session=False,
            )
        )
        if updated:
            if reconnected:
                ok += 1
            else:
                inconclusive += 1
                # Починка не помогла (или человек так и не подключился) —
                # единственный пуш админу по этой жалобе (services/repair_alerts).
                from .repair_alerts import alert_repair_not_fixed

                alert_repair_not_fixed(db, report, reason="inconclusive")

    if pending:
        db.commit()
        logger.info(
            "operator_reports: resolved %s pending (ok=%s, inconclusive=%s)",
            len(pending), ok, inconclusive,
        )
    return {
        "checked": len(pending),
        "resolved_ok": ok,
        "resolved_inconclusive": inconclusive,
    }
