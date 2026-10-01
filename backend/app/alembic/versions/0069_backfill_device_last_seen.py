"""Бэкфилл devices.last_seen_at из node_traffic_samples за окно ретеншна.

Revision ID: 0069_backfill_device_last_seen
Revises: 0068_device_pending_swap

Колонка devices.last_seen_at существовала с 0062, но никем не писалась —
с этого релиза её штампует тик traffic_stats (_touch_devices_last_seen).
Без бэкфилла индикатор «активен за 24ч» в админке первые сутки после
деплоя красил бы ВСЕХ юзеров как неактивных — по нему делают вывод
«старые юзеры не остаются», то есть пустая колонка тут не «нет данных»,
а прямое враньё (тот же урок, что у воронки онбординга: см. докстринг
services/onboarding_funnel.py).

Источник — тот же, каким /api/stats считает users_active_24h: имена из
NodeTrafficSample.details[proto]["users"] → Credential.access_username
(по node_id сэмпла) → device_id. Предпочитаем ключи "user_bytes" (в них
только положительные дельты), fallback — список "users"; legacy-строки,
где "users" — голый int, пропускаем. Окно 30 дней = ретеншн сэмплов
(TRAFFIC_SAMPLE_RETENTION_DAYS); старше — честный NULL («активности не
видели»).

Схему не меняет; идемпотентна и монотонна: last_seen_at только
поднимается (guard last_seen_at IS NULL OR < :ts), так что повторный
прогон и гонка с уже успевшим отработать тиком безопасны. На свежей БД
сэмплов нет — no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_backfill_device_last_seen"
down_revision = "0068_device_pending_swap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(
        sa.text(
            """
            SELECT node_id, observed_at, details
            FROM node_traffic_samples
            WHERE observed_at >= now() - interval '30 days'
              AND details IS NOT NULL
            """
        )
    ).all()

    # (node_id, access_username) → max(observed_at)
    seen: dict[tuple[int, str], object] = {}
    for node_id, observed_at, details in rows:
        if not isinstance(details, dict):
            continue
        for proto, proto_data in details.items():
            if proto == "_errors" or not isinstance(proto_data, dict):
                continue
            user_bytes = proto_data.get("user_bytes")
            if isinstance(user_bytes, dict):
                names = user_bytes.keys()
            else:
                users = proto_data.get("users")
                names = users if isinstance(users, list) else ()
            for name in names:
                if not isinstance(name, str) or not name:
                    continue
                key = (node_id, name)
                if key not in seen or seen[key] < observed_at:
                    seen[key] = observed_at
    if not seen:
        return

    cred_rows = bind.execute(
        sa.text(
            """
            SELECT node_id, access_username, device_id
            FROM credentials
            WHERE device_id IS NOT NULL
              AND access_username IN :names
            """
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": sorted({name for (_, name) in seen})},
    ).all()

    device_ts: dict[int, object] = {}
    for node_id, name, device_id in cred_rows:
        ts = seen.get((node_id, name))
        if ts is None:
            continue
        if device_id not in device_ts or device_ts[device_id] < ts:
            device_ts[device_id] = ts
    for device_id, ts in sorted(device_ts.items()):
        bind.execute(
            sa.text(
                """
                UPDATE devices SET last_seen_at = :ts
                WHERE id = :id
                  AND (last_seen_at IS NULL OR last_seen_at < :ts)
                """
            ),
            {"id": device_id, "ts": ts},
        )


def downgrade() -> None:
    # Данные, а не схема: откат вернул бы «активности не видели» юзерам, про
    # которых мы знаем обратное. Штампы безвредны — оставляем как есть.
    pass
