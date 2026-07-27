"""ad_links.cost_kopecks — стоимость размещения, чтобы считать CAC.

Revision ID: 0063_ad_link_cost
Revises: 0062_user_first_config_fetch

Воронка по рекламной метке (started→trial→paid→revenue) уже была, но без затрат
она отвечает только на «сколько пришло». Вопрос закупки другой — «окупилось
ли»: CAC = cost_kopecks / paid, ROI = revenue_kopecks / cost_kopecks.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0063_ad_link_cost"
down_revision = "0062_user_first_config_fetch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("ad_links", "cost_kopecks"):
        op.add_column("ad_links", sa.Column("cost_kopecks", sa.Integer(), nullable=True))


def downgrade() -> None:
    if has_column("ad_links", "cost_kopecks"):
        op.drop_column("ad_links", "cost_kopecks")
