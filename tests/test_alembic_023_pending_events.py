"""Alembic 023: user_action_echoes becomes pending_events, with released rows copied over."""

import importlib.util
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from alembic import command  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from tests.db_fixtures import temp_sqlite_database  # noqa: E402

_PATH = Path(__file__).resolve().parents[1] / "syncbot" / "db" / "alembic" / "versions" / "023_pending_events.py"
_spec = importlib.util.spec_from_file_location("migration_023", _PATH)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def _convert(kind, fingerprint, *, team="T2", user="U_MAPPED", teams=None):
    return migration.convert_row(kind, team, user, fingerprint, teams or {})


class TestConvertRow:
    def test_message_and_file_echoes(self):
        assert _convert("message", "C_TGT:200.5") == {
            "target_team_id": "T2",
            "target_user_id": "U_MAPPED",
            "target_channel_id": "C_TGT",
            "target_ts": Decimal("200.500000"),
        }
        assert _convert("file", "F99") == {"target_team_id": "T2", "target_user_id": "U_MAPPED", "file_id": "F99"}

    def test_a_reaction_echo_keeps_an_emoji_with_colons(self):
        assert _convert("reaction_added", "C_TGT:200.000000:thumbsup::skin-tone-2") == {
            "target_team_id": "T2",
            "target_user_id": "U_MAPPED",
            "target_channel_id": "C_TGT",
            "target_ts": Decimal("200.000000"),
            "reaction": "thumbsup::skin-tone-2",
        }

    def test_a_reaction_event_parses_from_both_ends(self):
        assert _convert("reaction_event", "C_TGT:200.000000:thumbsup::skin-tone-2:2.000001", user="U_SRC") == {
            "target_team_id": "T2",
            "source_user_id": "U_SRC",
            "target_channel_id": "C_TGT",
            "target_ts": Decimal("200.000000"),
            "reaction": "thumbsup::skin-tone-2",
            "event_ts": Decimal("2.000001"),
        }

    def test_a_pending_share_unpacks_and_names_the_source_team(self):
        packed = "v1|postabc|7|U_SRC|5|U_MAPPED"

        assert _convert("pending_share", "C_TGT:F1", user=packed, teams={5: "T_SRC"}) == {
            "target_team_id": "T2",
            "target_channel_id": "C_TGT",
            "file_id": "F1",
            "post_id": "postabc",
            "target_sync_channel_id": 7,
            "source_team_id": "T_SRC",
            "source_user_id": "U_SRC",
            "target_user_id": "U_MAPPED",
        }
        assert _convert("pending_share", "C_TGT:F1", user="v1|postabc|7|||")["source_team_id"] is None

    @pytest.mark.parametrize(
        ("kind", "fingerprint", "user"),
        [
            ("follow_up", "C1:1.000000", "1.000000"),
            ("message", "C_TGT", "U_MAPPED"),
            ("message", "C_TGT:not-a-ts", "U_MAPPED"),
            ("reaction_added", "C_TGT:200.000000", "U_MAPPED"),
            ("reaction_event", "C_TGT:200.000000:wave", "U_SRC"),
            ("pending_share", "C_TGT:F1", "v2|postabc|7|||"),
            ("pending_share", "C_TGT:F1", "v1|postabc|seven|||"),
            ("something_else", "x", "U1"),
        ],
    )
    def test_unreleased_and_unparsable_rows_are_dropped(self, kind, fingerprint, user):
        assert _convert(kind, fingerprint, user=user) is None


@pytest.fixture
def real_db(tmp_path):
    with temp_sqlite_database(tmp_path / "alembic023.db", with_instance=True):
        yield


def _tables():
    from db import get_engine

    return set(inspect(get_engine()).get_table_names())


def _seed_old_rows(now):
    from db import DbManager, get_engine, schemas
    from federation.core import get_instance_id

    source = DbManager.create_record(
        schemas.Workspace(team_id="T_SRC", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    rows = [
        ("T2", "U_MAPPED", "message", "C_TGT:200.000000"),
        ("T2", "U_MAPPED", "file", "F99"),
        ("T2", "U_MAPPED", "reaction_added", "C_TGT:200.000000:thumbsup::skin-tone-2"),
        ("T2", "U_SRC", "reaction_event", "C_TGT:200.000000:wave:3.000000"),
        ("T2", f"v1|postabc|7|U_SRC|{source.id}|U_MAPPED", "pending_share", "C_TGT:F1"),
        ("T1", "1.000000", "follow_up", "C1:1.000000"),
        ("T2", "U_MAPPED", "message", "C_TGT"),
    ]
    with get_engine().begin() as conn:
        for team_id, user_id, kind, fingerprint in rows:
            conn.execute(
                text(
                    "INSERT INTO user_action_echoes (team_id, user_id, kind, fingerprint, created_at) "
                    "VALUES (:team_id, :user_id, :kind, :fingerprint, :created_at)"
                ),
                {"team_id": team_id, "user_id": user_id, "kind": kind, "fingerprint": fingerprint, "created_at": now},
            )
    return source


def test_023_copies_released_rows_and_the_helpers_read_them(real_db):
    from db import DbManager, _alembic_config, schemas
    from helpers.pending_events import (
        FILE_ECHO,
        MESSAGE_ECHO,
        REACTION_ADDED_ECHO,
        check_pending_event,
        find_pending_file_share,
        reaction_event_ts_is_stale,
        take_pending_event,
    )

    assert "pending_events" in _tables()
    assert "user_action_echoes" not in _tables()
    command.downgrade(_alembic_config(), "022_data_consistency")
    assert "pending_events" not in _tables()
    assert "user_action_echoes" in _tables()

    source = _seed_old_rows(datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" "))
    command.upgrade(_alembic_config(), "023_pending_events")

    assert "user_action_echoes" not in _tables()
    target = {"target_team_id": "T2", "target_user_id": "U_MAPPED"}
    assert check_pending_event(MESSAGE_ECHO, target_channel_id="C_TGT", target_ts="200.0", **target) is True
    assert check_pending_event(FILE_ECHO, file_id="F99", **target) is True
    assert take_pending_event(
        REACTION_ADDED_ECHO, target_channel_id="C_TGT", target_ts="200.0", reaction="thumbsup::skin-tone-2", **target
    )
    order = {"team_id": "T2", "source_user_id": "U_SRC", "channel_id": "C_TGT", "ts": "200.0", "reaction": "wave"}
    assert reaction_event_ts_is_stale(event_ts="2.0", **order) is True
    assert reaction_event_ts_is_stale(event_ts="4.0", **order) is False
    pending = find_pending_file_share(team_id="T2", channel_id="C_TGT", file_id="F1")
    assert (pending.post_id, pending.sync_channel_id, pending.source_workspace_id) == ("postabc", 7, source.id)
    assert (pending.source_user_id, pending.posted_as_user_id) == ("U_SRC", "U_MAPPED")
    kinds = sorted(row.kind for row in DbManager.find_records(schemas.PendingEvent, []))
    assert kinds == ["file", "message", "pending_share", "reaction_event"]


def test_023_downgrade_and_upgrade_again_on_an_empty_table(real_db):
    from db import _alembic_config, get_engine

    command.downgrade(_alembic_config(), "022_data_consistency")
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM user_action_echoes")).scalar() == 0
    command.upgrade(_alembic_config(), "023_pending_events")

    assert "pending_events" in _tables()
    assert "user_action_echoes" not in _tables()
    indexes = {index["name"] for index in inspect(get_engine()).get_indexes("pending_events")}
    assert {name for name, _columns in migration._INDEXES} <= indexes
