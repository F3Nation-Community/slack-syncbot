"""Pending events: addresses only, one trio for every kind, live rows only, swept on the pulse."""

import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import DECIMAL, DateTime, Integer, String, event, inspect

from db import schemas
from helpers import pending_events
from helpers.pending_events import (
    FILE_ECHO,
    MESSAGE_ECHO,
    PENDING_SHARE,
    REACTION_ADDED_ECHO,
    REACTION_ORDER,
    REACTION_REMOVED_ECHO,
    REPLAY,
    check_pending_event,
    find_pending_file_share,
    purge_expired_pending_events,
    reaction_event_ts_is_stale,
    remember_pending_event,
    take_pending_event,
    take_pending_events_to_replay,
)
from helpers.slack_ts import post_meta_ts, slack_message_ts

TS = "1795710083.439439"
MESSAGE = {"target_team_id": "T2", "target_user_id": "U_MAPPED", "target_channel_id": "C_TGT", "target_ts": "200.0"}
REACTION = {**MESSAGE, "reaction": "thumbsup::skin-tone-2"}
ORDER = {
    "target_team_id": "T_TGT",
    "source_user_id": "U_SRC",
    "target_channel_id": "C_TGT",
    "target_ts": "100.0",
    "reaction": "thumbsup",
}
ORDER_CHECK = {"team_id": "T_TGT", "source_user_id": "U_SRC", "channel_id": "C_TGT", "ts": "100.0"}
SHARE = {"target_team_id": "T2", "target_channel_id": "C_TGT", "file_id": "F1"}


@pytest.fixture
def pending_db(tmp_path):
    import db as db_mod
    from db import get_engine, initialize_database

    url = f"sqlite:///{tmp_path / 'pending.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        db_mod.GLOBAL_ENGINE = None
        db_mod.GLOBAL_SCHEMA = None
        initialize_database()
        yield get_engine()
        if db_mod.GLOBAL_ENGINE:
            db_mod.GLOBAL_ENGINE.dispose()
        db_mod.GLOBAL_ENGINE = old_engine
        db_mod.GLOBAL_SCHEMA = old_schema


def _rows():
    from db import DbManager

    return DbManager.find_records(schemas.PendingEvent, [])


def _later():
    """A clock just past the TTL."""
    return pending_events._utcnow() + pending_events._TTL + timedelta(seconds=1)


def _statements(engine, run):
    """Every SQL statement *run* sends to ``pending_events``, upper-cased."""
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        run()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return [statement for statement in seen if "PENDING_EVENTS" in statement]


def _replay(**columns):
    return {"source_team_id": "T1", "source_channel_id": "C1", "action": "edit", "source_ts": TS, **columns}


class TestSlackTs:
    def test_string_form_pads_six_decimals(self):
        assert slack_message_ts("100.0") == "100.000000"
        assert slack_message_ts("100.000001") == "100.000001"
        assert slack_message_ts(100.0) == "100.000000"

    def test_post_meta_ts_is_six_decimal_not_float(self):
        slack_ts = "1757529600.123456"
        exact = Decimal("1757529600.123456")
        assert post_meta_ts(slack_ts) == exact
        assert post_meta_ts(slack_ts) != float(slack_ts)
        edge = "9999999999.123456"
        assert post_meta_ts(edge) == Decimal(edge)
        assert post_meta_ts(edge) != Decimal(str(float(edge)))


