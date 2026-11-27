"""Replies, edits, deletes, and reactions that come before their message's copy: left pending, then replayed."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from db import schemas
from handlers.message import _replay_pending_events, respond_to_message_event
from helpers.pending_events import REPLAY, remember_pending_event, take_pending_event, take_pending_events_to_replay
from helpers.post_meta import leave_pending_event
from helpers.slack_api import fetch_source_message
from helpers.slack_ts import post_meta_ts, slack_message_ts
from tests.event_fixtures import make_event_context

MESSAGE_TS = "1795710083.439439"
REPLY_TS = "1795710090.000100"
RECORDS = [("meta", "row", "workspace")]
EDIT = {"action": "edit", "source_ts": MESSAGE_TS}


@pytest.fixture
def pending_db(tmp_path):
    import db as db_mod
    from db import get_engine, initialize_database

    url = f"sqlite:///{tmp_path / 'replay.db'}"
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


def _take(message_ts=MESSAGE_TS):
    return [
        (row.action, slack_message_ts(row.source_ts))
        for row in take_pending_events_to_replay(team_id="T1", channel_id="C1", message_ts=message_ts)
    ]


def _remember(**columns):
    return remember_pending_event(
        REPLAY, source_team_id="T1", source_channel_id="C1", **{"source_thread_ts": None, **EDIT, **columns}
    )


class TestAtomicTake:
    def test_take_back_is_one_delete_by_id_with_no_read_first(self, pending_db):
        from sqlalchemy import event

        pending_id = _remember()
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args):
            statements.append(" ".join(statement.split()).upper())

        event.listen(pending_db, "before_cursor_execute", record)
        try:
            assert take_pending_event(REPLAY, id=pending_id) is True
        finally:
            event.remove(pending_db, "before_cursor_execute", record)

        sql = [statement for statement in statements if "PENDING_EVENTS" in statement]
        assert len(sql) == 1
        assert sql[0].startswith("DELETE")
        assert "PENDING_EVENTS.ID" in sql[0]
        assert take_pending_event(REPLAY, id=pending_id) is False


class TestLeavePendingEvent:
    def test_event_first_leaves_a_row_the_create_takes_once(self, pending_db):
        with patch("helpers.post_meta.get_post_records", return_value=[]):
            assert leave_pending_event("T1", "C1", **EDIT) == []

        assert _take() == [("edit", MESSAGE_TS)]
        assert _take() == []

    def test_create_first_the_event_takes_its_own_row_back_and_applies(self, pending_db):
        with patch("helpers.post_meta.get_post_records", return_value=RECORDS):
            assert leave_pending_event("T1", "C1", **EDIT) == RECORDS

        assert _take() == []

    def test_when_the_create_took_the_row_the_event_does_not_apply(self, pending_db):
        def create_finishes_and_takes(_ts):
            assert _take() == [("edit", MESSAGE_TS)]
            return RECORDS

        with patch("helpers.post_meta.get_post_records", side_effect=create_finishes_and_takes):
            assert leave_pending_event("T1", "C1", **EDIT) == []

    def test_a_reply_looks_for_its_parent(self, pending_db):
        with patch("helpers.post_meta.get_post_records", return_value=[]) as records:
            assert (
                leave_pending_event("T1", "C1", action="create", source_ts=REPLY_TS, source_thread_ts=MESSAGE_TS) == []
            )

        records.assert_called_once_with(MESSAGE_TS)
        assert _take() == [("create", REPLY_TS)]

    def test_a_thread_ts_counts_only_on_a_new_reply(self, pending_db):
        with patch("helpers.post_meta.get_post_records", return_value=[]) as records:
            leave_pending_event("T1", "C1", action="edit", source_ts=REPLY_TS, source_thread_ts=MESSAGE_TS)

        records.assert_called_once_with(REPLY_TS)
        assert _take() == []
        assert _take(REPLY_TS) == [("edit", REPLY_TS)]

    def test_a_row_that_was_not_written_applies_when_the_copy_is_there(self, pending_db):
        with (
            patch("helpers.post_meta.remember_pending_event", return_value=None),
            patch("helpers.post_meta.get_post_records", return_value=RECORDS),
        ):
            assert leave_pending_event("T1", "C1", **EDIT) == RECORDS

    def test_nothing_kept_and_nothing_to_apply_is_none(self, pending_db):
        """A row that could not be written, and no PostMeta: the caller releases its claim."""
        with (
            patch("helpers.post_meta.remember_pending_event", return_value=None),
            patch("helpers.post_meta.get_post_records", return_value=[]),
        ):
            assert leave_pending_event("T1", "C1", **EDIT) is None

    def test_two_of_the_same_edit_each_take_back_their_own_row(self, pending_db):
        """No unique key: a second edit in flight is not folded into the first one's row."""
        with patch("helpers.post_meta.get_post_records", return_value=RECORDS):
            first = _remember()
            assert leave_pending_event("T1", "C1", **EDIT) == RECORDS

        assert take_pending_event(REPLAY, id=first) is True


