"""create_or_coalesce_node_bootstrap — defer vs immediate при RECONCILER_ENABLED=1.

Контракт, на котором держится включение реконсайлера: edit-сайты дефёрят
(коалесятся реконсайлером), а ручные/operator-эндпоинты зовут
defer_to_reconciler=False и получают НЕМЕДЛЕННУЮ таску (иначе кнопка «висит»).
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import make_node


def test_bootstrap_defer_vs_immediate_reconciler_on(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECONCILER_ENABLED", "1")
    node = make_node(db_session, name="rec-1", region="ru")
    orch = ProvisioningOrchestrator(db_session)

    def _bootstrap_tasks() -> int:
        return (
            db_session.query(models.ProvisioningTask)
            .filter_by(target_type="node", target_id=node.id, action="bootstrap")
            .count()
        )

    # edit-сайт (дефолт) → дефёр: ни таски, ни диспатча.
    task, created = orch.create_or_coalesce_node_bootstrap(node, {"x": 1})
    db_session.commit()
    assert task is None
    assert created is False
    assert _bootstrap_tasks() == 0          # таска НЕ создана

    # ручной эндпоинт → defer_to_reconciler=False → немедленная таска.
    task2, created2 = orch.create_or_coalesce_node_bootstrap(
        node, {"x": 1}, defer_to_reconciler=False
    )
    db_session.commit()
    assert created2 is True
    assert task2 is not None
    assert _bootstrap_tasks() == 1          # ровно одна реальная bootstrap-таска
