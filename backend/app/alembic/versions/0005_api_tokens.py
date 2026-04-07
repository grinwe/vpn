"""Scoped API tokens for probe/collector auth.

Revision ID: 0005_api_tokens
Revises: 0004_payment_nullable_subscription
Create Date: 2026-04-06

Adds ``api_tokens`` so probe rigs and node-side traffic collectors can
authenticate with narrow-scope credentials instead of sharing the admin
token. Only a SHA-256 hash is stored — the plaintext is shown once at
creation.
"""
from __future__ import annotations

from alembic import op

revision = "0005_api_tokens"
down_revision = "0004_payment_nullable_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            scopes TEXT[] NOT NULL DEFAULT '{}',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_api_tokens_token_hash ON api_tokens(token_hash)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_api_tokens_token_hash")
    op.execute("DROP TABLE IF EXISTS api_tokens")
