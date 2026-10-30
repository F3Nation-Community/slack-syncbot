"""Destructive confirmations use a red in-modal button, not a modal submit button.

A Slack modal's submit button is rendered in the workspace theme colour and
cannot be styled, so every destructive confirmation presents its affirmative
action as a ``danger`` (red) button inside the modal body and runs the work
as a *block action*. A checkbox on Disband Group is an actions element, not
an input, so the modal still has no submit button. These tests lock in that
contract and the routing/prefix wiring that makes it reachable.
"""

import contextlib
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from helpers.core import get_request_type  # noqa: E402
from helpers.synced_by import Viewer  # noqa: E402
from routing import ACTION_MAPPER, VIEW_MAPPER  # noqa: E402
from slack import actions  # noqa: E402

_CONFIRM_ACTIONS = (
    actions.CONFIG_LEAVE_GROUP_CONFIRM,
    actions.CONFIG_DISBAND_GROUP_CONFIRM,
    actions.CONFIG_DEMOTE_SELF_CONFIRM,
    actions.CONFIG_PROMOTE_TO_OWNER_CONFIRM,
    actions.CONFIG_CANCEL_GROUP_INVITE_CONFIRM,
    actions.CONFIG_LEAVE_SYNC_CONFIRM,
    actions.CONFIG_LEAVE_EXTERNAL_CONNECTION_CONFIRM,
    actions.CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION_CONFIRM,
)


def _danger_buttons(view: dict) -> list[dict]:
    """Return every ``danger``-styled button element in a modal view."""
    found = []
    for block in view.get("blocks", []):
        if block.get("type") != "actions":
            continue
        for element in block.get("elements", []):
            if element.get("type") == "button" and element.get("style") == "danger":
                found.append(element)
    return found


class TestConfirmActionRouting:
    def test_confirmations_are_block_actions_not_view_submissions(self):
        for action_id in _CONFIRM_ACTIONS:
            assert action_id in ACTION_MAPPER, f"{action_id!r} must be a block action"
            assert action_id not in VIEW_MAPPER, f"{action_id!r} must not be a view submission"

    def test_confirm_ids_do_not_collapse_onto_a_trigger_prefix(self):
        """The prefix matcher must not fold e.g. ``confirm_leave_group`` into ``leave_group``."""
        for action_id in _CONFIRM_ACTIONS:
            body = {"type": "block_actions", "actions": [{"action_id": action_id}]}
            category, resolved = get_request_type(body)
            assert category == "block_actions"
            assert resolved == action_id


