"""Alembic 021: privacy columns and status backfill."""

import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from alembic import command  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'alembic021.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def test_021_backfills_left_uninstalled_and_deleted(real_db):
    from db import DbManager, _alembic_config, get_engine, schemas
    from federation.core import get_or_create_instance_keypair

    get_or_create_instance_keypair()
    from federation.core import get_instance_id

    now = datetime.now(UTC).replace(tzinfo=None)
    live = DbManager.create_record(
        schemas.Workspace(team_id="TLIVE", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    gone = DbManager.create_record(
        schemas.Workspace(
            team_id="TGONE",
            workspace_name="Workspace B",
            instance_id=get_instance_id(),
            deleted_at=now,
        )
    )
    sync = DbManager.create_record(schemas.Sync(title="general", sync_mode="group", uid="sync-021"))

    def _channel(workspace, channel_id, status, deleted_at):
        return DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=workspace.id,
                channel_id=channel_id,
                status=status,
                publishes=True,
                subscribes=True,
                created_at=now,
                deleted_at=deleted_at,
            )
        )

    left_row = _channel(gone, "CLEFT", "left", now)
    uninstalled_row = _channel(gone, "CPAUSE", "paused", now)
    deleted_row = _channel(live, "CGONE", "paused", now)
    active_row = _channel(live, "COPEN", "active", None)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '020_file_part_blob_indexes'"))
    command.upgrade(_alembic_config(), "021_sync_channel_privacy")

    inspector = inspect(engine)
    cols = {column["name"] for column in inspector.get_columns("sync_channels")}
    assert "synced_by_user_id" in cols
    assert "is_private" in cols
    # The two retention and archive columns are gone: the table carries exactly the model's columns.
    assert cols == {column.name for column in schemas.SyncChannel.__table__.columns}

    def _status(row):
        return DbManager.get_record(schemas.SyncChannel, id=row.id).status

    assert _status(left_row) == "deleted"
    assert _status(uninstalled_row) == "uninstalled"
    assert _status(deleted_row) == "deleted"
    assert _status(active_row) == "active"
