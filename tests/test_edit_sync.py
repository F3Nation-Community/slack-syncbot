"""Tests for Home Edit modal (policy + reactions)."""

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
from slack import actions  # noqa: E402


def _body_with_value(value: str, *, action_id: str | None = None) -> dict:
    return {
        "trigger_id": "trig",
        "actions": [{"value": value, "action_id": action_id or f"{actions.CONFIG_EDIT_SYNC}_c_1"}],
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }


class TestParseEditSyncRef:
    def test_channel_and_sync_encodings(self):
        assert _parse_edit_sync_ref(_body_with_value("c:12")) == ("channel", 12)
        assert _parse_edit_sync_ref(_body_with_value("s:42")) == ("sync", 42)

    def test_bare_integer_is_rejected(self):
        assert _parse_edit_sync_ref(_body_with_value("12")) == (None, None)

    def test_action_id_fallback(self):
        body = {"actions": [{"value": "", "action_id": f"{actions.CONFIG_EDIT_SYNC}_s_9"}]}
        assert _parse_edit_sync_ref(body) == ("sync", 9)


class TestHandleEditSyncOpen:
    WORKSPACE = SimpleNamespace(id=1, team_id="T1")
    SYNC = SimpleNamespace(
        id=42,
        group_id=5,
        sync_mode="group",
        target_workspace_id=None,
        publisher_workspace_id=1,
    )
    CHANNEL = SimpleNamespace(
        id=10,
        sync_id=42,
        workspace_id=1,
        deleted_at=None,
        reaction_direction="both",
        reaction_style="direct_only",
    )

    def test_publisher_modal_includes_policy_and_reactions(self):
        client = MagicMock()
        body = _body_with_value("c:10", action_id=f"{actions.CONFIG_EDIT_SYNC}_c_10")
        captured: dict = {}

        def capture_post_modal(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        with (
            patch(
                "handlers.channel_sync._get_authorized_workspace",
                return_value=("U1", self.WORKSPACE),
            ),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=self.CHANNEL),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync._get_group_members", return_value=[]),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture_post_modal),
        ):
            handle_edit_sync(body, client, MagicMock(), {})

        assert captured["kwargs"]["title_text"] == "Edit"
        assert captured["kwargs"]["callback_id"] == actions.CONFIG_EDIT_SYNC_SUBMIT
        assert captured["kwargs"]["body"] is body
        assert captured["kwargs"]["parent_metadata"]["sync_id"] == 42
        assert captured["kwargs"]["parent_metadata"]["sync_channel_id"] == 10
        actions_seen = [getattr(b, "action", None) for b in captured["blocks"]]
        assert actions.CONFIG_PUBLISH_SYNC_MODE in actions_seen
        assert actions.CONFIG_PUBLISH_REACTION_DIRECTION in actions_seen

    def test_open_keeps_hybrid_type_when_direction_is_off(self):
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            deleted_at=None,
            reaction_direction=constants.REACTION_DIRECTION_OFF,
            reaction_style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        )
        captured: dict = {}

        def capture_post_modal(self, **kwargs):
            captured["blocks"] = list(self.blocks)

        body = _body_with_value("c:10", action_id=f"{actions.CONFIG_EDIT_SYNC}_c_10")
        with (
            patch(
                "handlers.channel_sync._get_authorized_workspace",
                return_value=("U1", self.WORKSPACE),
            ),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync._get_group_members", return_value=[]),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture_post_modal),
        ):
            handle_edit_sync(body, MagicMock(), MagicMock(), {})

        style_block = next(
            b for b in captured["blocks"] if getattr(b, "action", None) == actions.CONFIG_PUBLISH_REACTION_STYLE
        )
        assert style_block.element.initial_value == constants.REACTION_STYLE_THREADED_AND_DIRECT

    def test_extra_manager_subscriber_gets_reactions_only(self):
        workspace = SimpleNamespace(id=2, team_id="T2")
        channel = SimpleNamespace(
            id=11,
            sync_id=42,
            workspace_id=2,
            deleted_at=None,
            reaction_direction="both",
            reaction_style="direct_only",
        )
        sync = SimpleNamespace(
            id=42,
            group_id=5,
            sync_mode="group",
            target_workspace_id=None,
            publisher_workspace_id=1,
        )
        captured: dict = {}

        def capture_post_modal(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        body = _body_with_value("c:11", action_id=f"{actions.CONFIG_EDIT_SYNC}_c_11")
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U2", workspace)),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.helpers.is_workspace_owner", return_value=False),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture_post_modal),
        ):
            handle_edit_sync(body, MagicMock(), MagicMock(), {})

        actions_seen = [getattr(b, "action", None) for b in captured["blocks"]]
        assert actions.CONFIG_PUBLISH_SYNC_MODE not in actions_seen
        assert actions.CONFIG_PUBLISH_REACTION_DIRECTION in actions_seen

    def test_group_owner_available_row_is_policy_only(self):
        workspace = SimpleNamespace(id=99, team_id="T99")
        sync = SimpleNamespace(
            id=42,
            group_id=5,
            sync_mode="direct",
            target_workspace_id=2,
            publisher_workspace_id=1,
        )
        captured: dict = {}

        def capture_post_modal(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        body = _body_with_value("s:42", action_id=f"{actions.CONFIG_EDIT_SYNC}_s_42")
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U9", workspace)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.helpers.is_workspace_owner", return_value=True),
            patch("handlers.channel_sync._get_group_members", return_value=[]),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture_post_modal),
        ):
            handle_edit_sync(body, MagicMock(), MagicMock(), {})

        actions_seen = [getattr(b, "action", None) for b in captured["blocks"]]
        assert actions.CONFIG_PUBLISH_SYNC_MODE in actions_seen
        assert actions.CONFIG_PUBLISH_REACTION_DIRECTION not in actions_seen
        assert "sync_channel_id" not in captured["kwargs"]["parent_metadata"]

    def test_non_owner_available_row_is_denied(self):
        workspace = SimpleNamespace(id=99, team_id="T99")
        sync = SimpleNamespace(
            id=42,
            group_id=5,
            sync_mode="group",
            target_workspace_id=None,
            publisher_workspace_id=1,
        )
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U9", workspace)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.helpers.is_workspace_owner", return_value=False),
            patch("handlers.channel_sync.orm.BlockView.post_modal") as post_modal,
        ):
            handle_edit_sync(
                _body_with_value("s:42", action_id=f"{actions.CONFIG_EDIT_SYNC}_s_42"),
                MagicMock(),
                MagicMock(),
                {},
            )
        post_modal.assert_not_called()