class TestOnlyAddresses:
    """The rule in AGENTS.md: a row holds only addresses. Adding a column means editing this test."""

    COLUMNS = {
        "id": Integer,
        "kind": String,
        "target_team_id": String,
        "target_channel_id": String,
        "target_ts": DECIMAL,
        "target_user_id": String,
        "target_sync_channel_id": Integer,
        "source_team_id": String,
        "source_channel_id": String,
        "source_ts": DECIMAL,
        "source_thread_ts": DECIMAL,
        "source_user_id": String,
        "action": String,
        "post_id": String,
        "file_id": String,
        "reaction": String,
        "event_ts": DECIMAL,
        "created_at": DateTime,
    }

    def test_the_columns_are_these_and_none_can_hold_content(self):
        table = schemas.PendingEvent.__table__

        assert {column.name for column in table.columns} == set(self.COLUMNS)
        for column in table.columns:
            assert isinstance(column.type, self.COLUMNS[column.name]), column.name
            if isinstance(column.type, String):
                assert column.type.length <= 100, column.name

    def test_the_migrated_table_has_the_same_columns(self, pending_db):
        names = {column["name"] for column in inspect(pending_db).get_columns("pending_events")}

        assert names == set(self.COLUMNS)

    def test_every_kind_uses_only_these_columns(self):
        usable = set(self.COLUMNS) - {"id", "kind", "created_at"}
        for kind, spec in pending_events._COLUMNS.items():
            assert {*spec.key, *spec.required, *spec.optional} <= usable, kind


class TestEachKindsColumns:
    def test_a_column_the_kind_does_not_use_writes_nothing_and_warns(self, pending_db):
        with patch("helpers.pending_events.log_warning") as warn:
            assert remember_pending_event(MESSAGE_ECHO, **MESSAGE, reaction="wave") is None

        assert _rows() == []
        assert warn.call_args.args[0] == "pending_event_failed"
        assert warn.call_args.kwargs["op"] == "remember"

    def test_a_missing_column_writes_nothing_and_warns(self, pending_db):
        address = {name: value for name, value in MESSAGE.items() if name != "target_ts"}
        with patch("helpers.pending_events.log_warning") as warn:
            assert remember_pending_event(MESSAGE_ECHO, **address) is None

        assert _rows() == []
        warn.assert_called_once()

    def test_an_empty_value_writes_nothing_quietly(self, pending_db):
        with patch("helpers.pending_events.log_warning") as warn:
            assert remember_pending_event(MESSAGE_ECHO, **{**MESSAGE, "target_user_id": None}) is None

        assert _rows() == []
        warn.assert_not_called()

    def test_a_check_with_part_of_the_key_matches_nothing(self, pending_db):
        """A partial key would match every echo of that person and skip a real message."""
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)
        partial = {name: value for name, value in MESSAGE.items() if name != "target_ts"}

        with patch("helpers.pending_events.log_warning") as warn:
            assert check_pending_event(MESSAGE_ECHO, **partial) is False
            assert take_pending_event(MESSAGE_ECHO, **partial) is False

        assert warn.call_count == 2
        assert len(_rows()) == 1

    def test_an_unknown_kind_is_refused(self, pending_db):
        with patch("helpers.pending_events.log_warning") as warn:
            assert remember_pending_event("follow_up", **_replay()) is None

        warn.assert_called_once()
        assert _rows() == []


class TestEchoes:
    def test_a_message_echo_is_checked_not_taken(self, pending_db):
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)

        for _ in range(2):
            assert check_pending_event(MESSAGE_ECHO, **{**MESSAGE, "target_ts": "200.000000"}) is True
        assert check_pending_event(MESSAGE_ECHO, **{**MESSAGE, "target_user_id": "U_OTHER"}) is False
        assert check_pending_event(MESSAGE_ECHO, **{**MESSAGE, "target_ts": "200.000001"}) is False
        (row,) = _rows()
        assert (row.kind, row.target_team_id, row.target_user_id, row.target_channel_id) == (
            "message",
            "T2",
            "U_MAPPED",
            "C_TGT",
        )
        assert row.target_ts == Decimal("200.000000")

    def test_a_file_echo_matches_its_file(self, pending_db):
        for file_id in ("F98", "F99"):
            remember_pending_event(FILE_ECHO, target_team_id="T2", target_user_id="U_MAPPED", file_id=file_id)

        assert check_pending_event(FILE_ECHO, target_team_id="T2", target_user_id="U_MAPPED", file_id="F99") is True
        assert check_pending_event(FILE_ECHO, target_team_id="T2", target_user_id="U_MAPPED", file_id="F01") is False
        assert sorted(row.file_id for row in _rows()) == ["F98", "F99"]

    def test_a_write_again_within_the_ttl_keeps_its_own_window(self, pending_db):
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)

        assert len(_rows()) == 2

    def test_a_reaction_echo_is_taken_once_by_the_matching_event(self, pending_db):
        remember_pending_event(REACTION_ADDED_ECHO, **REACTION)

        assert take_pending_event(REACTION_REMOVED_ECHO, **REACTION) is False
        assert take_pending_event(REACTION_ADDED_ECHO, **{**REACTION, "reaction": "thumbsup"}) is False
        assert take_pending_event(REACTION_ADDED_ECHO, **REACTION) is True
        assert take_pending_event(REACTION_ADDED_ECHO, **REACTION) is False

    def test_a_take_is_one_delete_with_no_read_first(self, pending_db):
        remember_pending_event(REACTION_ADDED_ECHO, **REACTION)

        sql = _statements(pending_db, lambda: take_pending_event(REACTION_ADDED_ECHO, **REACTION))

        assert len(sql) == 1
        assert sql[0].startswith("DELETE")
        assert "REACTION" in sql[0]