def _ctx(**overrides):
    fields = {"team_id": "T1", "channel_id": "C1", "ts": MESSAGE_TS, "user_id": "U0ADA", **overrides}
    return make_event_context(**fields)


def _slack_error(code):
    """A SlackApiError whose response looks like Slack's (status code and ``error``)."""
    response = MagicMock(status_code=200)
    response.get.side_effect = {"ok": False, "error": code}.get
    return SlackApiError(code, response)


def _pending(action, ts=MESSAGE_TS, *, thread_ts=None, event_ts=None, **columns):
    """A REPLAY row as ``take_pending_events_to_replay`` returns it."""
    return schemas.PendingEvent(
        kind=REPLAY,
        source_team_id="T1",
        source_channel_id="C1",
        action=action,
        source_ts=post_meta_ts(ts),
        source_thread_ts=post_meta_ts(thread_ts) if thread_ts else None,
        event_ts=post_meta_ts(event_ts) if event_ts else None,
        **columns,
    )


def _replay(rows, *, fetched=None, fetch_error=None):
    with (
        patch("handlers.message.helpers.take_pending_events_to_replay", return_value=rows),
        patch(
            "handlers.message.helpers.fetch_source_message",
            side_effect=fetch_error,
            return_value=fetched,
        ) as fetch,
        patch("handlers.message.respond_to_message_event") as respond,
        patch("handlers.message.helpers.get_post_records", return_value=RECORDS),
        patch("handlers.reaction_event._sync_reaction_records") as reaction,
    ):
        _replay_pending_events(MagicMock(), MagicMock(), _ctx())
    return respond, fetch, reaction


class TestReplay:
    def test_a_message_still_there_replays_as_an_edit(self):
        message = {"ts": MESSAGE_TS, "user": "U0ADA", "text": "see", "attachments": [{"is_msg_unfurl": True}]}

        respond, fetch, _reaction = _replay([_pending("edit")], fetched=message)

        fetch.assert_called_once()
        assert fetch.call_args.kwargs["thread_ts"] is None
        event = respond.call_args.args[0]["event"]
        assert event["subtype"] == "message_changed"
        assert event["message"] == message
        assert respond.call_args.args[0]["team_id"] == "T1"

    def test_a_gone_message_replays_as_a_delete(self):
        respond, _fetch, _reaction = _replay([_pending("delete")], fetched=None)

        event = respond.call_args.args[0]["event"]
        assert event["subtype"] == "message_deleted"
        assert event["previous_message"] == {"ts": MESSAGE_TS, "user": "U0ADA"}

    def test_an_edit_and_a_delete_of_the_message_read_it_once(self):
        respond, fetch, _reaction = _replay([_pending("edit"), _pending("delete"), _pending("edit")], fetched=None)

        fetch.assert_called_once()
        respond.assert_called_once()

    def test_a_reply_replays_as_a_new_message_in_this_thread(self):
        reply = {"ts": REPLY_TS, "thread_ts": MESSAGE_TS, "user": "U0ADA", "text": "first!"}

        respond, fetch, _reaction = _replay([_pending("create", REPLY_TS, thread_ts=MESSAGE_TS)], fetched=reply)

        assert fetch.call_args.args[2] == REPLY_TS
        assert fetch.call_args.kwargs["thread_ts"] == MESSAGE_TS
        event = respond.call_args.args[0]["event"]
        assert event["ts"] == REPLY_TS
        assert "subtype" not in event

    def test_a_reaction_replays_through_the_reaction_sync_with_its_event_ts(self):
        added = _pending("add", source_user_id="U0BEN", reaction="wave", event_ts="1795710085.0002")
        removed = _pending("remove", source_user_id="U0BEN", reaction="wave")

        respond, fetch, reaction = _replay([added, removed])

        fetch.assert_not_called()
        respond.assert_not_called()
        first, second = (call.args[0]["event"] for call in reaction.call_args_list)
        assert first["type"] == "reaction_added"
        assert (first["user"], first["reaction"], first["event_ts"]) == ("U0BEN", "wave", "1795710085.000200")
        assert first["item"] == {"type": "message", "channel": "C1", "ts": MESSAGE_TS}
        assert second["type"] == "reaction_removed"
        assert "event_ts" not in second
        assert reaction.call_args.args[2] == RECORDS

    def test_a_slack_error_while_reading_does_not_replay_a_delete(self):
        error = SlackApiError("slow", {"ok": False, "error": "ratelimited"})

        respond, _fetch, _reaction = _replay([_pending("edit")], fetch_error=error)

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
            patch("handlers.message.helpers.check_pending_event", side_effect=lambda kind, **_: kind == "message"),
            patch("handlers.message._handle_message_edit") as edit,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        edit.assert_not_called()