class TestEditSyncAck:
    SYNC = SimpleNamespace(id=42, group_id=5, publisher_workspace_id=1, sync_mode="group", target_workspace_id=None)
    WORKSPACE = SimpleNamespace(id=1, team_id="T1")

    def _view_body(self, *, mode: str, target: str | None):
        values = {
            "b1": {
                actions.CONFIG_PUBLISH_SYNC_MODE: {
                    "type": "radio_buttons",
                    "selected_option": {"value": mode},
                }
            }
        }
        if target is not None:
            values["b2"] = {
                actions.CONFIG_PUBLISH_DIRECT_TARGET: {
                    "type": "static_select",
                    "selected_option": {"value": target},
                }
            }
        return {
            "user": {"id": "U1"},
            "view": {
                "private_metadata": '{"sync_id": 42}',
                "state": {"values": values},
            },
        }

    def test_specific_with_extra_subscribers_errors(self):
        live = [
            SimpleNamespace(workspace_id=1, deleted_at=None),
            SimpleNamespace(workspace_id=2, deleted_at=None),
            SimpleNamespace(workspace_id=3, deleted_at=None),
        ]
        with (
            patch(
                "handlers.channel_sync._get_authorized_workspace",
                return_value=("U1", self.WORKSPACE),
            ),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync.DbManager.find_records", return_value=live),
        ):
            result = handle_edit_sync_submit_ack(self._view_body(mode="direct", target="2"), MagicMock(), {})

        assert result is not None
        assert result["response_action"] == "errors"
        assert actions.CONFIG_PUBLISH_SYNC_MODE in result["errors"]

    def test_widen_to_group_ok(self):
        with (
            patch(
                "handlers.channel_sync._get_authorized_workspace",
                return_value=("U1", self.WORKSPACE),
            ),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
        ):
            assert handle_edit_sync_submit_ack(self._view_body(mode="group", target=None), MagicMock(), {}) is None