class TestReactionOrder:
    def test_an_older_or_equal_event_is_stale_and_a_newer_one_is_not(self, pending_db):
        remember_pending_event(REACTION_ORDER, **ORDER, event_ts="2.000000")

        assert reaction_event_ts_is_stale(reaction="thumbsup", event_ts="1.000001", **ORDER_CHECK) is True
        assert reaction_event_ts_is_stale(reaction="thumbsup", event_ts="2.0", **ORDER_CHECK) is True
        assert reaction_event_ts_is_stale(reaction="thumbsup", event_ts="3.000000", **ORDER_CHECK) is False

    def test_order_is_per_reaction_person_and_message(self, pending_db):
        remember_pending_event(REACTION_ORDER, **ORDER, event_ts="2.000000")

        assert reaction_event_ts_is_stale(reaction="wave", event_ts="1.0", **ORDER_CHECK) is False
        other_person = {**ORDER_CHECK, "source_user_id": "U_OTHER"}
        assert reaction_event_ts_is_stale(reaction="thumbsup", event_ts="1.0", **other_person) is False
        other_message = {**ORDER_CHECK, "ts": "100.000001"}
        assert reaction_event_ts_is_stale(reaction="thumbsup", event_ts="1.0", **other_message) is False

    def test_the_reaction_order_row_is_never_taken(self, pending_db):
        remember_pending_event(REACTION_ORDER, **ORDER, event_ts="2.000000")
        reaction_event_ts_is_stale(reaction="thumbsup", event_ts="1.0", **ORDER_CHECK)

        assert len(_rows()) == 1


class TestPendingFileShare:
    def test_find_then_take(self, pending_db):
        remember_pending_event(PENDING_SHARE, **SHARE, post_id="postabc", target_sync_channel_id=7)

        for _ in range(2):
            pending = find_pending_file_share(team_id="T2", channel_id="C_TGT", file_id="F1")
            assert (pending.post_id, pending.sync_channel_id) == ("postabc", 7)
        assert find_pending_file_share(team_id="T2", channel_id="C_OTHER", file_id="F1") is None
        assert take_pending_event(PENDING_SHARE, **SHARE) is True
        assert take_pending_event(PENDING_SHARE, **SHARE) is False
        assert find_pending_file_share(team_id="T2", channel_id="C_TGT", file_id="F1") is None

    def test_it_comes_back_as_the_copys_post_meta_with_the_source_workspace(self, pending_db):
        from db import DbManager
        from federation import core as federation_core

        federation_core._INSTANCE_ID = None
        federation_core._cached_private_key = None
        federation_core._cached_public_pem = None
        federation_core.get_or_create_instance_keypair()
        workspace = DbManager.create_record(
            schemas.Workspace(
                team_id="T_SRC", workspace_name="Workspace A", instance_id=federation_core.get_instance_id()
            )
        )
        remember_pending_event(
            PENDING_SHARE,
            **SHARE,
            post_id="postabc",
            target_sync_channel_id=7,
            source_team_id="T_SRC",
            source_user_id="U_SRC",
            target_user_id="U_MAPPED",
        )

        (row,) = _rows()
        assert (row.source_team_id, row.source_user_id, row.target_user_id) == ("T_SRC", "U_SRC", "U_MAPPED")
        pending = find_pending_file_share(team_id="T2", channel_id="C_TGT", file_id="F1")
        assert pending.source_workspace_id == workspace.id
        assert (pending.source_user_id, pending.posted_as_user_id) == ("U_SRC", "U_MAPPED")

    def test_an_unknown_source_team_has_no_workspace(self, pending_db):
        remember_pending_event(PENDING_SHARE, **SHARE, post_id="P", target_sync_channel_id=7, source_team_id="T_GONE")

        assert find_pending_file_share(team_id="T2", channel_id="C_TGT", file_id="F1").source_workspace_id is None


