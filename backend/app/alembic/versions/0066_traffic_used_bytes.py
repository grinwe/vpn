"""subscriptions.traffic_used_bytes — информационный счётчик трафика юзера.

Revision ID: 0066_traffic_used_bytes
Revises: 0065_credential_leg_publication

Шкала трафика в VPN-клиенте (subscription-userinfo) требует «сколько ушло за
период», а per-user байты тик traffic_stats до сих пор выбрасывал, суммируя в
счётчик ноды. Теперь тик наливает их сюда, каждое продление обнуляет.

Отдельная колонка, а не traffic_used_mb, намеренно: на старом поле висел
блокирующий ингест (used > limit → block + ревок всех устройств, без сброса —
навсегда), и наливать в него информационные байты значило взвести бомбу для
любой подписки с не-NULL лимитом. Ингест удалён этим же релизом, старое поле
оставлено мёртвым. Байты, а не МБ: дельты за 5-минутный тик бывают < 1 МБ, и
округление съедало бы трафик лёгких юзеров подчистую.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0066_traffic_used_bytes"
down_revision = "0065_credential_leg_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("subscriptions", "traffic_used_bytes"):
        op.add_column(
            "subscriptions",
            sa.Column(
                "traffic_used_bytes",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if has_column("subscriptions", "traffic_used_bytes"):
        op.drop_column("subscriptions", "traffic_used_bytes")