class TestLeaveSyncConfirmModal:
    NO_PUBLISHER_BULLET = "The other Channels are told this Sync has no publisher until a Manager there publishes"

    def _body(self):
        return {"actions": [{"action_id": f"{actions.CONFIG_LEAVE_SYNC}_42", "value": "42"}], "trigger_id": "tr"}

    def _rows(self, *, last_publisher=False):
        mine = SimpleNamespace(
            id=10, workspace_id=1, channel_id="C1", status="active", publishes=True, is_private=False
        )
        other = SimpleNamespace(
            id=11,
            workspace_id=2,
            channel_id="C2",
            status="active",
            publishes=not last_publisher,
            is_private=False,
        )
        return [mine, other]

    def _patches(self, rows):
        workspace = SimpleNamespace(id=1, team_id="T1")
        return (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch(
                "handlers.channel_sync.viewer_context",
                return_value=Viewer(
                    team_id="T1",
                    user_id="U1",
                    workspace_id=1,
                    is_manager=True,
                    allow_private=True,
                    has_user_token=True,
                    private_channel_ids=frozenset({"C1"}),
                ),
            ),
            patch("handlers.channel_sync.DbManager.get_record", return_value=SimpleNamespace(id=42, group_id=5)),
            patch("handlers.channel_sync.DbManager.find_records", return_value=rows),
            patch("handlers.channel_sync.soft_delete_retention_days", return_value=30),
            patch("handlers.channel_sync._group_name", return_value="Shared"),
            patch("handlers.channel_sync._format_channel_ref", return_value="#general"),
        )

    def _view(self, *, last_publisher=False):
        from handlers.channel_sync import handle_leave_sync

        client = MagicMock()
        with contextlib.ExitStack() as stack:
            for patcher in self._patches(self._rows(last_publisher=last_publisher)):
                stack.enter_context(patcher)
            handle_leave_sync(self._body(), client, MagicMock(), context={})
        return client.views_update.call_args.kwargs["view"]

    @staticmethod
    def _texts(view: dict) -> list[str]:
        return [block.get("text", {}).get("text", "") for block in view.get("blocks", [])]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_LEAVE_SYNC_CONFIRM
        assert buttons[0]["text"]["text"] == ":wave: Leave Sync"
        assert json.loads(view["private_metadata"]) == {"sync_id": 42}

    def test_warns_what_leaving_does_without_ending_the_sync(self):
        texts = self._texts(self._view())
        warning = next(text for text in texts if text.startswith(":warning: *Leave "))
        assert "Remove this Channel from that Sync; SyncBot leaves unless another Sync still uses it" in warning
        assert "Keep its history for 30 days" in warning
        assert "Other Workspaces in that Sync continue" in warning
        assert warning.endswith("_No messages are deleted from any Channel._")
        assert "The notice lists who is still in the Sync" in warning
        assert self.NO_PUBLISHER_BULLET not in warning

    def test_last_publisher_leaves_with_the_same_notice(self):
        texts = self._texts(self._view(last_publisher=True))
        warning = next(text for text in texts if text.startswith(":warning: *Leave "))
        assert self.NO_PUBLISHER_BULLET not in warning
        assert "The notice lists who is still in the Sync" in warning
        assert "Other Workspaces in that Sync continue" in warning

    def test_private_denial_replaces_the_modal_with_its_sentence(self):
        from handlers.channel_sync import handle_leave_sync

        rows = self._rows()
        rows[0].is_private = True
        denial = ":lock: Only a workspace manager can leave it."
        client = MagicMock()
        with contextlib.ExitStack() as stack:
            for patcher in self._patches(rows):
                stack.enter_context(patcher)
            gate = stack.enter_context(patch("handlers.channel_sync.can_manage_sync", return_value=(False, denial)))
            close_modal = stack.enter_context(patch("handlers.channel_sync._close_modal_done"))
            body = self._body()
            handle_leave_sync(body, client, MagicMock(), context={})

        gate.assert_called_once()
        assert gate.call_args.kwargs["action"] == "leave it"
        close_modal.assert_called_once_with(client, body, denial)
        client.views_update.assert_not_called()


class TestPauseSyncConfirmModal:
    def test_has_a_non_red_button_and_no_submit_button(self):
        from handlers.channel_sync import handle_pause_sync

        client = MagicMock()
        body = {"actions": [{"action_id": f"{actions.CONFIG_PAUSE_SYNC}_42", "value": "42"}], "trigger_id": "tr"}
        handle_pause_sync(body, client, MagicMock(), context={})
        view = client.views_update.call_args.kwargs["view"]
        assert "submit" not in view
        assert _danger_buttons(view) == []
        buttons = [
            element
            for block in view.get("blocks", [])
            if block.get("type") == "actions"
            for element in block.get("elements", [])
            if element.get("type") == "button"
        ]
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_PAUSE_SYNC_CONFIRM


class TestLeaveGroupConfirmModal:
    def _view(self):
        from handlers import group_manage

        client = MagicMock()
        body = {
            "user": {"id": "U1", "team_id": "T1"},
            "team": {"id": "T1"},
            "actions": [{"action_id": f"{actions.CONFIG_LEAVE_GROUP}_5"}],
            "trigger_id": "tr",
        }
        group = SimpleNamespace(id=5, name="G")
        workspace = SimpleNamespace(id=2, team_id="T1", deleted_at=None)
        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.group_manage.helpers.can_workspace_leave", return_value=(True, "")),
            patch("handlers.group_manage.helpers.soft_delete_retention_days", return_value=14),
        ):
            group_manage.handle_leave_group(body, client, MagicMock(), context={})
        return client.views_update.call_args.kwargs["view"]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_LEAVE_GROUP_CONFIRM
        assert "14 days" in str(view)
        assert "user mappings now" in str(view).lower()


class TestSoleOwnerBlockedModal:
    """The sole-owner explanation only does one thing (dismiss), so it has one button."""

    def _view(self):
        from handlers import group_manage

        client = MagicMock()
        body = {
            "user": {"id": "U1", "team_id": "T1"},
            "team": {"id": "T1"},
            "actions": [{"action_id": f"{actions.CONFIG_LEAVE_GROUP}_5"}],
            "trigger_id": "tr",
        }
        group = SimpleNamespace(id=5, name="G")
        workspace = SimpleNamespace(id=2, team_id="T1", deleted_at=None)
        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.group_manage.helpers.can_workspace_leave", return_value=(False, "sole_owner")),
        ):
            group_manage.handle_leave_group(body, client, MagicMock(), context={})
        return client.views_update.call_args.kwargs["view"]

    def test_has_exactly_one_dismiss_button(self):
        view = self._view()
        assert "submit" not in view
        assert view["close"]["text"] == "Close"
        assert _danger_buttons(view) == []


