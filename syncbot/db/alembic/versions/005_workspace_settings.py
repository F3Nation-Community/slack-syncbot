"""Add workspace_settings for per-workspace policy.

Revision ID: 005_workspace_settings
Revises: 004_instance_settings
Create Date: 2026-09-01

Copies the legacy instance-wide ``allow_private_channels`` value onto every
existing workspace so upgrades do not silently revert to public-only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_workspace_settings"
down_revision: str | None = "004_instance_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workspace_settings"
_KEY_ALLOW_PRIVATE = "allow_private_channels"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("workspace_id", "key"),
        )

    legacy = bind.execute(
        sa.text("SELECT value FROM instance_settings WHERE key = :key"),
        {"key": _KEY_ALLOW_PRIVATE},
    ).fetchone()
    if not legacy or legacy[0] is None:
        return
    legacy_val = str(legacy[0]).strip()
    if legacy_val.lower() not in ("true", "false", "1", "0", "yes", "no"):
        return

    workspaces = bind.execute(sa.text("SELECT id FROM workspaces WHERE deleted_at IS NULL")).fetchall()
    for (workspace_id,) in workspaces:
        exists = bind.execute(
            sa.text("SELECT 1 FROM workspace_settings WHERE workspace_id = :wid AND key = :key LIMIT 1"),
            {"wid": workspace_id, "key": _KEY_ALLOW_PRIVATE},
        ).fetchone()
        if exists:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO workspace_settings (workspace_id, key, value, updated_at) "
                "VALUES (:wid, :key, :val, CURRENT_TIMESTAMP)"
            ),
            {"wid": workspace_id, "key": _KEY_ALLOW_PRIVATE, "val": legacy_val.lower()},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
