"""Add vless-xhttp protocol and relay_config column.

Revision ID: 0016_vless_xhttp_protocol
Revises: 0015_billing_v2
"""
from alembic import op

revision = "0016_vless_xhttp_protocol"
down_revision = "0015_billing_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE vpnconfigprotocol ADD VALUE IF NOT EXISTS 'vless_xhttp'")
    op.execute(
        "ALTER TABLE vpn_nodes ADD COLUMN IF NOT EXISTS "
        "relay_config JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE vpn_nodes DROP COLUMN IF EXISTS relay_config")
