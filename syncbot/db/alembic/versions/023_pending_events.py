"""Replace user_action_echoes with pending_events: typed address columns, no packed values.

Revision ID: 023_pending_events
Revises: 022_data_consistency
Create Date: 2026-11-26

Every ``user_action_echoes`` row packed its values into ``user_id`` and
``fingerprint`` strings. ``pending_events`` gives each value its own column,
sized for an id or a timestamp, so content has nowhere to go.

Released kinds are copied over, so an echo in flight during the deploy still
skips SyncBot's own write (a lost one would sync back as a duplicate). Rows of
the unreleased kind ``follow_up`` (this table's ``replay``) are dropped, as is a
row that does not parse; the table is ephemeral with a 10-minute TTL.

The downgrade recreates ``user_action_echoes`` empty and drops ``pending_events``.

On a fresh database ``001_baseline`` already created ``pending_events`` from
the ORM, and ``007`` created an empty ``user_action_echoes``. Both cases end the same.
"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision: str = "023_pending_events"
down_revision: str | None = "022_data_consistency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("ix_pending_events_kind_target_channel", ("kind", "target_channel_id")),
    ("ix_pending_events_kind_source_channel", ("kind", "source_channel_id")),
    ("ix_pending_events_kind_file", ("kind", "file_id")),
    ("ix_pending_events_created_at", ("created_at",)),
)

_COLUMNS = (
    "kind",
    "target_team_id",
    "target_channel_id",
    "target_ts",
    "target_user_id",
    "target_sync_channel_id",
    "source_team_id",
    "source_channel_id",
    "source_ts",
    "source_thread_ts",
    "source_user_id",
    "action",
    "post_id",
    "file_id",
    "reaction",
    "event_ts",
    "created_at",
)

_REACTION_ECHOES = ("reaction_added", "reaction_removed")

_DECIMAL = sa.DECIMAL(precision=16, scale=6)
_TYPES = {
    "target_ts": _DECIMAL,
    "source_ts": _DECIMAL,
    "source_thread_ts": _DECIMAL,
    "event_ts": _DECIMAL,
    "target_sync_channel_id": sa.Integer(),
    "created_at": sa.DateTime(),
}


def _ts(raw: str | None) -> Decimal | None:
    """Six-decimal Slack ts, frozen here as ``post_meta_ts`` was at 023 (migrations do not import helpers)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    whole, _dot, frac = raw.partition(".")
    try:
        return Decimal(f"{whole}.{(frac + '000000')[:6]}")
    except (InvalidOperation, ArithmeticError):
        return None


def _channel_ts(fingerprint: str) -> tuple[str, Decimal, str] | None:
    """``channel:ts`` or ``channel:ts:rest``. Channel ids and ts hold no ``:``; an emoji may."""
    channel, _sep, rest = fingerprint.partition(":")
    ts_raw, _sep, tail = rest.partition(":")
    ts = _ts(ts_raw)
    if not channel or ts is None:
        return None
    return channel, ts, tail


def convert_row(kind: str, team_id: str, user_id: str, fingerprint: str, team_by_workspace_id: dict) -> dict | None:
    """One ``user_action_echoes`` row as ``pending_events`` columns, or ``None`` to drop it."""
    if not kind or not team_id or not user_id or not fingerprint:
        return None
    if kind == "message":
        parsed = _channel_ts(fingerprint)
        if not parsed or parsed[2]:
            return None
        channel, ts, _tail = parsed
        return {"target_team_id": team_id, "target_user_id": user_id, "target_channel_id": channel, "target_ts": ts}
    if kind == "file":
        return {"target_team_id": team_id, "target_user_id": user_id, "file_id": fingerprint}
    if kind in _REACTION_ECHOES:
        parsed = _channel_ts(fingerprint)
        if not parsed or not parsed[2]:
            return None
        channel, ts, emoji = parsed
        return {
            "target_team_id": team_id,
            "target_user_id": user_id,
            "target_channel_id": channel,
            "target_ts": ts,
            "reaction": emoji,
        }
    if kind == "reaction_event":
        parsed = _channel_ts(fingerprint)
        if not parsed:
            return None
        channel, ts, tail = parsed
        emoji, _sep, event_raw = tail.rpartition(":")
        event_ts = _ts(event_raw)
        if not emoji or event_ts is None:
            return None
        return {
            "target_team_id": team_id,
            "source_user_id": user_id,
            "target_channel_id": channel,
            "target_ts": ts,
            "reaction": emoji,
            "event_ts": event_ts,
        }
    if kind == "pending_share":
        channel, _sep, file_id = fingerprint.partition(":")
        parts = user_id.split("|")
        if not channel or not file_id or len(parts) != 6 or parts[0] != "v1":
            return None
        _version, post_id, sync_channel_raw, source_user, source_ws, posted_as = parts
        if not post_id or not sync_channel_raw.isdigit():
            return None
        return {
            "target_team_id": team_id,
            "target_channel_id": channel,
            "file_id": file_id,
            "post_id": post_id,
            "target_sync_channel_id": int(sync_channel_raw),
            "source_team_id": team_by_workspace_id.get(int(source_ws)) if source_ws.isdigit() else None,
            "source_user_id": source_user or None,
            "target_user_id": posted_as or None,
        }
    return None


