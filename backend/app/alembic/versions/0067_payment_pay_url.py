"""payments.pay_url — персист ссылки на оплату ради идемпотентного чекаута.

Revision ID: 0067_payment_pay_url
Revises: 0066_traffic_used_bytes

«Повторный тап → тот же pay_url» невозможен, пока URL живёт только в ответе
чекаута: каждый повтор ходил к провайдеру заново и плодил новые счета. Теперь
единый хелпер чекаута (services/payments/checkout.py) реюзает pending-Payment
с сохранённым pay_url — второй поход к провайдеру не нужен вовсе.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0067_payment_pay_url"
down_revision = "0066_traffic_used_bytes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("payments", "pay_url"):
        op.add_column("payments", sa.Column("pay_url", sa.String(), nullable=True))


def downgrade() -> None:
    if has_column("payments", "pay_url"):
        op.drop_column("payments", "pay_url")