class TestReplay:
    def test_a_row_is_taken_back_by_its_id_once(self, pending_db):
        pending_id = remember_pending_event(REPLAY, **_replay())

        assert take_pending_event(REPLAY, id=pending_id) is True
        assert take_pending_event(REPLAY, id=pending_id) is False
        assert take_pending_event(REPLAY, id=None) is False

    def test_a_reply_waits_on_its_parent_and_anything_else_on_its_own_message(self, pending_db):
        remember_pending_event(REPLAY, **_replay())
        remember_pending_event(REPLAY, **_replay(action="create", source_ts="1795710090.000100", source_thread_ts=TS))
        remember_pending_event(REPLAY, **_replay(source_ts="1795710090.000100"))

        taken = take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts=TS)
        assert [(row.action, slack_message_ts(row.source_ts)) for row in taken] == [
            ("edit", TS),
            ("create", "1795710090.000100"),
        ]
        later = take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts="1795710090.000100")
        assert [(row.action, row.source_thread_ts) for row in later] == [("edit", None)]

    def test_rows_are_keyed_by_team_and_channel(self, pending_db):
        remember_pending_event(REPLAY, **_replay())

        assert take_pending_events_to_replay(team_id="T1", channel_id="C2", message_ts=TS) == []
        assert take_pending_events_to_replay(team_id="T2", channel_id="C1", message_ts=TS) == []
        assert len(take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts=TS)) == 1
        assert take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts=TS) == []

    def test_a_reaction_comes_back_with_who_what_and_when(self, pending_db):
        name = "a-custom-emoji-whose-name-runs-well-past-the-old-packed-limit::skin-tone-2"
        remember_pending_event(
            REPLAY, **_replay(action="add", source_user_id="U0BEN", reaction=name, event_ts="1795710085.0002")
        )

        (row,) = take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts=TS)
        assert (row.action, row.source_user_id, row.reaction) == ("add", "U0BEN", name)
        assert slack_message_ts(row.event_ts) == "1795710085.000200"


class TestLiveRowsOnly:
    """Correctness never waits on the sweep: every read and take ignores an expired row."""

    def test_check_take_find_order_and_replay_ignore_an_expired_row(self, pending_db):
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)
        remember_pending_event(REACTION_ADDED_ECHO, **REACTION)
        remember_pending_event(REACTION_ORDER, **ORDER, event_ts="2.0")
        remember_pending_event(PENDING_SHARE, **SHARE, post_id="P", target_sync_channel_id=7)
        remember_pending_event(REPLAY, **_replay())

        with patch("helpers.pending_events._utcnow", return_value=_later()):
            assert check_pending_event(MESSAGE_ECHO, **MESSAGE) is False
            assert take_pending_event(REACTION_ADDED_ECHO, **REACTION) is False
            assert reaction_event_ts_is_stale(reaction="thumbsup", event_ts="1.0", **ORDER_CHECK) is False
            assert find_pending_file_share(team_id="T2", channel_id="C_TGT", file_id="F1") is None
            assert take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts=TS) == []

        assert len(_rows()) == 5

    def test_a_handler_call_never_sweeps(self, pending_db):
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)

        def handler_calls():
            check_pending_event(MESSAGE_ECHO, **MESSAGE)
            remember_pending_event(FILE_ECHO, target_team_id="T2", target_user_id="U_MAPPED", file_id="F1")
            take_pending_event(REACTION_ADDED_ECHO, **REACTION)

        sql = _statements(pending_db, handler_calls)

        assert sql
        assert not [statement for statement in sql if "CREATED_AT <" in statement]


