"""Follow-ups that come before their message's copy: the note, the take, and the replay."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from handlers.message import _replay_follow_ups, respond_to_message_event
from helpers.post_meta import hand_off_follow_up
from helpers.slack_api import fetch_source_message
from helpers.user_action_echo import (
    pack_reaction_note,
    remember_follow_up,
    remember_user_action,
    take_follow_ups,
    take_user_action_echo,
    unpack_reaction_note,
)
from tests.event_fixtures import make_event_context

MESSAGE_TS = "1795710083.439439"
RECORDS = [("meta", "row", "workspace")]


@pytest.fixture
def echo_db(tmp_path):
    import db as db_mod
    from db import get_engine, initialize_database

    url = f"sqlite:///{tmp_path / 'echo.db'}"
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


class TestAtomicTake:
    def test_take_is_one_delete_with_no_read_first(self, echo_db):
        from sqlalchemy import event

        remember_user_action("T1", "U1", "reaction_added", "C1:1.000000:wave")
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args):
            statements.append(" ".join(statement.split()).upper())

        event.listen(echo_db, "before_cursor_execute", record)
        try:
            assert take_user_action_echo("T1", "U1", "reaction_added", "C1:1.000000:wave") is True
        finally:
            event.remove(echo_db, "before_cursor_execute", record)

        echo_sql = [sql for sql in statements if "USER_ACTION_ECHOES" in sql]
        assert not [sql for sql in echo_sql if sql.startswith("SELECT")]
        assert len([sql for sql in echo_sql if sql.startswith("DELETE") and "FINGERPRINT" in sql]) == 1
        assert take_user_action_echo("T1", "U1", "reaction_added", "C1:1.000000:wave") is False


class TestHandOff:
    def test_follow_up_first_leaves_a_note_the_create_takes_once(self, echo_db):
        with patch("helpers.post_meta.get_post_records", return_value=[]):
            assert hand_off_follow_up("T1", "C1", MESSAGE_TS, MESSAGE_TS) == []

        assert take_follow_ups("T1", "C1", MESSAGE_TS) == [MESSAGE_TS]
        assert take_follow_ups("T1", "C1", MESSAGE_TS) == []

    def test_create_first_the_follow_up_takes_its_own_note_and_applies(self, echo_db):
        with patch("helpers.post_meta.get_post_records", return_value=RECORDS):
            assert hand_off_follow_up("T1", "C1", MESSAGE_TS, MESSAGE_TS) == RECORDS

        assert take_follow_ups("T1", "C1", MESSAGE_TS) == []

    def test_when_the_create_took_the_note_the_follow_up_does_not_apply(self, echo_db):
        def create_finishes_and_takes(_ts):
            assert take_follow_ups("T1", "C1", MESSAGE_TS) == [MESSAGE_TS]
            return RECORDS

        with patch("helpers.post_meta.get_post_records", side_effect=create_finishes_and_takes):
            assert hand_off_follow_up("T1", "C1", MESSAGE_TS, MESSAGE_TS) == []

    def test_notes_come_back_oldest_first_and_are_keyed_by_message(self, echo_db):
        remember_follow_up("T1", "C1", MESSAGE_TS, MESSAGE_TS)
        remember_follow_up("T1", "C1", MESSAGE_TS, "1795710090.000100")
        remember_follow_up("T1", "C1", "1795710099.000000", "1795710099.000000")

        assert take_follow_ups("T1", "C1", MESSAGE_TS) == [MESSAGE_TS, "1795710090.000100"]

    def test_reaction_notes_pack_and_a_long_one_is_not_written(self, echo_db):
        note = pack_reaction_note("reaction_added", "U0ADA", "thumbsup::skin-tone-2")

        assert unpack_reaction_note(note) == ("reaction_added", "U0ADA", "thumbsup::skin-tone-2")
        assert unpack_reaction_note(MESSAGE_TS) is None
        assert pack_reaction_note("reaction_added", "U0ADA", "x" * 100) is None


def _ctx(**overrides):
    fields = {"team_id": "T1", "channel_id": "C1", "ts": MESSAGE_TS, "user_id": "U0ADA", **overrides}
    return make_event_context(**fields)


def _slack_error(code):
    """A SlackApiError whose response looks like Slack's (status code and ``error``)."""
    response = MagicMock(status_code=200)
    response.get.side_effect = {"ok": False, "error": code}.get
    return SlackApiError(code, response)


