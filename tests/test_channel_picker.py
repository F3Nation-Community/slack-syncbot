"""Tests for the native channel picker and its submit-time validation.

The picker used to be a static list built by enumerating ``conversations.list``,
which silently capped at ~100 options and made larger channels unreachable. It is
now Slack's ``conversations_select``, which cannot filter by app-side state, so
eligibility is checked when the modal is submitted instead.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from handlers.channel_sync import (
    _channel_picker_block,
    _channel_picker_help_text,
    _validate_channel_selection,
)
from slack import actions


@pytest.fixture
def client():
    return MagicMock()


class TestPickerBlock:
    def test_uses_native_conversations_select(self):
        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            block = _channel_picker_block("Channel to Publish", actions.CONFIG_PUBLISH_CHANNEL_SELECT)

        rendered = block.as_form_field()
        assert rendered["element"]["type"] == "conversations_select"
        assert rendered["element"]["filter"]["include"] == ["public"]

    def test_private_channels_included_when_policy_allows(self):
        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True):
            block = _channel_picker_block("Channel to Publish", actions.CONFIG_PUBLISH_CHANNEL_SELECT)

        include = block.as_form_field()["element"]["filter"]["include"]
        assert "private" in include

    def test_help_text_warns_when_private_channels_allowed(self):
        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True):
            assert "Private Channels are currently allowed" in _channel_picker_help_text()

        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            assert "Only public Channels can be synced" in _channel_picker_help_text()

        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            assert "receive the published Channel" in _channel_picker_help_text(subscribe=True)


class TestValidateChannelSelection:
    ACTION = actions.CONFIG_PUBLISH_CHANNEL_SELECT

    def test_missing_selection_is_an_error(self, client):
        result = _validate_channel_selection(client, None, self.ACTION)
        assert result["response_action"] == "errors"
        assert self.ACTION in result["errors"]

    def test_placeholder_selection_is_an_error(self, client):
        result = _validate_channel_selection(client, "__none__", self.ACTION)
        assert result["response_action"] == "errors"

    def test_channel_already_synced_is_rejected(self, client):
        with patch("handlers.channel_sync.DbManager.find_records", return_value=[object()]):
            result = _validate_channel_selection(client, "C1", self.ACTION)

        assert "already part of a Channel Sync" in result["errors"][self.ACTION]

    def test_already_synced_check_is_global_not_workspace_scoped(self, client):
        """A channel may belong to only one sync instance-wide.

        ``get_sync_list`` resolves a channel to the first matching sync, so a
        channel in two syncs has undefined send fan-out.
        """
        captured = {}

        def fake_find_records(schema, filters):
            captured["filters"] = filters
            return []

        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", side_effect=fake_find_records),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

        assert not any("workspace_id" in str(f) for f in captured["filters"])

    def test_eligible_public_channel_passes(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

    def test_private_channel_rejected_by_default(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION)

        assert "Private Channels cannot be synced" in result["errors"][self.ACTION]

    def test_private_channel_allowed_when_setting_is_on_and_bot_is_a_member(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": True}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

    def test_private_channel_rejected_when_bot_is_not_a_member(self, client):
        """The native picker shows channels the user can see; the bot may not be in them."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION)

        assert "Invite it first" in result["errors"][self.ACTION]

    def test_public_channel_passes_even_if_bot_is_not_yet_a_member(self, client):
        """conversations.join can add the bot to a public Channel in the work phase."""
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

    def test_unreadable_channel_fails_closed(self, client):
        """A channel SyncBot cannot inspect is one it cannot join either."""
        client.conversations_info.side_effect = Exception("channel_not_found")
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION)

        assert "could not read that Channel" in result["errors"][self.ACTION]

    def test_soft_deleted_sync_channel_does_not_block_reuse(self, client):
        """Republishing a previously unpublished channel must remain possible."""
        captured = {}

        def fake_find_records(schema, filters):
            captured["filters"] = filters
            return []

        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", side_effect=fake_find_records),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

        assert any("deleted_at IS NULL" in str(f) for f in captured["filters"])


class TestSubscribeAckSurfacesErrors:
    def test_ineligible_channel_returns_errors_response(self):
        from handlers.channel_sync import handle_subscribe_channel_submit_ack

        client = MagicMock()
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cdup"),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[object()]),
        ):
            result = handle_subscribe_channel_submit_ack({}, client, {})

        assert result["response_action"] == "errors"
        assert actions.CONFIG_SUBSCRIBE_CHANNEL_SELECT in result["errors"]

    def test_missing_sync_id_does_not_claim_success(self):
        from handlers.channel_sync import handle_subscribe_channel_submit_ack

        client = MagicMock()
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
        ):
            assert handle_subscribe_channel_submit_ack({}, client, {}) is None

    def test_eligible_channel_acks_empty(self):
        from handlers.channel_sync import handle_subscribe_channel_submit_ack

        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        workspace = SimpleNamespace(id=10)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cnew"),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert handle_subscribe_channel_submit_ack({}, client, {}) is None


class TestSubscribeIsRoutedForDeferredAck:
    def test_subscribe_submit_has_an_ack_handler(self):
        """Without a VIEW_ACK_MAPPER entry the field errors never reach Slack."""
        import handlers
        import routing

        assert routing.VIEW_ACK_MAPPER[actions.CONFIG_SUBSCRIBE_CHANNEL_SUBMIT] is (
            handlers.handle_subscribe_channel_submit_ack
        )


class TestLegacyFormsRespectPolicy:
    """The legacy new/join sync modals share the picker, so they share the policy."""

    def test_deep_copied_form_can_be_switched_to_public_only(self):
        import copy

        from slack import forms

        form = copy.deepcopy(forms.NEW_SYNC_FORM)
        form.set_conversations_include_private(False)
        rendered = form.as_form_field()

        pickers = [b["element"] for b in rendered if b.get("element", {}).get("type") == "conversations_select"]
        assert pickers
        assert all(p["filter"]["include"] == ["public"] for p in pickers)

    def test_deep_copied_form_can_include_private(self):
        import copy

        from slack import forms

        form = copy.deepcopy(forms.JOIN_SYNC_FORM)
        form.set_conversations_include_private(True)
        rendered = form.as_form_field()

        pickers = [b["element"] for b in rendered if b.get("element", {}).get("type") == "conversations_select"]
        assert pickers
        assert all("private" in p["filter"]["include"] for p in pickers)