def _create_pending_events() -> None:
    op.create_table(
        "pending_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target_team_id", sa.String(length=32), nullable=True),
        sa.Column("target_channel_id", sa.String(length=100), nullable=True),
        sa.Column("target_ts", sa.DECIMAL(precision=16, scale=6), nullable=True),
        sa.Column("target_user_id", sa.String(length=100), nullable=True),
        sa.Column("target_sync_channel_id", sa.Integer(), nullable=True),
        sa.Column("source_team_id", sa.String(length=32), nullable=True),
        sa.Column("source_channel_id", sa.String(length=100), nullable=True),
        sa.Column("source_ts", sa.DECIMAL(precision=16, scale=6), nullable=True),
        sa.Column("source_thread_ts", sa.DECIMAL(precision=16, scale=6), nullable=True),
        sa.Column("source_user_id", sa.String(length=100), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("post_id", sa.String(length=100), nullable=True),
        sa.Column("file_id", sa.String(length=100), nullable=True),
        sa.Column("reaction", sa.String(length=100), nullable=True),
        sa.Column("event_ts", sa.DECIMAL(precision=16, scale=6), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in _INDEXES:
        op.create_index(name, "pending_events", list(columns))


def _copy_echoes(bind) -> None:
    # Typed columns, so SQLite binds Decimal and reads created_at as a datetime.
    echoes = sa.table(
        "user_action_echoes",
        sa.column("id", sa.Integer()),
        sa.column("kind", sa.String()),
        sa.column("team_id", sa.String()),
        sa.column("user_id", sa.String()),
        sa.column("fingerprint", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    rows = bind.execute(
        sa.select(
            echoes.c.kind, echoes.c.team_id, echoes.c.user_id, echoes.c.fingerprint, echoes.c.created_at
        ).order_by(echoes.c.id)
    ).all()
    if not rows:
        return
    team_by_workspace_id: dict = {}
    if any(row[0] == "pending_share" for row in rows):
        team_by_workspace_id = {
            ws_id: team for ws_id, team in bind.execute(sa.text("SELECT id, team_id FROM workspaces")).all()
        }
    converted = []
    for kind, team_id, user_id, fingerprint, created_at in rows:
        columns = convert_row(kind, team_id, user_id, fingerprint, team_by_workspace_id)
        if columns is not None:
            converted.append({**dict.fromkeys(_COLUMNS), **columns, "kind": kind, "created_at": created_at})
    if converted:
        table = sa.table("pending_events", *(sa.column(name, _TYPES.get(name, sa.String())) for name in _COLUMNS))
        bind.execute(table.insert(), converted)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "pending_events" not in tables:
        _create_pending_events()
    if "user_action_echoes" in tables:
        _copy_echoes(bind)
        op.drop_table("user_action_echoes")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "user_action_echoes" not in tables:
        op.create_table(
            "user_action_echoes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("team_id", sa.String(length=32), nullable=False),
            sa.Column("user_id", sa.String(length=100), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("fingerprint", sa.String(length=256), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "team_id",
                "user_id",
                "kind",
                "fingerprint",
                name="uq_user_action_echoes_team_user_kind_fp",
            ),
        )
    if "pending_events" in tables:
        op.drop_table("pending_events")
