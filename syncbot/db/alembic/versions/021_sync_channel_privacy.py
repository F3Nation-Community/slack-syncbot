"""Private Channel Sync: who it runs under, and whether the Channel is private.

Revision ID: 021_sync_channel_privacy
Revises: 020_file_part_blob_indexes
Create Date: 2026-10-19

``synced_by_user_id`` is the person a private Channel Sync runs under.
``is_private`` is Slack's flag. ``null`` means unknown. Fresh ``001`` already
has both columns; this revision skips them when they exist.

Status backfill, in order, so a row that already left stays ``deleted`` and is
not revived when its workspace is soft-deleted:

* ``left`` becomes ``deleted``
* a paused row with ``deleted_at`` on a soft-deleted workspace becomes ``uninstalled``
* any other row with ``deleted_at`` becomes ``deleted``
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021_sync_channel_privacy"
down_revision: str | None = "020_file_part_blob_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sync_channels" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("sync_channels")}
    if "synced_by_user_id" not in cols:
        op.add_column("sync_channels", sa.Column("synced_by_user_id", sa.String(length=32), nullable=True))
    if "is_private" not in cols:
        op.add_column("sync_channels", sa.Column("is_private", sa.Boolean(), nullable=True))
    op.execute(sa.text("UPDATE sync_channels SET status = 'deleted' WHERE status = 'left'"))
    op.execute(
        sa.text(
            "UPDATE sync_channels SET status = 'uninstalled' "
            "WHERE deleted_at IS NOT NULL AND status = 'paused' "
            "AND workspace_id IN (SELECT id FROM workspaces WHERE deleted_at IS NOT NULL)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE sync_channels SET status = 'deleted' "
            "WHERE deleted_at IS NOT NULL AND status NOT IN ('uninstalled', 'deleted')"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sync_channels" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("sync_channels")}
    if "is_private" in cols:
        op.drop_column("sync_channels", "is_private")
    if "synced_by_user_id" in cols:
        op.drop_column("sync_channels", "synced_by_user_id")