class TestSweep:
    def test_the_sweep_deletes_only_expired_rows(self, pending_db):
        remember_pending_event(MESSAGE_ECHO, **MESSAGE)
        with patch("helpers.pending_events._utcnow", return_value=pending_events._utcnow() - timedelta(minutes=11)):
            remember_pending_event(FILE_ECHO, target_team_id="T2", target_user_id="U_MAPPED", file_id="F_OLD")

        assert purge_expired_pending_events() == 1
        assert [row.kind for row in _rows()] == ["message"]
        assert purge_expired_pending_events() == 0

    def test_the_scheduled_pulse_runs_the_sweep(self):
        from federation.core import refresh_instance

        with (
            patch("helpers.notifications.purge_stale_soft_deletes"),
            patch("helpers.pending_events.purge_expired_pending_events") as sweep,
            patch("federation.core.refresh_federation_allowlists"),
        ):
            refresh_instance()

        sweep.assert_called_once_with()

    def test_a_failed_sweep_does_not_stop_the_pulse(self):
        from federation.core import refresh_instance

        with (
            patch("helpers.notifications.purge_stale_soft_deletes"),
            patch("helpers.pending_events.purge_expired_pending_events", side_effect=RuntimeError("down")),
            patch("federation.core.refresh_federation_allowlists") as allowlists,
        ):
            refresh_instance()

        allowlists.assert_called_once()