class TestDisbandGroupConfirmModal:
    def _view(self):
        from handlers import group_manage

        client = MagicMock()
        body = {
            "user": {"id": "U1", "team_id": "T1"},
            "team": {"id": "T1"},
            "actions": [{"action_id": f"{actions.CONFIG_DISBAND_GROUP}_5"}],
            "trigger_id": "tr",
        }
        group = SimpleNamespace(id=5, name="G")
        workspace = SimpleNamespace(id=2, team_id="T1", deleted_at=None)
        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.can_disband", return_value=(True, "")),
            patch("handlers.group_manage.helpers.get_active_members", return_value=[]),
            patch("handlers.group_manage.helpers.soft_delete_retention_days", return_value=14),
        ):
            group_manage.handle_disband_group(body, client, MagicMock(), context={})
        return client.views_update.call_args.kwargs["view"]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_DISBAND_GROUP_CONFIRM
        assert not any(block.get("type") == "input" for block in view["blocks"])
        labels = [
            option.get("text", {}).get("text")
            for block in view["blocks"]
            if block.get("type") == "actions"
            for element in block.get("elements", [])
            if element.get("type") == "checkboxes"
            for option in element.get("options") or []
        ]
        assert "Also delete synced message history now" in labels
        text = str(view)
        assert "Disband `G`?" in text
        assert "`14` days" in text
        assert "every Workspace in this Group" in text
        assert "User mappings are deleted now" in text

    def test_the_checkbox_stores_the_choice_for_the_red_button(self):
        from handlers.group_manage import handle_disband_delete_history

        client = MagicMock()
        group = SimpleNamespace(id=5, name="G")
        body = {
            "view": {"id": "V1", "private_metadata": '{"group_id": 5}'},
            "actions": [{"selected_options": [{"value": "delete"}]}],
        }
        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", SimpleNamespace(id=2))),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.get_active_members", return_value=[]),
            patch("handlers.group_manage.helpers.soft_delete_retention_days", return_value=14),
        ):
            handle_disband_delete_history(body, client, MagicMock(), {})
        view = client.views_update.call_args.kwargs["view"]
        assert "submit" not in view
        assert json.loads(view["private_metadata"])["delete_history"] is True
        checked = [
            element.get("initial_options")
            for block in view["blocks"]
            if block.get("type") == "actions"
            for element in block.get("elements", [])
            if element.get("type") == "checkboxes"
        ]
        assert checked == [
            [
                {
                    "text": {"type": "plain_text", "text": "Also delete synced message history now", "emoji": True},
                    "value": "delete",
                }
            ]
        ]


class TestCancelPendingConnectionConfirmModal:
    def _view(self):
        from handlers.federation_cmds import handle_cancel_pending_external_connection

        client = MagicMock()
        pairing = SimpleNamespace(id=3, subject_team_id=None, label="Partner Org", created_at=None)
        body = {
            "user": {"id": "U_ADMIN"},
            "team": {"id": "T1"},
            "trigger_id": "tr",
            "actions": [{"action_id": f"{actions.CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION}_3", "value": "3"}],
        }
        with (
            patch("handlers.federation_cmds._require_primary_admin", return_value=SimpleNamespace(id=1)),
            patch("handlers.federation_cmds.DbManager.get_record", return_value=pairing),
        ):
            handle_cancel_pending_external_connection(body, client, MagicMock(), {})
        return client.views_update.call_args.kwargs["view"]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        assert view["close"]["text"] == "Keep"
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION_CONFIRM

    def test_updates_the_view_when_a_view_id_is_present(self):
        from handlers._common import _close_modal_done

        client = MagicMock()
        _close_modal_done(client, {"view": {"id": "V1"}}, "done")
        assert client.views_update.called
        assert client.views_update.call_args.kwargs["view_id"] == "V1"

    def test_is_a_no_op_without_a_view_id(self):
        from handlers._common import _close_modal_done

        client = MagicMock()
        _close_modal_done(client, {}, "done")
        assert not client.views_update.called
