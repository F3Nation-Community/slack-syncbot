"""Copies that were refused or reduced: the author's DM, the note, broken targets, and old messages."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from slack_sdk.errors import SlackApiError  # noqa: E402

from federation.deliver import deliver_remote  # noqa: E402
from handlers.message import _handle_thread_reply, _is_before_sync, _notify_author  # noqa: E402
from helpers.message_blocks import NOTE_COPY_DIFFERS, add_copy_note  # noqa: E402
from helpers.slack_write import slack_write_create, slack_write_delete  # noqa: E402
from helpers.sync_apply import ApplyOutcome, apply_target  # noqa: E402
from helpers.sync_pipeline import run_sync_pipeline  # noqa: E402
from tests.event_fixtures import make_event_context  # noqa: E402


def _row(channel_id, *, row_id, name=None, private=False):
    return SimpleNamespace(
        id=row_id,
        channel_id=channel_id,
        channel_name=name,
        is_private=private,
        sync_id=7,
        subscribes=True,
        status="active",
    )


def _workspace(row_id, name):
    return SimpleNamespace(id=row_id, team_id=f"T{row_id}", workspace_name=name, deleted_at=None)


def _envelope(**overrides):
    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "P1",
        "source_workspace_id": 1,
        "source_user_id": "U0ADA",
        "text": "hello",
    }
    envelope.update(overrides)
    return envelope


def _pipeline(targets, apply_side_effect, **extra):
    report: list = []
    with (
        patch("helpers.sync_pipeline.iter_publish_targets", return_value=targets),
        patch("helpers.sync_pipeline.get_active_sync_channel", side_effect=lambda row: row),
        patch("helpers.sync_pipeline.is_local_workspace", return_value=True),
        patch("helpers.sync_pipeline.get_post_records_for_post_id", return_value=[]),
        patch("helpers.sync_pipeline._sleep_between_attempts"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.sync_pipeline.apply_target", side_effect=apply_side_effect) as apply,
        patch("helpers.sync_pipeline.log_warning") as warn,
    ):
        result = run_sync_pipeline(_envelope(**extra), source_channel_id="C0SOURCE", report=report)
    return result, report, apply, warn


class TestPipelineReport:
    def test_a_slack_code_is_final_and_reported(self):
        general = (_row("C0GENERAL", row_id=1, name="general"), _workspace(10, "Workspace B"))
        other = (_row("C0OTHER", row_id=2, name="other"), _workspace(20, "Partner Org"))

        result, report, apply, warn = _pipeline(
            [general, other],
            [ApplyOutcome(error="not_in_channel"), ApplyOutcome(created=["copy"], ts="1.0")],
        )

        assert result == ["copy"]
        assert apply.call_count == 2
        assert report == [(general[0], general[1], "not_in_channel", False)]
        assert warn.call_args_list[0].args[0] == "message_not_copied"

    def test_a_reduced_copy_is_reported_as_copied(self):
        general = (_row("C0GENERAL", row_id=1), _workspace(10, "Workspace B"))

        result, report, _apply, warn = _pipeline(
            [general], [ApplyOutcome(created=["copy"], ts="1.0", error="invalid_attachments")]
        )

        assert result == ["copy"]
        assert report == [(general[0], general[1], "invalid_attachments", True)]
        assert warn.call_args_list[0].args[0] == "message_degraded"

    def test_a_raised_error_is_tried_again_and_not_reported_once_it_posts(self):
        general = (_row("C0GENERAL", row_id=1), _workspace(10, "Workspace B"))

        result, report, apply, _warn = _pipeline(
            [general], [TimeoutError("slow"), ApplyOutcome(created=["copy"], ts="1.0")]
        )

        assert result == ["copy"]
        assert apply.call_count == 2
        assert report == []

    def test_three_raised_errors_report_the_error_type(self):
        general = (_row("C0GENERAL", row_id=1), _workspace(10, "Workspace B"))

        _result, report, apply, _warn = _pipeline([general], TimeoutError("slow"))

        assert apply.call_count == 3
        assert report == [(general[0], general[1], "TimeoutError", False)]

    def test_a_gate_is_not_reported(self):
        paused = _row("C0PAUSED", row_id=1)
        paused.status = "paused"
        with (
            patch("helpers.sync_pipeline.iter_publish_targets", return_value=[(paused, _workspace(10, "B"))]),
            patch("helpers.sync_pipeline.get_active_sync_channel", return_value=None),
            patch("helpers.sync_pipeline.is_local_workspace", return_value=True),
            patch("helpers.sync_pipeline.get_post_records_for_post_id", return_value=[]),
            patch("helpers.workspace.get_workspace_by_id", return_value=None),
            patch("helpers.sync_pipeline.apply_target") as apply,
        ):
            report: list = []
            run_sync_pipeline(_envelope(), source_channel_id="C0SOURCE", report=report)

        apply.assert_not_called()
        assert report == []


class TestBrokenTargetPauses:
    def _apply(self, error, *, ts=None):
        row = _row("C0GENERAL", row_id=1)
        with (
            patch("helpers.sync_apply.get_active_sync_channel", side_effect=lambda r: r),
            patch("helpers.sync_apply.get_target_post_meta", return_value=None),
            patch("helpers.sync_apply.slack_write_create", return_value=(ts, None, error)),
            patch("helpers.sync_apply.DbManager.create_records"),
            patch("helpers.sync_lifecycle.pause_sync_channel") as pause,
        ):
            outcome = apply_target(_envelope(), row, _workspace(10, "Workspace B"))
        return outcome, pause, row

    def test_channel_codes_pause_the_row_once(self):
        for code, via in (
            ("not_in_channel", "kick"),
            ("channel_not_found", "kick"),
            ("is_archived", "archive"),
            ("restricted_action", "restricted"),
        ):
            outcome, pause, row = self._apply(code)

            assert outcome.error == code
            pause.assert_called_once_with(row, via=via, leave=False)

    def _delete(self, error, *, posted_as):
        row = _row("C0GENERAL", row_id=1)
        meta = SimpleNamespace(ts="5.000000", posted_as_user_id=posted_as)
        with (
            patch("helpers.sync_apply.get_active_sync_channel", side_effect=lambda r: r),
            patch("helpers.sync_apply.get_target_post_meta", return_value=meta),
            patch("helpers.sync_apply.slack_write_delete", return_value=(False, error)),
            patch("helpers.sync_lifecycle.pause_sync_channel") as pause,
        ):
            outcome = apply_target(_envelope(action="delete"), row, _workspace(10, "Workspace B"))
        return outcome, pause, row

    def test_a_delete_refused_to_the_bot_pauses(self):
        outcome, pause, row = self._delete("not_in_channel", posted_as=None)

        assert outcome.error == "not_in_channel"
        pause.assert_called_once_with(row, via="kick", leave=False)

    def test_a_delete_refused_to_the_person_it_was_posted_as_does_not_pause(self):
        """Only their token can delete their copy, and their access is not SyncBot's.

        A private Channel the person left answers ``channel_not_found`` while SyncBot is still a member.
        """
        for code in ("not_in_channel", "channel_not_found", "restricted_action", "is_archived"):
            outcome, pause, _row_out = self._delete(code, posted_as="U0ADA")

            assert outcome.error == code
            pause.assert_not_called()

    def test_a_message_code_or_a_reduced_copy_does_not_pause(self):
        for code, ts in (("msg_too_long", None), ("invalid_attachments", "1.0")):
            _outcome, pause, _row_out = self._apply(code, ts=ts)
            pause.assert_not_called()

    def test_restricted_action_on_a_user_token_tries_the_bot(self):
        refused = SlackApiError("no", {"ok": False, "error": "restricted_action"})
        with (
            patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
            patch("helpers.slack_write.WebClient"),
            patch("helpers.workspace.get_workspace_by_id", return_value=None),
            patch("helpers.slack_write.remember_pending_event"),
            patch("helpers.slack_write.post_message", side_effect=[refused, {"ts": "2.0"}]) as post,
        ):
            result = slack_write_create(
                envelope=_envelope(mapped_user_id="U_TARGET", user_name="Ada Lovelace"),
                sync_channel=_row("C0GENERAL", row_id=1),
                workspace=_workspace(10, "Workspace B"),
            )

        assert result == ("2.0", None, None)
        assert post.call_args_list[1].kwargs["bot_token"] == "xoxb-bot"

    def test_a_delete_of_a_copy_that_is_gone_is_not_a_failure(self):
        gone = SlackApiError("gone", {"ok": False, "error": "message_not_found"})
        meta = SimpleNamespace(ts="1.000000", posted_as_user_id=None)
        with (
            patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.slack_write.delete_message", side_effect=gone),
        ):
            assert slack_write_delete(
                sync_channel=_row("C0GENERAL", row_id=1), workspace=_workspace(10, "B"), target_post_meta=meta
            ) == (True, None)


def _notify(report, *, action="create", user_id="U0ADA", **ctx_overrides):
    ctx = make_event_context(channel_id="C0SOURCE", ts="1733497200.423469", **ctx_overrides)
    with (
        patch("handlers.message.helpers.resolve_workspace_name", side_effect=lambda ws: ws.workspace_name),
        patch("handlers.message.helpers.lookup_channel_name", return_value=None),
        patch("handlers.message.helpers.notify_source_user_error") as notify,
    ):
        _notify_author(MagicMock(), user_id, ctx, action, report)
    return notify


class TestAuthorDm:
    def test_one_dm_names_each_target_and_asks_to_report_it(self):
        general = (_row("C0GENERAL", row_id=1, name="general"), _workspace(10, "Workspace B"))
        announce = (_row("C0ANN", row_id=2, name="announcements"), _workspace(20, "Partner Org"))

        notify = _notify([(*general, "not_in_channel", False), (*announce, "invalid_attachments", True)])

        notify.assert_called_once()
        summary = notify.call_args.kwargs["summary"]
        details = notify.call_args.kwargs["details"]
        assert summary.startswith(":warning: SyncBot could not copy your message to `#general (Workspace B)`.")
        assert "copied your message to `#announcements (Partner Org)`, but it may not look the same there." in summary
        assert summary.endswith("Please send this to a workspace admin so it can be fixed.")
        assert details["event"] == "message_not_copied"
        assert details["#general (Workspace B)"] == "not_in_channel"
        assert details["#announcements (Partner Org)"] == "invalid_attachments"
        assert details["channel"] == "C0SOURCE"

    def test_a_private_target_is_named_by_its_workspace_only(self):
        secret = (_row("G0SECRET", row_id=1, name="secret", private=None), _workspace(10, "Workspace B"))

        notify = _notify([(*secret, "channel_not_found", False)])

        assert "`Workspace B`" in notify.call_args.kwargs["summary"]
        assert "secret" not in str(notify.call_args.kwargs)

    def test_storage_full_and_edit_and_delete_wording(self):
        general = (_row("C0GENERAL", row_id=1, name="general"), _workspace(10, "Workspace B"))

        storage = _notify([(*general, "storage_limit_reached", False)])
        edit = _notify([(*general, "cant_update_message", False)], action="edit")
        delete = _notify([(*general, "cant_delete_message", False)], action="delete")

        assert "That workspace's file storage is full." in storage.call_args.kwargs["summary"]
        assert "could not copy your edit to" in edit.call_args.kwargs["summary"]
        assert "could not delete the copy of your message in" in delete.call_args.kwargs["summary"]
        assert delete.call_args.kwargs["details"]["action"] == "delete"

    def test_reduced_before_fan_out_and_unreadable_files(self):
        notify = _notify([], degraded=["unknown_subtype", "download_empty"], files_failed=True)

        summary = notify.call_args.kwargs["summary"]
        assert "may not look the same in the other Channels." in summary
        assert "could not copy your file to the other Channels." in summary
        assert notify.call_args.kwargs["details"]["event"] == "message_degraded"
        assert notify.call_args.kwargs["details"]["reason"] == "unknown_subtype, download_empty"

    def test_nothing_to_report_or_no_author_sends_nothing(self):
        general = (_row("C0GENERAL", row_id=1), _workspace(10, "Workspace B"))

        _notify([]).assert_not_called()
        _notify([(*general, "not_in_channel", False)], user_id=None).assert_not_called()


class TestCopyNote:
    def test_text_only_gets_an_italic_line_and_blocks_get_a_context(self):
        rich = [{"type": "rich_text", "elements": []}]

        text, blocks = add_copy_note("hello", [])
        text_b, blocks_b = add_copy_note("hello", rich)

        assert text == f"hello\n_{NOTE_COPY_DIFFERS}_"
        assert blocks == []
        assert blocks_b[-1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": NOTE_COPY_DIFFERS}]}
        assert add_copy_note(text_b, blocks_b) == (text_b, blocks_b)


class TestPeerAnswers:
    def _deliver(self, answer):
        errors: list = []
        row = _row("C0REMOTE", row_id=3)
        with (
            patch("federation.deliver.push_message", return_value={"_http_status": 200, **answer}),
            patch("federation.deliver.DbManager.create_records"),
            patch("federation.deliver.DbManager.get_record", return_value=None),
        ):
            created = deliver_remote(_envelope(), MagicMock(), row, errors=errors)
        return created, errors

    def test_refused_reduced_skipped_and_sanitized(self):
        assert self._deliver({"ok": False, "error": "channel_not_found"}) == ([], [("channel_not_found", False)])
        created, errors = self._deliver({"ok": True, "ts": "5.0", "error": "invalid_blocks"})
        assert len(created) == 1 and errors == [("invalid_blocks", True)]
        assert self._deliver({"ok": True, "ts": None}) == ([], [])
        assert self._deliver({"ok": False, "error": "bad<code>"})[1] == [("badcode", False)]


class TestOldMessages:
    def _memberships(self, created_at):
        row = _row("C0SOURCE", row_id=1)
        row.created_at = created_at
        return [(row, _workspace(1, "Workspace A"))]

    def test_older_than_the_sync_is_done_and_newer_is_retried(self):
        started = datetime(2026, 11, 1, tzinfo=UTC)
        with (
            patch("handlers.message.helpers.get_channel_memberships", return_value=self._memberships(started)),
            patch("handlers.message.helpers.channel_publishes", return_value=True),
        ):
            assert _is_before_sync("C0SOURCE", "1700000000.000100") is True
            assert _is_before_sync("C0SOURCE", "1795670312.814729") is False

    def test_a_reply_under_an_old_thread_completes_the_claim(self):
        ctx = make_event_context(channel_id="C0SOURCE", thread_ts="1700000000.000100", ts="1795670312.814729")
        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message._is_before_sync", return_value=True),
        ):
            assert _handle_thread_reply({}, MagicMock(), MagicMock(), ctx, [], []) is None
        with (
            patch("handlers.message.helpers.get_post_records", return_value=[]),
            patch("handlers.message._is_before_sync", return_value=False),
            patch("handlers.message.helpers.leave_pending_event", return_value=[]) as leave,
        ):
            assert _handle_thread_reply({}, MagicMock(), MagicMock(), ctx, [], []) is None
        leave.assert_called_once()
