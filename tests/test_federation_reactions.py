"""Tests for federated reaction payload and fallback behavior."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from federation import api as federation_api
from federation import core as federation_core


class TestFederationReactionPayload:
    def test_build_reaction_payload_includes_user_fields(self):
        payload = federation_core.build_reaction_payload(
            post_id="post-1",
            channel_id="C123",
            reaction="custom_emoji",
            action="add",
            user_name="Alice",
            user_avatar_url="https://avatar.example/alice.png",
            workspace_name="Workspace A",
            timestamp="100.000001",
        )

        assert payload["post_id"] == "post-1"
        assert payload["channel_id"] == "C123"
        assert payload["reaction"] == "custom_emoji"
        assert payload["action"] == "add"
        assert payload["user_name"] == "Alice"
        assert payload["user_avatar_url"] == "https://avatar.example/alice.png"
        assert payload["workspace_name"] == "Workspace A"
        assert payload["timestamp"] == "100.000001"
        assert "user_id" not in payload
        assert "user_token" not in payload
        assert not any(str(v).startswith("xox") for v in payload.values() if v is not None)

    def test_build_reaction_payload_includes_user_id_when_set(self):
        payload = federation_core.build_reaction_payload(
            post_id="post-1",
            channel_id="C123",
            reaction="thumbsup",
            action="add",
            user_name="Alice",
            timestamp="1.0",
            user_id="U_REMOTE",
        )
        assert payload["user_id"] == "U_REMOTE"


class TestFederationMessageInbound:
    def test_mapped_author_suppresses_workspace_suffix(self):
        body = {
            "channel_id": "C123",
            "text": "hi",
            "post_id": "",
            "user": {
                "display_name": "Alice Remote",
                "avatar_url": "https://remote.example/a.png",
                "workspace_name": "Partner WS",
                "user_id": "U_REMOTE",
            },
        }
        fed_ws = SimpleNamespace(instance_id="remote-instance")
        sync_channel = SimpleNamespace(id=101, channel_id="C123")
        workspace = SimpleNamespace(id=55, bot_token="enc-token")
        mapping = SimpleNamespace(target_user_id="ULOCAL")

        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sync_channel, workspace)),
            patch.object(federation_api, "_pick_user_mapping_for_federated_target", return_value=mapping),
            patch.object(federation_api.helpers, "decrypt_bot_token", return_value="xoxb-test"),
            patch.object(federation_api, "WebClient", MagicMock()),
            patch.object(
                federation_api.helpers, "get_user_info", return_value=("Local Nacho", "https://local.example/n.png")
            ),
            patch.object(federation_api, "_resolve_mentions_for_federated", side_effect=lambda t, *_: t),
            patch.object(federation_api.helpers, "resolve_channel_references", side_effect=lambda t, *a, **k: t),
            patch.object(federation_api.helpers, "post_message", return_value={"ts": "99.000001"}) as post_message_mock,
        ):
            status, resp = federation_api.handle_message(body, fed_ws)

        assert status == 200
        assert resp["ok"] is True
        post_message_mock.assert_called_once_with(
            bot_token="xoxb-test",
            channel_id="C123",
            msg_text="hi",
            user_name="Local Nacho",
            user_profile_url="https://local.example/n.png",
            workspace_name=None,
            blocks=None,
            thread_ts=None,
        )


class TestFederationReactionFallback:
    def _react(self, body, *, apply_result=("direct", None)):
        fed_ws = SimpleNamespace(instance_id="remote-instance")
        sync_channel = SimpleNamespace(
            id=101,
            channel_id="C123",
            reaction_direction="both",
            reaction_style="threaded_and_direct",
        )
        workspace = SimpleNamespace(id=55, bot_token="enc-token")
        post_meta = SimpleNamespace(ts=123.456)

        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sync_channel, workspace)),
            patch.object(federation_api, "_find_post_records", return_value=[post_meta]),
            patch("helpers.reactions.apply_reaction_to_target", return_value=apply_result) as apply_mock,
        ):
            status, resp = federation_api.handle_message_react(body, fed_ws)
        return status, resp, apply_mock

    def test_hybrid_thread_result_counts_as_applied(self):
        body = {
            "post_id": "post-1",
            "channel_id": "C123",
            "reaction": "missing_custom",
            "action": "add",
            "user_name": "Alice",
        }
        status, resp, apply_mock = self._react(body, apply_result=("thread", SimpleNamespace()))
        assert status == 200
        assert resp["applied"] == 1
        apply_mock.assert_called_once()

    def test_successful_direct_apply(self):
        body = {
            "post_id": "post-1",
            "channel_id": "C123",
            "reaction": "thumbsup",
            "action": "add",
            "user_name": "Alice",
        }
        status, resp, _apply_mock = self._react(body, apply_result=("direct", None))
        assert status == 200
        assert resp["applied"] == 1

    def test_skipped_apply_is_not_counted(self):
        body = {
            "post_id": "post-1",
            "channel_id": "C123",
            "reaction": "missing_custom",
            "action": "add",
        }
        status, resp, apply_mock = self._react(body, apply_result=("skipped", None))
        assert status == 200
        assert resp["applied"] == 0
        apply_mock.assert_called_once()

    def test_receive_off_skips_without_calling_apply(self):
        body = {
            "post_id": "post-1",
            "channel_id": "C123",
            "reaction": "thumbsup",
            "action": "add",
        }
        fed_ws = SimpleNamespace(instance_id="remote-instance")
        sync_channel = SimpleNamespace(
            id=101,
            channel_id="C123",
            reaction_direction="off",
            reaction_style=None,
        )
        workspace = SimpleNamespace(id=55, bot_token="enc-token")

        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sync_channel, workspace)),
            patch.object(federation_api, "_find_post_records", return_value=[SimpleNamespace(ts=1.0)]),
            patch("helpers.reactions.apply_reaction_to_target") as apply_mock,
        ):
            status, resp = federation_api.handle_message_react(body, fed_ws)

        assert status == 200
        assert resp["applied"] == 0
        apply_mock.assert_not_called()


class TestFederationInboundTokenLookup:
    def test_maps_remote_user_then_looks_up_local_token(self):
        body = {
            "post_id": "post-1",
            "channel_id": "C123",
            "reaction": "thumbsup",
            "action": "add",
            "user_id": "U_REMOTE",
            "user_name": "Remote Alice",
        }
        fed_ws = SimpleNamespace(instance_id="remote-instance")
        sync_channel = SimpleNamespace(
            id=101,
            channel_id="C123",
            reaction_direction="both",
            reaction_style="direct_only",
        )
        workspace = SimpleNamespace(id=55, team_id="T_DEST", bot_token="enc-token")
        post_meta = SimpleNamespace(ts=123.456)
        mapping = SimpleNamespace(target_user_id="U_LOCAL")
        user_client = MagicMock()

        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sync_channel, workspace)),
            patch.object(federation_api, "_find_post_records", return_value=[post_meta]),
            patch.object(federation_api, "_pick_user_mapping_for_federated_target", return_value=mapping),
            patch.object(federation_api.helpers, "decrypt_bot_token", return_value="xoxb-bot"),
            patch.object(federation_api.helpers, "get_user_info", return_value=("Local Alice", None)),
            patch("helpers.reactions.get_user_token", return_value="xoxp-local") as get_token,
            patch("helpers.reactions.decrypt_bot_token", return_value="xoxb-bot"),
            patch("helpers.reactions.WebClient", return_value=user_client),
        ):
            status, resp = federation_api.handle_message_react(body, fed_ws)

        assert status == 200
        assert resp["applied"] == 1
        get_token.assert_called_once_with("T_DEST", "U_LOCAL")
        assert user_client.reactions_add.call_count == 2
        user_client.reactions_remove.assert_called_once()
        assert all("xoxp" not in str(v) for v in body.values())
