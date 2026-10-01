"""Phase 0 reconciler: coalescing dedup для node-bootstrap.

Revision ID: 0041_provisioning_bootstrap_coalesce
Revises: 0040_operator_node_reports

Инвариант: ≤1 активный (pending|running) bootstrap на ноду. Узкий partial
unique index ловит гонку на уровне БД; колонка `rerun_requested` — флаг «во
время прогона прилетели правки, нужен ровно ОДИН повтор после финиша».

Индекс намеренно узкий (target_type='node' AND action='bootstrap') — чтобы
НЕ задеть прочие типы тасок (device apply, exit bootstrap, relay_tunnel,
resync), которые свой IntegrityError не ловят.

Перед созданием индекса схлопываем уже существующие дубликаты активных
node-bootstrap'ов (оставляем новейший по id, остальные → failed), иначе
CREATE UNIQUE INDEX упадёт на дубль-ключе.

См. docs/operations/provisioning_reconciler_epic.md (Phase 0). Idempotent.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column


revision = "0041_provisioning_bootstrap_coalesce"
down_revision = "0040_operator_node_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("provisioning_tasks", "rerun_requested"):
        op.add_column(
            "provisioning_tasks",
            sa.Column(
                "rerun_requested",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )

    # Защита от гонки на rolling-deploy: старый backend/worker может INSERT'ить
    # новый активный bootstrap между collapse'ом и CREATE INDEX → дубль-ключ →
    # вся миграция (одна транзакция) abort'ится → backend crash-loop'ит на
    # старте. SHARE ROW EXCLUSIVE блокирует писателей (не читателей) до commit'а
    # миграции и совместим с SHARE-локом CREATE INDEX. (CONCURRENTLY нельзя —
    # не работает внутри транзакции миграции.)
    op.execute("LOCK TABLE provisioning_tasks IN SHARE ROW EXCLUSIVE MODE")

    # Схлопываем существующие дубли активных node-bootstrap'ов (оставляем
    # новейший по id) — без этого unique index не построится. timezone('utc',
    # now()) = naive-UTC, как utcnow() в коде (не зависим от server TimeZone).
    op.execute(
        """
        UPDATE provisioning_tasks t
        SET status = 'failed',
            error_message = 'superseded by coalescing migration 0041',
            finished_at = timezone('utc', now())
        WHERE t.target_type = 'node'
          AND t.action = 'bootstrap'
          AND t.status IN ('pending', 'running')
          AND t.id < (
              SELECT MAX(t2.id) FROM provisioning_tasks t2
              WHERE t2.target_type = 'node'
                AND t2.action = 'bootstrap'
                AND t2.status IN ('pending', 'running')
                AND t2.target_id = t.target_id
          )
        """
    )

    # ≤1 активный node-bootstrap на ноду. Partial unique — действует только
    # пока pending|running; success/failed выходят из набора, и следующий
    # bootstrap можно создавать. Узкий (только node/bootstrap).
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_node_bootstrap
        ON provisioning_tasks (target_id)
        WHERE status IN ('pending', 'running')
          AND target_type = 'node'
          AND action = 'bootstrap'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_active_node_bootstrap")
    op.drop_column("provisioning_tasks", "rerun_requested")