class TestEditSyncSubmit:
    WORKSPACE = SimpleNamespace(id=1, team_id="T1")
    SYNC = SimpleNamespace(
        id=42,
        group_id=5,
        sync_mode="direct",
        target_workspace_id=2,
        publisher_workspace_id=1,
    )
    CHANNEL = SimpleNamespace(id=10, sync_id=42, workspace_id=1, deleted_at=None)

    def test_publisher_can_widen_to_group(self):
        body = {
            "user": {"id": "U1"},
            "view": {
                "private_metadata": '{"sync_id": 42, "sync_channel_id": 10}',
                "state": {
                    "values": {
                        "b1": {
                            actions.CONFIG_PUBLISH_SYNC_MODE: {
                                "type": "radio_buttons",
                                "selected_option": {"value": "group"},
                            }
                        },
                        "b2": {
                            actions.CONFIG_PUBLISH_REACTION_DIRECTION: {
                                "type": "radio_buttons",
                                "selected_option": {"value": constants.REACTION_DIRECTION_SEND},
                            }
                        },
                    }
                },
            },
        }
        with (
            patch(
                "handlers.channel_sync._get_authorized_workspace",
                return_value=("U1", self.WORKSPACE),
            ),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync.DbManager.update_records") as update,
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=self.CHANNEL),
            patch("helpers.reactions.update_sync_channel_reactions"),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync._refresh_group_member_homes") as refresh_group,
        ):
            handle_edit_sync_submit(body, MagicMock(), MagicMock(), {})

        update.assert_called_once()
        refresh_group.assert_called_once()

    def test_extra_manager_cannot_write_sync_mode(self):
        workspace = SimpleNamespace(id=2, team_id="T2")
        channel = SimpleNamespace(id=11, sync_id=42, workspace_id=2, deleted_at=None)
        sync = SimpleNamespace(
            id=42,
            group_id=5,
            sync_mode="group",
            target_workspace_id=None,
            publisher_workspace_id=1,
        )
        body = {
            "user": {"id": "U2"},
            "view": {
                "private_metadata": '{"sync_id": 42, "sync_channel_id": 11}',
                "state": {
                    "values": {
                        "b1": {
                            actions.CONFIG_PUBLISH_SYNC_MODE: {
                                "type": "radio_buttons",
                                "selected_option": {"value": "direct"},
                            }
                        },
                        "b2": {
                            actions.CONFIG_PUBLISH_DIRECT_TARGET: {
                                "type": "static_select",
                                "selected_option": {"value": "9"},
                            }
                        },
                        "b3": {
                            actions.CONFIG_PUBLISH_REACTION_DIRECTION: {
                                "type": "radio_buttons",
                                "selected_option": {"value": constants.REACTION_DIRECTION_BOTH},
                            }
                        },
                        "b4": {
                            actions.CONFIG_PUBLISH_REACTION_STYLE: {
                                "type": "radio_buttons",
                                "selected_option": {"value": constants.REACTION_STYLE_DIRECT_ONLY},
                            }
                        },
                    }
                },
            },
        }
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U2", workspace)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.helpers.is_workspace_owner", return_value=False),
            patch("handlers.channel_sync.DbManager.update_records") as update,
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("helpers.reactions.update_sync_channel_reactions") as upd_rxn,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync._refresh_group_member_homes") as refresh_group,
        ):
            handle_edit_sync_submit(body, MagicMock(), MagicMock(), {})

        update.assert_not_called()
        refresh_group.assert_not_called()
        upd_rxn.assert_called_once()

    def test_submit_off_keeps_stored_hybrid_when_style_omitted(self):
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            deleted_at=None,
            reaction_direction=constants.REACTION_DIRECTION_BOTH,
            reaction_style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        )
        body = {
            "user": {"id": "U1"},
            "view": {
                "private_metadata": '{"sync_id": 42, "sync_channel_id": 10}',
                "state": {
                    "values": {
                        "b1": {
                            actions.CONFIG_PUBLISH_REACTION_DIRECTION: {
                                "type": "radio_buttons",
                                "selected_option": {"value": constants.REACTION_DIRECTION_OFF},
                            }
                        },
                    }
                },
            },
        }
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync._can_edit_sync_policy", return_value=False),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("helpers.reactions.update_sync_channel_reactions") as upd_rxn,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
        ):
            handle_edit_sync_submit(body, MagicMock(), MagicMock(), {})

        upd_rxn.assert_called_once_with(
            10,
            direction=constants.REACTION_DIRECTION_OFF,
            style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        )

    def test_submit_off_saves_style_from_form(self):
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            deleted_at=None,
            reaction_direction=constants.REACTION_DIRECTION_BOTH,
            reaction_style=constants.REACTION_STYLE_DIRECT_ONLY,
        )
        body = {
            "user": {"id": "U1"},
            "view": {
                "private_metadata": '{"sync_id": 42, "sync_channel_id": 10}',
                "state": {
                    "values": {
                        "b1": {
                            actions.CONFIG_PUBLISH_REACTION_DIRECTION: {
                                "type": "radio_buttons",
                                "selected_option": {"value": constants.REACTION_DIRECTION_OFF},
                            }
                        },
                        "b2": {
                            actions.CONFIG_PUBLISH_REACTION_STYLE: {
                                "type": "radio_buttons",
                                "selected_option": {"value": constants.REACTION_STYLE_THREADED_AND_DIRECT},
                            }
                        },
                    }
                },
            },
        }
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync._can_edit_sync_policy", return_value=False),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("helpers.reactions.update_sync_channel_reactions") as upd_rxn,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
        ):
            handle_edit_sync_submit(body, MagicMock(), MagicMock(), {})

        upd_rxn.assert_called_once_with(
            10,
            direction=constants.REACTION_DIRECTION_OFF,
            style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        )
