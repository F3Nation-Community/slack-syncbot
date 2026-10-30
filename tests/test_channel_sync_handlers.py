"""Focused unit tests for channel sync handler branches."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.channel_sync import (  # noqa: E402
    handle_create_sync_submit_ack,
    handle_join_sync_submit,
    handle_leave_sync_confirm,
)
from helpers.synced_by import Viewer  # noqa: E402
from slack import actions  # noqa: E402


class TestLeaveSyncConfirm:
    """Leave soft-deletes this workspace's row; the last publisher leaves like anyone else."""

    SYNC_ID = 31
    LEFT = ":wave: You left the Sync in this Channel. You can close this now."

    def _channel(self, cid, ws, *, publishes=True):
        return SimpleNamespace(
            id=cid,
            sync_id=self.SYNC_ID,
            workspace_id=ws,
            channel_id=f"C_{cid}",
            status="active",
            publishes=publishes,
            subscribes=True,
            is_private=False,
            deleted_at=None,
        )

    def _run(self, *, my_channel, removed=1, denial=None):
        """Run the confirm with the acting workspace's live row (``None`` for no row) and return the mocks."""
        workspace = SimpleNamespace(id=my_channel.workspace_id if my_channel else 10, team_id="T1", deleted_at=None)
        client = MagicMock()
        logger = MagicMock()
        context = {"bot_user_id": "UBOT"}
        body = {"view": {"id": "V1"}}
        viewer = Viewer(
            team_id="T1",
            user_id="U1",
            workspace_id=workspace.id,
            is_manager=True,
            allow_private=True,
            has_user_token=True,
            private_channel_ids=frozenset({getattr(my_channel, "channel_id", "C_10")}),
        )

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": self.SYNC_ID}),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[my_channel] if my_channel else []),
            patch(
                "handlers.channel_sync.DbManager.get_record",
                return_value=SimpleNamespace(id=self.SYNC_ID, title="general", group_id=None),
            ),
            patch("handlers.channel_sync.viewer_context", return_value=viewer),
            patch(
                "handlers.channel_sync.can_manage_sync",
                return_value=(False, denial) if denial else (True, ""),
            ) as gate,
            patch(
                "handlers.channel_sync.helpers.format_admin_label",
                return_value="`Ada Lovelace (Workspace A)`",
            ),
            patch("handlers.channel_sync.soft_delete_sync_channels", return_value=removed) as soft_delete,
            patch("handlers.channel_sync.helpers.purge_sync") as purge_sync,
            patch("federation.replicate.replicate_sync_ended") as ended,
            patch("handlers.channel_sync.log_error") as error_log,
            patch("handlers.channel_sync.log_warning") as warning_log,
            patch("handlers.channel_sync.helpers.format_error_dm", return_value="error-dm") as format_dm,
            patch("handlers.channel_sync.helpers.notify_admins_dm") as notify,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh,
            patch("handlers.channel_sync._close_modal_done") as close_modal,
        ):
            handle_leave_sync_confirm(body, client, logger, context=context)

        return SimpleNamespace(
            workspace=workspace,
            client=client,
            logger=logger,
            context=context,
            body=body,
            viewer=viewer,
            gate=gate,
            soft_delete=soft_delete,
            purge_sync=purge_sync,
            ended=ended,
            error_log=error_log,
            warning_log=warning_log,
            format_dm=format_dm,
            notify=notify,
            refresh=refresh,
            close_modal=close_modal,
        )

    def test_leave_soft_deletes_this_workspaces_row(self):
        mine = self._channel(10, 2, publishes=False)
        run = self._run(my_channel=mine)

        run.soft_delete.assert_called_once_with(
            run.workspace,
            "C_10",
            "left_sync",
            sync_ids={self.SYNC_ID},
            person="`Ada Lovelace (Workspace A)`",
        )
        run.error_log.assert_not_called()

    def test_last_publisher_leaves_exactly_like_anyone_else(self):
        mine = self._channel(10, 10, publishes=True)
        run = self._run(my_channel=mine)

        run.soft_delete.assert_called_once_with(
            run.workspace,
            "C_10",
            "left_sync",
            sync_ids={self.SYNC_ID},
            person="`Ada Lovelace (Workspace A)`",
        )
        run.purge_sync.assert_not_called()
        run.ended.assert_not_called()
        run.close_modal.assert_called_once_with(run.client, run.body, self.LEFT)

    def test_nothing_removed_logs_leave_sync_failed_and_dms_admins(self):
        mine = self._channel(10, 10, publishes=True)
        run = self._run(my_channel=mine, removed=0)

        run.error_log.assert_called_once()
        assert run.error_log.call_args.args[0] == "leave_sync_failed"
        assert run.error_log.call_args.kwargs["sync_id"] == self.SYNC_ID
        assert run.error_log.call_args.kwargs["channel_id"] == "C_10"
        assert run.format_dm.call_args.args[1] == {"event": "leave_sync_failed", "sync": self.SYNC_ID}
        run.notify.assert_called_once_with(run.client, "error-dm", team_id="T1")
        run.refresh.assert_not_called()
        run.close_modal.assert_not_called()

    def test_success_refreshes_the_acting_users_home_and_closes_the_modal(self):
        mine = self._channel(10, 10, publishes=True)
        run = self._run(my_channel=mine)

        run.refresh.assert_called_once_with(run.workspace, run.logger, context=run.context, user_id="U1")
        run.close_modal.assert_called_once_with(run.client, run.body, self.LEFT)
        run.notify.assert_not_called()

    def test_private_denial_closes_the_modal_with_its_sentence(self):
        mine = self._channel(10, 10, publishes=True)
        mine.is_private = True
        denial = ":lock: Only a workspace manager can leave it."
        run = self._run(my_channel=mine, denial=denial)

        run.gate.assert_called_once_with(mine, run.viewer, action="leave it")
        run.close_modal.assert_called_once_with(run.client, run.body, denial)
        run.soft_delete.assert_not_called()
        run.refresh.assert_not_called()

    def test_no_live_row_for_this_workspace_only_warns(self):
        run = self._run(my_channel=None)

        run.warning_log.assert_called_once_with("leave_sync_confirm", reason="no channel", sync_id=self.SYNC_ID)
        run.gate.assert_not_called()
        run.soft_delete.assert_not_called()
        run.close_modal.assert_not_called()