def _replay(notes, *, fetched=None, fetch_error=None):
    with (
        patch("handlers.message.helpers.take_follow_ups", return_value=notes),
        patch(
            "handlers.message.helpers.fetch_source_message",
            side_effect=fetch_error,
            return_value=fetched,
        ) as fetch,
        patch("handlers.message.respond_to_message_event") as respond,
        patch("handlers.message.helpers.get_post_records", return_value=RECORDS),
        patch("handlers.reaction_event._sync_reaction_records") as reaction,
    ):
        _replay_follow_ups(MagicMock(), MagicMock(), _ctx())
    return respond, fetch, reaction


class TestReplay:
    def test_a_message_still_there_replays_as_an_edit(self):
        message = {"ts": MESSAGE_TS, "user": "U0ADA", "text": "see", "attachments": [{"is_msg_unfurl": True}]}

        respond, fetch, _reaction = _replay([MESSAGE_TS], fetched=message)

        fetch.assert_called_once()
        assert fetch.call_args.kwargs["thread_ts"] is None
        event = respond.call_args.args[0]["event"]
        assert event["subtype"] == "message_changed"
        assert event["message"] == message
        assert respond.call_args.args[0]["team_id"] == "T1"

    def test_a_gone_message_replays_as_a_delete(self):
        respond, _fetch, _reaction = _replay([MESSAGE_TS], fetched=None)

        event = respond.call_args.args[0]["event"]
        assert event["subtype"] == "message_deleted"
        assert event["previous_message"] == {"ts": MESSAGE_TS, "user": "U0ADA"}

    def test_a_reply_replays_as_a_new_message_in_this_thread(self):
        reply = {"ts": "1795710090.000100", "thread_ts": MESSAGE_TS, "user": "U0ADA", "text": "first!"}

        respond, fetch, _reaction = _replay(["1795710090.000100"], fetched=reply)

        assert fetch.call_args.kwargs["thread_ts"] == MESSAGE_TS
        event = respond.call_args.args[0]["event"]
        assert event["ts"] == "1795710090.000100"
        assert "subtype" not in event

    def test_a_reaction_replays_through_the_reaction_sync(self):
        note = pack_reaction_note("reaction_added", "U0BEN", "wave")

        respond, fetch, reaction = _replay([note])

        fetch.assert_not_called()
        respond.assert_not_called()
        body = reaction.call_args.args[0]
        assert body["event"]["type"] == "reaction_added"
        assert body["event"]["item"] == {"type": "message", "channel": "C1", "ts": MESSAGE_TS}
        assert reaction.call_args.args[2] == RECORDS

    def test_a_slack_error_while_reading_does_not_replay_a_delete(self):
        error = SlackApiError("slow", {"ok": False, "error": "ratelimited"})

        respond, _fetch, _reaction = _replay([MESSAGE_TS], fetch_error=error)

        respond.assert_not_called()


class TestReplayStillRunsTheGuards:
    def test_a_replayed_edit_of_an_echo_is_skipped(self):
        body = {
            "team_id": "T1",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C1",
                "message": {"ts": MESSAGE_TS, "user": "U0ADA", "text": "copy"},
            },
        }
        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_channel_sync", return_value=True),
            patch("handlers.message.helpers.has_user_action_echo", return_value=True),
            patch("handlers.message._handle_message_edit") as edit,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        edit.assert_not_called()


