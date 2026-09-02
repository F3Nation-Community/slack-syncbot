"""Tests for per-channel reaction direction, pairing, and apply helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from slack_sdk.errors import SlackApiError

import constants
from helpers.reactions import (
    apply_reaction_to_target,
    channel_receives_reactions,
    channel_sends_reactions,
    default_reaction_style_for_new_channel,
    find_source_sync_channel,
    reaction_style,
    should_sync_reaction_between,
)
from slack import actions


def _sync_channel(direction: str, style: str | None = None, *, channel_id: str = "C_SRC"):
    return SimpleNamespace(
        reaction_direction=direction,
        reaction_style=style,
        channel_id=channel_id,
        id=1,
    )


def _apply(**kwargs):
    defaults = dict(
        action="add",
        reaction="thumbsup",
        source_user_id="U_SRC",
        source_workspace_id=1,
        source_sync_channel=_sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_SRC"),
        target_sync_channel=_sync_channel(
            constants.REACTION_DIRECTION_BOTH,
            constants.REACTION_STYLE_THREADED_AND_DIRECT,
            channel_id="C_DST",
        ),
        target_post_meta=SimpleNamespace(ts=100.0, post_id="post-parent"),
        target_workspace=SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc"),
        display_name="Alice",
        icon_url=None,
        posted_from="(A)",
        author_is_mapped=True,
    )
    defaults.update(kwargs)
    return apply_reaction_to_target(**defaults)


class TestPairing:
    def test_both_sends_and_receives(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_DST")
        assert should_sync_reaction_between(source, target)

    def test_send_only_pairs_with_receive_only(self):
        source = _sync_channel(constants.REACTION_DIRECTION_SEND)
        target = _sync_channel(constants.REACTION_DIRECTION_RECEIVE, channel_id="C_DST")
        assert should_sync_reaction_between(source, target)

    def test_send_only_does_not_pair_with_send_only(self):
        source = _sync_channel(constants.REACTION_DIRECTION_SEND)
        target = _sync_channel(constants.REACTION_DIRECTION_SEND, channel_id="C_DST")
        assert not should_sync_reaction_between(source, target)

    def test_receive_only_does_not_send(self):
        source = _sync_channel(constants.REACTION_DIRECTION_RECEIVE)
        target = _sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_DST")
        assert not should_sync_reaction_between(source, target)

    def test_off_never_pairs(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(constants.REACTION_DIRECTION_OFF, channel_id="C_DST")
        assert not should_sync_reaction_between(source, target)
        assert not channel_sends_reactions(target)
        assert channel_receives_reactions(source)


class TestDefaults:
    def test_new_receive_defaults_to_direct_only(self):
        assert default_reaction_style_for_new_channel(constants.REACTION_DIRECTION_BOTH) == (
            constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE
        )
        assert default_reaction_style_for_new_channel(constants.REACTION_DIRECTION_SEND) is None

    def test_existing_null_style_is_hybrid_when_receiving(self):
        existing = _sync_channel(constants.REACTION_DIRECTION_BOTH, None)
        assert reaction_style(existing) == constants.DEFAULT_REACTION_STYLE_EXISTING


class TestSkipOrigin:
    def test_find_source_matches_event_channel(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_SRC")
        other = _sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_DST")
        records = [(None, source, None), (None, other, None)]
        assert find_source_sync_channel(records, "C_SRC") is source

    def test_sync_does_not_apply_on_origin_channel(self):
        from handlers.messages import _sync_reaction_records

        source = _sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_SRC")
        source.sync_id = 9
        target = _sync_channel(constants.REACTION_DIRECTION_BOTH, channel_id="C_DST")
        source_ws = SimpleNamespace(id=1, team_id="T1", bot_token="enc")
        dest_ws = SimpleNamespace(id=2, team_id="T2", bot_token="enc")
        origin_meta = SimpleNamespace(ts=1.0, post_id="p1")
        dest_meta = SimpleNamespace(ts=2.0, post_id="p2")
        body = {
            "event": {
                "type": "reaction_added",
                "reaction": "thumbsup",
                "user": "U_SRC",
                "item": {"channel": "C_SRC"},
            }
        }
        records = [
            (origin_meta, source, source_ws),
            (dest_meta, target, dest_ws),
        ]

        with (
            patch("handlers.messages.helpers.get_federated_workspace_for_sync", return_value=None),
            patch("handlers.messages.helpers.get_user_info", return_value=("Alice", None)),
            patch("handlers.messages.helpers.get_workspace_by_id", return_value=source_ws),
            patch("handlers.messages.helpers.resolve_workspace_name", return_value="A"),
            patch("handlers.messages.helpers.decrypt_bot_token", return_value="xoxb"),
            patch(
                "handlers.messages.helpers.get_display_name_and_icon_for_synced_message",
                return_value=("Alice", None, True),
            ),
            patch("helpers.reactions.apply_reaction_to_target", return_value=("direct", None)) as apply_mock,
        ):
            _sync_reaction_records(body, MagicMock(), records)

        applied_channels = [c.kwargs["target_sync_channel"].channel_id for c in apply_mock.call_args_list]
        assert applied_channels == ["C_DST"]


class TestApplyDirect:
    def test_uses_destination_team_for_user_token(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test") as get_token,
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        get_token.assert_called_once_with("T_DEST", "U_MAPPED")
        decrypt.assert_not_called()
        user_client.reactions_add.assert_called_once_with(
            channel="C_DST",
            timestamp="100.000000",
            name="thumbsup",
        )
        user_client.reactions_remove.assert_not_called()
        assert result == "direct"
        assert notice is None

    def test_direct_only_skips_invalid_name(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test") as get_token,
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        get_token.assert_called_once_with("T_DEST", "U_MAPPED")
        decrypt.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_direct_only_no_token_skips_without_probe(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)

        with (
            patch("helpers.reactions.get_user_token", return_value=None),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient") as web_client,
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        decrypt.assert_not_called()
        web_client.assert_not_called()

    def test_direct_remove_reactions_remove_only(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        user_client = MagicMock()

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.WebClient", return_value=user_client),
            patch("helpers.reactions.delete_notices_for_unreact") as delete_notices,
        ):
            result, notice = _apply(
                action="remove",
                source_sync_channel=source,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "direct"
        assert notice is None
        user_client.reactions_remove.assert_called_once()
        user_client.chat_delete.assert_not_called()
        user_client.chat_postMessage.assert_not_called()
        delete_notices.assert_called_once()

    def test_direct_only_auth_error_skips_without_probe(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "token_revoked"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        decrypt.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_with_token_does_not_probe(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "direct"
        assert notice is None
        decrypt.assert_not_called()
        user_client.reactions_add.assert_called_once()
        user_client.reactions_remove.assert_not_called()

    def test_hybrid_invalid_name_skips(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        decrypt.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_already_reacted_is_direct_no_thread(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "already_reacted"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "direct"
        assert notice is None
        decrypt.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_unknown_error_does_not_thread(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "message_not_found"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "failed"
        assert notice is None
        decrypt.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_no_token_threads(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

        with (
            patch("helpers.reactions.get_user_token", return_value=None),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.WebClient", return_value=bot_client),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        bot_client.reactions_add.assert_called_once()
        bot_client.reactions_remove.assert_called_once()
        bot_client.chat_postMessage.assert_called_once()

    def test_hybrid_no_token_invalid_name_skips(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        bot_client = MagicMock()
        bot_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reactions.get_user_token", return_value=None),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.WebClient", return_value=bot_client),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        bot_client.chat_postMessage.assert_not_called()

    def test_hybrid_probe_unsettled_does_not_thread(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        bot_client = MagicMock()
        bot_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "message_not_found"})

        with (
            patch("helpers.reactions.get_user_token", return_value=None),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.WebClient", return_value=bot_client),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        bot_client.chat_postMessage.assert_not_called()

    def test_hybrid_auth_error_threads(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "token_revoked"})
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
            patch("helpers.reactions.WebClient", side_effect=[user_client, bot_client]),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        user_client.reactions_add.assert_called_once()
        bot_client.reactions_add.assert_called_once()
        bot_client.reactions_remove.assert_called_once()
        bot_client.chat_postMessage.assert_called_once()

    def test_hybrid_auth_error_invalid_name_skips(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_auth"})
        bot_client = MagicMock()
        bot_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
            patch("helpers.reactions.WebClient", side_effect=[user_client, bot_client]),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        bot_client.chat_postMessage.assert_not_called()

    def test_successful_native_reaction_remembers_echo(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token") as decrypt,
            patch("helpers.reactions.WebClient", return_value=user_client),
            patch("helpers.reactions.remember_user_action") as remember_mock,
        ):
            _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        decrypt.assert_not_called()
        remember_mock.assert_called_once_with(
            "T_DEST",
            "U_MAPPED",
            "reaction_added",
            "C_DST:100.000000:thumbsup",
        )

    def test_reaction_removed_never_threads(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_remove.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reactions.get_user_token", return_value="xoxp-test"),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.WebClient", return_value=user_client),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
            patch("helpers.reactions.delete_notices_for_unreact") as delete_notices,
        ):
            result, notice = _apply(
                action="remove",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        user_client.reactions_remove.assert_called_once()
        delete_notices.assert_called_once()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_unreact_chat_deletes_notice_ts(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        notice = SimpleNamespace(
            id=9,
            post_id="rxn-a",
            ts=200.000001,
            source_workspace_id=1,
            source_user_id="U_SRC",
        )
        bot_client = MagicMock()

        with (
            patch("helpers.reactions.get_user_token", return_value=None),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
            patch("helpers.reactions.WebClient", return_value=bot_client),
            patch("helpers.reaction_notices.equivalent_actor_pairs", return_value={(1, "U_SRC")}),
            patch("helpers.reaction_notices.find_notices_for_unreact", return_value=[notice]),
            patch("helpers.reaction_notices._child_notices_on_channel", return_value=[]),
            patch("helpers.reaction_notices.DbManager.delete_records"),
        ):
            result, posted = _apply(
                action="remove",
                source_sync_channel=source,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert posted is None
        bot_client.chat_delete.assert_called()
        bot_client.chat_postMessage.assert_not_called()

    def test_name_probe_runs_once_per_workspace_when_cached(self):
        source = _sync_channel(constants.REACTION_DIRECTION_BOTH)
        target = _sync_channel(
            constants.REACTION_DIRECTION_BOTH, constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_DST"
        )
        workspace = SimpleNamespace(id=2, team_id="T_DEST", bot_token="enc")
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.side_effect = [{"ts": "200.000001"}, {"ts": "200.000002"}]
        cache: dict = {}

        with (
            patch("helpers.reactions.get_user_token", return_value=None),
            patch("helpers.reactions._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
            patch("helpers.reactions.WebClient", return_value=bot_client),
        ):
            _apply(
                source_sync_channel=source,
                target_post_meta=SimpleNamespace(ts=100.0),
                target_sync_channel=target,
                target_workspace=workspace,
                name_probe_cache=cache,
            )
            _apply(
                source_sync_channel=source,
                target_post_meta=SimpleNamespace(ts=101.0),
                target_sync_channel=target,
                target_workspace=workspace,
                name_probe_cache=cache,
            )

        assert bot_client.reactions_add.call_count == 1
        assert bot_client.chat_postMessage.call_count == 2


class TestPublishSubscribeBuilders:
    def test_publish_step2_hides_type_when_not_receiving(self):
        from handlers.channel_sync import _build_publish_step2

        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            hidden = _build_publish_step2(
                "group",
                [],
                team_id="T1",
                reaction_direction=constants.REACTION_DIRECTION_SEND,
            )
            shown = _build_publish_step2(
                "group",
                [],
                team_id="T1",
                reaction_direction=constants.REACTION_DIRECTION_BOTH,
            )

        hidden_ids = [
            b["element"]["action_id"] for b in hidden.as_form_field() if b.get("element", {}).get("action_id")
        ]
        shown_ids = [b["element"]["action_id"] for b in shown.as_form_field() if b.get("element", {}).get("action_id")]
        assert actions.CONFIG_PUBLISH_REACTION_STYLE not in hidden_ids
        assert actions.CONFIG_PUBLISH_REACTION_STYLE in shown_ids

    def test_parse_send_only_clears_style(self):
        from handlers.channel_sync import _parse_reaction_fields

        direction, style = _parse_reaction_fields(
            {"view": {"state": {"values": {}}}},
            {"reaction_direction": constants.REACTION_DIRECTION_SEND},
            style_action=actions.CONFIG_PUBLISH_REACTION_STYLE,
        )
        assert direction == constants.REACTION_DIRECTION_SEND
        assert style is None