class TestCreateSyncSubmitAck:
    def test_missing_group_id_exits_early(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_create_sync_submit_ack({}, client, context)

        assert result is None
        create_record.assert_not_called()

    def test_missing_channel_selection_returns_ack_error(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="__none__"),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_create_sync_submit_ack({}, client, context)

        assert result is not None
        assert result["response_action"] == "errors"
        assert "Select a Channel." in result["errors"].values()
        create_record.assert_not_called()

    def test_existing_sync_channel_can_publish_again(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C123"),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
            patch("handlers.channel_sync.helpers.has_user_token", return_value=True),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_create_sync_submit_ack({}, client, context)

        assert result is None
        create_record.assert_not_called()


class TestJoinSyncSubmit:
    def test_missing_sync_id_exits_early(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            handle_join_sync_submit({}, client, logger, context)

        create_record.assert_not_called()

    def test_missing_channel_selection_exits_early(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="__none__"),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            handle_join_sync_submit({}, client, logger, context)

        create_record.assert_not_called()

    def test_duplicate_source_validation_stops_work(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cdup"),
            patch(
                "handlers.channel_sync._validate_channel_selection",
                return_value=({"response_action": "errors"}, None),
            ),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh_home,
        ):
            handle_join_sync_submit({"user": {"id": "U1"}}, client, logger, context)

        create_record.assert_not_called()
        client.conversations_join.assert_not_called()
        refresh_home.assert_not_called()


class TestJoinSyncModal:
    def test_opens_one_screen_with_source_group_picker_and_reaction_type(self):
        from handlers.channel_sync import handle_join_sync

        workspace = SimpleNamespace(id=10, team_id="T1")
        pub = SimpleNamespace(id=1, channel_id="C_PUB", workspace_id=99, publishes=True)
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        body = {
            "trigger_id": "tr",
            "actions": [{"value": "55", "action_id": "join_sync_55"}],
        }
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch(
                "handlers.channel_sync.viewer_context",
                return_value=Viewer(
                    team_id="T1",
                    user_id="U1",
                    workspace_id=10,
                    is_manager=True,
                    allow_private=True,
                    has_user_token=True,
                    private_channel_ids=frozenset({"C_MINE"}),
                ),
            ),
            patch("handlers.channel_sync.DbManager.get_record", return_value=SimpleNamespace(id=55, group_id=5)),
            patch("handlers.channel_sync._publishing_sync_channels", return_value=[pub]),
            patch("handlers.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(id=99)),
            patch("handlers.channel_sync._group_name", return_value="Shared"),
            patch("handlers.channel_sync._format_channel_ref", return_value="#source (Other)"),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_join_sync(body, MagicMock(), MagicMock(), {})

        ids = [getattr(block, "action", None) for block in captured["blocks"]]
        texts = [getattr(getattr(block, "element", None), "initial_value", None) for block in captured["blocks"]]
        participation = next(
            block for block in captured["blocks"] if getattr(block, "action", None) == actions.CONFIG_SYNC_PARTICIPATION
        )
        option_values = [opt.value for opt in participation.element.options]
        assert option_values == ["publish_and_subscribe", "publish_only", "subscribe_only"]
        assert actions.CONFIG_JOIN_SYNC_SELECT in ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in ids
        assert captured["kwargs"]["callback_id"] == actions.CONFIG_JOIN_SYNC_SUBMIT
        assert captured["kwargs"]["submit_button_text"] == "Join Sync"
        assert captured["kwargs"]["title_text"] == "Join Sync"
        assert any(text and "Shared" in text and "#source (Other)" in text for text in texts)

    def test_private_publish_without_a_private_channel_is_close_only(self):
        from handlers.channel_sync import handle_join_sync

        workspace = SimpleNamespace(id=10, team_id="T1")
        pub = SimpleNamespace(id=1, channel_id="C_PUB", workspace_id=99, publishes=True, is_private=True)
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        body = {"trigger_id": "tr", "actions": [{"value": "55", "action_id": "join_sync_55"}]}
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch(
                "handlers.channel_sync.viewer_context",
                return_value=Viewer(
                    team_id="T1",
                    user_id="U1",
                    workspace_id=10,
                    is_manager=True,
                    allow_private=True,
                    has_user_token=True,
                    private_channel_ids=frozenset(),
                ),
            ),
            patch("handlers.channel_sync._publishing_sync_channels", return_value=[pub]),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_join_sync(body, MagicMock(), MagicMock(), {})

        ids = [getattr(block, "action", None) for block in captured["blocks"]]
        assert actions.CONFIG_JOIN_SYNC_SELECT not in ids
        assert captured["kwargs"]["submit_button_text"] is None
        assert captured["kwargs"]["close_button_text"] == "Close"
        text = " ".join(getattr(block, "label", "") or "" for block in captured["blocks"])
        assert "not a member of a private Channel" in text


class TestChannelSyncNotices:
    def test_action_notice_names_the_manager_the_action_and_the_group(self):
        from helpers.sync_participation import format_action_notice

        with patch(
            "helpers.sync_participation.format_workspaces_in_sync",
            return_value="`Workspace B [Publish and Subscribe]`",
        ):
            created = format_action_notice(
                action="Create",
                workspace_name="Workspace A",
                group_name="Partner Org",
                manager="Ada Lovelace",
                sync_id=5,
            )
            plain = format_action_notice(
                action="Create",
                workspace_name="Workspace A",
                manager="Ada Lovelace",
                sync_id=5,
            )
        assert created.startswith(":outbox_tray: *SyncBot Action Notice*")
        assert "Action: Create" in created
        assert "Workspace Group: Partner Org" in created
        assert "Acting Manager: Ada Lovelace (Workspace A)" in created
        assert "Workspace Group:" not in plain