class TestCompleteCopyPendingShare:
    @pytest.fixture
    def real_db(self, tmp_path):
        import db as db_mod
        from db import initialize_database
        from helpers._cache import clear_all_caches

        url = f"sqlite:///{tmp_path / 'pending_copy.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_session = db_mod.GLOBAL_SESSION
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SESSION = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                from federation import core as federation_core

                federation_core._INSTANCE_ID = None
                federation_core._cached_private_key = None
                federation_core._cached_public_pem = None
                federation_core.get_or_create_instance_keypair()
                clear_all_caches()
                yield
            finally:
                clear_all_caches()
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SESSION = old_session
                db_mod.GLOBAL_SCHEMA = old_schema

    def _channel(self, channel_id="C_TGT"):
        from datetime import UTC, datetime
        from uuid import uuid4

        from db import DbManager
        from federation.core import get_instance_id

        workspace = DbManager.create_record(
            schemas.Workspace(
                team_id="T_TGT",
                workspace_name="Workspace A",
                instance_id=get_instance_id(),
            )
        )
        sync = DbManager.create_record(schemas.Sync(title="pending-copy", sync_mode="group", uid=str(uuid4())))
        sync_channel = DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=workspace.id,
                channel_id=channel_id,
                status="active",
                publishes=True,
                subscribes=True,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        return workspace, sync_channel

    def test_a_ts_matches_only_in_its_own_channel(self, real_db):
        """Two Channels can each have a message at the same ts; only the asked Channel's matches."""
        from decimal import Decimal

        from db import DbManager
        from helpers.post_meta import get_post_records

        workspace, here = self._channel("C_HERE")
        there = DbManager.create_record(
            schemas.SyncChannel(
                sync_id=here.sync_id,
                workspace_id=workspace.id,
                channel_id="C_THERE",
                status="active",
                publishes=True,
                subscribes=True,
                created_at=here.created_at,
            )
        )
        DbManager.create_record(schemas.PostMeta(post_id="P_HERE", sync_channel_id=here.id, ts=Decimal("300.000001")))
        DbManager.create_record(schemas.PostMeta(post_id="P_THERE", sync_channel_id=there.id, ts=Decimal("300.000001")))

        assert {pm.post_id for pm, _sc, _ws in get_post_records("300.000001", channel_id="C_HERE")} == {"P_HERE"}
        assert get_post_records("300.000001", channel_id="C_ELSEWHERE") == []
        assert {pm.post_id for pm, _sc, _ws in get_post_records("300.000001")} == {"P_HERE", "P_THERE"}

    def _remember(self, sync_channel, file_id, post_id, **columns):
        remember_pending_event(
            PENDING_SHARE,
            target_team_id="T_TGT",
            target_channel_id=sync_channel.channel_id,
            file_id=file_id,
            post_id=post_id,
            target_sync_channel_id=sync_channel.id,
            **columns,
        )

    def test_writes_copy_without_local_origin_records(self, real_db):
        from helpers.post_meta import complete_copy_ts_from_pending_share, get_post_records

        workspace, sync_channel = self._channel()
        self._remember(
            sync_channel,
            "F1",
            "postfed",
            source_team_id="T_TGT",
            source_user_id="U_SRC",
            target_user_id="U_MAPPED",
        )
        assert complete_copy_ts_from_pending_share("T_TGT", "C_TGT", "200.000001", ["F1"]) is True
        rows = get_post_records("200.000001")
        assert len(rows) == 1
        post_meta, sc, _ws = rows[0]
        assert post_meta.post_id == "postfed"
        assert sc.id == sync_channel.id
        assert post_meta.source_user_id == "U_SRC"
        assert post_meta.source_workspace_id == workspace.id
        assert post_meta.posted_as_user_id == "U_MAPPED"
        assert find_pending_file_share(team_id="T_TGT", channel_id="C_TGT", file_id="F1") is None

    def test_writes_only_the_remembered_sync_channel(self, real_db):
        from datetime import UTC, datetime
        from uuid import uuid4

        from db import DbManager
        from helpers.post_meta import complete_copy_ts_from_pending_share

        workspace, first = self._channel()
        other_sync = DbManager.create_record(schemas.Sync(title="other-sync", sync_mode="group", uid=str(uuid4())))
        second = DbManager.create_record(
            schemas.SyncChannel(
                sync_id=other_sync.id,
                workspace_id=workspace.id,
                channel_id="C_TGT",
                status="active",
                publishes=True,
                subscribes=True,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        self._remember(first, "F1", "postfed")
        assert complete_copy_ts_from_pending_share("T_TGT", "C_TGT", "200.000001", ["F1"]) is True
        rows = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "postfed"])
        assert [pm.sync_channel_id for pm in rows] == [first.id]
        assert second.id not in {pm.sync_channel_id for pm in rows}

    def test_writes_file_ts_when_parent_row_already_exists(self, real_db):
        from db import DbManager
        from helpers.post_meta import complete_copy_ts_from_pending_share, get_post_records

        _workspace, sync_channel = self._channel()
        DbManager.create_record(
            schemas.PostMeta(
                post_id="postsplit",
                sync_channel_id=sync_channel.id,
                ts=post_meta_ts("100.000001"),
            )
        )
        self._remember(sync_channel, "F2", "postsplit")
        assert complete_copy_ts_from_pending_share("T_TGT", "C_TGT", "200.000002", ["F2"]) is True
        rows = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "postsplit"])
        assert {str(pm.ts) for pm in rows} == {"100.000001", "200.000002"}
        assert [pm.post_id for pm, _sc, _ws in get_post_records("200.000002")] == ["postsplit"]

    def test_releases_claim_when_sync_channel_is_missing(self, real_db):
        from helpers.post_meta import complete_copy_ts_from_pending_share

        remember_pending_event(
            PENDING_SHARE,
            target_team_id="T_TGT",
            target_channel_id="C_NONE",
            file_id="F3",
            post_id="postnone",
            target_sync_channel_id=99999,
        )
        assert complete_copy_ts_from_pending_share("T_TGT", "C_NONE", "200.000003", ["F3"]) is False
        pending = find_pending_file_share(team_id="T_TGT", channel_id="C_NONE", file_id="F3")
        assert pending is not None
        assert pending.post_id == "postnone"
