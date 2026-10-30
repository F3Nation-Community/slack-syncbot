"""Tests for editing channel participation."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

import constants  # noqa: E402
from handlers.channel_sync import (  # noqa: E402
    _parse_edit_sync_ref,
    handle_edit_sync,
    handle_edit_sync_submit,
    handle_edit_sync_submit_ack,
)
from helpers.synced_by import Viewer  # noqa: E402
from slack import actions  # noqa: E402


def _viewer(workspace, user_id="U1") -> Viewer:
    return Viewer(
        team_id=workspace.team_id,
        user_id=user_id,
        workspace_id=workspace.id,
        is_manager=True,
        allow_private=True,
        has_user_token=True,
        private_channel_ids=frozenset({"C_LOCAL", "C_SUB"}),
    )


def _action_body(value: str, action_id: str) -> dict:
    return {
        "trigger_id": "trig",
        "actions": [{"value": value, "action_id": action_id}],
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }


def _submit_body(participation_action: str, participation: str, *, publish_flow: bool) -> dict:
    return {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": (
                f'{{"sync_id": 42, "sync_channel_id": 10, "publish_flow": {str(publish_flow).lower()}' + "}"
            ),
            "state": {
                "values": {
                    "participation": {
                        participation_action: {
                            "type": "radio_buttons",
                            "selected_option": {"value": participation},
                        }
                    }
                }
            },
        },
    }


class TestParseEditSyncRef:
    def test_channel_and_sync_encodings(self):
        assert _parse_edit_sync_ref(_action_body("c:12", f"{actions.CONFIG_EDIT_SYNC}_c_12")) == (
            "channel",
            12,
        )
        assert _parse_edit_sync_ref(_action_body("s:42", f"{actions.CONFIG_EDIT_SYNC}_s_42")) == (
            "sync",
            42,
        )


class TestHandleEditSync:
    WORKSPACE = SimpleNamespace(id=1, team_id="T1")
    SYNC = SimpleNamespace(
        id=42,
        title="general",
        group_id=5,
        sync_mode="direct",
    )

    def test_publisher_edits_participation_without_mode_picker(self):
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            channel_id="C_LOCAL",
            deleted_at=None,
            publishes=True,
            subscribes=False,
            reaction_style=None,
        )
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        def get_record(model, *args, **kwargs):
            from db import schemas

            if model is schemas.SyncChannel:
                return channel
            if model is schemas.WorkspaceGroup:
                return SimpleNamespace(id=5, name="Shared")
            return self.SYNC

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.viewer_context", return_value=_viewer(self.WORKSPACE)),
            patch("handlers.channel_sync.DbManager.get_record", side_effect=get_record),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_edit_sync(
                _action_body("c:10", f"{actions.CONFIG_EDIT_SYNC}_c_10"),
                MagicMock(),
                MagicMock(),
                {},
            )

        action_ids = [getattr(block, "action", None) for block in captured["blocks"]]
        texts = [getattr(getattr(block, "element", None), "initial_value", None) for block in captured["blocks"]]
        assert actions.CONFIG_SYNC_PARTICIPATION in action_ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in action_ids
        assert any(text and "<#C_LOCAL>" in text and "Shared" in text for text in texts)
        assert captured["kwargs"]["title_text"] == "Edit Sync"
        assert self.SYNC.sync_mode == "direct"

    def test_subscriber_edits_participation_and_reaction_type(self):
        workspace = SimpleNamespace(id=2, team_id="T2")
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=2,
            channel_id="C_SUB",
            deleted_at=None,
            publishes=False,
            subscribes=True,
            reaction_style=constants.REACTION_STYLE_DIRECT_ONLY,
        )
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)

        def get_record(model, *args, **kwargs):
            from db import schemas

            if model is schemas.SyncChannel:
                return channel
            return self.SYNC

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U2", workspace)),
            patch("handlers.channel_sync.viewer_context", return_value=_viewer(workspace, "U2")),
            patch("handlers.channel_sync.DbManager.get_record", side_effect=get_record),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_edit_sync(
                _action_body("c:10", f"{actions.CONFIG_EDIT_SYNC}_c_10"),
                MagicMock(),
                MagicMock(),
                {},
            )

        action_ids = [getattr(block, "action", None) for block in captured["blocks"]]
        assert actions.CONFIG_SYNC_PARTICIPATION in action_ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in action_ids
        participation = next(
            block for block in captured["blocks"] if getattr(block, "action", None) == actions.CONFIG_SYNC_PARTICIPATION
        )
        assert [opt.value for opt in participation.element.options] == [
            "publish_and_subscribe",
            "publish_only",
            "subscribe_only",
        ]
        style = next(
            block
            for block in captured["blocks"]
            if getattr(block, "action", None) == actions.CONFIG_SYNC_REACTION_STYLE
        )
        assert [opt.value for opt in style.element.options] == [
            constants.REACTION_STYLE_THREADED_AND_DIRECT,
            constants.REACTION_STYLE_DIRECT_ONLY,
            constants.REACTION_STYLE_OFF,
        ]

    def test_sync_only_edit_reference_no_longer_opens_policy_picker(self):
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.orm.BlockView.post_modal") as post_modal,
        ):
            handle_edit_sync(
                _action_body("s:42", f"{actions.CONFIG_EDIT_SYNC}_s_42"),
                MagicMock(),
                MagicMock(),
                {},
            )
        post_modal.assert_not_called()

    def test_ack_has_no_mode_policy_validation(self):
        with patch(
            "handlers.channel_sync._get_authorized_workspace",
            return_value=("U1", self.WORKSPACE),
        ):
            assert handle_edit_sync_submit_ack({}, MagicMock(), {}) is None

    @staticmethod
    def _channel(*, is_private=None):
        return SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            channel_id="C1",
            deleted_at=None,
            status="active",
            publishes=True,
            subscribes=False,
            reaction_style=None,
            is_private=is_private,
            synced_by_user_id=None,
        )

    def _submit(self, channel, participation: str, *, denial=None):
        """Run the submit work phase against *channel* and return the mocks it may touch."""
        body = _submit_body(actions.CONFIG_SYNC_PARTICIPATION, participation, publish_flow=True)
        client = MagicMock()
        logger = MagicMock()
        viewer = _viewer(self.WORKSPACE)

        def get_record(model, *args, **kwargs):
            from db import schemas

            if model is schemas.SyncChannel:
                return channel
            return self.SYNC

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.DbManager.get_record", side_effect=get_record),
            patch("handlers.channel_sync.DbManager.update_records") as update_sync,
            patch("handlers.channel_sync.viewer_context", return_value=viewer),
            patch(
                "handlers.channel_sync.can_manage_sync",
                return_value=(False, denial) if denial else (True, ""),
            ) as gate,
            patch("helpers.reaction.update_sync_channel_reactions") as update_channel,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh_home,
            patch("handlers.channel_sync.invalidate_group_member_homes") as invalidate_homes,
            patch("handlers.channel_sync.helpers.notify_user_dm") as notify_user,
            patch("helpers.sync_lifecycle.notify_channel_or_managers") as notify_channels,
            patch(
                "handlers.channel_sync.helpers.format_admin_label",
                return_value="`Ada Lovelace (Workspace A)`",
            ),
            patch("handlers.channel_sync.deliver_action_notice") as announce,
            patch("helpers.workspace.resolve_workspace_name", return_value="Workspace A"),
        ):
            handle_edit_sync_submit(body, client, logger, {})

        return SimpleNamespace(
            client=client,
            logger=logger,
            viewer=viewer,
            gate=gate,
            update_sync=update_sync,
            update_channel=update_channel,
            refresh_home=refresh_home,
            invalidate_homes=invalidate_homes,
            notify_user=notify_user,
            notify_channels=notify_channels,
            announce=announce,
        )

    def test_submit_updates_participation_and_keeps_direct_sync_mode(self):
        channel = self._channel()
        run = self._submit(channel, "publish_and_subscribe")

        run.update_sync.assert_not_called()
        run.update_channel.assert_called_once_with(
            10,
            style=constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE,
            publishes=True,
            subscribes=True,
        )
        assert self.SYNC.sync_mode == "direct"

    def test_submit_replicates_status_and_invalidates_group_homes(self):
        channel = self._channel()
        run = self._submit(channel, "publish_and_subscribe")

        run.gate.assert_called_once_with(channel, run.viewer, action="edit it")
        run.announce.assert_called_once()
        assert run.announce.call_args.args[0] is self.WORKSPACE
        assert run.announce.call_args.args[1] is channel
        assert run.announce.call_args.kwargs["action"] == "Edit"
        assert run.announce.call_args.kwargs["sync"] is self.SYNC
        run.refresh_home.assert_called_once_with(self.WORKSPACE, run.logger, context={}, user_id="U1")
        run.invalidate_homes.assert_called_once_with(5, exclude_workspace_id=1)
        run.notify_user.assert_not_called()

    def test_submit_to_subscribe_only_tells_twins_the_sync_has_no_publisher(self):
        channel = self._channel()
        run = self._submit(channel, "subscribe_only")

        run.update_channel.assert_called_once_with(
            10,
            style=constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE,
            publishes=False,
            subscribes=True,
        )
        assert run.announce.call_args.kwargs["action"] == "Edit"

    def test_submit_denied_private_row_dms_the_person_and_writes_nothing(self):
        channel = self._channel(is_private=True)
        denial = ":lock: Only a workspace manager can edit it."
        run = self._submit(channel, "publish_and_subscribe", denial=denial)

        run.gate.assert_called_once_with(channel, run.viewer, action="edit it")
        run.notify_user.assert_called_once_with(run.client, "U1", denial)
        run.update_channel.assert_not_called()
        run.update_sync.assert_not_called()
        run.notify_channels.assert_not_called()
        run.announce.assert_not_called()
        run.refresh_home.assert_not_called()
        run.invalidate_homes.assert_not_called()
        assert channel.publishes is True
        assert channel.subscribes is False