class TestHandlersLeavePending:
    def test_an_edit_with_no_copy_yet_is_left_pending_and_completes(self):
        from handlers.message import _handle_message_edit

        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message.helpers.is_before_sync", return_value=False),
            patch("handlers.message.helpers.leave_pending_event", return_value=[]) as leave,
            patch("handlers.message.helpers.run_sync_pipeline") as pipeline,
        ):
            assert _handle_message_edit(MagicMock(), MagicMock(), _ctx(), []) is None

        leave.assert_called_once_with("T1", "C1", action="edit", source_ts=MESSAGE_TS, source_thread_ts=None)
        pipeline.assert_not_called()

    def test_a_reply_whose_parent_is_still_posting_is_left_pending(self):
        from handlers.message import _handle_thread_reply

        ctx = _ctx(ts=REPLY_TS, thread_ts=MESSAGE_TS)
        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message.helpers.is_before_sync", return_value=False),
            patch("handlers.message.helpers.leave_pending_event", return_value=[]) as leave,
            patch("handlers.message.helpers.cleanup_temp_files") as cleanup,
        ):
            assert _handle_thread_reply({}, MagicMock(), MagicMock(), ctx, [], [{"path": "/tmp/a"}]) is None

        leave.assert_called_once_with("T1", "C1", action="create", source_ts=REPLY_TS, source_thread_ts=MESSAGE_TS)
        cleanup.assert_called_once()

    def test_a_reaction_with_no_copy_yet_is_left_pending(self):
        from handlers.reaction_event import handle_reaction

        body = {
            "team_id": "T1",
            "event_id": "",
            "event": {
                "type": "reaction_added",
                "user": "U0BEN",
                "reaction": "wave",
                "item": {"type": "message", "channel": "C1", "ts": MESSAGE_TS},
                "event_ts": "1795710085.000200",
            },
        }
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="U0BOT"),
            patch("handlers.reaction_event.helpers.take_pending_event", return_value=False),
            patch("handlers.reaction_event.helpers.has_channel_sync", return_value=True),
            patch("handlers.reaction_event.helpers.origin_publishes_anywhere", return_value=True),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=[]),
            patch("handlers.reaction_event.helpers.is_before_sync", return_value=False),
            patch("handlers.reaction_event.helpers.leave_pending_event", return_value=[]) as leave,
            patch("handlers.reaction_event._sync_reaction_records") as sync,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})

        leave.assert_called_once_with(
            "T1",
            "C1",
            action="add",
            source_ts=MESSAGE_TS,
            source_user_id="U0BEN",
            reaction="wave",
            event_ts="1795710085.000200",
        )
        sync.assert_not_called()


class TestNothingKeptReleasesTheClaim:
    """``leave_pending_event`` returned ``None``: each handler returns ``False`` so the claim is released."""

    def test_a_reply_an_edit_and_a_delete_release_the_claim(self):
        from handlers.message import _handle_message_delete, _handle_message_edit, _handle_thread_reply

        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message.helpers.is_before_sync", return_value=False),
            patch("handlers.message.helpers.leave_pending_event", return_value=None),
            patch("handlers.message.helpers.cleanup_temp_files"),
            patch("handlers.message.helpers.run_sync_pipeline") as pipeline,
        ):
            reply = _handle_thread_reply({}, MagicMock(), MagicMock(), _ctx(ts=REPLY_TS, thread_ts=MESSAGE_TS), [], [])
            edit = _handle_message_edit(MagicMock(), MagicMock(), _ctx(), [])
            delete = _handle_message_delete(_ctx(), MagicMock(), MagicMock(), "U0ADA")

        assert (reply, edit, delete) == (False, False, False)
        pipeline.assert_not_called()

    def test_a_reaction_releases_the_claim(self):
        from handlers.reaction_event import handle_reaction

        body = {
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "reaction_added",
                "user": "U0BEN",
                "reaction": "wave",
                "item": {"type": "message", "channel": "C1", "ts": MESSAGE_TS},
            },
        }
        returned: list = []
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="U0BOT"),
            patch("handlers.reaction_event.helpers.take_pending_event", return_value=False),
            patch("handlers.reaction_event.helpers.has_channel_sync", return_value=True),
            patch("handlers.reaction_event.helpers.origin_publishes_anywhere", return_value=True),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=[]),
            patch("handlers.reaction_event.helpers.is_before_sync", return_value=False),
            patch("handlers.reaction_event.helpers.leave_pending_event", return_value=None),
            patch("handlers.reaction_event._sync_reaction_records") as sync,
            patch("handlers.reaction_event.run_claimed", side_effect=lambda _body, work: returned.append(work())),
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})

        assert returned == [False]
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
