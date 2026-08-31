"""Tests for the Authorize SyncBot section on the Home tab.

Slack will not let an app add itself to a private channel, so SyncBot needs a
user token from whoever is publishing. This section is how a person hands that
over, which is why it is shown to everyone rather than to admins only, and why
the Home tab content hash has to be per user: a Refresh straight after
authorizing must not replay cached blocks that still show the button.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from builders.home import (
    _build_authorize_section,
    _home_tab_content_hash,
    build_home_tab,
    home_tab_hash_key,
)
from slack import actions, orm

WORKSPACE = SimpleNamespace(id=10, team_id="T1", workspace_name="WS", bot_token=None, deleted_at=None)
AUTHORIZE_URL = "https://syncbot.example.com/slack/install"


def _rendered(blocks: list) -> list[dict]:
    return orm.BlockView(blocks=blocks).as_form_field()


def _text_of(rendered: list[dict]) -> str:
    return repr(rendered)


class TestAuthorizeSection:
    def test_hidden_when_this_user_already_authorized(self):
        blocks: list = []
        with patch("builders.home.helpers.has_user_token", return_value=True):
            shown = _build_authorize_section(blocks, "T1", "U1")

        assert shown is False
        assert blocks == []

    def test_shown_with_button_when_the_user_has_no_token(self):
        blocks: list = []
        with (
            patch("builders.home.helpers.has_user_token", return_value=False),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
        ):
            shown = _build_authorize_section(blocks, "T1", "U1")

        rendered = _rendered(blocks)
        assert shown is True
        assert rendered[0]["text"]["text"] == "Authorize SyncBot"
        assert "add SyncBot to private Channels" in rendered[1]["elements"][0]["text"]
        button = rendered[2]["elements"][0]
        assert button["url"] == AUTHORIZE_URL
        assert button["action_id"] == actions.CONFIG_AUTHORIZE_SYNCBOT

    def test_copy_does_not_promise_reactions_yet(self):
        """User-token reactions are a later release; the copy must not imply them."""
        blocks: list = []
        with (
            patch("builders.home.helpers.has_user_token", return_value=False),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
        ):
            _build_authorize_section(blocks, "T1", "U1")

        assert "react" not in _text_of(_rendered(blocks))

    def test_hidden_when_there_is_no_oauth_flow_to_link_to(self):
        """Local single-workspace mode has no install URL, so a button would be a dead end."""
        blocks: list = []
        with (
            patch("builders.home.helpers.has_user_token", return_value=False),
            patch("builders.home.helpers.authorize_url", return_value=None),
        ):
            shown = _build_authorize_section(blocks, "T1", "U1")

        assert shown is False
        assert blocks == []


class TestHomeTabAdminGate:
    BODY = {"team": {"id": "T1"}, "user": {"id": "U1"}}

    def _build(self, *, is_admin: bool, has_token: bool) -> list[dict]:
        client = MagicMock()
        with (
            patch("builders.home.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("builders.home.helpers.is_user_authorized", return_value=is_admin),
            patch("builders.home.helpers.has_user_token", return_value=has_token),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
            patch("builders.home._get_groups_for_workspace", return_value=[]),
            patch("builders.home.DbManager.find_records", return_value=[]),
            patch("builders.home.helpers.is_settings_visible_for_workspace", return_value=False),
            patch("builders.home.helpers.is_backup_visible_for_workspace", return_value=False),
            patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=False),
        ):
            return build_home_tab(self.BODY, client, MagicMock(), {}, user_id="U1", return_blocks=True)

    def test_non_admin_can_still_authorize(self):
        """REQUIRE_ADMIN restricts configuration, not the whole tab."""
        rendered = self._build(is_admin=False, has_token=False)
        text = _text_of(rendered)

        assert "Authorize SyncBot" in text
        assert "Only Workspace Admins can configure SyncBot" in text
        assert "Create Group" not in text
        assert "Publish Channel" not in text

    def test_non_admin_with_a_token_sees_only_the_lock_line(self):
        rendered = self._build(is_admin=False, has_token=True)
        text = _text_of(rendered)

        assert "Authorize SyncBot" not in text
        assert "Only Workspace Admins can configure SyncBot" in text

    def test_admin_without_a_token_gets_both_authorize_and_configuration(self):
        rendered = self._build(is_admin=True, has_token=False)
        text = _text_of(rendered)

        assert "Authorize SyncBot" in text
        assert "Create Group" in text
        assert "Only Workspace Admins can configure SyncBot" not in text

    def test_admin_with_a_token_sees_no_authorize_section(self):
        rendered = self._build(is_admin=True, has_token=True)

        assert "Authorize SyncBot" not in _text_of(rendered)


class TestContentHashIsPerUser:
    @pytest.fixture(autouse=True)
    def _empty_workspace(self):
        with (
            patch("builders.home._get_groups_for_workspace", return_value=[]),
            patch("builders.home.DbManager.find_records", return_value=[]),
            patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=False),
        ):
            yield

    def test_two_users_differ_when_only_one_has_authorized(self):
        def has_token(_team_id, user_id):
            return user_id == "U_AUTHORIZED"

        with patch("builders.home.helpers.has_user_token", side_effect=has_token):
            authorized = _home_tab_content_hash(WORKSPACE, "U_AUTHORIZED")
            not_authorized = _home_tab_content_hash(WORKSPACE, "U_OTHER")

        assert authorized != not_authorized

    def test_hash_changes_for_a_user_once_a_token_appears(self):
        with patch("builders.home.helpers.has_user_token", return_value=False):
            before = _home_tab_content_hash(WORKSPACE, "U1")
        with patch("builders.home.helpers.has_user_token", return_value=True):
            after = _home_tab_content_hash(WORKSPACE, "U1")

        assert before != after

    def test_hash_key_is_scoped_to_the_user_under_the_team_prefix(self):
        """Restore-time invalidation deletes by the ``home_tab_hash:{team_id}`` prefix."""
        key = home_tab_hash_key("T1", "U1")

        assert key.startswith("home_tab_hash:T1")
        assert key.endswith(":U1")


class TestRefreshUsesThePerUserKey:
    def test_refresh_home_reads_and_writes_the_per_user_hash(self):
        from handlers.sync import handle_refresh_home

        client = MagicMock()
        body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

        with (
            patch("handlers.sync.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("handlers.sync.builders._home_tab_content_hash", return_value="hash"),
            patch("handlers.sync.helpers.refresh_cooldown_check", return_value=("cached", [], None)) as check,
            patch("handlers.sync.helpers._cache_set"),
        ):
            handle_refresh_home(body, client, MagicMock(), {})

        assert check.call_args.args[1] == "home_tab_hash:T1:U1"
