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
    handle_publish_channel_submit_ack,
    handle_publish_mode_submit_ack,
    handle_stop_sync_confirm,
    handle_subscribe_channel_submit,
    handle_unpublish_channel,
)


class TestUnpublishChannel:
    """Unpublish must purge children before the sync, and must not fail silently."""

    SYNC_ID = 31

    def _run(self, *, purge_side_effect=None):
        workspace = SimpleNamespace(id=10, team_id="T1", bot_token=None, deleted_at=None)
        sync = SimpleNamespace(id=self.SYNC_ID, publisher_workspace_id=workspace.id, group_id=None)
        body = {"actions": [{"value": str(self.SYNC_ID), "action_id": "unpublish_channel"}]}

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Admin", "Admin (WS)")),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.purge_sync", side_effect=purge_side_effect) as purge,
            patch("handlers.channel_sync.helpers.notify_admins_dm") as notify,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh,
            patch("handlers.channel_sync._logger.error") as error_log,
        ):
            handle_unpublish_channel(body, MagicMock(), MagicMock(), context={})

        return purge, notify, refresh, error_log

    def test_purges_the_sync_through_the_ordered_helper(self):
        purge, _, refresh, error_log = self._run()

        purge.assert_called_once_with(self.SYNC_ID)
        assert refresh.called
        assert not error_log.called

    def test_purge_failure_is_logged_and_reported_to_the_admin(self):
        """A dead-looking button was the original symptom; failures must be visible."""
        purge, notify, refresh, error_log = self._run(purge_side_effect=RuntimeError("fk violation"))

        assert purge.called
        assert notify.called
        assert not refresh.called

        assert error_log.call_args.args[0] == "unpublish_failed"
        assert error_log.call_args.kwargs["extra"]["sync_id"] == self.SYNC_ID

    def test_non_publisher_cannot_unpublish(self):
        workspace = SimpleNamespace(id=10, team_id="T1")
        sync = SimpleNamespace(id=self.SYNC_ID, publisher_workspace_id=999, group_id=None)
        body = {"actions": [{"value": str(self.SYNC_ID), "action_id": "unpublish_channel"}]}

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Admin", "Admin (WS)")),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.helpers.purge_sync") as purge,
        ):
            handle_unpublish_channel(body, MagicMock(), MagicMock(), context={})

        assert not purge.called


class TestStopSyncConfirm:
    """Stop-sync is a subscriber action; the publisher must never strand a sync."""

    SYNC_ID = 42
    PUBLISHER_WS = 1
    SUBSCRIBER_WS = 2

    def _channel(self, cid, ws):
        return SimpleNamespace(id=cid, workspace_id=ws, channel_id=f"C_{cid}", status="active")

    def _run(self, *, acting_ws, publisher_ws, all_channels):
        workspace = SimpleNamespace(id=acting_ws, team_id="T1")
        sync = SimpleNamespace(id=self.SYNC_ID, publisher_workspace_id=publisher_ws, group_id=None)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": self.SYNC_ID}),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.DbManager.find_records", return_value=all_channels),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Admin", "Admin (WS)")),
            patch("handlers.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(bot_token=None)),
            patch("handlers.channel_sync.helpers.purge_sync_channels") as purge_channels,
            patch("handlers.channel_sync.helpers.purge_sync") as purge_sync,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
        ):
            handle_stop_sync_confirm({}, MagicMock(), MagicMock(), context={})

        return purge_channels, purge_sync

    def test_publisher_cannot_stop_sync(self):
        """The publisher's teardown is Unpublish; stop-sync must be a no-op for them."""
        mine = self._channel(10, self.PUBLISHER_WS)
        other = self._channel(11, self.SUBSCRIBER_WS)
        purge_channels, purge_sync = self._run(
            acting_ws=self.PUBLISHER_WS, publisher_ws=self.PUBLISHER_WS, all_channels=[mine, other]
        )

        assert not purge_channels.called
        assert not purge_sync.called

    def test_subscriber_stop_keeps_the_sync_when_others_remain(self):
        mine = self._channel(10, self.SUBSCRIBER_WS)
        other = self._channel(11, self.PUBLISHER_WS)
        purge_channels, purge_sync = self._run(
            acting_ws=self.SUBSCRIBER_WS, publisher_ws=self.PUBLISHER_WS, all_channels=[mine, other]
        )

        assert purge_channels.call_args.args[0] == [mine]
        assert not purge_sync.called

    def test_last_member_stop_purges_the_empty_sync(self):
        """A member stranded after the publisher left clears the orphan by stopping."""
        mine = self._channel(10, self.SUBSCRIBER_WS)
        purge_channels, purge_sync = self._run(
            acting_ws=self.SUBSCRIBER_WS, publisher_ws=self.PUBLISHER_WS, all_channels=[mine]
        )

        assert purge_channels.call_args.args[0] == [mine]
        purge_sync.assert_called_once_with(self.SYNC_ID)


class TestPublishModeSubmitAck:
    def test_missing_group_id_logs_warning(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)
        body = {"view": {"team_id": "T1", "private_metadata": "{}"}}

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync._logger.warning") as warn_log,
        ):
            result = handle_publish_mode_submit_ack(body, client, context)

        assert result is None
        assert warn_log.call_args is not None
        assert "publish_mode_submit: missing group_id in metadata" in warn_log.call_args.args[0]


class TestPublishChannelSubmitAck:
    def test_missing_group_id_exits_early(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_publish_channel_submit_ack({}, client, context)

        assert result is None
        create_record.assert_not_called()

    def test_missing_channel_selection_returns_ack_error(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="__none__"),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_publish_channel_submit_ack({}, client, context)

        assert result is not None
        assert result["response_action"] == "errors"
        assert "Select a Channel to publish." in result["errors"].values()
        create_record.assert_not_called()

    def test_existing_sync_channel_returns_ack_error(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C123"),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[object()]),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_publish_channel_submit_ack({}, client, context)

        assert result is not None
        assert result["response_action"] == "errors"
        assert "already being synced" in next(iter(result["errors"].values()))
        create_record.assert_not_called()


class TestSubscribeChannelSubmit:
    def test_missing_sync_id_exits_early(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            handle_subscribe_channel_submit({}, client, logger, context)

        create_record.assert_not_called()

    def test_missing_channel_selection_exits_early(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="__none__"),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            handle_subscribe_channel_submit({}, client, logger, context)

        create_record.assert_not_called()

    def test_duplicate_channel_skips_join_and_create(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10)
        sync_record = SimpleNamespace(group_id=None)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cdup"),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync_record),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[object()]),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh_home,
        ):
            handle_subscribe_channel_submit({"user": {"id": "U1"}}, client, logger, context)

        create_record.assert_not_called()
        client.conversations_join.assert_not_called()
        refresh_home.assert_called_once()
