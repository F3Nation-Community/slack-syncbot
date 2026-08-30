"""Home-tab builder tests for publisher vs subscriber teardown routing.

Regression cover for the crossed-wires bug: the publisher used to get a
"Stop Syncing" button (a subscriber action) on their own published channel,
which stranded the sync with a publisher that no longer had a channel.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from builders.channel_sync import _build_inline_channel_sync  # noqa: E402
from db.schemas import Sync  # noqa: E402
from slack import actions  # noqa: E402

GROUP_ID = 5
SYNC_ID = 42
PUBLISHER_WS = 1
SUBSCRIBER_WS = 2
VIEWER_WS = 99


def _channel(cid, ws):
    return SimpleNamespace(
        id=cid,
        workspace_id=ws,
        channel_id=f"C_{cid}",
        status="active",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _buttons(blocks):
    """Flatten every (label, action_id) button across ActionsBlocks."""
    out = []
    for block in blocks:
        for element in getattr(block, "elements", None) or []:
            action = getattr(element, "action", None)
            if action:
                out.append((getattr(element, "label", None), action))
    return out


def _render(*, viewer_ws, publisher_ws, channels):
    sync = SimpleNamespace(
        id=SYNC_ID,
        group_id=GROUP_ID,
        title="2nd-f",
        sync_mode="group",
        publisher_workspace_id=publisher_ws,
        target_workspace_id=None,
    )
    group = SimpleNamespace(id=GROUP_ID)
    workspace_record = SimpleNamespace(id=viewer_ws, team_id="T1")

    def find_records(model, _filters):
        return [sync] if model is Sync else channels

    blocks: list = []
    with (
        patch("builders.channel_sync.DbManager.find_records", side_effect=find_records),
        patch("builders.channel_sync.DbManager.count_records", return_value=0),
        patch("builders.channel_sync.helpers.resolve_workspace_name", return_value="WS"),
        patch("builders.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(bot_token=None)),
        patch("builders.channel_sync._format_channel_ref", return_value="#c"),
    ):
        _build_inline_channel_sync(blocks, group, workspace_record, other_members=[], context={})
    return blocks


def test_publisher_active_row_offers_unpublish_not_stop():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_UNPUBLISH_CHANNEL) for a in actions_seen)
    assert not any(a.startswith(actions.CONFIG_STOP_SYNC) for a in actions_seen)


def test_subscriber_active_row_offers_stop_not_unpublish():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS), _channel(11, PUBLISHER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_STOP_SYNC) for a in actions_seen)
    assert not any(a.startswith(actions.CONFIG_UNPUBLISH_CHANNEL) for a in actions_seen)


def test_orphaned_sync_is_not_advertised_as_available():
    """Publisher (WS 1) has left; a viewer with no channel must not see it offered."""
    blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(11, SUBSCRIBER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert not any(a.startswith(actions.CONFIG_SUBSCRIBE_CHANNEL) for a in actions_seen)


def test_stranded_member_can_stop_the_orphan():
    """The remaining subscriber (publisher gone) gets a Stop Syncing button."""
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_STOP_SYNC) for a in actions_seen)
