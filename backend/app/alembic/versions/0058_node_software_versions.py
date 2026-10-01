"""Версии софта на ноде: xray_version, release_version, versions_checked_at.

Revision ID: 0058_node_software_versions
Revises: 0057_fk_indexes_notnull

До этой миграции фактическая версия xray на ноде нигде не сохранялась — её
знал только bash-скрипт установки (xray-core-fetch.sh), результат жил в stdout
ansible-таски и умирал вместе с ней. Понять «какие ноды уже с новым ядром, а
какие отстали» можно было только руками по одной.

Три колонки заполняет ``tick-node-versions`` (services/node_versions.py):
* ``xray_version`` — вывод ``xray version`` с ноды;
* ``release_version`` — наша версия кода из ``/etc/vpn-node-release.json``
  (маркер пишет site.yml post_tasks после успешного прогона всех ролей);
* ``versions_checked_at`` — когда снимали; NULL = ни разу не опрашивали, и это
  осознанно отличается от «опросили, но версии не нашли».

Идемпотентна: на свежей БД (0001 create_all уже применил текущую модель)
шаги — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0058_node_software_versions"
down_revision = "0057_fk_indexes_notnull"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("xray_version", sa.String()),
    ("release_version", sa.String()),
    ("versions_checked_at", sa.DateTime()),
]


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        if not has_column("vpn_nodes", name):
            op.add_column("vpn_nodes", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLUMNS):
        if has_column("vpn_nodes", name):
            op.drop_column("vpn_nodes", name)