class TestHandlersHandOff:
    def test_an_edit_with_no_copy_yet_hands_off_and_completes(self):
        from handlers.message import _handle_message_edit

        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message._is_before_sync", return_value=False),
            patch("handlers.message.helpers.hand_off_follow_up", return_value=[]) as hand_off,
            patch("handlers.message.helpers.run_sync_pipeline") as pipeline,
        ):
            assert _handle_message_edit(MagicMock(), MagicMock(), _ctx(), []) is None

        hand_off.assert_called_once_with("T1", "C1", MESSAGE_TS, MESSAGE_TS)
        pipeline.assert_not_called()

    def test_a_reply_whose_parent_is_still_posting_hands_off(self):
        from handlers.message import _handle_thread_reply

        ctx = _ctx(ts="1795710090.000100", thread_ts=MESSAGE_TS)
        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message._is_before_sync", return_value=False),
            patch("handlers.message.helpers.hand_off_follow_up", return_value=[]) as hand_off,
            patch("handlers.message.helpers.cleanup_temp_files") as cleanup,
        ):
            assert _handle_thread_reply({}, MagicMock(), MagicMock(), ctx, [], [{"path": "/tmp/a"}]) is None

        hand_off.assert_called_once_with("T1", "C1", MESSAGE_TS, "1795710090.000100")
        cleanup.assert_called_once()

    def test_a_reaction_with_no_copy_yet_hands_off(self):
        from handlers.reaction_event import handle_reaction

        body = {
            "team_id": "T1",
            "event_id": "",
            "event": {
                "type": "reaction_added",
                "user": "U0BEN",
                "reaction": "wave",
                "item": {"type": "message", "channel": "C1", "ts": MESSAGE_TS},
            },
        }
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="U0BOT"),
            patch("handlers.reaction_event.helpers.take_user_action_echo", return_value=False),
            patch("handlers.reaction_event.helpers.has_channel_sync", return_value=True),
            patch("handlers.reaction_event.helpers.origin_publishes_anywhere", return_value=True),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=[]),
            patch("handlers.reaction_event.helpers.hand_off_follow_up", return_value=[]) as hand_off,
            patch("handlers.reaction_event._sync_reaction_records") as sync,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})

        hand_off.assert_called_once_with("T1", "C1", MESSAGE_TS, "reaction|reaction_added|U0BEN|wave")
        sync.assert_not_called()


class TestFetchSourceMessage:
    def test_finds_the_message_among_the_thread_and_gone_is_none(self):
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [{"ts": MESSAGE_TS, "text": "parent"}, {"ts": "1795710090.000100", "text": "reply"}]
        }

        found = fetch_source_message(client, "C1", "1795710090.000100", thread_ts=MESSAGE_TS)

        assert found["text"] == "reply"
        kwargs = client.conversations_replies.call_args.kwargs
        assert (kwargs["ts"], kwargs["oldest"], kwargs["latest"], kwargs["inclusive"]) == (
            MESSAGE_TS,
            "1795710090.000100",
            "1795710090.000100",
            True,
        )
        client.conversations_replies.side_effect = _slack_error("thread_not_found")
        assert fetch_source_message(client, "C1", MESSAGE_TS) is None

    def test_another_error_is_raised(self):
        client = MagicMock()
        client.conversations_replies.side_effect = _slack_error("not_in_channel")

        with pytest.raises(SlackApiError):
            fetch_source_message(client, "C1", MESSAGE_TS)


class TestPeerParentMissing:
    def test_a_final_parent_missing_on_an_edit_is_a_skip(self):
        from helpers.sync_pipeline import run_sync_pipeline

        stub = SimpleNamespace(id=9, team_id="TSTUB", deleted_at=None, instance_id="peer")
        row = SimpleNamespace(id=3, channel_id="C0REMOTE", sync_id=7, subscribes=True, status="active")
        peer = SimpleNamespace(instance_id="peer", trust_status="trusted")
        envelope = {"kind": "message", "action": "edit", "post_id": "P1", "source_workspace_id": 1}
        report: list = []
        with (
            patch("helpers.sync_pipeline.iter_publish_targets", return_value=[(row, stub)]),
            patch("helpers.sync_pipeline.get_active_sync_channel", side_effect=lambda r: r),
            patch("helpers.sync_pipeline.is_stub_workspace", return_value=True),
            patch("helpers.sync_pipeline.is_local_workspace", return_value=False),
            patch("helpers.sync_pipeline.peer_for_workspace", return_value=peer),
            patch("helpers.sync_pipeline.peer_is_trusted", return_value=True),
            patch("helpers.sync_pipeline.workspace_allowed_for_peer", return_value=True),
            patch("helpers.sync_pipeline.get_post_records_for_post_id", return_value=[]),
            patch("helpers.sync_pipeline._sleep_between_attempts"),
            patch("helpers.workspace.get_workspace_by_id", return_value=None),
            patch("federation.deliver.stage_unique_files_for_peer", return_value=True),
            patch("federation.deliver.DbManager.get_record", return_value=None),
            patch(
                "federation.deliver.push_edit",
                return_value={"_http_status": 409, "error": "parent_missing"},
            ) as push,
        ):
            run_sync_pipeline(envelope, source_channel_id="C0SOURCE", report=report)

        assert push.call_count == 3
        assert report == []
